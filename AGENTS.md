# git-agent-runner — Agent Contract

## Role

Treat this repository as the git-agent-runner operating layer. The orchestrator (`agent_runner/`) wires `git-worktree-runner` (gtr) and `pr-agent` together into a dynamic agent pipeline. Individual coding agents and review tools are replaceable components within the harness.

## Control Flow

```text
task → gtr creates worktree → AI agent works → push → PR → pr-agent reviews → iterate
```

## Component Boundaries

- **Root orchestrator** (`agent_runner/`) — Python CLI, the only entry point for the integrated pipeline
- **git-worktree-runner** (`git-worktree-runner/`) — Bash CLI, manages worktrees and launches agents
- **pr-agent** (`pr-agent/`) — Python PR review agent, reviews/improves/describes PRs
- **Adapters** (`git-worktree-runner/adapters/ai/pr-agent.sh`) — gtr adapter that bridges to pr-agent
- **Hooks** (`.gtrconfig`) — lifecycle hooks for the pipeline

## Working Rules

- Keep changes small and reversible
- Preserve unrelated local modifications
- Avoid touching `.architect/`, cache directories, build artifacts, generated outputs, or secrets
- Treat `agent_runner/` as the main source tree for orchestrator work
- gtr adapters live in `git-worktree-runner/adapters/ai/`
- pr-agent configuration lives in `pr-agent/.pr_agent.toml`

## Validation

```bash
make setup          # Install gtr + pr-agent + orchestrator
make lint           # ruff check agent_runner/
make test           # pytest agent_runner/tests/
make all            # lint + test
```

## Completion Contract

Every implementation report must include:

1. What changed
2. Exact files changed
3. Validation commands and actual results
4. Remaining risks or blocked work