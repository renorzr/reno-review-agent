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
