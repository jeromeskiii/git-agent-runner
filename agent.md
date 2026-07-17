# git-agent-runner — Dynamic Agent Pipeline

This file exists for tools that look for `agent.md`. The authoritative instructions for this repo are in `AGENTS.md`; if there is any conflict, `AGENTS.md` wins.

## Architecture

```
agent-runner (root orchestrator)
├── git-worktree-runner (gtr) — Bash CLI for worktree management
│   └── adapters/ai/pr-agent.sh — gtr → pr-agent bridge
└── pr-agent — Python PR review agent
```

## Pipeline

```
agent-runner run --task "fix auth bug" --repo . --agent claude
    │
    ├── gtr new agent/fix-auth-bug --ai claude
    │   └── Creates isolated worktree, launches Claude Code
    │
    ├── AI agent works in worktree, commits, pushes, opens PR
    │
    ├── gh pr list --head agent/fix-auth-bug → PR URL
    │
    └── pr-agent --pr_url <url> review + improve
```

## Commands

```bash
# Full pipeline
agent-runner run --task "fix auth bug" --repo . --agent claude

# Review only
agent-runner review --pr-url https://github.com/owner/repo/pull/42

# Improve only
agent-runner improve --pr-url https://github.com/owner/repo/pull/42

# Check status
agent-runner status
```

## gtr Integration

```bash
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
make all       # lint + test
```

## Working Rules

- `agent_runner/` is the orchestrator source tree
- `git-worktree-runner/` is the gtr submodule (Bash)
- `pr-agent/` is the PR agent submodule (Python)
- Keep changes small and reversible
- Test before committing