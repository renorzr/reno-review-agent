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

APP_ROOT = Path(__file__).resolve().parent.parent
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
