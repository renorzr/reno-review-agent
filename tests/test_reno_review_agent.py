from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import reno_review_agent as agent

BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
ISSUE_SHA = "4" * 40


def make_pr(
    *, body: str = "please @RenoReviewAgent review", head: str = HEAD_SHA
) -> agent.PullRequest:
    return agent.PullRequest(
        repo_full_name="0xLazAI/example",
        repo_name="example",
        number=7,
        url="https://github.com/0xLazAI/example/pull/7",
        title="Example",
        body=body,
        body_sha256=agent.body_digest(body),
        base_ref="main",
        base_sha=BASE_SHA,
        head_sha=head,
    )


def github_payload(*, body: str = "please @RenoReviewAgent review") -> dict:
    return {
        "number": 7,
        "html_url": "https://github.com/0xLazAI/example/pull/7",
        "title": "Example",
        "body": body,
        "state": "open",
        "draft": False,
        "base": {
            "ref": "main",
            "sha": BASE_SHA,
            "repo": {
                "full_name": "0xLazAI/example",
                "name": "example",
                "owner": {"login": "0xLazAI"},
            },
        },
        "head": {"sha": HEAD_SHA},
    }


def make_issue(
    *,
    body: str = "Contract proposal",
    request_body: str = "@RenoReviewAgent please review",
    request_id: int | None = 42,
    has_matching_review: bool = False,
) -> agent.Issue:
    return agent.Issue(
        repo_full_name="0xLazAI/example",
        repo_name="example",
        number=9,
        url="https://github.com/0xLazAI/example/issues/9",
        title="Contract proposal",
        body=body,
        body_sha256=agent.body_digest(body),
        code_ref="develop",
        code_sha=ISSUE_SHA,
        request_comment_id=request_id,
        request_comment_url=(
            "https://github.com/0xLazAI/example/issues/9#issuecomment-42"
            if request_id is not None
            else None
        ),
        request_comment_updated_at="2026-08-11T00:00:00Z"
        if request_id is not None
        else None,
        request_body=request_body,
        request_body_sha256=agent.body_digest(request_body),
        has_matching_review=has_matching_review,
    )


def issue_payload(*, body: str = "Contract proposal") -> dict:
    return {
        "number": 9,
        "html_url": "https://github.com/0xLazAI/example/issues/9",
        "title": "Contract proposal",
        "body": body,
        "state": "open",
    }


def repository_payload() -> dict:
    return {
        "full_name": "0xLazAI/example",
        "name": "example",
        "owner": {"login": "0xLazAI"},
        "default_branch": "main",
    }


def request_comment(*, body: str = "@RenoReviewAgent please review") -> dict:
    return {
        "id": 42,
        "html_url": "https://github.com/0xLazAI/example/issues/9#issuecomment-42",
        "created_at": "2026-08-11T00:00:00Z",
        "updated_at": "2026-08-11T00:00:00Z",
        "body": body,
    }


def finding_payload(
    *,
    title: str = "Unbounded request",
    path: str | None = "a.py",
    line: int | None = 7,
    blocking: bool = True,
) -> dict:
    return {
        "severity": "P1" if blocking else "P3",
        "blocking": blocking,
        "title": title,
        "path": path,
        "line": line,
        "contract_clause": None,
        "evidence": "The request value reaches allocation without a bound.",
        "impact": "A caller can exhaust worker memory.",
        "reproduction": "Submit a request with count=1000000000.",
        "required_fix": "Reject count above the documented maximum before allocation.",
    }


def sweep_payload(
    *,
    head_sha: str = HEAD_SHA,
    files: tuple[str, ...] = ("a.py",),
    findings: list[dict] | None = None,
    status: str = "complete",
    sweep_complete: bool = True,
) -> dict:
    findings = [] if findings is None else findings
    return {
        "status": status,
        "reviewed_head_sha": head_sha,
        "summary": "Full sweep complete",
        "sweep_complete": sweep_complete,
        "reviewed_files": [
            {
                "path": path,
                "status": "reviewed",
                "evidence": f"Inspected behavior and call sites in {path}.",
            }
            for path in files
        ],
        "coverage": [
            {
                "area": area,
                "status": "covered",
                "evidence": f"Traced {area} through the changed behavior.",
            }
            for area in agent.COVERAGE_AREAS
        ],
        "checks_run": [
            {
                "command": "python3 -m unittest",
                "result": "passed",
                "details": "All targeted tests passed.",
            }
        ],
        "findings": findings,
        "residual_risks": [],
    }


