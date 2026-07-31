# git-agent-runner — Compatibility Summary

This file exists for tools that look for `agent.md`. The authoritative instructions for this repo are in `AGENTS.md`; if there is any conflict, `AGENTS.md` wins.

## Role

Treat this repository as the all-in-one `git-agent-runner` operating layer. The orchestrator (`agent_runner/`) wires vendored `git-worktree-runner` (gtr) and `pr-agent` together into a dynamic agent pipeline. Individual coding agents and review tools are replaceable components within the harness.

## Control Flow

```text
task → gtr creates worktree → AI agent works → push → PR → pr-agent reviews → iterate
```

## Component Boundaries

- **Root orchestrator** (`agent_runner/`) — Python CLI, the only first-party entry point for the integrated pipeline
- **Vendored git-worktree-runner** (`vendor/git-worktree-runner/`) — Bash CLI, manages worktrees and launches agents
- **Vendored pr-agent** (`vendor/pr-agent/`) — Python PR review agent, reviews/improves/describes PRs
- **Adapters** (`vendor/git-worktree-runner/adapters/ai/pr-agent.sh`) — gtr adapter that bridges to pr-agent
- **Hooks** (`.gtrconfig`) — lifecycle hooks for the pipeline

## Architecture

```text
agent-runner (root orchestrator)
├── agent_runner/ — first-party Python orchestrator package
└── vendor/
    ├── git-worktree-runner/ — vendored Bash CLI for worktree management
    │   └── adapters/ai/pr-agent.sh — gtr → pr-agent bridge
    └── pr-agent/ — vendored Python PR review agent
```

## Pipeline

```text
agent-runner run --task "fix auth bug" --repo . --agent claude
    │
    ├── pre-flight checks (gtr, gh auth, pr-agent, repo state)
    │
    ├── gtr new agent/fix-auth-bug
    │   └── Creates isolated worktree
    │
    ├── gtr ai agent/fix-auth-bug --ai claude -- --args task
    │   └── Launches AI agent in worktree
    │
    ├── AI agent works in worktree, commits, pushes, opens PR
    │
    ├── gh pr list --head agent/fix-auth-bug → PR URL
    │
    ├── pr-agent review (feedback back to agent if iterations > 1)
    │
    └── pr-agent improve
```

## Commands

```bash
# Full pipeline
agent-runner run --task "fix auth bug" --repo . --agent claude

# Pre-flight validation only
agent-runner preflight --repo .

# Review only
agent-runner review --pr-url https://github.com/owner/repo/pull/42

# Improve only
agent-runner improve --pr-url https://github.com/owner/repo/pull/42

# Check status (Rich table)
agent-runner status

# Show config as JSON
agent-runner config --repo .

# View run history (Rich table)
agent-runner logs --limit 20

# Reconcile worktrees + auto-fix orphans
agent-runner doctor --repo . --fix

# Use pr-agent as an AI tool via gtr
git gtr ai <worktree-id> --ai pr-agent -- review

# Set pr-agent as default AI tool
git gtr config set gtr.ai.default pr-agent
```

## Validation

```bash
make setup     # Install everything
make lint      # ruff check
make test      # pytest
make all       # lint + format + typecheck + test (128 tests)
```

## Working Rules

- `agent_runner/` is the first-party orchestrator source tree
- `vendor/git-worktree-runner/` is the vendored gtr engine (Bash)
- `vendor/pr-agent/` is the vendored PR agent engine (Python)
- Keep changes small and reversible
- Preserve unrelated local modifications
- Avoid touching `.architect/`, cache directories, build artifacts, generated outputs, or secrets
- Test before committing

## Completion Contract

Every implementation report must include:

1. What changed
2. Exact files changed
3. Validation commands and actual results
4. Remaining risks or blocked work
