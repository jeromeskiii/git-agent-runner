"""Tests for the pipeline module."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_runner.config import AgentSpec, Config
from agent_runner.pipeline import (
    PipelineContext,
    PrerequisiteError,
    _gtr_base,
    _resolve_worktree_path,
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


def _make_repo() -> Path:
    """A real (empty) git-repo-shaped dir so the fail-closed guard passes."""
    p = Path(tempfile.mkdtemp(prefix="gar-test-repo-"))
    (p / ".git").mkdir()
    return p


def _ctx(**kwargs: object) -> PipelineContext:
    defaults: dict[str, object] = {"repo_root": _make_repo(), "task": "test task"}
    defaults.update(kwargs)
    return PipelineContext(**defaults)  # type: ignore[arg-type]


def _hash(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8", errors="replace")).hexdigest()[:6]


class TestSanitizeBranchName:
    def test_simple_task(self) -> None:
        assert (
            _sanitize_branch_name("fix auth bug") == f"agent/fix-auth-bug-{_hash('fix auth bug')}"
        )

    def test_long_task(self) -> None:
        long_task = "a" * 80
        result = _sanitize_branch_name(long_task)
        assert result.startswith("agent/")
        assert len(result) <= 56

    def test_distinct_long_tasks_get_distinct_branches(self) -> None:
        """Truncation alone made same-prefix tasks share a branch (collision)."""
        prefix = "shared prefix that is definitely longer than fifty characters "
        a = _sanitize_branch_name(prefix + "alpha")
        b = _sanitize_branch_name(prefix + "bravo")
        assert a != b
        assert a.split("-")[-1] == _hash(prefix + "alpha")

    def test_special_chars_collapse(self) -> None:
        assert (
            _sanitize_branch_name("fix: auth & login")
            == f"agent/fix-auth-login-{_hash('fix: auth & login')}"
        )

    def test_leading_trailing_hyphens(self) -> None:
        assert _sanitize_branch_name("---fix bug---") == f"agent/fix-bug-{_hash('---fix bug---')}"

    def test_unicode_normalized(self) -> None:
        assert _sanitize_branch_name("cafe naive") == f"agent/cafe-naive-{_hash('cafe naive')}"

    def test_all_stripped_raises(self) -> None:
        with pytest.raises(ValueError, match="no usable branch name"):
            _sanitize_branch_name("!!!")


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


class TestResolveWorktreePath:
    """Path resolution goes through `gtr go` — gtr's machine-readable contract."""

    @patch("agent_runner.pipeline.run")
    def test_resolves_from_gtr_go(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout=f"{tmp_path}\n", stderr="")
        ctx = _ctx(branch_name="agent/x")
        assert _resolve_worktree_path("/fake/gtr", ctx) == str(tmp_path)
        argv = mock_run.call_args.args[0]
        assert argv == ["/fake/gtr", "go", "agent/x"]

    @patch("agent_runner.pipeline.run")
    def test_gtr_go_failure_returns_none(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, returncode=1, stdout="", stderr="boom")
        ctx = _ctx(branch_name="agent/x")
        assert _resolve_worktree_path("/fake/gtr", ctx) is None

    @patch("agent_runner.pipeline.run")
    def test_nonexistent_path_returns_none(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="/nope/here\n", stderr="")
        ctx = _ctx(branch_name="agent/x")
        assert _resolve_worktree_path("/fake/gtr", ctx) is None


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
    def test_create_argv_has_no_ai_flag(self, mock_run: MagicMock, tmp_path: Path) -> None:
        """Regression: `gtr new --ai <name>` is a boolean in gtr and launches the
        configured default AI inside the create step — never pass it."""
        mock_run.side_effect = [
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),
            MagicMock(ok=True, returncode=0, stdout=f"{tmp_path}\n", stderr=""),
        ]
        ctx = _ctx(task="hello world")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            stage_create_worktree(ctx)
        new_argv = mock_run.call_args_list[0].args[0]
        assert new_argv == ["/fake/gtr", "new", _sanitize_branch_name("hello world")]

    @patch("agent_runner.pipeline.run")
    def test_resolves_worktree_path(self, mock_run: MagicMock, tmp_path: Path) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),
            MagicMock(ok=True, returncode=0, stdout=f"{tmp_path}\n", stderr=""),
        ]
        ctx = _ctx(task="hello world")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = stage_create_worktree(ctx)
        assert result.worktree_path == str(tmp_path)
        assert result.branch_name == _sanitize_branch_name("hello world")
        go_argv = mock_run.call_args_list[1].args[0]
        assert go_argv == ["/fake/gtr", "go", _sanitize_branch_name("hello world")]

    @patch("agent_runner.pipeline.run")
    def test_resolve_failure_warns_without_error(self, mock_run: MagicMock) -> None:
        """A path-resolution hiccup must warn, not halt a healthy run."""
        mock_run.side_effect = [
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),
            MagicMock(ok=False, returncode=1, stdout="", stderr="boom"),
        ]
        ctx = _ctx(task="hello world")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = stage_create_worktree(ctx)
        assert result.errors == []
        assert len(result.warnings) == 1
        assert result.worktree_path == ""

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
        assert result.branch_name == _sanitize_branch_name("dry")
        assert result.worktree_path.startswith("<dry-run>")