def validated_sweep(
    *,
    findings: list[dict] | None = None,
    files: tuple[str, ...] = ("a.py",),
    head_sha: str = HEAD_SHA,
) -> agent.SweepResult:
    return agent.validate_sweep_result(
        sweep_payload(head_sha=head_sha, files=files, findings=findings),
        head_sha,
        files,
    )


class FakeRunner:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def json(self, args, *, cwd=None, timeout=None):
        self.calls.append(list(args))
        return self.responses.pop(0)

    def run(self, args, *, cwd=None, input_text=None, timeout=None):
        self.calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")


class MentionTests(unittest.TestCase):
    def test_exact_handle(self) -> None:
        self.assertTrue(
            agent.contains_exact_mention("@RenoReviewAgent please", "@RenoReviewAgent")
        )
        self.assertTrue(
            agent.contains_exact_mention("(@RenoReviewAgent)", "@RenoReviewAgent")
        )
        self.assertFalse(
            agent.contains_exact_mention("@RenoReviewAgent2", "@RenoReviewAgent")
        )
        self.assertFalse(
            agent.contains_exact_mention("@RenoReviewAgent-extra", "@RenoReviewAgent")
        )
        self.assertFalse(
            agent.contains_exact_mention("x@RenoReviewAgent", "@RenoReviewAgent")
        )
        self.assertFalse(
            agent.contains_exact_mention("@renoreviewagent", "@RenoReviewAgent")
        )


class CommandRunnerTests(unittest.TestCase):
    def test_long_command_emits_heartbeats(self) -> None:
        with self.assertLogs("codex-review-agent", level="INFO") as captured:
            completed = agent.CommandRunner().run_with_heartbeat(
                [
                    "python3",
                    "-c",
                    "import time; time.sleep(0.04); print('complete')",
                ],
                label="test pass",
                timeout=2,
                heartbeat_seconds=0.01,
            )
        self.assertEqual(completed.stdout.strip(), "complete")
        self.assertTrue(
            any("test pass still running" in line for line in captured.output)
        )

    @mock.patch("reno_review_agent.time.sleep")
    @mock.patch("reno_review_agent.subprocess.run")
    def test_git_fetch_retries_transient_network_failure(self, run, sleep) -> None:
        failure = subprocess.CalledProcessError(
            128,
            ["git", "fetch", "origin"],
            stderr=(
                "fatal: unable to access 'https://github.com/org/repo.git/': "
                "Failed to connect to github.com port 443"
            ),
        )
        success = subprocess.CompletedProcess(["git", "fetch", "origin"], 0, "", "")
        run.side_effect = [failure, success]

        completed = agent.CommandRunner().run(["git", "fetch", "origin"])

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(run.call_count, 2)
        sleep.assert_called_once_with(5)
        self.assertEqual(run.call_args.kwargs["timeout"], 60)

    @mock.patch("reno_review_agent.time.sleep")
    @mock.patch("reno_review_agent.subprocess.run")
    def test_network_failure_after_three_attempts_is_typed(self, run, sleep) -> None:
        run.side_effect = subprocess.CalledProcessError(
            128,
            ["git", "fetch", "origin"],
            stderr="fatal: Could not resolve host: github.com",
        )

        with self.assertRaises(agent.TransientNetworkError):
            agent.CommandRunner().run(["git", "fetch", "origin"])

        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 15])

    @mock.patch("reno_review_agent.time.sleep")
    @mock.patch("reno_review_agent.subprocess.run")
    def test_github_write_is_not_blindly_retried(self, run, sleep) -> None:
        run.side_effect = subprocess.CalledProcessError(
            1,
            ["gh", "pr", "comment", "7"],
            stderr="Failed to connect to github.com port 443",
        )

        with self.assertRaises(agent.AgentError) as raised:
            agent.CommandRunner().run(["gh", "pr", "comment", "7"])

        self.assertNotIsInstance(raised.exception, agent.TransientNetworkError)
        self.assertEqual(run.call_count, 1)
        sleep.assert_not_called()

    def test_only_read_only_github_api_calls_are_retryable(self) -> None:
        self.assertEqual(
            agent._safe_network_operation(
                ["gh", "api", "-X", "GET", "search/issues", "-f", "q=test"]
            ),
            "GitHub API read",
        )
        self.assertIsNone(
            agent._safe_network_operation(
                ["gh", "api", "repos/org/repo/issues", "-f", "body=test"]
            )
        )


