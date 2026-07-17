# git-agent-runner — Dynamic Agent Pipeline

[![CI](https://github.com/jeromeskiii/git-agent-runner/actions/workflows/ci.yml/badge.svg)](./.github/workflows/ci.yml)

Orchestrator that wires [`git-worktree-runner`](./git-worktree-runner/) (gtr) and [`pr-agent`](./pr-agent/) together into a single, observable agent pipeline:

```
task → gtr creates worktree → AI agent works → push → PR → pr-agent reviews → iterate
```

## Features

- **Composable pipeline stages** — `create_worktree → launch_agent → wait_for_pr → review_pr → improve_pr`
- **Dry-run mode** — `agent-runner run --dry-run --task "..."` plans the pipeline without invoking anything
- **Retry with backoff** — transient failures (timeouts, network flakes) are retried with exponential backoff
- **Structured logging** — every run is logged to stderr *and* a rotating JSON file under `~/.cache/git-agent-runner/logs/`
- **Persistent run history** — every pipeline run writes to `~/.cache/git-agent-runner/runs.jsonl`; query with `agent-runner logs`
- **Typed results** — `review_pr` / `improve_pr` return `PrActionResult`, not raw dicts
- **Plugin-style agents** — register agents in config instead of hardcoding them
- **Timeouts everywhere** — every external command has a configurable timeout; nothing wedges forever

## Requirements

- Python 3.12+
- [`git-worktree-runner`](./git-worktree-runner/) installed (the `git-gtr` CLI on PATH)
- [`pr-agent`](./pr-agent/) installed (`pr-agent` CLI on PATH)
- `gh` CLI authenticated (for PR polling)

## Install

```bash
make setup
```

This installs both submodules and the orchestrator in editable mode.

## Commands

### `agent-runner run`

Run the full pipeline against a task description.

```bash
agent-runner run --task "fix auth bug" --repo . --agent claude
agent-runner run --task "..." --repo . --timeout 600 --dry-run
```

| Flag | Description |
|------|-------------|
| `--task` | Task description passed to the AI agent (required) |
| `--repo` | Path to the git repository (default: `.`) |
| `--agent` | Agent name; overrides `default_agent` from config |
| `--timeout` | Seconds to wait for PR creation (overrides config) |
| `--dry-run` | Plan the pipeline without invoking any external commands |

### `agent-runner review` / `agent-runner improve`

Standalone pr-agent invocation against an existing PR.

```bash
agent-runner review --pr-url https://github.com/owner/repo/pull/42
agent-runner improve --pr-url https://github.com/owner/repo/pull/42
```

### `agent-runner status`

Check whether `git-gtr` and `pr-agent` are installed and on PATH.

### `agent-runner config`

Print the resolved configuration as JSON. Useful for debugging which file
the orchestrator loaded.

### `agent-runner logs`

Show recent pipeline runs from the JSONL history.

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
retry_attempts = 3
retry_backoff = 1.5

[agents.claude]
command = ["claude"]
description = "Anthropic Claude Code"

[agents.cursor]
command = ["cursor", "--yes"]
description = "Cursor agent"
```

### Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_RUNNER_AGENT` | `claude` | Default agent name |
| `AGENT_RUNNER_TIMEOUT` | `300` | Seconds to wait for PR |
| `AGENT_RUNNER_POLL_INTERVAL` | `10` | Seconds between gh polls |
| `AGENT_RUNNER_SUBPROCESS_TIMEOUT` | `120` | Per-command timeout |
| `AGENT_RUNNER_RETRY_ATTEMPTS` | `3` | Total attempts (incl. first) |
| `AGENT_RUNNER_RETRY_BACKOFF` | `1.5` | Backoff multiplier |
| `AGENT_RUNNER_LOG_DIR` | `~/.cache/git-agent-runner/logs` | Log file directory |
| `AGENT_RUNNER_HISTORY` | `~/.cache/git-agent-runner/runs.jsonl` | History file path |
| `AGENT_RUNNER_HISTORY_MAX_BYTES` | `0` (disabled) | Rotate `runs.jsonl` once it exceeds N bytes |
| `AGENT_RUNNER_HISTORY_MAX_AGE_DAYS` | `0` (disabled) | Auto-prune records older than N days at startup |

## Validation

### Local

```bash
make setup       # install everything
make lint        # ruff check
make format      # ruff format
make typecheck   # mypy --strict
make test        # pytest (93 tests)
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
├── process_utils.py    # subprocess wrapper: timeouts + retries + CommandResult
├── config.py           # Config loader (TOML + env) + AgentSpec registry
├── runs.py             # JSONL run-history store
├── logging_utils.py    # structured logging (stderr + rotating file)
└── tests/              # pytest suite (78 tests)
```

Every external command goes through `agent_runner.process_utils.run()`,
which gives uniform timeouts, retries, and result types.
