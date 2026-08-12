"""Internal module for the Codex GitHub review agent."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from .core import *
from .models import *

class StateStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_path = state_dir / "state.json"
        self.lock_path = state_dir / "agent.lock"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentError(
                    f"another agent is already using {self.state_dir}"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": STATE_VERSION, "pull_requests": {}, "issues": {}}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(
                f"cannot read state file {self.state_path}: {exc}"
            ) from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != STATE_VERSION
            or not isinstance(payload.get("pull_requests"), dict)
            or ("issues" in payload and not isinstance(payload.get("issues"), dict))
        ):
            raise AgentError(f"unsupported or malformed state file: {self.state_path}")
        return payload

    def save(self, state: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        data = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            directory_fd = os.open(self.state_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as exc:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise AgentError(
                f"cannot save state file {self.state_path}: {exc}"
            ) from exc


def needs_review(state: dict[str, Any], pull_request: PullRequest) -> bool:
    record = state["pull_requests"].get(pull_request.key)
    if not isinstance(record, dict):
        return True
    return any(
        record.get(key) != value for key, value in pull_request.fingerprint.items()
    )


def needs_issue_review(state: dict[str, Any], issue: Issue) -> bool:
    if issue.has_matching_review:
        return False
    issues = state.get("issues")
    record = issues.get(issue.key) if isinstance(issues, dict) else None
    if not isinstance(record, dict):
        return True
    return any(
        record.get(key) != value for key, value in issue.request_fingerprint.items()
    )


class RepositoryWorkspace:
    def __init__(
        self,
        runner: CommandRunner,
        *,
        gh_bin: str,
        state_dir: Path,
        worktree_root: Path,
    ) -> None:
        self.runner = runner
        self.gh_bin = gh_bin
        self.repos_dir = state_dir / "repos"
        self.worktree_root = worktree_root.resolve()

    def _cache_path(self, repo_full_name: str) -> Path:
        owner, _, repo = repo_full_name.partition("/")
        if (
            not owner
            or not repo
            or any(SAFE_COMPONENT_RE.fullmatch(part) is None for part in (owner, repo))
        ):
            raise AgentError(f"unsafe repository name: {repo_full_name!r}")
        return self.repos_dir / f"{owner}__{repo}"

    def _ensure_cache(self, repo_full_name: str) -> Path:
        cache = self._cache_path(repo_full_name)
        self.repos_dir.mkdir(parents=True, exist_ok=True)
        if not cache.exists():
            self.runner.run(
                [
                    self.gh_bin,
                    "repo",
                    "clone",
                    repo_full_name,
                    str(cache),
                    "--",
                    "--filter=blob:none",
                    "--no-checkout",
                ]
            )
        if not (cache / ".git").exists():
            raise AgentError(f"repository cache is invalid: {cache}")
        return cache

    def _git_output(self, cache: Path, *args: str) -> str:
        return self.runner.run(["git", "-C", str(cache), *args]).stdout.strip()

    @contextlib.contextmanager
    def checkout(self, pull_request: PullRequest) -> Iterator[Path]:
        cache = self._ensure_cache(pull_request.repo_full_name)
        review_ref = f"refs/reno-review/pr-{pull_request.number}"
        self.runner.run(
            [
                "git",
                "-C",
                str(cache),
                "fetch",
                "--force",
                "--no-tags",
                "origin",
                f"+refs/heads/{pull_request.base_ref}:refs/remotes/origin/{pull_request.base_ref}",
                f"+refs/pull/{pull_request.number}/head:{review_ref}",
            ]
        )
        fetched_head = self._git_output(cache, "rev-parse", review_ref).lower()
        fetched_base = self._git_output(
            cache, "rev-parse", f"refs/remotes/origin/{pull_request.base_ref}"
        ).lower()
        if (
            fetched_head != pull_request.head_sha
            or fetched_base != pull_request.base_sha
        ):
            raise AgentError(
                f"{pull_request.key} changed while preparing the checkout; will retry"
            )

        self.worktree_root.mkdir(parents=True, exist_ok=True)
        prefix = f"{pull_request.repo_name}-pr-{pull_request.number}-{pull_request.head_sha[:12]}-"
        worktree = Path(
            tempfile.mkdtemp(prefix=prefix, dir=self.worktree_root)
        ).resolve()
        worktree.rmdir()
        added = False
        try:
            self.runner.run(
                [
                    "git",
                    "-C",
                    str(cache),
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    review_ref,
                ]
            )
            added = True
            yield worktree
        finally:
            if added:
                with contextlib.suppress(AgentError):
                    self.runner.run(
                        [
                            "git",
                            "-C",
                            str(cache),
                            "worktree",
                            "remove",
                            "--force",
                            str(worktree),
                        ]
                    )
                with contextlib.suppress(AgentError):
                    self.runner.run(["git", "-C", str(cache), "worktree", "prune"])
            if worktree.exists():
                try:
                    worktree.relative_to(self.worktree_root)
                except ValueError as exc:
                    raise AgentError(
                        f"refusing to clean unsafe worktree path: {worktree}"
                    ) from exc
                shutil.rmtree(worktree)
    @contextlib.contextmanager
    def checkout_issue(self, issue: Issue) -> Iterator[Path]:
        cache = self._ensure_cache(issue.repo_full_name)
        review_ref = f"refs/reno-review/issue-{issue.number}"
        self.runner.run(
            [
                "git",
                "-C",
                str(cache),
                "fetch",
                "--force",
                "--no-tags",
                "origin",
                f"+refs/heads/{issue.code_ref}:{review_ref}",
            ]
        )
        fetched_sha = self._git_output(cache, "rev-parse", review_ref).lower()
        if fetched_sha != issue.code_sha:
            raise AgentError(
                f"{issue.key} changed while preparing the checkout; will retry"
            )

        self.worktree_root.mkdir(parents=True, exist_ok=True)
        prefix = f"{issue.repo_name}-issue-{issue.number}-{issue.code_sha[:12]}-"
        worktree = Path(
            tempfile.mkdtemp(prefix=prefix, dir=self.worktree_root)
        ).resolve()
        worktree.rmdir()
        added = False
        try:
            self.runner.run(
                [
                    "git",
                    "-C",
                    str(cache),
                    "worktree",
                    "add",
                    "--detach",
                    str(worktree),
                    review_ref,
                ]
            )
            added = True
            yield worktree
        finally:
            if added:
                with contextlib.suppress(AgentError):
                    self.runner.run(
                        [
                            "git",
                            "-C",
                            str(cache),
                            "worktree",
                            "remove",
                            "--force",
                            str(worktree),
                        ]
                    )
                with contextlib.suppress(AgentError):
                    self.runner.run(["git", "-C", str(cache), "worktree", "prune"])
            if worktree.exists():
                try:
                    worktree.relative_to(self.worktree_root)
                except ValueError as exc:
                    raise AgentError(
                        f"refusing to clean unsafe worktree path: {worktree}"
                    ) from exc
                shutil.rmtree(worktree)