class PullRequestTests(unittest.TestCase):
    def test_parse_and_filter(self) -> None:
        parsed = agent.parse_pull_request(
            github_payload(), "0xLazAI", "@RenoReviewAgent"
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.head_sha, HEAD_SHA)

        payload = github_payload(body="no mention")
        self.assertIsNone(
            agent.parse_pull_request(payload, "0xLazAI", "@RenoReviewAgent")
        )

    def test_repository_selection_normalizes_and_rejects_other_organizations(self) -> None:
        self.assertEqual(
            agent.normalize_repositories(
                ["api,example-org/web", "api"], "example-org"
            ),
            ("example-org/api", "example-org/web"),
        )
        with self.assertRaises(ValueError):
            agent.normalize_repositories(["another-org/api"], "example-org")

    def test_discovery_refetches_exact_pr(self) -> None:
        runner = FakeRunner(
            [
                {
                    "items": [
                        {
                            "repository_url": "https://api.github.com/repos/0xLazAI/example",
                            "number": 7,
                        }
                    ]
                },
                github_payload(),
            ]
        )
        results = agent.discover_pull_requests(
            runner, gh_bin="gh", org="0xLazAI", mention="@RenoReviewAgent"
        )
        self.assertEqual([item.key for item in results], ["0xLazAI/example#7"])
        self.assertIn("repos/0xLazAI/example/pulls/7", runner.calls[1])