class TestStageLaunchAgent:
    def test_skips_when_no_branch(self) -> None:
        ctx = _ctx()
        assert stage_launch_agent(ctx) is ctx

    def test_dry_run(self) -> None:
        ctx = _ctx(branch_name="agent/x", dry_run=True)
        assert stage_launch_agent(ctx).errors == []

    @patch("agent_runner.pipeline.run")
    def test_launches_by_branch_without_worktree_path(self, mock_run: MagicMock) -> None:
        """gtr ai resolves by branch — a create-stage path warning must not skip the launch."""
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        ctx = _ctx(branch_name="agent/x")  # no worktree_path
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            assert stage_launch_agent(ctx).errors == []
        assert mock_run.called

    @patch("agent_runner.pipeline.run")
    def test_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        ctx = _ctx(worktree_path="/some/where", branch_name="agent/x")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            assert stage_launch_agent(ctx).errors == []

    @patch("agent_runner.pipeline.run")
    def test_failure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=False, returncode=2, stdout="", stderr="", timed_out=False
        )
        ctx = _ctx(worktree_path="/some/where", branch_name="agent/x")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = stage_launch_agent(ctx)
        assert len(result.errors) == 1
        assert "exit=2" in result.errors[0]

    @patch("agent_runner.pipeline.run")
    def test_launch_argv_passes_task_through(self, mock_run: MagicMock) -> None:
        """The task text must reach the agent, after gtr's `--` separator."""
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        ctx = _ctx(task="fix the auth bug", branch_name="agent/fix")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            stage_launch_agent(ctx)
        argv = mock_run.call_args.args[0]
        assert argv[:4] == ["/fake/gtr", "ai", "agent/fix", "--ai"]
        assert argv[-2:] == ["--", "fix the auth bug"]

    @patch("agent_runner.pipeline.run")
    def test_launch_uses_agent_args_and_agent_timeout(self, mock_run: MagicMock) -> None:
        """Configured agent args (headless mode) + agent_timeout, not subprocess_timeout."""
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        cfg = Config(
            subprocess_timeout=120,
            agent_timeout=900,
            agents={"claude": AgentSpec(name="claude", command=["claude"], args=["-p"])},
        )
        ctx = _ctx(task="do work", branch_name="agent/x", config=cfg)
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            stage_launch_agent(ctx)
        call = mock_run.call_args
        assert call.args[0][-3:] == ["--", "-p", "do work"]
        assert call.kwargs["timeout"] == 900
        assert call.kwargs["stream"] is True

    @patch("agent_runner.pipeline.run")
    def test_launch_defaults_timeout_without_config(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        ctx = _ctx(branch_name="agent/x")
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            stage_launch_agent(ctx)
        assert mock_run.call_args.kwargs["timeout"] == 1800


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
    def test_returns_repo_root_always(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        ctx = _ctx(repo_root=tmp_path)
        assert _gtr_base(ctx) == tmp_path

    def test_no_parent_fallback_without_dotgit(self, tmp_path: Path) -> None:
        # Fail-closed: no .git → still repo_root (never the parent, which
        # could be an unintended enclosing repository).
        ctx = _ctx(repo_root=tmp_path)
        assert _gtr_base(ctx) == tmp_path

    def test_create_errors_on_non_git_repo(self, tmp_path: Path) -> None:
        # No .git in tmp_path → the create stage must refuse before calling gtr.
        ctx = _ctx(repo_root=tmp_path, task="hello")
        result = stage_create_worktree(ctx)
        assert any("not a git repository" in e for e in result.errors)


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


class TestStageRetries:
    @patch("agent_runner.pipeline.run")
    def test_create_passes_config_retries(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),
        ]
        cfg = Config(retry_attempts=3, retry_backoff=2.0)
        ctx = _ctx(task="retry me", config=cfg)
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            stage_create_worktree(ctx)
        kwargs = mock_run.call_args_list[0].kwargs
        assert kwargs["retries"] == 2
        assert kwargs["backoff"] == 2.0

    @patch("agent_runner.pipeline.run")
    def test_review_uses_unified_helper(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="ok", stderr="")
        cfg = Config(retry_attempts=3, subprocess_timeout=99)
        ctx = _ctx(pr_url="https://x", config=cfg)
        stage_review_pr(ctx)
        call = mock_run.call_args
        assert call.args[0] == ["pr-agent", "--pr_url", "https://x", "review"]
        assert call.kwargs["timeout"] == 99
        assert call.kwargs["retries"] == 2
        assert call.kwargs["retry_on_timeout"] is False


class TestReviewIterationLoop:
    @patch("agent_runner.pipeline.shutil.which", return_value="/usr/bin/gh")
    @patch("agent_runner.pipeline.run")
    def test_review_feedback_relaunches_agent(
        self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path
    ) -> None:
        ok = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        mock_run.side_effect = [
            ok,  # gtr new
            MagicMock(ok=True, returncode=0, stdout=f"{tmp_path}\n", stderr=""),  # gtr go
            ok,  # gtr ai (initial launch)
            ok,  # gh auth status
            MagicMock(  # gh pr list
                ok=True, returncode=0, stdout="https://x/pull/1\n", stderr=""
            ),
            MagicMock(ok=True, returncode=0, stdout="fix the typo", stderr=""),  # review 1
            ok,  # gtr ai (iteration launch)
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),  # review 2
            ok,  # improve
        ]
        cfg = Config(review_iterations=2)
        ctx = _ctx(task="do work", config=cfg)
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = run_full_pipeline(ctx)
        assert result.errors == []
        iteration_launch = mock_run.call_args_list[6]
        assert "Address the following PR review feedback" in iteration_launch.args[0][-1]
        assert "fix the typo" in iteration_launch.args[0][-1]

    @patch("agent_runner.pipeline.shutil.which", return_value="/usr/bin/gh")
    @patch("agent_runner.pipeline.run")
    def test_single_iteration_by_default(
        self, mock_run: MagicMock, mock_which: MagicMock, tmp_path: Path
    ) -> None:
        ok = MagicMock(ok=True, returncode=0, stdout="", stderr="")
        mock_run.side_effect = [
            ok,
            MagicMock(ok=True, returncode=0, stdout=f"{tmp_path}\n", stderr=""),
            ok,
            ok,  # gh auth status
            MagicMock(ok=True, returncode=0, stdout="https://x/pull/1\n", stderr=""),
            MagicMock(ok=True, returncode=0, stdout="feedback", stderr=""),
            ok,  # improve
        ]
        ctx = _ctx(task="do work", config=Config())
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = run_full_pipeline(ctx)
        assert result.errors == []
        # 7 calls: new, go, ai, auth, pr list, review, improve — no iteration launch.
        assert mock_run.call_count == 7


