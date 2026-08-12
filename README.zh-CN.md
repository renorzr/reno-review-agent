# Codex GitHub Review Agent

[English](README.md)

这是一个基于 GitHub Issue 和 Pull Request 的评审 Agent。它会轮询指定范围内的 GitHub 仓库，查找包含配置提及（mention）的 Issue 和开放 Pull Request，调用 Codex 评审精确的版本或 Issue 方案，并发布与该版本绑定的评审评论。

组织、仓库范围、触发提及、审查机器人名称、Issue 代码分支、目录、超时、会话和 Codex 沙箱行为均通过 `.env` 或命令行参数配置。

实现采用 Python 包结构：`core` 负责命令执行和通用工具，`github` 负责 GitHub 发现与数据校验，`storage` 管理状态和 worktree，`reviews` 运行并校验 Codex 评审，`app` 包含配置和轮询循环。原有的 `reno_review_agent.py` 保留为兼容启动入口；新集成可使用 `python -m review_agent`。

## 推荐开发模式

建议在开发期间持续运行 Agent，并将轮询周期设为五分钟：

```dotenv
REVIEW_INTERVAL=300
```

1. 创建或更新 Issue，并加入配置的 mention，例如：`@review-bot 请评审这个方案`。Agent 会进行一次有边界的方案/契约评审，指出会阻碍实现的工作流、接口、兼容性、失败处理或验收标准缺失。
2. 创建非草稿 Pull Request，并在 **PR 正文** 中加入同一 mention。Agent 会检出该 PR 的精确版本，发布 `APPROVED` 或 `CHANGES REQUESTED`，并附上证据与必要修复项。
3. 修复阻断问题后，推送新的 commit 或更新 PR 正文。新的版本会在下一次轮询时再次进入评审。
4. 持续迭代，直到 Agent 不再报告阻断项。通过结果是一个评审评论；它不会自动合并 PR，也不能替代仓库要求的人类审批和 CI 检查。

也可以通过含有 mention 的 Issue 评论触发评审。Agent 会记录已评审的指纹，因此不会对未变化的内容反复评论。

## 运行要求

- Python 3.11 或更高版本
- Git
- 已登录的 GitHub CLI（`gh`），并具备读取仓库和发布评论的权限
- 已登录的 Codex CLI，以及用于正式审查的可恢复会话 ID

```bash
gh auth status
codex --version
```

## 配置

先复制示例文件，再填写实际值：

```bash
cp .env.example .env
```

至少需要设置 `REVIEW_ORG` 和 `REVIEW_MENTION`。可选设置 `REVIEW_REPOS` 以逗号分隔的白名单限定仓库，例如 `api,web`（也支持 `your-org/api`）；留空时会监控该组织中当前账号可访问的全部仓库。正式运行还需要设置 `REVIEW_SESSION_ID`，也可以通过 `--session-id` 传入。`.env` 已被 Git 忽略；已存在的 shell/CI 环境变量会覆盖 `.env`，明确传入的命令行参数优先级最高。

`REVIEW_ISSUE_BRANCH` 可留空。留空时，Issue 审查使用每个仓库的默认分支；只有所有被监控仓库明确使用同一集成分支（例如 `develop`）时才应设置它。

`REVIEW_CODEX_BYPASS_SANDBOX` 默认是 `false`。启用后会向 Codex 传递绕过审批和沙箱的参数，仅应在可信机器上使用。

## 启动

先执行仅发现模式。它不会克隆仓库、调用 Codex 或发布评论：

```bash
python3 reno_review_agent.py --once --dry-run
```

启动前，程序会检查 Git 与 `gh` 是否已安装，并检查 `gh` 是否已登录；正式运行还会检查 `codex` 及 `codex login status`。检查失败时会退出并给出对应的安装或登录命令；仅发现模式不会调用 Codex，因此不要求 Codex 可用。

执行一次正式轮询：

```bash
python3 reno_review_agent.py --once
```

按配置的间隔持续运行：

```bash
python3 reno_review_agent.py
```

命令行可以临时覆盖配置：

```bash
python3 reno_review_agent.py \
  --org example-org \
  --repo api --repo web \
  --mention @review-bot \
  --issue-branch develop \
  --session-id '<codex-session-id>' \
  --once
```

使用 `python3 reno_review_agent.py --help` 查看全部选项。
如需临时覆盖 `.env` 中的 `true`，可使用 `--no-codex-bypass-sandbox` 禁用无沙箱模式。

## 配置项

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `REVIEW_ORG` | 要监控的 GitHub 组织 | 必填 |
| `REVIEW_REPOS` | 组织内的仓库白名单，使用逗号分隔 | 该组织中当前账号可访问的全部仓库 |
| `REVIEW_MENTION` | 请求审查的精确 GitHub 提及 | `@codex-review` |
| `REVIEW_AGENT_NAME` / `REVIEW_MARKER` | 评论标题与防止自触发的标记 | `Codex Review Agent` / `codex-review-agent` |
| `REVIEW_ISSUE_BRANCH` | Issue 审查使用的优先代码分支 | 仓库默认分支 |
| `REVIEW_SESSION_ID` | 要恢复的 Codex 会话 | 可使用 `CODEX_THREAD_ID` |
| `REVIEW_INTERVAL` | 轮询间隔（秒） | `120`（示例使用推荐值 `300`） |
| `REVIEW_STATE_DIR` / `REVIEW_WORKTREE_ROOT` | 本地状态和临时检出目录 | `.state` / 系统临时目录 |
| `REVIEW_CODEX_TIMEOUT` / `REVIEW_ISSUE_CODEX_TIMEOUT` | PR 和 Issue 审查超时（秒） | `720` / `600` |
| `REVIEW_GH_BIN` / `REVIEW_CODEX_BIN` | 可执行文件名称或路径 | `gh` / `codex` |
| `REVIEW_CONTEXT` | 每次审查附带给 Codex 的额外范围说明 | 通用的“以仓库文档为准”说明 |
| `REVIEW_CODEX_BYPASS_SANDBOX` | 是否使用 Codex 无限制绕过模式 | `false` |

## 行为与安全性

- 进程锁和原子状态写入避免同一台机器重复执行。
- 仅处理正文包含精确提及的、开放且非草稿的 PR；Issue 可由正文或评论触发。
- 每次审查都使用发现时 SHA 的 detached checkout。发布前如果 PR、Issue 请求或代码锚点发生变化，结果会被丢弃。
- 状态文件会记录指纹，未变化的版本只审查一次；PR 的 head/base/body 变化或 Issue 请求变化后才会再次审查。
- 可配置的评论标记会排除机器人自身在 Issue 下的评论，避免自我触发循环。
- GitHub 读取、克隆和拉取会重试瞬时网络错误；发布评论不会盲目重试，以避免不确定响应造成重复评论。
- Codex 返回符合 schema 的结果；控制器负责计算 `APPROVED` 或 `CHANGES REQUESTED` 并发布评论。

默认的审查范围以仓库文档为准。可通过 `REVIEW_CONTEXT` 写入你的支持环境、安全边界、规模限制或合规要求，而不必将这些假设写死在代码中。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile reno_review_agent.py
```

开发实现说明见 [DEVELOPER_AGENT_PROMPT.md](DEVELOPER_AGENT_PROMPT.md)。
