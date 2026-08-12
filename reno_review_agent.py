#!/usr/bin/env python3
"""Poll GitHub pull requests and issues for a configured Codex review agent."""

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

APP_ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = APP_ROOT / ".state"
DEFAULT_WORKTREE_ROOT = Path(tempfile.gettempdir()) / "codex-review-agent"
SCHEMA_PATH = APP_ROOT / "review-result.schema.json"
STATE_VERSION = 1
LOG = logging.getLogger("codex-review-agent")
DEFAULT_AGENT_NAME = "Codex Review Agent"
DEFAULT_MARKER = "codex-review-agent"
DEFAULT_REVIEW_CONTEXT = (
    "Follow the repository's documented product, security, scale, and compatibility "
    "requirements. Do not invent requirements that are not present in the Issue, pull "
    "request, or repository documentation."
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
COVERAGE_AREAS = (
    "workflow_correctness",
    "data_lifecycle",
    "compatibility_integration",
    "ordinary_error_handling",
    "retry_idempotency",
    "tests_observability",
)
CODEX_HEARTBEAT_SECONDS = 120
NETWORK_ATTEMPTS = 3
NETWORK_RETRY_DELAYS = (5, 15)
NETWORK_COMMAND_TIMEOUT = 60
TRANSIENT_NETWORK_MARKERS = (
    "could not resolve host",
    "couldn't connect to server",
    "failed to connect",
    "connection refused",
    "connection reset by peer",
    "connection timed out",
    "network is unreachable",
    "temporary failure in name resolution",
    "tls connect error",
    "the requested url returned error: 502",
    "the requested url returned error: 503",
    "the requested url returned error: 504",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "rpc failed; curl 28",
    "rpc failed; curl 35",
    "rpc failed; curl 52",
    "rpc failed; curl 56",
    "unexpected disconnect while reading sideband",
    "the remote end hung up unexpectedly",
)


class AgentError(RuntimeError):
    """An expected operational failure that should be retried."""


class TransientNetworkError(AgentError):
    """A read-only network operation failed after bounded retries."""


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE pairs without a third-party dependency.

    Existing environment variables win, allowing CI and secret managers to
    override a local .env file.
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AgentError(f"cannot read environment file {path}: {exc}") from exc
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise AgentError(f"invalid .env entry at {path}:{line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise AgentError(f"{name} must be true/false, yes/no, 1/0, or on/off")


def _is_transient_network_detail(detail: str) -> bool:
    normalized = detail.casefold()
    return any(marker in normalized for marker in TRANSIENT_NETWORK_MARKERS)


def _safe_network_operation(args: Sequence[str]) -> str | None:
    """Return a retry label only for idempotent Git/GitHub network commands."""
    if not args:
        return None
    executable = Path(args[0]).name
    arguments = list(args[1:])
    if executable == "git":
        if "fetch" in arguments:
            return "git fetch"
        if "clone" in arguments:
            return "git clone"
        if "ls-remote" in arguments:
            return "git ls-remote"
        return None
    if executable != "gh":
        return None
    if len(arguments) >= 2 and arguments[:2] == ["repo", "clone"]:
        return "GitHub repository clone"
    if not arguments or arguments[0] != "api":
        return None

    method = "GET"
    method_is_explicit = False
    has_fields = False
    for index, argument in enumerate(arguments[1:], start=1):
        if argument in {"-X", "--method"} and index + 1 < len(arguments):
            method = arguments[index + 1].upper()
            method_is_explicit = True
        elif argument.startswith("--method="):
            method = argument.partition("=")[2].upper()
            method_is_explicit = True
        elif argument in {"-f", "--raw-field", "-F", "--field"} or argument.startswith(
            ("--raw-field=", "--field=")
        ):
            has_fields = True
    if method == "GET" and (method_is_explicit or not has_fields):
        return "GitHub API read"
    return None


class CommandRunner:
    """Small subprocess wrapper, kept injectable for tests."""

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        LOG.debug("running: %s", " ".join(args))
        network_label = _safe_network_operation(args)
        attempts = NETWORK_ATTEMPTS if network_label is not None else 1
        command_timeout = (
            NETWORK_COMMAND_TIMEOUT
            if network_label is not None and timeout is None
            else timeout
        )
        for attempt in range(1, attempts + 1):
            try:
                completed = subprocess.run(
                    list(args),
                    cwd=cwd,
                    input=input_text,
                    text=True,
                    capture_output=True,
                    check=True,
                    timeout=command_timeout,
                )
                if attempt > 1:
                    LOG.info(
                        "%s recovered on attempt %d/%d",
                        network_label,
                        attempt,
                        attempts,
                    )
                return completed
            except subprocess.TimeoutExpired as exc:
                if network_label is None:
                    raise AgentError(
                        f"command timed out after {command_timeout}s: {args[0]}"
                    ) from exc
                detail = f"timed out after {command_timeout}s"
                error: AgentError = TransientNetworkError(f"{network_label} {detail}")
            except subprocess.CalledProcessError as exc:
                stderr = (exc.stderr or "").strip()
                stdout = (exc.stdout or "").strip()
                detail = stderr or stdout or f"exit {exc.returncode}"
                if network_label is None or not _is_transient_network_detail(detail):
                    raise AgentError(f"command failed ({args[0]}): {detail}") from exc
                error = TransientNetworkError(f"{network_label} failed: {detail}")
            except FileNotFoundError as exc:
                raise AgentError(f"required command not found: {args[0]}") from exc

            if attempt == attempts:
                raise error
            delay = NETWORK_RETRY_DELAYS[attempt - 1]
            LOG.warning(
                "%s failed because the network is unavailable; retrying in %ds "
                "(attempt %d/%d): %s",
                network_label,
                delay,
                attempt + 1,
                attempts,
                error,
            )
            time.sleep(delay)
        raise AssertionError("unreachable network retry state")

    def run_with_heartbeat(
        self,
        args: Sequence[str],
        *,
        label: str,
        cwd: Path | None = None,
        input_text: str | None = None,
        timeout: int | None = None,
        heartbeat_seconds: float = CODEX_HEARTBEAT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        """Run a long command while logging periodic liveness without streaming output."""
        LOG.debug("running: %s", " ".join(args))
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                list(args),
                cwd=cwd,
                stdin=subprocess.PIPE if input_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise AgentError(f"required command not found: {args[0]}") from exc

        pending_input = input_text
        try:
            while True:
                elapsed = time.monotonic() - started
                if timeout is not None and elapsed >= timeout:
                    process.kill()
                    stdout, stderr = process.communicate()
                    raise AgentError(f"command timed out after {timeout}s: {args[0]}")
                wait_seconds = heartbeat_seconds
                if timeout is not None:
                    wait_seconds = min(wait_seconds, max(timeout - elapsed, 0.001))
                try:
                    stdout, stderr = process.communicate(
                        input=pending_input,
                        timeout=wait_seconds,
                    )
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    elapsed = time.monotonic() - started
                    if timeout is not None and elapsed >= timeout:
                        process.kill()
                        stdout, stderr = process.communicate()
                        raise AgentError(
                            f"command timed out after {timeout}s: {args[0]}"
                        )
                    LOG.info("%s still running (%ds elapsed)", label, round(elapsed))
        except BaseException:
            if process.poll() is None:
                process.kill()
                process.communicate()
            raise

        completed = subprocess.CompletedProcess(
            list(args), process.returncode, stdout, stderr
        )
        if process.returncode:
            detail = (stderr or "").strip() or (stdout or "").strip()
            raise AgentError(
                f"command failed ({args[0]}): {detail or f'exit {process.returncode}'}"
            )
        return completed

    def json(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> Any:
        completed = self.run(args, cwd=cwd, timeout=timeout)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise AgentError(f"{args[0]} returned invalid JSON") from exc


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


def parse_pull_request(
    payload: dict[str, Any], org: str, mention: str
) -> PullRequest | None:
    try:
        base_repo = payload["base"]["repo"]
        repo_full_name = base_repo["full_name"]
        repo_owner = base_repo["owner"]["login"]
        repo_name = base_repo["name"]
        body = payload.get("body") or ""
    except (KeyError, TypeError) as exc:
        raise AgentError("GitHub returned incomplete pull request metadata") from exc

    if not isinstance(repo_full_name, str) or not isinstance(repo_name, str):
        raise AgentError("GitHub returned invalid repository metadata")
    if repo_owner.casefold() != org.casefold():
        return None
    if payload.get("state") != "open" or payload.get("draft") is True:
        return None
    if not isinstance(body, str) or not contains_exact_mention(body, mention):
        return None
    if SAFE_COMPONENT_RE.fullmatch(repo_name) is None:
        raise AgentError(f"unsafe repository name returned by GitHub: {repo_name!r}")

    try:
        number = int(payload["number"])
        url = str(payload["html_url"])
        title = str(payload["title"])
        base_ref = str(payload["base"]["ref"])
        base_sha = _validate_sha(payload["base"]["sha"], "base SHA")
        head_sha = _validate_sha(payload["head"]["sha"], "head SHA")
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentError("GitHub returned incomplete pull request metadata") from exc
    if number <= 0 or not base_ref:
        raise AgentError("GitHub returned invalid pull request metadata")

    return PullRequest(
        repo_full_name=repo_full_name,
        repo_name=repo_name,
        number=number,
        url=url,
        title=title,
        body=body,
        body_sha256=body_digest(body),
        base_ref=base_ref,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def discover_pull_requests(
    runner: CommandRunner,
    *,
    gh_bin: str,
    org: str,
    mention: str,
    repositories: Sequence[str] = (),
) -> list[PullRequest]:
    candidates: dict[tuple[str, int], None] = {}

    # GitHub search exposes at most 1,000 results. This agent intentionally works
    # serially, but pagination avoids silently losing a mention.
    scopes = [f"repo:{repository}" for repository in repositories] or [f"org:{org}"]
    for scope in scopes:
        query = f'{scope} is:pr is:open in:body "{mention}"'
        for page in range(1, 11):
            payload = runner.json(
                [
                    gh_bin, "api", "-X", "GET", "search/issues", "-f", f"q={query}",
                    "-f", "per_page=100", "-f", f"page={page}",
                ]
            )
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise AgentError("GitHub search returned an invalid response")
            for item in items:
                if not isinstance(item, dict):
                    continue
                repository_url = item.get("repository_url")
                number = item.get("number")
                if not isinstance(repository_url, str) or "/repos/" not in repository_url:
                    continue
                try:
                    full_name = repository_url.split("/repos/", 1)[1]
                    pr_number = int(number)
                except (ValueError, TypeError):
                    continue
                if (
                    full_name.casefold().startswith(f"{org}/".casefold())
                    and repository_is_selected(full_name, repositories)
                    and pr_number > 0
                ):
                    candidates[(full_name, pr_number)] = None
            if len(items) < 100:
                break

    pull_requests: list[PullRequest] = []
    for full_name, number in sorted(candidates):
        payload = runner.json([gh_bin, "api", f"repos/{full_name}/pulls/{number}"])
        if not isinstance(payload, dict):
            raise AgentError(
                f"GitHub returned invalid metadata for {full_name}#{number}"
            )
        pull_request = parse_pull_request(payload, org, mention)
        if pull_request is not None:
            pull_requests.append(pull_request)
    return pull_requests


def fetch_issue_comments(
    runner: CommandRunner,
    *,
    gh_bin: str,
    repo_full_name: str,
    number: int,
) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    for page in range(1, 11):
        payload = runner.json(
            [
                gh_bin,
                "api",
                "-X",
                "GET",
                f"repos/{repo_full_name}/issues/{number}/comments",
                "-f",
                "per_page=100",
                "-f",
                f"page={page}",
            ]
        )
        if not isinstance(payload, list):
            raise AgentError(
                f"GitHub returned invalid comments for {repo_full_name}#{number}"
            )
        comments.extend(comment for comment in payload if isinstance(comment, dict))
        if len(payload) < 100:
            break
    return comments


def fetch_issue_code_anchor(
    runner: CommandRunner,
    *,
    gh_bin: str,
    repo_full_name: str,
    repository_payload: dict[str, Any],
    preferred_branch: str | None = None,
) -> tuple[str, str]:
    default_branch = repository_payload.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        raise AgentError(f"GitHub returned no default branch for {repo_full_name}")

    code_ref = preferred_branch or default_branch
    reference_payload = runner.json(
        [gh_bin, "api", f"repos/{repo_full_name}/git/ref/heads/{code_ref}"]
    )
    if not isinstance(reference_payload, dict):
        raise AgentError(f"GitHub returned invalid branch metadata for {repo_full_name}")

    try:
        code_sha = _validate_sha(reference_payload["object"]["sha"], "issue code SHA")
    except (KeyError, TypeError) as exc:
        raise AgentError(
            f"GitHub returned incomplete branch metadata for {repo_full_name}"
        ) from exc
    return code_ref, code_sha


def _is_controller_issue_comment(body: str, marker: str = DEFAULT_MARKER) -> bool:
    return f"<!-- {marker}:v1 kind=issue " in body


def _has_matching_issue_review(
    issue: Issue,
    comments: list[dict[str, Any]],
    mention: str,
    marker: str = DEFAULT_MARKER,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> bool:
    request_token = (
        "body" if issue.request_comment_id is None else str(issue.request_comment_id)
    )
    for comment in comments:
        comment_body = comment.get("body") or ""
        if not isinstance(comment_body, str):
            continue
        for raw_fields in re.findall(
            rf"<!-- {re.escape(marker)}:v1 ([^>]+) -->", comment_body
        ):
            fields = dict(
                part.split("=", 1) for part in raw_fields.split() if "=" in part
            )
            if (
                fields.get("kind") == "issue"
                and fields.get("repo") == issue.repo_full_name
                and fields.get("issue") == str(issue.number)
                and fields.get("body") == issue.body_sha256
                and fields.get("request") == request_token
                and fields.get("request_body") == issue.request_body_sha256
            ):
                return True

        # Before Issue support was automated, this agent may have posted manual
        # responses. Recognize only an exact-body result after
        # the triggering comment so upgrading the controller does not duplicate it.
        try:
            comment_id = int(comment["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            issue.request_comment_id is not None
            and comment_id <= issue.request_comment_id
        ):
            continue
        if (
            issue.body_sha256 in comment_body
            and agent_name in comment_body
            and not contains_exact_mention(comment_body, mention)
            and ("APPROVED" in comment_body or "CHANGES REQUESTED" in comment_body)
        ):
            return True
    return False


def parse_issue(
    payload: dict[str, Any],
    repository_payload: dict[str, Any],
    comments: list[dict[str, Any]],
    *,
    org: str,
    mention: str,
    code_ref: str,
    code_sha: str,
    marker: str = DEFAULT_MARKER,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> Issue | None:
    try:
        repo_full_name = repository_payload["full_name"]
        repo_owner = repository_payload["owner"]["login"]
        repo_name = repository_payload["name"]
        body = payload.get("body") or ""
    except (KeyError, TypeError) as exc:
        raise AgentError("GitHub returned incomplete issue metadata") from exc

    if not all(
        isinstance(value, str)
        for value in (repo_full_name, repo_owner, repo_name, body)
    ):
        raise AgentError("GitHub returned invalid issue repository metadata")
    if repo_owner.casefold() != org.casefold():
        return None
    if payload.get("state") != "open" or payload.get("pull_request") is not None:
        return None
    if SAFE_COMPONENT_RE.fullmatch(repo_name) is None:
        raise AgentError(f"unsafe repository name returned by GitHub: {repo_name!r}")

    request_comments: list[dict[str, Any]] = []
    for comment in comments:
        comment_body = comment.get("body") or ""
        if (
            isinstance(comment_body, str)
            and not _is_controller_issue_comment(comment_body, marker)
            and contains_exact_mention(comment_body, mention)
        ):
            request_comments.append(comment)

    if request_comments:

        def request_order(comment: dict[str, Any]) -> tuple[str, int]:
            created_at = comment.get("created_at")
            try:
                comment_id = int(comment.get("id"))
            except (TypeError, ValueError):
                comment_id = 0
            return (created_at if isinstance(created_at, str) else "", comment_id)

        request = max(request_comments, key=request_order)
        try:
            request_comment_id = int(request["id"])
            request_comment_url = str(request["html_url"])
            request_comment_updated_at = str(request["updated_at"])
            request_body = request.get("body") or ""
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError(
                "GitHub returned incomplete issue request metadata"
            ) from exc
        if request_comment_id <= 0 or not isinstance(request_body, str):
            raise AgentError("GitHub returned invalid issue request metadata")
    elif contains_exact_mention(body, mention):
        request_comment_id = None
        request_comment_url = None
        request_comment_updated_at = None
        request_body = body
    else:
        return None

    try:
        number = int(payload["number"])
        url = str(payload["html_url"])
        title = str(payload["title"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AgentError("GitHub returned incomplete issue metadata") from exc
    if number <= 0 or not code_ref:
        raise AgentError("GitHub returned invalid issue metadata")

    issue = Issue(
        repo_full_name=repo_full_name,
        repo_name=repo_name,
        number=number,
        url=url,
        title=title,
        body=body,
        body_sha256=body_digest(body),
        code_ref=code_ref,
        code_sha=code_sha,
        request_comment_id=request_comment_id,
        request_comment_url=request_comment_url,
        request_comment_updated_at=request_comment_updated_at,
        request_body=request_body,
        request_body_sha256=body_digest(request_body),
    )
    return dataclasses.replace(
        issue,
        has_matching_review=_has_matching_issue_review(
            issue, comments, mention, marker, agent_name
        ),
    )


def fetch_issue(
    runner: CommandRunner,
    *,
    gh_bin: str,
    org: str,
    mention: str,
    repo_full_name: str,
    number: int,
    preferred_branch: str | None = None,
    marker: str = DEFAULT_MARKER,
    agent_name: str = DEFAULT_AGENT_NAME,
    repositories: Sequence[str] = (),
) -> Issue | None:
    payload = runner.json([gh_bin, "api", f"repos/{repo_full_name}/issues/{number}"])
    repository_payload = runner.json([gh_bin, "api", f"repos/{repo_full_name}"])
    if not isinstance(payload, dict) or not isinstance(repository_payload, dict):
        raise AgentError(
            f"GitHub returned invalid metadata for {repo_full_name}#{number}"
        )
    comments = fetch_issue_comments(
        runner,
        gh_bin=gh_bin,
        repo_full_name=repo_full_name,
        number=number,
    )
    code_ref, code_sha = fetch_issue_code_anchor(
        runner,
        gh_bin=gh_bin,
        repo_full_name=repo_full_name,
        repository_payload=repository_payload,
        preferred_branch=preferred_branch,
    )
    issue = parse_issue(
        payload,
        repository_payload,
        comments,
        org=org,
        mention=mention,
        code_ref=code_ref,
        code_sha=code_sha,
        marker=marker,
        agent_name=agent_name,
    )
    if issue is not None and not repository_is_selected(
        issue.repo_full_name, repositories
    ):
        return None
    return issue


def discover_issues(
    runner: CommandRunner,
    *,
    gh_bin: str,
    org: str,
    mention: str,
    preferred_branch: str | None = None,
    marker: str = DEFAULT_MARKER,
    agent_name: str = DEFAULT_AGENT_NAME,
    repositories: Sequence[str] = (),
) -> list[Issue]:
    candidates: dict[tuple[str, int], None] = {}
    scopes = [f"repo:{repository}" for repository in repositories] or [f"org:{org}"]
    for scope in scopes:
        query = f'{scope} is:issue is:open in:body,comments "{mention}"'
        for page in range(1, 11):
            payload = runner.json(
                [
                    gh_bin, "api", "-X", "GET", "search/issues", "-f", f"q={query}",
                    "-f", "per_page=100", "-f", f"page={page}",
                ]
            )
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise AgentError("GitHub issue search returned an invalid response")
            for item in items:
                if not isinstance(item, dict) or item.get("pull_request") is not None:
                    continue
                repository_url = item.get("repository_url")
                number = item.get("number")
                if not isinstance(repository_url, str) or "/repos/" not in repository_url:
                    continue
                try:
                    full_name = repository_url.split("/repos/", 1)[1]
                    issue_number = int(number)
                except (ValueError, TypeError):
                    continue
                if (
                    full_name.casefold().startswith(f"{org}/".casefold())
                    and repository_is_selected(full_name, repositories)
                    and issue_number > 0
                ):
                    candidates[(full_name, issue_number)] = None
            if len(items) < 100:
                break

    issues: list[Issue] = []
    for full_name, number in sorted(candidates):
        issue = fetch_issue(
            runner,
            gh_bin=gh_bin,
            org=org,
            mention=mention,
            repo_full_name=full_name,
            number=number,
            preferred_branch=preferred_branch,
            marker=marker,
            agent_name=agent_name,
            repositories=repositories,
        )
        if issue is not None:
            issues.append(issue)
    return issues


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


def _coverage_area_text() -> str:
    return "\n".join(f"  - {area}" for area in COVERAGE_AREAS)


def _changed_file_text(changed_files: Sequence[str]) -> str:
    return "\n".join(f"  - {path}" for path in changed_files) or "  - (none)"


def build_review_prompt(
    pull_request: PullRequest,
    changed_files: Sequence[str] = (),
    review_context: str = DEFAULT_REVIEW_CONTEXT,
) -> str:
    return f"""Continue this thread's existing multi-round review work and context.

Review this exact pull request revision:
- PR: {pull_request.url}
- Repository: {pull_request.repo_full_name}
- PR number: {pull_request.number}
- Base ref: {pull_request.base_ref}
- Base SHA: {pull_request.base_sha}
- Head SHA: {pull_request.head_sha}
- PR body SHA-256: {pull_request.body_sha256}

The checkout at your current working directory is detached at the exact head SHA. Review the
complete base..head diff, not only the latest commit. Judge whether the change delivers the
intended workflow reliably within the repository's stated scope.

Additional review context configured for this agent:
{review_context}

Do not implement fixes, commit, push, publish a GitHub review, or post a GitHub comment; the
controller publishes only after revalidating all SHAs. Remove temporary test changes.

The controller's exact changed-file inventory is:
{_changed_file_text(changed_files)}

Complete one product-focused pass before returning status=complete:
1. Account for every inventory path exactly once in reviewed_files. Briefly state what was
   inspected; generated/docs-only files do not need deep analysis.
2. Map the linked Issue's acceptance criteria to the changed code and tests. Inspect sibling
   repositories only when a changed interface actually depends on their current contract.
3. Trace the normal end-to-end workflow and its data lifecycle: ingress, validation, persistence,
   readback/downstream use, retry, and user-visible failure behavior where applicable.
4. Audit each coverage area exactly once. Use not_applicable with a concrete reason:
{_coverage_area_text()}
5. Re-verify earlier findings against this exact revision. Run the smallest relevant tests,
   linters, format checks, and targeted probes needed to decide normal workflow correctness.
6. After finding a blocker, finish the remaining changed files and normal workflow once, but do
   not expand into speculative hardening or an open-ended search for corner cases.

Return only the JSON object required by the supplied schema. findings must contain every finding
from this pass, with a precise file+line or contract clause, evidence, impact, reproduction, and
required fix. Set blocking=true only when a realistic normal workflow is broken, ordinary data can
be lost/corrupted, a required acceptance criterion is absent, a supported integration or legacy
path regresses, an ordinary retry/failure is unsafe, or relevant tests cannot establish the core
behavior. Evaluate security hardening, extreme limits, theoretical races, and unsupported inputs
according to the repository requirements and configured review context; do not turn speculative
concerns into blockers. Use status=superseded if the exact revision cannot be verified
and status=error only if the review cannot be completed reliably. The controller derives the
verdict.

Set reviewed_head_sha to exactly {pull_request.head_sha}.
"""


def build_issue_review_prompt(
    issue: Issue, review_context: str = DEFAULT_REVIEW_CONTEXT
) -> str:
    request_source = (
        "Issue body"
        if issue.request_comment_id is None
        else f"comment {issue.request_comment_id} ({issue.request_comment_url})"
    )
    return f"""Continue this thread's existing multi-round review work and context.

Review this exact GitHub Issue revision and the proposal/contract it contains:
- Issue: {issue.url}
- Repository: {issue.repo_full_name}
- Issue number: {issue.number}
- Issue body SHA-256: {issue.body_sha256}
- Triggering review request: {request_source}
- Review request body SHA-256: {issue.request_body_sha256}
- Code checkout ref: {issue.code_ref}
- Code checkout SHA: {issue.code_sha}

The checkout is detached at the exact code checkout SHA. This is a single, bounded
pre-implementation review. Decide
whether a developer can implement a useful end-to-end workflow without making a major product or
integration decision that the Issue leaves undefined. Existing code is feasibility and
compatibility evidence; the absence of the proposed feature is not a finding.

Additional review context configured for this agent:
{review_context}

Blocking threshold:
- Block only when the core workflow, ownership/interface, persistence/data lifecycle, supported
  compatibility path, ordinary failure behavior, or testable acceptance criteria are contradictory
  or materially undefined and would force the implementer to redesign or guess product behavior.
- Treat implementation details that can be decided and tested in the PR as non-blocking: exact
  helper structure, command spelling, log wording, exhaustive fixtures, rare restart ordering, and
  similar mechanics.
- Follow the repository's documented security, scale, compatibility, and operational requirements.
  Do not add speculative requirements that are not in scope.
- Put future slices and optional hardening in residual_risks. Do not require this Issue to design
  the entire future system.

Keep investigation proportional. Read the Issue, explicitly linked authority, and only the minimum
existing code/docs needed to confirm current interfaces. Do not inventory the repository, rerun
broad suites, or speculate about the later implementation.

Do not implement fixes, edit the Issue, change labels, commit, push, publish a GitHub review, or
post a GitHub comment; the controller will publish your result only after it revalidates the body,
request, and code SHA. Remove temporary changes.

Before returning status=complete, you must:
1. Map the requested user workflow from trigger/input through result and user-visible failure.
2. Identify the owner and interface at each changed integration boundary, plus any persisted shape
   or compatibility requirement that the implementation must preserve.
3. Confirm ordinary failure, retry/duplicate, and recovery expectations only where relevant to the
   proposed workflow.
4. Check that acceptance criteria can be demonstrated by proportionate implementation tests.
5. List only sources actually relied upon in reviewed_files and audit each coverage area once:
{_coverage_area_text()}
6. Re-verify relevant earlier findings under this product-focused threshold. Use targeted commands
   only when they can decide a concrete contract question.

The exact Issue body at discovery time is enclosed below:
<exact_issue_body>
{issue.body}
</exact_issue_body>

The exact triggering review request is enclosed below:
<exact_review_request>
{issue.request_body}
</exact_review_request>

Return only the JSON object required by the supplied schema. Findings need a precise Issue clause
or code anchor with evidence, realistic impact, reproduction, and required fix. Apply the blocking
threshold above. Use status=complete after this one bounded pass, status=superseded if the exact
revision cannot be verified, and status=error only if the review cannot be completed reliably.

For schema compatibility, set reviewed_head_sha to the code checkout SHA exactly:
{issue.code_sha}
"""


def build_codex_command(
    *,
    codex_bin: str,
    session_id: str,
    worktree: Path,
    output_path: Path,
    bypass_sandbox: bool = False,
) -> list[str]:
    command = [codex_bin]
    if bypass_sandbox:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    return [
        *command,
        "-C",
        str(worktree),
        "exec",
        "--output-schema",
        str(SCHEMA_PATH),
        "--output-last-message",
        str(output_path),
        "resume",
        session_id,
        "-",
    ]


def validate_sweep_result(
    payload: Any,
    expected_head_sha: str,
    required_files: Sequence[str] | None,
) -> SweepResult:
    if not isinstance(payload, dict):
        raise AgentError("Codex sweep result is not a JSON object")
    expected_fields = {
        "status",
        "reviewed_head_sha",
        "summary",
        "sweep_complete",
        "reviewed_files",
        "coverage",
        "checks_run",
        "findings",
        "residual_risks",
    }
    if set(payload) != expected_fields:
        raise AgentError("Codex sweep result has unexpected fields")

    status = payload.get("status")
    reviewed_head_sha = payload.get("reviewed_head_sha")
    summary = payload.get("summary")
    sweep_complete = payload.get("sweep_complete")
    if status not in {"complete", "superseded", "error"}:
        raise AgentError("Codex sweep result has an invalid status")
    if (
        not isinstance(reviewed_head_sha, str)
        or SHA_RE.fullmatch(reviewed_head_sha) is None
    ):
        raise AgentError("Codex sweep result has an invalid reviewed_head_sha")
    if reviewed_head_sha != expected_head_sha:
        raise AgentError("Codex reviewed a different head SHA; will retry")
    if not isinstance(summary, str) or not summary.strip():
        raise AgentError("Codex sweep result has an empty summary")
    if not isinstance(sweep_complete, bool):
        raise AgentError("Codex sweep result has an invalid sweep_complete")

    raw_files = payload.get("reviewed_files")
    if not isinstance(raw_files, list):
        raise AgentError("Codex sweep result has invalid reviewed_files")
    reviewed_files: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    allowed_file_statuses = {
        "reviewed",
        "generated",
        "test_only",
        "documentation",
        "not_applicable",
    }
    for item in raw_files:
        if not isinstance(item, dict) or set(item) != {"path", "status", "evidence"}:
            raise AgentError("Codex sweep result has an invalid reviewed file")
        path = item.get("path")
        file_status = item.get("status")
        evidence = item.get("evidence")
        if not isinstance(path, str) or not path.strip() or path in seen_paths:
            raise AgentError(
                "Codex sweep result has an invalid or duplicate reviewed path"
            )
        if file_status not in allowed_file_statuses:
            raise AgentError("Codex sweep result has an invalid reviewed file status")
        if not isinstance(evidence, str) or not evidence.strip():
            raise AgentError("Codex sweep result has empty reviewed file evidence")
        seen_paths.add(path)
        reviewed_files.append(
            {"path": path, "status": str(file_status), "evidence": evidence.strip()}
        )

    raw_coverage = payload.get("coverage")
    if not isinstance(raw_coverage, list):
        raise AgentError("Codex sweep result has invalid coverage")
    coverage: list[dict[str, str]] = []
    seen_areas: set[str] = set()
    for item in raw_coverage:
        if not isinstance(item, dict) or set(item) != {"area", "status", "evidence"}:
            raise AgentError("Codex sweep result has an invalid coverage item")
        area = item.get("area")
        coverage_status = item.get("status")
        evidence = item.get("evidence")
        if area not in COVERAGE_AREAS or area in seen_areas:
            raise AgentError(
                "Codex sweep result has an invalid or duplicate coverage area"
            )
        if coverage_status not in {"covered", "finding", "not_applicable"}:
            raise AgentError("Codex sweep result has an invalid coverage status")
        if not isinstance(evidence, str) or not evidence.strip():
            raise AgentError("Codex sweep result has empty coverage evidence")
        seen_areas.add(str(area))
        coverage.append(
            {
                "area": str(area),
                "status": str(coverage_status),
                "evidence": evidence.strip(),
            }
        )

    raw_checks = payload.get("checks_run")
    if not isinstance(raw_checks, list):
        raise AgentError("Codex sweep result has invalid checks_run")
    checks_run: list[dict[str, str]] = []
    for item in raw_checks:
        if not isinstance(item, dict) or set(item) != {"command", "result", "details"}:
            raise AgentError("Codex sweep result has an invalid check")
        command = item.get("command")
        check_result = item.get("result")
        details = item.get("details")
        if not isinstance(command, str) or not command.strip():
            raise AgentError("Codex sweep result has an empty check command")
        if check_result not in {"passed", "failed", "not_run"}:
            raise AgentError("Codex sweep result has an invalid check result")
        if not isinstance(details, str) or not details.strip():
            raise AgentError("Codex sweep result has empty check details")
        checks_run.append(
            {
                "command": command.strip(),
                "result": str(check_result),
                "details": details.strip(),
            }
        )

    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise AgentError("Codex sweep result has invalid findings")
    findings: list[Finding] = []
    finding_fields = {
        "severity",
        "blocking",
        "title",
        "path",
        "line",
        "contract_clause",
        "evidence",
        "impact",
        "reproduction",
        "required_fix",
    }
    for item in raw_findings:
        if not isinstance(item, dict) or set(item) != finding_fields:
            raise AgentError("Codex sweep result has an invalid finding")
        severity = item.get("severity")
        blocking = item.get("blocking")
        title = item.get("title")
        path = item.get("path")
        line = item.get("line")
        contract_clause = item.get("contract_clause")
        if severity not in {"P0", "P1", "P2", "P3"} or not isinstance(blocking, bool):
            raise AgentError("Codex sweep result has an invalid finding priority")
        if not isinstance(title, str) or not title.strip():
            raise AgentError("Codex sweep result has an empty finding title")
        if path is not None and (not isinstance(path, str) or not path.strip()):
            raise AgentError("Codex sweep result has an invalid finding path")
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line <= 0
        ):
            raise AgentError("Codex sweep result has an invalid finding line")
        if line is not None and path is None:
            raise AgentError("Codex sweep finding line has no path")
        if contract_clause is not None and (
            not isinstance(contract_clause, str) or not contract_clause.strip()
        ):
            raise AgentError("Codex sweep result has an invalid contract clause")
        if (path is None or line is None) and contract_clause is None:
            raise AgentError("Codex sweep finding has no precise source anchor")
        details: dict[str, str] = {}
        for field in ("evidence", "impact", "reproduction", "required_fix"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise AgentError(f"Codex sweep finding has empty {field}")
            details[field] = value.strip()
        findings.append(
            Finding(
                severity=str(severity),
                blocking=blocking,
                title=title.strip(),
                path=path.strip() if isinstance(path, str) else None,
                line=line,
                contract_clause=(
                    contract_clause.strip()
                    if isinstance(contract_clause, str)
                    else None
                ),
                evidence=details["evidence"],
                impact=details["impact"],
                reproduction=details["reproduction"],
                required_fix=details["required_fix"],
            )
        )

    raw_risks = payload.get("residual_risks")
    if not isinstance(raw_risks, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_risks
    ):
        raise AgentError("Codex sweep result has invalid residual_risks")
    residual_risks = tuple(item.strip() for item in raw_risks)

    if status == "complete":
        if not sweep_complete:
            raise AgentError("Codex marked a complete sweep as incomplete")
        if seen_areas != set(COVERAGE_AREAS):
            missing = sorted(set(COVERAGE_AREAS) - seen_areas)
            raise AgentError(
                f"Codex sweep omitted coverage areas: {', '.join(missing)}"
            )
        if required_files is not None and not set(required_files).issubset(seen_paths):
            missing = sorted(set(required_files) - seen_paths)
            raise AgentError(f"Codex sweep file inventory mismatch: missing={missing}")
        if any(item["status"] == "finding" for item in coverage) and not findings:
            raise AgentError("Codex sweep reports finding coverage without findings")
    elif sweep_complete:
        raise AgentError("Codex marked a non-complete sweep as complete")

    return SweepResult(
        status=str(status),
        reviewed_head_sha=reviewed_head_sha,
        summary=summary.strip(),
        sweep_complete=sweep_complete,
        reviewed_files=tuple(reviewed_files),
        coverage=tuple(coverage),
        checks_run=tuple(checks_run),
        findings=tuple(findings),
        residual_risks=residual_risks,
    )


def _invoke_codex_sweep(
    runner: CommandRunner,
    *,
    codex_bin: str,
    session_id: str,
    worktree: Path,
    prompt: str,
    expected_head_sha: str,
    required_files: Sequence[str] | None,
    state_dir: Path,
    timeout: int,
    pass_label: str,
    bypass_sandbox: bool = False,
) -> SweepResult:
    temp_dir = state_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, output_name = tempfile.mkstemp(
        prefix="sweep-result-", suffix=".json", dir=temp_dir
    )
    os.close(file_descriptor)
    output_path = Path(output_name)
    started = time.monotonic()
    LOG.info("starting Codex %s", pass_label)
    try:
        runner.run_with_heartbeat(
            build_codex_command(
                codex_bin=codex_bin,
                session_id=session_id,
                worktree=worktree,
                output_path=output_path,
                bypass_sandbox=bypass_sandbox,
            ),
            label=f"Codex {pass_label}",
            input_text=prompt,
            timeout=timeout,
        )
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentError(
                "Codex did not write a valid structured sweep result"
            ) from exc
        result = validate_sweep_result(payload, expected_head_sha, required_files)
        LOG.info(
            "completed Codex %s in %ds: status=%s, findings=%d",
            pass_label,
            round(time.monotonic() - started),
            result.status,
            len(result.findings),
        )
        return result
    finally:
        with contextlib.suppress(OSError):
            output_path.unlink()


def _finding_body(
    findings: Sequence[Finding], residual_risks: Sequence[str]
) -> str | None:
    if not findings and not residual_risks:
        return None
    sections: list[str] = []
    for index, finding in enumerate(findings, start=1):
        if finding.path is not None and finding.line is not None:
            location = f"`{finding.path}:{finding.line}`"
        elif finding.path is not None:
            location = f"`{finding.path}`"
        else:
            location = "contract"
        lines = [
            f"### {index}. [{finding.severity}] {finding.title}",
            "",
            f"- Blocking: `{'yes' if finding.blocking else 'no'}`",
            f"- Location: {location}",
        ]
        if finding.contract_clause is not None:
            lines.append(f"- Contract: {finding.contract_clause}")
        lines.extend(
            [
                f"- Evidence: {finding.evidence}",
                f"- Impact: {finding.impact}",
                f"- Reproduction: {finding.reproduction}",
                f"- Required fix: {finding.required_fix}",
            ]
        )
        sections.append("\n".join(lines))
    if residual_risks:
        risks = "\n".join(f"- {risk}" for risk in residual_risks)
        sections.append(f"### Residual risks\n\n{risks}")
    return "\n\n".join(sections)


def synthesize_review_result(
    expected_head_sha: str,
    sweeps: Sequence[SweepResult],
    findings: Sequence[Finding],
    *,
    strategy: str = "product_review",
) -> ReviewResult:
    blocking_count = sum(finding.blocking for finding in findings)
    verdict = "changes_requested" if blocking_count else "approved"
    reviewed_paths = sorted(
        {item["path"] for sweep in sweeps for item in sweep.reviewed_files}
    )
    risks = tuple(
        dict.fromkeys(risk for sweep in sweeps for risk in sweep.residual_risks)
    )
    if strategy == "bounded_issue_contract":
        opening = "Bounded Issue workflow and contract review completed in one pass"
    elif strategy == "product_review":
        opening = "Product-focused review completed in one pass"
    else:
        raise AgentError(f"unknown review strategy: {strategy}")
    summary = (
        f"{opening}, covering {len(reviewed_paths)} evidence source(s) and all "
        f"{len(COVERAGE_AREAS)} review areas. Found {blocking_count} blocking and "
        f"{len(findings) - blocking_count} non-blocking finding(s)."
    )
    sweep_details = {
        "strategy": strategy,
        "passes": len(sweeps),
        "reviewed_files": reviewed_paths,
        "coverage": [dict(item) for item in sweeps[-1].coverage],
        "checks_run": [dict(item) for sweep in sweeps for item in sweep.checks_run],
        "findings": [finding.as_dict() for finding in findings],
        "residual_risks": list(risks),
    }
    return ReviewResult(
        verdict=verdict,
        reviewed_head_sha=expected_head_sha,
        summary=summary,
        review_body=_finding_body(findings, risks),
        sweep=sweep_details,
    )


def _run_product_review(
    runner: CommandRunner,
    *,
    codex_bin: str,
    session_id: str,
    worktree: Path,
    expected_head_sha: str,
    required_files: Sequence[str] | None,
    primary_prompt: str,
    state_dir: Path,
    primary_timeout: int,
    review_label: str,
    bypass_sandbox: bool = False,
) -> ReviewResult:
    primary = _invoke_codex_sweep(
        runner,
        codex_bin=codex_bin,
        session_id=session_id,
        worktree=worktree,
        prompt=primary_prompt,
        expected_head_sha=expected_head_sha,
        required_files=required_files,
        state_dir=state_dir,
        timeout=primary_timeout,
        pass_label=f"{review_label} product review",
        bypass_sandbox=bypass_sandbox,
    )
    if primary.status != "complete":
        return ReviewResult(primary.status, expected_head_sha, primary.summary, None)

    return synthesize_review_result(
        expected_head_sha,
        (primary,),
        primary.findings,
        strategy="product_review",
    )


def _run_issue_contract_review(
    runner: CommandRunner,
    *,
    codex_bin: str,
    session_id: str,
    worktree: Path,
    issue: Issue,
    state_dir: Path,
    timeout: int,
    review_context: str = DEFAULT_REVIEW_CONTEXT,
    bypass_sandbox: bool = False,
) -> ReviewResult:
    primary = _invoke_codex_sweep(
        runner,
        codex_bin=codex_bin,
        session_id=session_id,
        worktree=worktree,
        prompt=build_issue_review_prompt(issue, review_context),
        expected_head_sha=issue.code_sha,
        required_files=None,
        state_dir=state_dir,
        timeout=timeout,
        pass_label=f"Issue {issue.key} workflow contract review",
        bypass_sandbox=bypass_sandbox,
    )
    if primary.status != "complete":
        return ReviewResult(primary.status, issue.code_sha, primary.summary, None)

    return synthesize_review_result(
        issue.code_sha,
        (primary,),
        primary.findings,
        strategy="bounded_issue_contract",
    )


def changed_files_for_pull_request(
    runner: CommandRunner,
    *,
    worktree: Path,
    pull_request: PullRequest,
) -> tuple[str, ...]:
    completed = runner.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACDMRTUXB",
            "-z",
            f"{pull_request.base_sha}...{pull_request.head_sha}",
        ],
        cwd=worktree,
    )
    changed_files = tuple(path for path in completed.stdout.split("\0") if path)
    if not changed_files or len(changed_files) != len(set(changed_files)):
        raise AgentError(
            f"cannot build exact changed-file inventory for {pull_request.key}"
        )
    return changed_files


def run_codex_review(
    runner: CommandRunner,
    *,
    codex_bin: str,
    session_id: str,
    worktree: Path,
    pull_request: PullRequest,
    state_dir: Path,
    timeout: int,
    review_context: str = DEFAULT_REVIEW_CONTEXT,
    bypass_sandbox: bool = False,
) -> ReviewResult:
    changed_files = changed_files_for_pull_request(
        runner, worktree=worktree, pull_request=pull_request
    )
    return _run_product_review(
        runner,
        codex_bin=codex_bin,
        session_id=session_id,
        worktree=worktree,
        expected_head_sha=pull_request.head_sha,
        required_files=changed_files,
        primary_prompt=build_review_prompt(pull_request, changed_files, review_context),
        state_dir=state_dir,
        primary_timeout=timeout,
        review_label=pull_request.key,
        bypass_sandbox=bypass_sandbox,
    )


def run_codex_issue_review(
    runner: CommandRunner,
    *,
    codex_bin: str,
    session_id: str,
    worktree: Path,
    issue: Issue,
    state_dir: Path,
    timeout: int,
    review_context: str = DEFAULT_REVIEW_CONTEXT,
    bypass_sandbox: bool = False,
) -> ReviewResult:
    return _run_issue_contract_review(
        runner,
        codex_bin=codex_bin,
        session_id=session_id,
        worktree=worktree,
        issue=issue,
        state_dir=state_dir,
        timeout=timeout,
        review_context=review_context,
        bypass_sandbox=bypass_sandbox,
    )


def same_revision(left: PullRequest, right: PullRequest) -> bool:
    return left.key == right.key and left.fingerprint == right.fingerprint


def same_issue_revision(left: Issue, right: Issue) -> bool:
    return left.key == right.key and left.fingerprint == right.fingerprint


def fetch_pull_request(
    runner: CommandRunner,
    *,
    gh_bin: str,
    org: str,
    mention: str,
    repo_full_name: str,
    number: int,
    repositories: Sequence[str] = (),
) -> PullRequest | None:
    payload = runner.json([gh_bin, "api", f"repos/{repo_full_name}/pulls/{number}"])
    if not isinstance(payload, dict):
        raise AgentError(
            f"GitHub returned invalid metadata for {repo_full_name}#{number}"
        )
    pull_request = parse_pull_request(payload, org, mention)
    if pull_request is not None and not repository_is_selected(
        pull_request.repo_full_name, repositories
    ):
        return None
    return pull_request


def render_review_comment(
    pull_request: PullRequest,
    result: ReviewResult,
    *,
    agent_name: str = DEFAULT_AGENT_NAME,
    marker: str = DEFAULT_MARKER,
) -> str:
    label = "APPROVED" if result.verdict == "approved" else "CHANGES REQUESTED"
    body = result.review_body or "Review completed."
    marker = (
        f"<!-- {marker}:v1 "
        f"repo={pull_request.repo_full_name} pr={pull_request.number} "
        f"base={pull_request.base_sha} head={pull_request.head_sha} "
        f"body={pull_request.body_sha256} verdict={result.verdict} -->"
    )
    return (
        f"## {agent_name} review — {label}\n\n"
        f"Exact head: `{pull_request.head_sha}`  \n"
        f"Base: `{pull_request.base_sha}`\n\n"
        f"{result.summary}\n\n{body}\n\n"
        f"_Generated by Codex using the continuing {agent_name} review thread._\n\n"
        f"{marker}\n"
    )


def render_issue_review_comment(
    issue: Issue,
    result: ReviewResult,
    *,
    agent_name: str = DEFAULT_AGENT_NAME,
    marker: str = DEFAULT_MARKER,
) -> str:
    label = "APPROVED" if result.verdict == "approved" else "CHANGES REQUESTED"
    body = result.review_body or "No blocking findings."
    request_token = (
        "body" if issue.request_comment_id is None else str(issue.request_comment_id)
    )
    marker = (
        f"<!-- {marker}:v1 kind=issue "
        f"repo={issue.repo_full_name} issue={issue.number} "
        f"code={issue.code_sha} body={issue.body_sha256} "
        f"request={request_token} request_body={issue.request_body_sha256} "
        f"verdict={result.verdict} -->"
    )
    return (
        f"## {agent_name} Issue review — {label}\n\n"
        f"Exact Issue body: `{issue.body_sha256}`  \n"
        f"Code anchor: `{issue.code_ref}@{issue.code_sha}`\n\n"
        f"{result.summary}\n\n{body}\n\n"
        f"_Generated by Codex using the continuing {agent_name} review thread._\n\n"
        f"{marker}\n"
    )


def publish_review_comment(
    runner: CommandRunner,
    *,
    gh_bin: str,
    pull_request: PullRequest,
    result: ReviewResult,
    state_dir: Path,
    agent_name: str = DEFAULT_AGENT_NAME,
    marker: str = DEFAULT_MARKER,
) -> str:
    temp_dir = state_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, body_name = tempfile.mkstemp(
        prefix="review-comment-", suffix=".md", dir=temp_dir
    )
    body_path = Path(body_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                render_review_comment(
                    pull_request, result, agent_name=agent_name, marker=marker
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
        completed = runner.run(
            [
                gh_bin,
                "pr",
                "comment",
                str(pull_request.number),
                "--repo",
                pull_request.repo_full_name,
                "--body-file",
                str(body_path),
            ]
        )
        return completed.stdout.strip()
    finally:
        with contextlib.suppress(OSError):
            body_path.unlink()


def publish_issue_review_comment(
    runner: CommandRunner,
    *,
    gh_bin: str,
    issue: Issue,
    result: ReviewResult,
    state_dir: Path,
    agent_name: str = DEFAULT_AGENT_NAME,
    marker: str = DEFAULT_MARKER,
) -> str:
    temp_dir = state_dir / "tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, body_name = tempfile.mkstemp(
        prefix="issue-review-comment-", suffix=".md", dir=temp_dir
    )
    body_path = Path(body_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(
                render_issue_review_comment(
                    issue, result, agent_name=agent_name, marker=marker
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
        completed = runner.run(
            [
                gh_bin,
                "issue",
                "comment",
                str(issue.number),
                "--repo",
                issue.repo_full_name,
                "--body-file",
                str(body_path),
            ]
        )
        return completed.stdout.strip()
    finally:
        with contextlib.suppress(OSError):
            body_path.unlink()


def record_review(
    state: dict[str, Any],
    pull_request: PullRequest,
    result: ReviewResult,
    review_url: str,
) -> None:
    state["pull_requests"][pull_request.key] = {
        **pull_request.fingerprint,
        "verdict": result.verdict,
        "review_url": review_url,
        "reviewed_at": utc_now(),
        **({"sweep": result.sweep} if result.sweep is not None else {}),
    }


def record_issue_review(
    state: dict[str, Any],
    issue: Issue,
    result: ReviewResult,
    review_url: str,
) -> None:
    issues = state.setdefault("issues", {})
    if not isinstance(issues, dict):
        raise AgentError("issue state is malformed")
    issues[issue.key] = {
        **issue.fingerprint,
        "verdict": result.verdict,
        "review_url": review_url,
        "reviewed_at": utc_now(),
        **({"sweep": result.sweep} if result.sweep is not None else {}),
    }


@dataclasses.dataclass(frozen=True)
class Config:
    org: str
    repositories: tuple[str, ...]
    mention: str
    agent_name: str
    marker: str
    issue_branch: str | None
    review_context: str
    session_id: str | None
    interval: int
    once: bool
    dry_run: bool
    state_dir: Path
    worktree_root: Path
    codex_timeout: int
    issue_codex_timeout: int
    gh_bin: str
    codex_bin: str
    codex_bypass_sandbox: bool


def process_cycle(
    config: Config,
    runner: CommandRunner,
    store: StateStore,
    state: dict[str, Any],
) -> bool:
    pull_requests = discover_pull_requests(
        runner,
        gh_bin=config.gh_bin,
        org=config.org,
        mention=config.mention,
        repositories=config.repositories,
    )
    LOG.info(
        "found %d open, non-draft pull request(s) with %s",
        len(pull_requests),
        config.mention,
    )
    issue_discovery_succeeded = True
    try:
        issues = discover_issues(
            runner,
            gh_bin=config.gh_bin,
            org=config.org,
            mention=config.mention,
            preferred_branch=config.issue_branch,
            marker=config.marker,
            agent_name=config.agent_name,
            repositories=config.repositories,
        )
    except TransientNetworkError as exc:
        issue_discovery_succeeded = False
        issues = []
        LOG.warning(
            "Issue discovery remains pending after network retries: %s",
            exc,
        )
    except AgentError as exc:
        issue_discovery_succeeded = False
        issues = []
        LOG.error("Issue discovery failed: %s", exc)
    LOG.info("found %d open Issue(s) with %s", len(issues), config.mention)
    workspace = RepositoryWorkspace(
        runner,
        gh_bin=config.gh_bin,
        state_dir=config.state_dir,
        worktree_root=config.worktree_root,
    )
    successful = issue_discovery_succeeded

    for pull_request in pull_requests:
        if not needs_review(state, pull_request):
            LOG.info(
                "up to date: %s at %s", pull_request.key, pull_request.head_sha[:12]
            )
            continue
        if config.dry_run:
            LOG.info("would review: %s %s", pull_request.key, pull_request.url)
            continue
        if config.session_id is None:
            raise AgentError("--session-id or REVIEW_SESSION_ID is required")

        LOG.info("reviewing: %s at %s", pull_request.key, pull_request.head_sha[:12])
        try:
            with workspace.checkout(pull_request) as worktree:
                result = run_codex_review(
                    runner,
                    codex_bin=config.codex_bin,
                    session_id=config.session_id,
                    worktree=worktree,
                    pull_request=pull_request,
                    state_dir=config.state_dir,
                    timeout=config.codex_timeout,
                    review_context=config.review_context,
                    bypass_sandbox=config.codex_bypass_sandbox,
                )
            if result.verdict in {"superseded", "error"}:
                LOG.warning(
                    "Codex returned %s for %s: %s",
                    result.verdict,
                    pull_request.key,
                    result.summary,
                )
                successful = False
                continue

            current = fetch_pull_request(
                runner,
                gh_bin=config.gh_bin,
                org=config.org,
                mention=config.mention,
                repo_full_name=pull_request.repo_full_name,
                number=pull_request.number,
                repositories=config.repositories,
            )
            if current is None or not same_revision(pull_request, current):
                LOG.warning(
                    "%s changed before publication; discarding stale result",
                    pull_request.key,
                )
                successful = False
                continue

            review_url = publish_review_comment(
                runner,
                gh_bin=config.gh_bin,
                pull_request=pull_request,
                result=result,
                state_dir=config.state_dir,
                agent_name=config.agent_name,
                marker=config.marker,
            )
            record_review(state, pull_request, result, review_url)
            store.save(state)
            LOG.info(
                "published %s for %s%s",
                result.verdict,
                pull_request.key,
                f": {review_url}" if review_url else "",
            )
        except TransientNetworkError as exc:
            successful = False
            LOG.warning(
                "network unavailable for %s after retries; revision remains pending "
                "for the next poll: %s",
                pull_request.key,
                exc,
            )
        except AgentError as exc:
            successful = False
            LOG.error("review failed for %s: %s", pull_request.key, exc)

    for issue in issues:
        if not needs_issue_review(state, issue):
            LOG.info("up to date: Issue %s body %s", issue.key, issue.body_sha256[:12])
            continue
        if config.dry_run:
            LOG.info("would review Issue: %s %s", issue.key, issue.url)
            continue
        if config.session_id is None:
            raise AgentError("--session-id or REVIEW_SESSION_ID is required")

        LOG.info(
            "reviewing Issue: %s body %s at %s",
            issue.key,
            issue.body_sha256[:12],
            issue.code_sha[:12],
        )
        try:
            with workspace.checkout_issue(issue) as worktree:
                result = run_codex_issue_review(
                    runner,
                    codex_bin=config.codex_bin,
                    session_id=config.session_id,
                    worktree=worktree,
                    issue=issue,
                    state_dir=config.state_dir,
                    timeout=config.issue_codex_timeout,
                    review_context=config.review_context,
                    bypass_sandbox=config.codex_bypass_sandbox,
                )
            if result.verdict in {"superseded", "error"}:
                LOG.warning(
                    "Codex returned %s for Issue %s: %s",
                    result.verdict,
                    issue.key,
                    result.summary,
                )
                successful = False
                continue

            current = fetch_issue(
                runner,
                gh_bin=config.gh_bin,
                org=config.org,
                mention=config.mention,
                repo_full_name=issue.repo_full_name,
                number=issue.number,
                preferred_branch=config.issue_branch,
                marker=config.marker,
                agent_name=config.agent_name,
                repositories=config.repositories,
            )
            if (
                current is None
                or current.has_matching_review
                or not same_issue_revision(issue, current)
            ):
                LOG.warning(
                    "Issue %s changed before publication; discarding stale result",
                    issue.key,
                )
                successful = False
                continue

            review_url = publish_issue_review_comment(
                runner,
                gh_bin=config.gh_bin,
                issue=issue,
                result=result,
                state_dir=config.state_dir,
                agent_name=config.agent_name,
                marker=config.marker,
            )
            record_issue_review(state, issue, result, review_url)
            store.save(state)
            LOG.info(
                "published %s for Issue %s%s",
                result.verdict,
                issue.key,
                f": {review_url}" if review_url else "",
            )
        except TransientNetworkError as exc:
            successful = False
            LOG.warning(
                "network unavailable for Issue %s after retries; request remains "
                "pending for the next poll: %s",
                issue.key,
                exc,
            )
        except AgentError as exc:
            successful = False
            LOG.error("Issue review failed for %s: %s", issue.key, exc)
    return successful


def parse_args(argv: Sequence[str] | None = None) -> Config:
    try:
        load_dotenv(APP_ROOT / ".env")
    except AgentError as exc:
        raise SystemExit(str(exc)) from exc
    parser = argparse.ArgumentParser(
        description=(
            "Poll open GitHub PRs and Issues for a configured mention, resume one Codex "
            "session, and publish revision-scoped review comments."
        )
    )
    parser.add_argument("--org", default=os.environ.get("REVIEW_ORG"))
    parser.add_argument(
        "--repo",
        action="append",
        default=None,
        help="repository to monitor (repeatable; accepts repo or org/repo, and comma-separated values)",
    )
    parser.add_argument(
        "--mention", default=os.environ.get("REVIEW_MENTION", "@codex-review")
    )
    parser.add_argument(
        "--agent-name", default=os.environ.get("REVIEW_AGENT_NAME", DEFAULT_AGENT_NAME)
    )
    parser.add_argument(
        "--marker", default=os.environ.get("REVIEW_MARKER", DEFAULT_MARKER)
    )
    parser.add_argument(
        "--issue-branch",
        default=os.environ.get("REVIEW_ISSUE_BRANCH") or None,
        help="preferred Issue code anchor; defaults to each repository's default branch",
    )
    parser.add_argument(
        "--review-context",
        default=os.environ.get("REVIEW_CONTEXT", DEFAULT_REVIEW_CONTEXT),
        help="additional scope sent to Codex for every review",
    )
    parser.add_argument(
        "--session-id",
        default=os.environ.get("REVIEW_SESSION_ID")
        or os.environ.get("CODEX_THREAD_ID"),
        help=(
            "Codex session to resume (default: REVIEW_SESSION_ID, then CODEX_THREAD_ID)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("REVIEW_INTERVAL", "120")),
        help="poll interval in seconds",
    )
    parser.add_argument(
        "--once", action="store_true", help="run one polling cycle and exit"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="discover and report work without cloning, invoking Codex, or commenting",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(os.environ.get("REVIEW_STATE_DIR", DEFAULT_STATE_DIR)),
    )
    parser.add_argument(
        "--worktree-root",
        type=Path,
        default=Path(os.environ.get("REVIEW_WORKTREE_ROOT", DEFAULT_WORKTREE_ROOT)),
    )
    parser.add_argument(
        "--codex-timeout",
        type=int,
        default=int(os.environ.get("REVIEW_CODEX_TIMEOUT", "720")),
        help="maximum seconds for the PR product review",
    )
    parser.add_argument(
        "--approval-check-timeout",
        type=int,
        default=300,
        help="deprecated compatibility option; approval check has been removed",
    )
    parser.add_argument(
        "--issue-codex-timeout",
        type=int,
        default=int(os.environ.get("REVIEW_ISSUE_CODEX_TIMEOUT", "600")),
        help="maximum seconds for the single Issue workflow/contract review",
    )
    parser.add_argument(
        "--max-review-passes",
        type=int,
        default=2,
        help="deprecated compatibility option; PR review now uses one pass",
    )
    parser.add_argument("--gh-bin", default=os.environ.get("REVIEW_GH_BIN", "gh"))
    parser.add_argument(
        "--codex-bin", default=os.environ.get("REVIEW_CODEX_BIN", "codex")
    )
    parser.add_argument(
        "--codex-bypass-sandbox",
        action=argparse.BooleanOptionalAction,
        default=env_flag("REVIEW_CODEX_BYPASS_SANDBOX"),
        help="run Codex without its approval and sandbox protections (trusted hosts only)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not args.org or not args.org.strip():
        parser.error("--org or REVIEW_ORG is required")
    try:
        repositories = normalize_repositories(
            args.repo if args.repo is not None else [os.environ.get("REVIEW_REPOS", "")],
            args.org,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.codex_timeout <= 0:
        parser.error("--codex-timeout must be positive")
    if args.issue_codex_timeout <= 0:
        parser.error("--issue-codex-timeout must be positive")
    if not args.dry_run and not args.session_id:
        parser.error(
            "--session-id or REVIEW_SESSION_ID is required unless --dry-run is used"
        )
    try:
        contains_exact_mention("", args.mention)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.agent_name.strip():
        parser.error("--agent-name must not be empty")
    if SAFE_COMPONENT_RE.fullmatch(args.marker) is None:
        parser.error("--marker must contain only letters, numbers, dot, underscore, or hyphen")
    if not args.review_context.strip():
        parser.error("--review-context must not be empty")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime
    return Config(
        org=args.org,
        repositories=repositories,
        mention=args.mention,
        agent_name=args.agent_name.strip(),
        marker=args.marker,
        issue_branch=args.issue_branch,
        review_context=args.review_context.strip(),
        session_id=args.session_id,
        interval=args.interval,
        once=args.once,
        dry_run=args.dry_run,
        state_dir=args.state_dir.resolve(),
        worktree_root=args.worktree_root.resolve(),
        codex_timeout=args.codex_timeout,
        issue_codex_timeout=args.issue_codex_timeout,
        gh_bin=args.gh_bin,
        codex_bin=args.codex_bin,
        codex_bypass_sandbox=args.codex_bypass_sandbox,
    )


def verify_prerequisites(config: Config, runner: CommandRunner) -> None:
    """Fail early with setup instructions before a polling cycle starts."""
    try:
        runner.run(["git", "--version"])
    except AgentError as exc:
        raise AgentError(
            "Git is unavailable. Install Git and ensure 'git --version' succeeds."
        ) from exc
    try:
        runner.run([config.gh_bin, "--version"])
    except AgentError as exc:
        raise AgentError(
            f"GitHub CLI ({config.gh_bin!r}) is unavailable. Install it from "
            "https://cli.github.com/, then run 'gh auth login'."
        ) from exc
    try:
        runner.run([config.gh_bin, "auth", "status"])
    except AgentError as exc:
        raise AgentError(
            "GitHub CLI is not authenticated or its credentials are invalid. Run "
            "'gh auth login', then verify repository access with 'gh auth status'."
        ) from exc
    if config.dry_run:
        return
    try:
        runner.run([config.codex_bin, "--version"])
    except AgentError as exc:
        raise AgentError(
            f"Codex CLI ({config.codex_bin!r}) is unavailable. Install Codex, then "
            "run 'codex login'."
        ) from exc
    try:
        runner.run([config.codex_bin, "login", "status"])
    except AgentError as exc:
        raise AgentError(
            "Codex CLI is not authenticated. Run 'codex login', then confirm with "
            "'codex login status'."
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_args(argv)
    runner = CommandRunner()
    store = StateStore(config.state_dir)
    try:
        verify_prerequisites(config, runner)
        with store.locked():
            state = store.load()
            while True:
                try:
                    successful = process_cycle(config, runner, store, state)
                except TransientNetworkError as exc:
                    successful = False
                    LOG.warning(
                        "GitHub network remains unavailable after retries; all "
                        "unrecorded work remains pending: %s",
                        exc,
                    )
                except AgentError as exc:
                    successful = False
                    LOG.error("polling cycle failed: %s", exc)
                if config.once:
                    return 0 if successful else 1
                LOG.info("next poll in %d seconds", config.interval)
                time.sleep(config.interval)
    except AgentError as exc:
        LOG.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOG.info("stopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