class TestCleanupOnError:
    @patch("agent_runner.pipeline.run")
    def test_cleanup_runs_gtr_rm_on_failure(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),  # gtr new
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),  # gtr go (unresolved ok)
            MagicMock(ok=False, returncode=1, stdout="", stderr="", timed_out=False),  # ai
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),  # gtr rm
        ]
        cfg = Config(cleanup_on_error=True)
        ctx = _ctx(task="will fail", config=cfg)
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            result = run_full_pipeline(ctx)
        assert result.errors  # the launch failure is still reported
        rm_call = mock_run.call_args_list[-1]
        assert rm_call.args[0] == ["/fake/gtr", "rm", result.branch_name, "--yes"]
        assert any("cleanup" in w for w in result.warnings)

    @patch("agent_runner.pipeline.run")
    def test_no_cleanup_by_default(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),
            MagicMock(ok=False, returncode=1, stdout="", stderr="", timed_out=False),
        ]
        ctx = _ctx(task="will fail", config=Config())
        with patch("agent_runner.pipeline._gtr_bin", return_value="/fake/gtr"):
            run_full_pipeline(ctx)
        assert mock_run.call_count == 3  # no gtr rm call


class TestCollectAgentWorktrees:
    @patch("agent_runner.pipeline.run")
    def test_parses_porcelain(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=True,
            returncode=0,
            stdout=(
                "worktree /repo\n"
                "HEAD abc\n"
                "branch refs/heads/main\n"
                "\n"
                "worktree /repo/.worktrees/agent-foo\n"
                "HEAD def\n"
                "branch refs/heads/agent/foo\n"
                "\n"
                "worktree /detached\n"
                "HEAD 123\n"
                "detached\n"
            ),
            stderr="",
        )
        from agent_runner.pipeline import collect_agent_worktrees

        result = collect_agent_worktrees(Path("/repo"))
        assert result == [{"path": "/repo/.worktrees/agent-foo", "branch": "agent/foo"}]

    @patch("agent_runner.pipeline.run")
    def test_git_failure_returns_empty(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=False, returncode=1, stdout="", stderr="bad")
        from agent_runner.pipeline import collect_agent_worktrees

        assert collect_agent_worktrees(Path("/repo")) == []