class IssueTests(unittest.TestCase):
    def test_parse_comment_mention_and_filter_pull_request(self) -> None:
        parsed = agent.parse_issue(
            issue_payload(),
            repository_payload(),
            [request_comment()],
            org="0xLazAI",
            mention="@RenoReviewAgent",
            code_ref="develop",
            code_sha=ISSUE_SHA,
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.request_comment_id, 42)
        self.assertEqual(parsed.code_sha, ISSUE_SHA)

        payload = issue_payload()
        payload["pull_request"] = {"url": "https://api.github.test/pulls/9"}
        self.assertIsNone(
            agent.parse_issue(
                payload,
                repository_payload(),
                [request_comment()],
                org="0xLazAI",
                mention="@RenoReviewAgent",
                code_ref="develop",
                code_sha=ISSUE_SHA,
            )
        )

    def test_body_mention_is_a_request(self) -> None:
        body = "@RenoReviewAgent review this contract"
        parsed = agent.parse_issue(
            issue_payload(body=body),
            repository_payload(),
            [],
            org="0xLazAI",
            mention="@RenoReviewAgent",
            code_ref="main",
            code_sha=ISSUE_SHA,
        )
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed.request_comment_id)
        self.assertEqual(parsed.request_body, body)

    def test_discovery_uses_configured_issue_branch(self) -> None:
        runner = FakeRunner(
            [
                {
                    "items": [
                        {
                            "repository_url": "https://api.github.com/repos/0xLazAI/example",
                            "number": 9,
                        }
                    ]
                },
                issue_payload(),
                repository_payload(),
                [request_comment()],
                {"ref": "refs/heads/develop", "object": {"sha": ISSUE_SHA}},
            ]
        )
        results = agent.discover_issues(
            runner,
            gh_bin="gh",
            org="0xLazAI",
            mention="@RenoReviewAgent",
            preferred_branch="develop",
        )
        self.assertEqual([item.key for item in results], ["0xLazAI/example#9"])
        self.assertEqual(results[0].code_ref, "develop")
        self.assertTrue(any("issues/9/comments" in part for part in runner.calls[3]))

    def test_controller_comment_is_not_a_new_request(self) -> None:
        issue = make_issue()
        result = agent.ReviewResult("approved", ISSUE_SHA, "Ready", None)
        review_comment = request_comment(
            body=agent.render_issue_review_comment(issue, result)
        )
        review_comment["id"] = 43
        parsed = agent.parse_issue(
            issue_payload(),
            repository_payload(),
            [request_comment(), review_comment],
            org="0xLazAI",
            mention="@RenoReviewAgent",
            code_ref="develop",
            code_sha=ISSUE_SHA,
        )
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.request_comment_id, 42)
        self.assertTrue(parsed.has_matching_review)

    def test_code_anchor_falls_back_to_default_branch(self) -> None:
        runner = FakeRunner(
            [
                {"ref": "refs/heads/main", "object": {"sha": ISSUE_SHA}},
            ]
        )
        code_ref, code_sha = agent.fetch_issue_code_anchor(
            runner,
            gh_bin="gh",
            repo_full_name="0xLazAI/example",
            repository_payload=repository_payload(),
        )
        self.assertEqual((code_ref, code_sha), ("main", ISSUE_SHA))
        self.assertTrue(any("git/ref/heads/main" in part for part in runner.calls[0]))

    def test_matching_manual_exact_body_review_is_migrated(self) -> None:
        body = "Contract proposal"
        manual_review = request_comment(
            body=(
                "## Codex Review Agent Issue review — CHANGES REQUESTED\n\n"
                f"Exact body: `{agent.body_digest(body)}`"
            )
        )
        manual_review["id"] = 43
        parsed = agent.parse_issue(
            issue_payload(body=body),
            repository_payload(),
            [request_comment(), manual_review],
            org="0xLazAI",
            mention="@RenoReviewAgent",
            code_ref="develop",
            code_sha=ISSUE_SHA,
        )
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.has_matching_review)


class StateTests(unittest.TestCase):
    def test_revision_change_requires_review(self) -> None:
        pull_request = make_pr()
        state = {"version": 1, "pull_requests": {}}
        self.assertTrue(agent.needs_review(state, pull_request))
        result = agent.ReviewResult(
            "approved", HEAD_SHA, "Looks good", "No blocking findings."
        )
        agent.record_review(state, pull_request, result, "https://example.test/comment")
        self.assertFalse(agent.needs_review(state, pull_request))
        self.assertTrue(agent.needs_review(state, make_pr(head="3" * 40)))

    def test_atomic_round_trip_and_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = agent.StateStore(Path(directory))
            state = {"version": 1, "pull_requests": {"x#1": {"head_sha": HEAD_SHA}}}
            store.save(state)
            self.assertEqual(store.load(), state)
            store.state_path.write_text("not json", encoding="utf-8")
            with self.assertRaises(agent.AgentError):
                store.load()

    def test_issue_request_change_requires_review(self) -> None:
        issue = make_issue()
        state = {"version": 1, "pull_requests": {}, "issues": {}}
        self.assertTrue(agent.needs_issue_review(state, issue))
        result = agent.ReviewResult("approved", ISSUE_SHA, "Ready", None)
        agent.record_issue_review(state, issue, result, "https://example.test/comment")
        self.assertFalse(agent.needs_issue_review(state, issue))
        next_request = make_issue(
            request_body="@RenoReviewAgent re-review", request_id=43
        )
        self.assertTrue(agent.needs_issue_review(state, next_request))
        self.assertFalse(
            agent.needs_issue_review(
                {"version": 1, "pull_requests": {}, "issues": {}},
                make_issue(has_matching_review=True),
            )
        )


