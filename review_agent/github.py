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
from .models import _validate_sha

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


def fetch_pull_request_base_sha(
    runner: CommandRunner,
    *,
    gh_bin: str,
    pull_request: PullRequest,
) -> str:
    """Return the live base-branch tip, not the possibly stale PR API base SHA."""
    reference_payload = runner.json(
        [
            gh_bin,
            "api",
            f"repos/{pull_request.repo_full_name}/git/ref/heads/{pull_request.base_ref}",
        ]
    )
    if not isinstance(reference_payload, dict):
        raise AgentError(
            f"GitHub returned invalid base branch metadata for {pull_request.key}"
        )
    try:
        return _validate_sha(reference_payload["object"]["sha"], "base SHA")
    except (KeyError, TypeError) as exc:
        raise AgentError(
            f"GitHub returned incomplete base branch metadata for {pull_request.key}"
        ) from exc


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
            pull_requests.append(
                dataclasses.replace(
                    pull_request,
                    base_sha=fetch_pull_request_base_sha(
                        runner, gh_bin=gh_bin, pull_request=pull_request
                    ),
                )
            )
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