class TestWaitForPrAuthPrecheck:
    @patch("agent_runner.pipeline.shutil.which", return_value="/usr/bin/gh")
    @patch("agent_runner.pipeline.run")
    def test_auth_failure_skips_polling(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            ok=False, returncode=1, stdout="", stderr="not logged into any hosts"
        )
        ctx = _ctx(branch_name="agent/x")
        result = stage_wait_for_pr(ctx, timeout=30, poll_interval=1)
        assert any("not authenticated" in e for e in result.errors)
        # One `gh auth status` call, zero `gh pr list` calls.
        assert mock_run.call_count == 1
        assert mock_run.call_args.args[0][:3] == ["gh", "auth", "status"]

    @patch("agent_runner.pipeline.time.sleep", return_value=None)
    @patch("agent_runner.pipeline.shutil.which", return_value="/usr/bin/gh")
    @patch("agent_runner.pipeline.run")
    def test_auth_ok_proceeds_to_poll(
        self, mock_run: MagicMock, mock_which: MagicMock, mock_sleep: MagicMock
    ) -> None:
        mock_run.side_effect = [
            MagicMock(ok=True, returncode=0, stdout="", stderr=""),  # gh auth status
            MagicMock(  # gh pr list
                ok=True, returncode=0, stdout="https://x/pull/1\n", stderr=""
            ),
        ]
        ctx = _ctx(branch_name="agent/x")
        result = stage_wait_for_pr(ctx, timeout=5, poll_interval=1)
        assert result.pr_url == "https://x/pull/1"
        assert result.errors == []


class TestPrAgentSilentFailure:
    @patch("agent_runner.pipeline.run")
    def test_exit_zero_with_error_marker_is_failure(self, mock_run: MagicMock) -> None:
        """pr-agent exits 0 on internal failure — the marker must flip it."""
        mock_run.return_value = MagicMock(
            ok=True,
            returncode=0,
            stdout="ERROR | Failed to process the command.\nTraceback ...",
            stderr="",
        )
        ctx = _ctx(pr_url="https://x")
        result = stage_review_pr(ctx)
        assert len(result.errors) == 1
        assert "review failed" in result.errors[0]
        assert "review" not in result.review_result

    @patch("agent_runner.pipeline.run")
    def test_clean_exit_zero_still_success(self, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(ok=True, returncode=0, stdout="all good", stderr="")
        ctx = _ctx(pr_url="https://x")
        result = stage_review_pr(ctx)
        assert result.errors == []
        assert result.review_result["review"] == "all good"
