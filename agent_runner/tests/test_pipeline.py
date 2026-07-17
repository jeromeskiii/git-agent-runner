"""Tests for the pipeline module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_runner.pipeline import (
    PipelineContext,
    PrerequisiteError,
    _gtr_base,
    _parse_worktree_path,
    _sanitize_branch_name,
    check_gh_available,
    run_full_pipeline,
    stage_create_worktree,
    stage_improve_pr,
    stage_launch_agent,
    stage_review_pr,
    stage_wait_for_pr,
)
from agent_runner.runs import RunHistory, RunRecord


def _ctx(**kwargs: object) -> PipelineContext:
    defaults: dict[str, object] = {"repo_root": Path("/tmp/repo"), "task": "test task"}
    defaults.update(kwargs)
    return PipelineContext(**defaults)  # type: ignore[arg-type]


class TestSanitizeBranchName:
    def test_simple_task(self) -> None:
        assert _sanitize_branch_name("fix auth bug") == "agent/fix-auth-bug"

    def test_long_task(self) -> None:
        long_task = "a" * 80
        result = _sanitize_branch_name(long_task)
        assert result.startswith("agent/")
        assert len(result) <= 56

    def test_special_chars_collapse(self) -> None:
        assert _sanitize_branch_name("fix: auth & login") == "agent/fix-auth-login"

    def test_leading_trailing_hyphens(self) -> None:
        assert _sanitize_branch_name("---fix bug---") == "agent/fix-bug"

    def test_unicode_normalized(self) -> None:
        assert _sanitize_branch_name("cafe naive") == "agent/cafe-naive"

    def test_all_stripped(self) -> None:
        assert _sanitize_branch_name("!!!") == "agent/"


class TestPipelineContext:
    def test_defaults(self) -> None:
        ctx = PipelineContext(repo_root=Path("/tmp/repo"), task="test task")
        assert ctx.ai_agent == "claude"
        assert ctx.branch_name == ""
        assert ctx.worktree_path == ""
        assert ctx.pr_url == ""
        assert ctx.errors == []
        assert ctx.dry_run is False
        assert ctx.run_id

    def test_run_id_is_unique(self) -> None:
        a = PipelineContext(repo_root=Path("/tmp/repo"), task="a")
        b = PipelineContext(repo_root=Path("/tmp/repo"), task="b")
        assert a.run_id != b.run_id


class TestParseWorktreePath:
    def test_explicit_marker(self, tmp_path: Path) -> None:
        stdout = f"preamble\nCreated worktree at: {tmp_path}\nmore"
        assert _parse_worktree_path(stdout) == str(tmp_path)

    def test_fallback_lookalike(self, tmp_path: Path) -> None:
        stdout = f"Worktree ready at {tmp_path}"
        assert _parse_worktree_path(stdout) == str(tmp_path)

    def test_no_match(self) -> None:
        assert _parse_worktree_path("no paths here") is None

    def test_nonexistent_path_skipped(self) -> None:
        assert _parse_worktree_path("Created worktree at: /nope/here") is None


class TestStageCreateWorktree:
    @patch("agent_runner.pipeline.run")
    def test_handles_gtr_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, returncode=1, stderr="gtr failed", stdout="")
        ctx = _ctx(task="test task")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = stage_create_worktree(ctx)
        assert len(result.errors) > 0
        assert result.worktree_path == ""

    @patch("agent_runner.pipeline.run")
    def test_parses_worktree_path(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(
            ok=True,
            returncode=0,
            stdout=f"Created worktree at: {tmp_path}\n",
            stderr="",
        )
        ctx = _ctx(task="hello world")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = stage_create_worktree(ctx)
        assert result.worktree_path == str(tmp_path)
        assert result.branch_name == "agent/hello-world"

    def test_gtr_missing(self) -> None:
        ctx = _ctx(task="hello")
        # Simulate the real error from _gtr_bin when gtr is not installed.
        with patch(
            "agent_runner.pipeline._gtr_bin",
            side_effect=FileNotFoundError("git-gtr not found. Run `make setup`."),
        ):
            result = stage_create_worktree(ctx)
        assert any("git-gtr not found" in e for e in result.errors)

    def test_dry_run_short_circuits(self) -> None:
        ctx = _ctx(task="dry", dry_run=True)
        result = stage_create_worktree(ctx)
        assert result.branch_name == "agent/dry"
        assert result.worktree_path.startswith("<dry-run>")


class TestStageLaunchAgent:
    def test_skips_when_no_worktree(self) -> None:
        ctx = _ctx()
        assert stage_launch_agent(ctx) is ctx

    def test_dry_run(self) -> None:
        ctx = _ctx(worktree_path="/some/where", dry_run=True)
        assert stage_launch_agent(ctx).errors == []

    @patch("agent_runner.pipeline.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        ctx = _ctx(worktree_path="/some/where", branch_name="agent/x")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            assert stage_launch_agent(ctx).errors == []

    @patch("agent_runner.pipeline.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, returncode=2, stdout="", stderr="boom")
        ctx = _ctx(worktree_path="/some/where", branch_name="agent/x")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            assert len(stage_launch_agent(ctx).errors) == 1


class TestStageWaitForPr:
    @patch("agent_runner.pipeline.time.sleep", return_value=None)
    @patch("agent_runner.pipeline.run")
    def test_finds_pr(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=True,
            returncode=0,
            stdout="https://github.com/owner/repo/pull/42\n",
            stderr="",
        )
        ctx = _ctx(branch_name="agent/test")
        result = stage_wait_for_pr(ctx, timeout=5, poll_interval=1)
        assert result.pr_url == "https://github.com/owner/repo/pull/42"

    @patch("agent_runner.pipeline.time.sleep", return_value=None)
    @patch("agent_runner.pipeline.run")
    def test_timeout(self, mock_run: MagicMock, mock_sleep: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        ctx = _ctx(branch_name="agent/test")
        result = stage_wait_for_pr(ctx, timeout=1, poll_interval=1)
        assert len(result.errors) > 0
        assert "No PR found" in result.errors[0]

    @patch("agent_runner.pipeline.run")
    def test_gh_unauth(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=False, returncode=1, stdout="", stderr="You are not logged in"
        )
        ctx = _ctx(branch_name="agent/test")
        result = stage_wait_for_pr(ctx, timeout=1, poll_interval=1)
        assert any("not authenticated" in e for e in result.errors)

    def test_dry_run(self) -> None:
        ctx = _ctx(branch_name="agent/x", dry_run=True)
        result = stage_wait_for_pr(ctx, timeout=10)
        assert result.pr_url.startswith("<dry-run>")


class TestStageReviewPr:
    @patch("agent_runner.pipeline.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="Review done", stderr="")
        ctx = _ctx(pr_url="https://github.com/owner/repo/pull/42")
        result = stage_review_pr(ctx)
        assert result.errors == []
        assert result.review_result["review"] == "Review done"

    @patch("agent_runner.pipeline.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, returncode=1, stdout="", stderr="Review failed")
        ctx = _ctx(pr_url="https://github.com/owner/repo/pull/42")
        result = stage_review_pr(ctx)
        assert len(result.errors) > 0

    def test_skips_without_pr(self) -> None:
        ctx = _ctx()
        assert stage_review_pr(ctx).errors == []


class TestStageImprovePr:
    @patch("agent_runner.pipeline.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="Improved", stderr="")
        ctx = _ctx(pr_url="https://github.com/owner/repo/pull/42")
        result = stage_improve_pr(ctx)
        assert result.review_result["improve"] == "Improved"

    @patch("agent_runner.pipeline.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, returncode=2, stdout="", stderr="nope")
        ctx = _ctx(pr_url="https://github.com/owner/repo/pull/42")
        assert len(stage_improve_pr(ctx).errors) == 1


class TestRunFullPipeline:
    def test_history_records_run_start_end(self, tmp_path: Path) -> None:
        history_path = tmp_path / "runs.jsonl"
        history = RunHistory(path=history_path)

        ctx = _ctx(history=history, dry_run=True)
        run_full_pipeline(ctx)

        records = [json.loads(line) for line in history_path.read_text().splitlines()]
        events = [r["event"] for r in records]
        assert events[0] == "run_start"
        assert events[-1] == "run_end"

    def test_pr_timeout_flows_through(self) -> None:
        ctx = _ctx(dry_run=True)
        run_full_pipeline(ctx, pr_timeout=42)
        assert ctx.errors == []

    def test_halts_after_error(self) -> None:
        ctx = _ctx(errors=["pre-existing"])
        result = run_full_pipeline(ctx)
        assert result.errors == ["pre-existing"]


def test_run_record_roundtrip() -> None:
    r = RunRecord(
        run_id="abc",
        ts=1.0,
        event="run_start",
        task="x",
        repo="/r",
        agent="claude",
        branch="agent/x",
        worktree="/w",
        pr_url="",
        duration_s=0.0,
        error="",
        extra={"k": 1},
    )
    payload = json.loads(r.to_jsonl())
    assert payload["run_id"] == "abc"
    assert payload["event"] == "run_start"
    assert payload["extra"] == {"k": 1}


class TestGtrBase:
    def test_prefers_repo_root_when_dotgit_exists(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        ctx = _ctx(repo_root=tmp_path)
        assert _gtr_base(ctx) == tmp_path

    def test_falls_back_to_parent_when_no_dotgit(self, tmp_path: Path) -> None:
        # No .git in tmp_path → use parent (legacy gtr behaviour).
        ctx = _ctx(repo_root=tmp_path)
        assert _gtr_base(ctx) == tmp_path.parent


class TestCheckGhAvailable:
    def test_raises_when_gh_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force shutil.which to return None for "gh".
        monkeypatch.setattr("agent_runner.pipeline.shutil.which", lambda _: None)
        with pytest.raises(PrerequisiteError, match="gh CLI not found"):
            check_gh_available()

    def test_returns_path_when_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agent_runner.pipeline.shutil.which", lambda name: f"/usr/bin/{name}")
        assert check_gh_available() == "/usr/bin/gh"


class TestStageWaitForPrGhMissing:
    def test_short_circuits_with_clear_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("agent_runner.pipeline.shutil.which", lambda _: None)
        # No gh on PATH → must error before polling.
        ctx = _ctx(branch_name="agent/x")
        result = stage_wait_for_pr(ctx, timeout=1, poll_interval=1)
        assert any("gh CLI not found" in e for e in result.errors)
        assert result.pr_url == ""
