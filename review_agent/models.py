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

@dataclasses.dataclass(frozen=True)
class PullRequest:
    repo_full_name: str
    repo_name: str
    number: int
    url: str
    title: str
    body: str
    body_sha256: str
    base_ref: str
    base_sha: str
    head_sha: str

    @property
    def key(self) -> str:
        return f"{self.repo_full_name}#{self.number}"

    @property
    def fingerprint(self) -> dict[str, str]:
        return {
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "body_sha256": self.body_sha256,
        }


@dataclasses.dataclass(frozen=True)
class Issue:
    repo_full_name: str
    repo_name: str
    number: int
    url: str
    title: str
    body: str
    body_sha256: str
    code_ref: str
    code_sha: str
    request_comment_id: int | None
    request_comment_url: str | None
    request_comment_updated_at: str | None
    request_body: str
    request_body_sha256: str
    has_matching_review: bool = False

    @property
    def key(self) -> str:
        return f"{self.repo_full_name}#{self.number}"

    @property
    def fingerprint(self) -> dict[str, Any]:
        return {
            "body_sha256": self.body_sha256,
            "code_ref": self.code_ref,
            "code_sha": self.code_sha,
            "request_comment_id": self.request_comment_id,
            "request_comment_updated_at": self.request_comment_updated_at,
            "request_body_sha256": self.request_body_sha256,
        }

    @property
    def request_fingerprint(self) -> dict[str, Any]:
        return {
            "body_sha256": self.body_sha256,
            "request_comment_id": self.request_comment_id,
            "request_comment_updated_at": self.request_comment_updated_at,
            "request_body_sha256": self.request_body_sha256,
        }


@dataclasses.dataclass(frozen=True)
class ReviewResult:
    verdict: str
    reviewed_head_sha: str
    summary: str
    review_body: str | None
    sweep: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class Finding:
    severity: str
    blocking: bool
    title: str
    path: str | None
    line: int | None
    contract_clause: str | None
    evidence: str
    impact: str
    reproduction: str
    required_fix: str

    @property
    def key(self) -> tuple[str, int | None, str | None, str]:
        return (
            self.path or "",
            self.line,
            self.contract_clause,
            self.title.casefold(),
        )

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class SweepResult:
    status: str
    reviewed_head_sha: str
    summary: str
    sweep_complete: bool
    reviewed_files: tuple[dict[str, str], ...]
    coverage: tuple[dict[str, str], ...]
    checks_run: tuple[dict[str, str], ...]
    findings: tuple[Finding, ...]
    residual_risks: tuple[str, ...]


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def contains_exact_mention(body: str, mention: str) -> bool:
    """Match a GitHub-style handle without accepting a longer handle."""
    if not mention.startswith("@") or len(mention) == 1:
        raise ValueError("mention must be a GitHub handle beginning with @")
    pattern = rf"(?<![A-Za-z0-9_-]){re.escape(mention)}(?![A-Za-z0-9_-])"
    return re.search(pattern, body) is not None


def normalize_repositories(raw_values: Sequence[str], org: str) -> tuple[str, ...]:
    """Normalize configured repo names to org/repo and reject out-of-scope values."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        for raw_repository in raw_value.split(","):
            repository = raw_repository.strip()
            if not repository:
                continue
            owner, separator, name = repository.partition("/")
            if not separator:
                owner, name = org, owner
            if (
                not owner
                or not name
                or "/" in name
                or any(SAFE_COMPONENT_RE.fullmatch(part) is None for part in (owner, name))
            ):
                raise ValueError(f"invalid repository name: {repository!r}")
            if owner.casefold() != org.casefold():
                raise ValueError(
                    f"repository {repository!r} is outside configured organization {org!r}"
                )
            full_name = f"{owner}/{name}"
            if full_name.casefold() not in seen:
                seen.add(full_name.casefold())
                normalized.append(full_name)
    return tuple(normalized)


def repository_is_selected(
    repo_full_name: str, selected_repositories: Sequence[str]
) -> bool:
    return not selected_repositories or any(
        repo_full_name.casefold() == repository.casefold()
        for repository in selected_repositories
    )

def _validate_sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or SHA_RE.fullmatch(value.lower()) is None:
        raise AgentError(f"GitHub returned an invalid {field}")
    return value.lower()
