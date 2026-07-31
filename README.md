# git-agent-runner — Dynamic Agent Pipeline

[![CI](https://github.com/jeromeskiii/git-agent-runner/actions/workflows/ci.yml/badge.svg)](./.github/workflows/ci.yml)

All-in-one orchestrator that vendors [`git-worktree-runner`](./vendor/git-worktree-runner/) (gtr) and [`pr-agent`](./vendor/pr-agent/) behind one `agent-runner` CLI:

```
task → gtr creates worktree → AI agent works → push → PR → pr-agent reviews → iterate
```

## Features

- **Pre-flight validation** — checks gtr, gh auth, pr-agent, and repo state *before* any stage runs; saves minutes of waiting when prerequisites are missing
- **Rich terminal UI** — styled tables, colored pass/fail indicators, panels, and structured output across all subcommands
- **Composable pipeline stages** — `create_worktree → launch_agent → wait_for_pr → review_pr ⇄ iterate → improve_pr`
- **Review feedback loop** — with `review_iterations > 1`, review output is fed back to the agent, which pushes fixes to the same PR before the next review round
- **Dry-run mode** — `agent-runner run --dry-run --task "..."` plans the pipeline without invoking anything
- **Retry with backoff + jitter** — transient failures are retried per config; pr-agent timeouts are never retried (a timed-out review may already have posted — retrying would duplicate comments)
- **Process-group kills** — a timed-out stage SIGKILLs the whole process group; no orphaned agent keeps working after a reported failure
- **Failure cleanup** — `--cleanup` removes the worktree of a failed run (the branch is kept for diagnosis)
- **Orphan reconciliation** — `agent-runner doctor` cross-checks `agent/*` worktrees on disk against run history; `--fix` auto-cleans orphans
- **Structured logging** — every run is logged to stderr *and* a rotating JSON file under `~/.cache/git-agent-runner/logs/`
- **Persistent run history** — every pipeline run writes to `~/.cache/git-agent-runner/runs.jsonl`; query with `agent-runner logs`
- **Typed results** — `review_pr` / `improve_pr` return `PrActionResult`, not raw dicts
- **Plugin-style agents** — register agents (with headless `args`) in config instead of hardcoding them
- **Timeouts everywhere** — every stage has a configurable timeout (`agent_timeout` for the agent stage, which runs far longer than a subprocess call)
- **Vendored engines** — upstream gtr and pr-agent live under `vendor/`; `agent_runner/` is the first-party source tree

## Repository Layout

```text
git-agent-runner/
├── agent_runner/                  # first-party orchestrator package and tests
├── vendor/
│   ├── git-worktree-runner/        # vendored gtr worktree engine
│   └── pr-agent/                   # vendored PR review engine
├── agent-runner.toml               # unified runtime config
├── Makefile                        # setup/test/lint entry points
└── README.md                       # this all-in-one project guide
```

The vendored folders are intentionally kept as separate upstream code drops, but the public product is this root package: **`git-agent-runner`**.

## Requirements

- Python 3.12+
- Vendored [`git-worktree-runner`](./vendor/git-worktree-runner/) installed via `make setup` or `git-gtr` on PATH
- Vendored [`pr-agent`](./vendor/pr-agent/) installed via `make setup` or `pr-agent` on PATH
- `gh` CLI authenticated (for PR polling)

## Install

```bash
make setup
```

This installs the vendored engines and the orchestrator in editable mode.

## Commands

### `agent-runner run`

Run the full pipeline against a task description. Pre-flight checks run automatically before any stage — use `--skip-preflight` to bypass them.

```bash
agent-runner run --task "fix auth bug" --repo . --agent claude
agent-runner run --task "..." --repo . --timeout 600 --dry-run
agent-runner run --task "..." --skip-preflight   # skip pre-flight checks
```

The task text reaches the agent as its prompt: the launch stage runs
`gtr ai <branch> --ai <agent> -- <agent args> <task>` (headless for the
default claude spec: `claude -p --permission-mode acceptEdits "<task>"`).
Branches look like `agent/<slug>-<hash>` — the hash keeps distinct tasks
from colliding. Agent output streams live, and the stage is bounded by
`agent_timeout` — on timeout the whole process group is killed, so no
orphaned agent keeps working.

| Flag | Description |
|------|-------------|
| `--task` | Task description passed to the AI agent (required) |
| `--repo` | Path to the git repository (default: `.`) |
| `--agent` | Agent name; overrides `default_agent` from config |
| `--timeout` | Seconds to wait for PR creation (overrides config) |
| `--agent-timeout` | Seconds to wait for the agent stage (overrides config) |
| `--iterations` | Review→agent feedback rounds (overrides config, default 1) |
| `--cleanup` | Remove the worktree if the run fails (branch is kept) |
| `--dry-run` | Plan the pipeline without invoking any external commands |
| `--skip-preflight` | Skip pre-flight validation checks |

#### Optional local Ternary-Bonsai backend

If you want a local GGUF backend, install `llama-cpp-python` and keep the model
cache available on the machine. Then register the bundled agent in
`agent-runner.toml`:

```toml
[agents.ternary-bonsai]
command = ["python", "-m", "agent_runner.ternary_bonsai_agent"]
args = ["--repo-id", "prism-ml/Ternary-Bonsai-27B-gguf", "--filename", "Ternary-Bonsai-27B-F16.gguf"]
description = "Local llama-cpp-python agent using Ternary-Bonsai 27B GGUF"
```

Usage:

```bash
pip install llama-cpp-python
agent-runner run --task "fix auth bug" --repo . --agent ternary-bonsai
```

Notes:

- The model is large; make sure your machine has enough RAM/VRAM.
- The wrapper reads the task prompt from stdin and prints a single chat completion.
- This backend is best used as a research or fallback agent, not the default.

### `agent-runner preflight`

Run pre-flight checks standalone — validates gtr, gh auth, pr-agent, and repo state without launching the pipeline.

```bash
agent-runner preflight --repo .
```

Exits 0 when all checks pass, non-zero when blocking errors are found. Warnings (e.g. dirty working tree) are shown but don't fail the check.

### `agent-runner review` / `agent-runner improve`

Standalone pr-agent invocation against an existing PR.

```bash
agent-runner review --pr-url https://github.com/owner/repo/pull/42
agent-runner improve --pr-url https://github.com/owner/repo/pull/42
```

### `agent-runner status`

Check whether `git-gtr` and `pr-agent` are installed and on PATH. Displays a Rich table with the registered agents and config path.

### `agent-runner config`

Print the resolved configuration as JSON. Useful for debugging which file the orchestrator loaded.

### `agent-runner logs`

Show recent pipeline runs from the JSONL history as a Rich table.

```bash
agent-runner logs --limit 20
agent-runner logs --compact           # collapse stage events of old runs
agent-runner logs --prune-days 30     # drop records older than 30 days
```

### `agent-runner clean`

Clear the local pipeline-run history.

```bash
agent-runner clean --dry-run   # show what would be removed
agent-runner clean             # actually remove it
```

### `agent-runner doctor`

Reconcile `agent/*` worktrees on disk against run history: flags orphans
(worktrees from runs that crashed, failed, or predate the history).
Exits non-zero when orphans are found.

```bash
agent-runner doctor --repo .
agent-runner doctor --repo . --fix          # remove orphan worktrees
agent-runner doctor --repo . --fix --dry-run  # preview what would be removed
```

## Configuration

The orchestrator loads config from (in increasing precedence):

1. Built-in defaults
2. `agent-runner.toml` at the repo root (top-level keys)
3. `[tool.agent-runner]` table in `pyproject.toml`
4. Environment variables (`AGENT_RUNNER_*`)

### Example `agent-runner.toml`

```toml
default_agent = "claude"
poll_interval = 10
default_timeout = 300
subprocess_timeout = 120
agent_timeout = 1800
retry_attempts = 3
retry_backoff = 1.5
review_iterations = 1      # >1 feeds review output back to the agent
cleanup_on_error = false   # true removes the failed run's worktree

[agents.claude]
command = ["claude"]
# headless print mode + auto-accept file edits in the worktree;
# task text is appended as the prompt
args = ["-p", "--permission-mode", "acceptEdits"]
description = "Anthropic Claude Code"

[agents.cursor]
command = ["cursor", "--yes"]
args = []
description = "Cursor agent"
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_RUNNER_AGENT` | `claude` | Default agent name |
| `AGENT_RUNNER_TIMEOUT` | `300` | Seconds to wait for PR |
| `AGENT_RUNNER_AGENT_TIMEOUT` | `1800` | Seconds to wait for the agent stage |
| `AGENT_RUNNER_POLL_INTERVAL` | `10` | Seconds between gh polls |
| `AGENT_RUNNER_SUBPROCESS_TIMEOUT` | `120` | Per-command timeout |
| `AGENT_RUNNER_RETRY_ATTEMPTS` | `3` | Total attempts (incl. first) |
| `AGENT_RUNNER_RETRY_BACKOFF` | `1.5` | Backoff multiplier |
| `AGENT_RUNNER_REVIEW_ITERATIONS` | `1` | Review→agent feedback rounds |
| `AGENT_RUNNER_CLEANUP_ON_ERROR` | `false` | Remove worktree of failed runs |
| `AGENT_RUNNER_LOG_DIR` | `~/.cache/git-agent-runner/logs` | Log file directory |
| `AGENT_RUNNER_HISTORY` | `~/.cache/git-agent-runner/runs.jsonl` | History file path |
| `AGENT_RUNNER_HISTORY_MAX_BYTES` | `10485760` (10 MB) | Rotate `runs.jsonl` once it exceeds N bytes (0 disables) |
| `AGENT_RUNNER_HISTORY_MAX_AGE_DAYS` | `0` (disabled) | Auto-prune records older than N days at startup |

## Validation

### Local

```bash
make setup       # install everything
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy --strict
make test        # pytest (128 tests, incl. end-to-end with real gtr)
make all         # lint + format + typecheck + test
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on every PR. Three
jobs run in parallel:

- **lint** — `ruff check` + `ruff format --check`
- **typecheck** — `mypy --strict` over `agent_runner/`
- **test** — pytest matrix across Python 3.12 and 3.13 with coverage
  uploaded as a workflow artifact

## Architecture

```
agent_runner/
├── cli.py              # argparse entry point + handler dispatch
├── orchestrator.py     # public API: run_pipeline, review_pr, improve_pr
├── pipeline.py         # composable stages + PipelineContext
├── preflight.py        # pre-pipeline validation (gtr, gh, pr-agent, repo)
├── process_utils.py    # subprocess wrapper: timeouts + retries + CommandResult
├── config.py           # Config loader (TOML + env) + AgentSpec registry
├── runs.py             # JSONL run-history store with rotation + pruning
├── rich_ui.py          # Rich-powered terminal output helpers
├── logging_utils.py    # structured logging (stderr + rotating file)
└── tests/              # pytest suite (128 tests: unit + end-to-end)

vendor/
├── git-worktree-runner/ # vendored Bash worktree runner
└── pr-agent/            # vendored Python PR agent
```

Every external command goes through `agent_runner.process_utils.run()`,
which gives uniform timeouts, retries, and result types.
