# Codex GitHub Review Agent

[中文](README.zh-CN.md)

This is a GitHub Issue and pull request review agent. It polls the selected repositories for Issues and open pull requests that mention a configured GitHub handle, asks Codex to review the exact revision or Issue contract, and publishes a revision-scoped review comment.

The organization, repository scope, mention, review identity, Issue code branch, directories, timeouts, session, and Codex sandbox behavior are configured through `.env` or command-line options.

The implementation is organized as a Python package: `core` handles command execution and shared utilities, `github` discovers and validates GitHub data, `storage` manages state and worktrees, `reviews` runs and validates Codex reviews, and `app` contains configuration and the polling loop. The existing `reno_review_agent.py` remains a compatibility launcher; new integrations can use `python -m review_agent`.

## Recommended development workflow

Run the agent continuously during development, with a five-minute polling interval:

```dotenv
REVIEW_INTERVAL=300
```

1. Create or update an Issue and include the configured mention (for example, `@review-bot please review this proposal`). The agent performs a bounded contract review and comments on missing workflow, interface, compatibility, failure-handling, or acceptance-criteria decisions that would block implementation.
2. Open a non-draft pull request whose **PR body** includes the same mention. The agent checks out the exact PR revision and publishes `APPROVED` or `CHANGES REQUESTED` with evidence and required fixes.
3. Address the blocking findings, then push another commit or update the PR body. The changed revision becomes eligible for review on the next polling cycle.
4. Repeat until the agent reports no blocking findings. A green result is a review comment; it does not merge the pull request or replace your repository's required human approval and CI checks.

Issues may also be triggered from a comment containing the mention. The agent records reviewed fingerprints, so it does not repeatedly comment on unchanged work.

## Requirements

- Python 3.11 or newer
- Git
- GitHub CLI (`gh`), authenticated with read access and permission to comment
- Codex CLI, authenticated and with a session ID to resume for live reviews

```bash
gh auth status
codex --version
```

## Configure

Copy the tracked template, then fill in the required values:

```bash
cp .env.example .env
```

At minimum, set `REVIEW_ORG` and `REVIEW_MENTION`. Optionally set `REVIEW_REPOS` to a comma-separated allowlist such as `api,web` (or `your-org/api`); leave it empty to monitor all accessible repositories in the organization. Set `REVIEW_SESSION_ID` for normal operation, or pass it with `--session-id`. `.env` is intentionally ignored by Git. Existing shell/CI environment variables override `.env`; explicit CLI arguments override both.

`REVIEW_ISSUE_BRANCH` is optional. When blank, Issue reviews use each repository's default branch. Set it only when a shared branch such as `develop` is the intended code anchor.

`REVIEW_CODEX_BYPASS_SANDBOX` defaults to `false`. Turning it on passes Codex's unrestricted bypass flag and should only be done on a trusted host.

## Run

Verify discovery first. This does not clone repositories, invoke Codex, or publish comments:

```bash
python3 reno_review_agent.py --once --dry-run
```

Before it starts, the program checks that Git and `gh` are installed, and that `gh` is authenticated. A live run also checks `codex` and `codex login status`. If a check fails, it exits with the matching command to install or authenticate; dry-run does not require Codex because it never invokes it.

Run one live polling cycle:

```bash
python3 reno_review_agent.py --once
```

Keep polling at the configured interval:

```bash
python3 reno_review_agent.py
```

Useful overrides:

```bash
python3 reno_review_agent.py \
  --org example-org \
  --repo api --repo web \
  --mention @review-bot \
  --issue-branch develop \
  --session-id '<codex-session-id>' \
  --once
```

Run `python3 reno_review_agent.py --help` for every available option.
Use `--no-codex-bypass-sandbox` to temporarily disable a `true` environment setting.

## Configuration reference

| Variable | Purpose | Default |
| --- | --- | --- |
| `REVIEW_ORG` | GitHub organization to search | Required |
| `REVIEW_REPOS` | Comma-separated repository allowlist within the organization | All accessible organization repositories |
| `REVIEW_MENTION` | Exact GitHub handle that requests a review | `@codex-review` |
| `REVIEW_AGENT_NAME` / `REVIEW_MARKER` | Comment heading and self-review marker | `Codex Review Agent` / `codex-review-agent` |
| `REVIEW_ISSUE_BRANCH` | Preferred branch for Issue code anchors | Repository default branch |
| `REVIEW_SESSION_ID` | Codex session to resume | `CODEX_THREAD_ID` if available |
| `REVIEW_INTERVAL` | Poll interval in seconds | `120` (the example uses the recommended `300`) |
| `REVIEW_STATE_DIR` / `REVIEW_WORKTREE_ROOT` | Local state and temporary checkout directories | `.state` / system temp directory |
| `REVIEW_CODEX_TIMEOUT` / `REVIEW_ISSUE_CODEX_TIMEOUT` | PR and Issue review time limits in seconds | `720` / `600` |
| `REVIEW_GH_BIN` / `REVIEW_CODEX_BIN` | Executable paths or names | `gh` / `codex` |
| `REVIEW_CONTEXT` | Additional review scope supplied to Codex | Generic documented-scope guidance |
| `REVIEW_CODEX_BYPASS_SANDBOX` | Use Codex's unrestricted bypass mode | `false` |

## Behavior and safety

- One process lock and atomically written state prevent duplicate local work.
- PRs are selected only when their open, non-draft PR body contains the exact configured mention. Issues can be triggered from their body or comments.
- Each review uses a detached checkout of the discovered SHA. The agent discards results when the PR, Issue request, or code anchor changes before publication.
- State records fingerprints, so unchanged revisions are reviewed once. A changed PR head/base/body or changed Issue request becomes eligible again.
- The comment marker is configurable and prevents the agent's own Issue comments from triggering another review.
- GitHub reads and clone/fetch operations retry transient network failures. Comment writes are not retried blindly, preventing duplicate comments after an uncertain response.
- Codex returns schema-validated findings; this controller determines the final `APPROVED` or `CHANGES REQUESTED` label and publishes the comment.

The default review context intentionally defers to repository documentation. Use `REVIEW_CONTEXT` to state your supported environment, security posture, scale limits, or compliance requirements without embedding those assumptions in source code.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile reno_review_agent.py
```

The reusable developer implementation brief is in [DEVELOPER_AGENT_PROMPT.md](DEVELOPER_AGENT_PROMPT.md).