class SweepResultTests(unittest.TestCase):
    def test_validate_complete_sweep(self) -> None:
        result = agent.validate_sweep_result(
            sweep_payload(files=("a.py", "b.py")), HEAD_SHA, ("a.py", "b.py")
        )
        self.assertTrue(result.sweep_complete)
        self.assertEqual(len(result.coverage), len(agent.COVERAGE_AREAS))

    def test_reject_wrong_head(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "different head SHA"):
            agent.validate_sweep_result(
                sweep_payload(head_sha="3" * 40), HEAD_SHA, ("a.py",)
            )

    def test_reject_missing_coverage_area(self) -> None:
        payload = sweep_payload()
        payload["coverage"].pop()
        with self.assertRaisesRegex(agent.AgentError, "omitted coverage areas"):
            agent.validate_sweep_result(payload, HEAD_SHA, ("a.py",))

    def test_reject_incomplete_changed_file_inventory(self) -> None:
        with self.assertRaisesRegex(agent.AgentError, "file inventory mismatch"):
            agent.validate_sweep_result(
                sweep_payload(files=("a.py",)), HEAD_SHA, ("a.py", "b.py")
            )

    def test_finding_requires_precise_anchor(self) -> None:
        finding = finding_payload(path=None, line=None)
        with self.assertRaisesRegex(agent.AgentError, "no precise source anchor"):
            agent.validate_sweep_result(
                sweep_payload(findings=[finding]), HEAD_SHA, ("a.py",)
            )

    def test_controller_derives_changes_requested(self) -> None:
        sweep = validated_sweep(findings=[finding_payload()])
        result = agent.synthesize_review_result(HEAD_SHA, (sweep,), sweep.findings)
        self.assertEqual(result.verdict, "changes_requested")
        self.assertIn("`a.py:7`", result.review_body)
        self.assertIn("Required fix", result.review_body)

    def test_controller_derives_approval(self) -> None:
        sweep = validated_sweep()
        result = agent.synthesize_review_result(HEAD_SHA, (sweep,), ())
        self.assertEqual(result.verdict, "approved")
        self.assertIsNone(result.review_body)

    @mock.patch("reno_review_agent._invoke_codex_sweep")
    def test_product_review_with_blocker_finishes_in_one_pass(self, invoke) -> None:
        invoke.return_value = validated_sweep(findings=[finding_payload()])

        result = agent._run_product_review(
            agent.CommandRunner(),
            codex_bin="codex",
            session_id="session",
            worktree=Path("/tmp/worktree"),
            expected_head_sha=HEAD_SHA,
            required_files=("a.py",),
            primary_prompt="primary",
            state_dir=Path("/tmp/state"),
            primary_timeout=10,
            review_label="example#7",
        )

        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result.verdict, "changes_requested")
        self.assertEqual(result.sweep["passes"], 1)
        self.assertEqual(result.sweep["strategy"], "product_review")

    @mock.patch("reno_review_agent._invoke_codex_sweep")
    def test_clean_product_review_approves_after_one_pass(self, invoke) -> None:
        invoke.return_value = validated_sweep()
        result = agent._run_product_review(
            agent.CommandRunner(),
            codex_bin="codex",
            session_id="session",
            worktree=Path("/tmp/worktree"),
            expected_head_sha=HEAD_SHA,
            required_files=("a.py",),
            primary_prompt="primary",
            state_dir=Path("/tmp/state"),
            primary_timeout=10,
            review_label="example#7",
        )

        self.assertEqual(result.verdict, "approved")
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result.sweep["passes"], 1)
        self.assertEqual(result.sweep["strategy"], "product_review")

    @mock.patch("reno_review_agent._invoke_codex_sweep")
    def test_nonblocking_finding_does_not_block_approval(self, invoke) -> None:
        invoke.return_value = validated_sweep(
            findings=[finding_payload(blocking=False)]
        )
        result = agent._run_product_review(
            agent.CommandRunner(),
            codex_bin="codex",
            session_id="session",
            worktree=Path("/tmp/worktree"),
            expected_head_sha=HEAD_SHA,
            required_files=("a.py",),
            primary_prompt="primary",
            state_dir=Path("/tmp/state"),
            primary_timeout=10,
            review_label="example#7",
        )

        self.assertEqual(result.verdict, "approved")
        self.assertIn("Unbounded request", result.review_body)
        self.assertEqual(invoke.call_count, 1)

    @mock.patch("reno_review_agent._invoke_codex_sweep")
    def test_issue_finishes_after_one_bounded_pass(self, invoke) -> None:
        first = validated_sweep(
            findings=[finding_payload(title="Primary contract gap")],
            head_sha=ISSUE_SHA,
        )
        invoke.return_value = first

        result = agent._run_issue_contract_review(
            agent.CommandRunner(),
            codex_bin="codex",
            session_id="session",
            worktree=Path("/tmp/worktree"),
            issue=make_issue(),
            state_dir=Path("/tmp/state"),
            timeout=10,
        )

        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(result.verdict, "changes_requested")
        self.assertEqual(result.sweep["passes"], 1)
        self.assertEqual(result.sweep["strategy"], "bounded_issue_contract")
        self.assertEqual(len(result.sweep["findings"]), 1)

    def test_changed_files_are_derived_from_exact_diff(self) -> None:
        class DiffRunner:
            def run(self, args, *, cwd=None, input_text=None, timeout=None):
                return subprocess.CompletedProcess(args, 0, "a.py\0b.py\0", "")

        files = agent.changed_files_for_pull_request(
            DiffRunner(), worktree=Path("/tmp/worktree"), pull_request=make_pr()
        )
        self.assertEqual(files, ("a.py", "b.py"))


class ReviewPresentationTests(unittest.TestCase):
    def test_prompt_and_comment_are_sha_scoped(self) -> None:
        pull_request = make_pr()
        prompt = agent.build_review_prompt(pull_request, ("a.py", "tests/test_a.py"))
        self.assertIn(HEAD_SHA, prompt)
        self.assertIn(BASE_SHA, prompt)
        self.assertIn("Do not implement fixes", prompt)
        self.assertIn("documented product, security", prompt)
        self.assertIn("ordinary retry/failure", prompt)
        self.assertNotIn("limit-1/limit/limit+1", prompt)
        self.assertIn("tests/test_a.py", prompt)
        for area in agent.COVERAGE_AREAS:
            self.assertIn(area, prompt)

        result = agent.ReviewResult(
            "approved", HEAD_SHA, "Ready", "No blocking findings."
        )
        comment = agent.render_review_comment(pull_request, result)
        self.assertIn("APPROVED", comment)
        self.assertIn(HEAD_SHA, comment)
        self.assertIn("codex-review-agent:v1", comment)

        issue = make_issue()
        issue_prompt = agent.build_issue_review_prompt(issue)
        self.assertIn(issue.body_sha256, issue_prompt)
        self.assertIn(issue.request_body, issue_prompt)
        self.assertIn(ISSUE_SHA, issue_prompt)
        self.assertIn("single, bounded", issue_prompt)
        self.assertIn("documented security, scale", issue_prompt)
        self.assertIn("Blocking threshold", issue_prompt)
        self.assertIn("Do not inventory the repository", issue_prompt)
        self.assertIn("Do not add speculative requirements", issue_prompt)
        issue_comment = agent.render_issue_review_comment(issue, result)
        self.assertIn("Issue review — APPROVED", issue_comment)
        self.assertIn("kind=issue", issue_comment)

    def test_codex_command_resumes_exact_session(self) -> None:
        command = agent.build_codex_command(
            codex_bin="codex",
            session_id="session-123",
            worktree=Path("/tmp/worktree"),
            output_path=Path("/tmp/result.json"),
        )
        self.assertIn("resume", command)
        self.assertEqual(command[command.index("resume") + 1], "session-123")
        self.assertIn("--output-schema", command)
        self.assertIn("/tmp/worktree", command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)

    def test_default_interval_is_two_minutes(self) -> None:
        config = agent.parse_args(["--dry-run", "--org", "example-org"])
        self.assertEqual(config.interval, 120)
        self.assertEqual(config.codex_timeout, 720)
        self.assertEqual(config.issue_codex_timeout, 600)

    def test_environment_configures_runtime_defaults(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "REVIEW_ORG": "example-org",
                "REVIEW_MENTION": "@example-reviewer",
                "REVIEW_AGENT_NAME": "Example Reviewer",
                "REVIEW_MARKER": "example-reviewer",
                "REVIEW_ISSUE_BRANCH": "integration",
                "REVIEW_INTERVAL": "45",
                "REVIEW_CODEX_BYPASS_SANDBOX": "true",
            },
            clear=False,
        ):
            config = agent.parse_args(["--dry-run"])
        self.assertEqual(config.org, "example-org")
        self.assertEqual(config.mention, "@example-reviewer")
        self.assertEqual(config.agent_name, "Example Reviewer")
        self.assertEqual(config.marker, "example-reviewer")
        self.assertEqual(config.issue_branch, "integration")
        self.assertEqual(config.interval, 45)
        self.assertTrue(config.codex_bypass_sandbox)

    def test_command_line_repositories_override_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"REVIEW_ORG": "example-org", "REVIEW_REPOS": "from-env"},
            clear=False,
        ):
            config = agent.parse_args(
                ["--dry-run", "--repo", "api,web", "--repo", "example-org/docs"]
            )
        self.assertEqual(
            config.repositories,
            ("example-org/api", "example-org/web", "example-org/docs"),
        )

    def test_prerequisites_check_github_and_codex_for_live_runs(self) -> None:
        config = agent.parse_args(
            ["--org", "example-org", "--session-id", "session-123"]
        )
        runner = FakeRunner([])
        agent.verify_prerequisites(config, runner)
        self.assertEqual(
            runner.calls,
            [
                ["git", "--version"],
                ["gh", "--version"],
                ["gh", "auth", "status"],
                ["codex", "--version"],
                ["codex", "login", "status"],
            ],
        )

    def test_missing_github_cli_has_setup_guidance(self) -> None:
        config = agent.parse_args(["--dry-run", "--org", "example-org"])

        class MissingRunner:
            def run(self, args, **_kwargs):
                if args[0] == "git":
                    return subprocess.CompletedProcess(args, 0, "", "")
                raise agent.AgentError("required command not found: gh")

        with self.assertRaisesRegex(agent.AgentError, "Install it.*gh auth login"):
            agent.verify_prerequisites(config, MissingRunner())

    def test_legacy_review_options_are_accepted(self) -> None:
        config = agent.parse_args(
            [
                "--dry-run",
                "--org",
                "example-org",
                "--max-review-passes",
                "1",
                "--approval-check-timeout",
                "1",
            ]
        )
        self.assertEqual(config.codex_timeout, 720)


