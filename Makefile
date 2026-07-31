# git-agent-runner — root Makefile
#
# Wires vendored git-worktree-runner (gtr) and pr-agent together into a
# single dynamic agent pipeline.

.PHONY: help install install-gtr install-pr-agent test test-orchestrator lint format typecheck setup all clean

help:
	@echo "git-agent-runner — dynamic agent pipeline"
	@echo "  setup             Install gtr + pr-agent + orchestrator"
	@echo "  install-gtr       Install vendored git-worktree-runner (gtr CLI)"
	@echo "  install-pr-agent  Install pr-agent in venv"
	@echo "  test              Run orchestrator tests (agent_runner/tests)"
	@echo "  test-orchestrator Alias for test"
	@echo "  lint              Run ruff check on agent_runner"
	@echo "  format            Run ruff format on agent_runner"
	@echo "  typecheck         Run mypy on agent_runner"
	@echo "  all               lint + format + typecheck + test"
	@echo "  clean             Remove build artifacts"

setup: install-gtr install-pr-agent
	@echo "✅ git-agent-runner is ready"
	@echo "   Run: agent-runner --help"

install-gtr:
	cd vendor/git-worktree-runner && bash install.sh
	@echo "✅ gtr installed"

install-pr-agent:
	uv sync --extra dev
	@echo "✅ pr-agent ready"

test test-orchestrator:
	uv run pytest agent_runner/tests -v

lint:
	uv run ruff check agent_runner/

format:
	uv run ruff format agent_runner/

typecheck:
	uv run mypy agent_runner

all: lint format typecheck test

clean:
	rm -rf .venv .pytest_cache .ruff_cache __pycache__ .mypy_cache
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +