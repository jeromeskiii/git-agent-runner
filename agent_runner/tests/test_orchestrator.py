"""Tests for the orchestrator module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_runner.orchestrator import (
    PrActionResult,
    _pr_action,
    improve_pr,
    review_pr,
    run_pipeline,
)


class TestPrActionResult:
    def test_from_command_success(self) -> None:
        cmd = MagicMock(
            ok=True,
            stdout="ok",
            stderr="",
            returncode=0,
            duration_s=0.1,
            timed_out=False,
            attempts=1,
        )
        r = PrActionResult.from_command("u", "review", cmd)
        assert r.success is True
        assert r.errors == []

    def test_from_command_failure(self) -> None:
        cmd = MagicMock(
            ok=False,
            stdout="",
            stderr="nope",
            returncode=2,
            duration_s=0.2,
            timed_out=False,
            attempts=3,
        )
        r = PrActionResult.from_command("u", "review", cmd)
        assert r.success is False
        assert any("pr-agent review failed" in e for e in r.errors)


class TestRunPipeline:
    def test_dry_run_short_circuits(self, tmp_path: Path) -> None:
        # No gtr / gh / pr-agent required when dry_run=True.
        ctx = run_pipeline(task="hello", repo=tmp_path, dry_run=True)
        # In dry_run we still set branch_name + worktree_path + pr_url.
        assert ctx.branch_name.startswith("agent/hello-")
        assert ctx.worktree_path.startswith("<dry-run>")
        assert ctx.errors == []

    @patch("agent_runner.orchestrator.load_config")
    def test_unknown_agent_reports_error(self, mock_load: MagicMock, tmp_path: Path) -> None:
        from agent_runner.config import AgentSpec, Config

        mock_load.return_value = Config(
            default_agent="claude",
            poll_interval=10,
            default_timeout=300,
            subprocess_timeout=120,
            retry_attempts=3,
            retry_backoff=1.5,
            agents={"claude": AgentSpec(name="claude", command=["claude"], description="")},
        )
        ctx = run_pipeline(task="x", repo=tmp_path, agent="nope", dry_run=True)
        assert any("Unknown agent" in e for e in ctx.errors)

    @patch("agent_runner.orchestrator.load_config")
    def test_default_agent_used(self, mock_load: MagicMock, tmp_path: Path) -> None:
        from agent_runner.config import AgentSpec, Config

        mock_load.return_value = Config(
            default_agent="codex",
            poll_interval=10,
            default_timeout=300,
            subprocess_timeout=120,
            retry_attempts=3,
            retry_backoff=1.5,
            agents={
                "codex": AgentSpec(name="codex", command=["codex"], description=""),
                "claude": AgentSpec(name="claude", command=["claude"], description=""),
            },
        )
        ctx = run_pipeline(task="x", repo=tmp_path, dry_run=True)
        assert ctx.ai_agent == "codex"
        assert ctx.errors == []


class TestPrActions:
    @patch("agent_runner.pipeline.run")
    def test_review_pr_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=True,
            returncode=0,
            stdout="ok",
            stderr="",
            duration_s=0.1,
            timed_out=False,
            attempts=1,
        )
        r = review_pr("u", ".")
        assert r.success is True
        assert r.action == "review"

    @patch("agent_runner.pipeline.run")
    def test_improve_pr_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=False,
            returncode=1,
            stdout="",
            stderr="boom",
            duration_s=0.1,
            timed_out=False,
            attempts=2,
        )
        r = improve_pr("u", ".")
        assert r.success is False
        assert r.action == "improve"

    @patch("agent_runner.pipeline.run")
    def test_pr_action_uses_config_timeouts(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=True, returncode=0, stdout="", stderr="", duration_s=0.1, timed_out=False, attempts=1
        )
        _pr_action("u", ".", "review")
        kwargs = mock_run.call_args.kwargs
        assert kwargs["timeout"] == 120  # default subprocess_timeout
        assert kwargs["retries"] == 2  # retry_attempts - 1
        assert kwargs["retry_on_timeout"] is False  # posted comments must not duplicate