class DryRunTests(unittest.TestCase):
    def test_dry_run_does_not_publish_or_invoke_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = agent.Config(
                org="0xLazAI",
                repositories=(),
                mention="@RenoReviewAgent",
                agent_name="Test Review Agent",
                marker="test-review-agent",
                issue_branch=None,
                review_context="Use the repository documentation.",
                session_id=None,
                interval=300,
                once=True,
                dry_run=True,
                state_dir=Path(directory),
                worktree_root=Path(directory) / "worktrees",
                codex_timeout=10,
                issue_codex_timeout=10,
                gh_bin="gh",
                codex_bin="codex",
                codex_bypass_sandbox=False,
            )
            runner = FakeRunner(
                [
                    {
                        "items": [
                            {
                                "repository_url": "https://api.github.com/repos/0xLazAI/example",
                                "number": 7,
                            }
                        ]
                    },
                    github_payload(),
                    {"items": []},
                ]
            )
            store = agent.StateStore(Path(directory))
            state = {"version": 1, "pull_requests": {}}
            self.assertTrue(agent.process_cycle(config, runner, store, state))
            flattened = [part for call in runner.calls for part in call]
            self.assertNotIn("codex", flattened)
            self.assertNotIn("comment", flattened)


class SchemaTests(unittest.TestCase):
    def test_schema_is_valid_json_and_closed(self) -> None:
        schema = json.loads(agent.SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            tuple(
                schema["properties"]["coverage"]["items"]["properties"]["area"]["enum"]
            ),
            agent.COVERAGE_AREAS,
        )
        self.assertEqual(set(schema["required"]), set(schema["properties"]))


if __name__ == "__main__":
    unittest.main()
