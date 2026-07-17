"""Tests for the CLI handlers."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_runner import cli
from agent_runner.orchestrator import PrActionResult


def _args(**kwargs: object) -> MagicMock:
    ns = MagicMock()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


def test_cmd_run_success(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = MagicMock(
        errors=[],
        run_id="abc123",
        branch_name="agent/x",
        worktree_path="/w",
        pr_url="https://x",
        dry_run=False,
    )
    with patch("agent_runner.cli.run_pipeline", return_value=ctx):
        rc = cli._cmd_run(_args(task="x", repo=".", agent=None, timeout=None, dry_run=False))
    assert rc == 0
    captured = capsys.readouterr()
    assert "✅ Pipeline completed" in captured.out
    assert "abc123" in captured.out


def test_cmd_run_failure(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = MagicMock(
        errors=["boom"], run_id="abc", branch_name="", worktree_path="", pr_url="", dry_run=False
    )
    with patch("agent_runner.cli.run_pipeline", return_value=ctx):
        rc = cli._cmd_run(_args(task="x", repo=".", agent=None, timeout=None, dry_run=False))
    assert rc == 1
    captured = capsys.readouterr()
    assert "boom" in captured.err


def test_cmd_run_dry_run_marker(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = MagicMock(
        errors=[],
        run_id="abc",
        branch_name="agent/x",
        worktree_path="/w",
        pr_url="https://x",
        dry_run=True,
    )
    with patch("agent_runner.cli.run_pipeline", return_value=ctx):
        cli._cmd_run(_args(task="x", repo=".", agent=None, timeout=None, dry_run=True))
    out = capsys.readouterr().out
    assert "(dry-run)" in out


def test_cmd_review_success(capsys: pytest.CaptureFixture[str]) -> None:
    result = PrActionResult(
        pr_url="u", action="review", success=True, stdout="looks good", duration_s=0.1, attempts=1
    )
    with patch("agent_runner.cli.review_pr", return_value=result):
        rc = cli._cmd_review(_args(pr_url="u", repo="."))
    assert rc == 0
    out = capsys.readouterr().out
    assert "✅ Review completed" in out


def test_cmd_review_failure(capsys: pytest.CaptureFixture[str]) -> None:
    result = PrActionResult(
        pr_url="u",
        action="review",
        success=False,
        stderr="nope",
        returncode=2,
        duration_s=0.1,
        attempts=1,
        errors=["pr-agent review failed: nope"],
    )
    with patch("agent_runner.cli.review_pr", return_value=result):
        rc = cli._cmd_review(_args(pr_url="u", repo="."))
    assert rc == 1
    err = capsys.readouterr().err
    assert "❌ Review failed" in err


def test_cmd_improve_success(capsys: pytest.CaptureFixture[str]) -> None:
    result = PrActionResult(
        pr_url="u", action="improve", success=True, stdout="", duration_s=0.1, attempts=1
    )
    with patch("agent_runner.cli.improve_pr", return_value=result):
        rc = cli._cmd_improve(_args(pr_url="u", repo="."))
    assert rc == 0


def test_cmd_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with patch("agent_runner.cli.shutil.which", return_value=None):
        rc = cli._cmd_status(_args(repo=str(tmp_path)))
    assert rc == 1
    out = capsys.readouterr().out
    assert "git-agent-runner status" in out
    assert "default agent" in out


def test_cmd_logs_empty(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("agent_runner.cli.RunHistory") as mh:
        mh.return_value.latest.return_value = []
        rc = cli._cmd_logs(_args(limit=10, prune_days=0, compact=False))
    assert rc == 0
    assert "No pipeline runs" in capsys.readouterr().out


def test_cmd_logs_with_records(capsys: pytest.CaptureFixture[str]) -> None:
    from agent_runner.runs import RunRecord

    rec = RunRecord(
        run_id="x",
        ts=1.0,
        event="run_start",
        task="hello",
        agent="claude",
        branch="agent/h",
        worktree="/w",
        pr_url="https://x",
        extra={"duration_s": 1.2},
    )
    with patch("agent_runner.cli.RunHistory") as mh:
        mh.return_value.latest.return_value = [rec]
        rc = cli._cmd_logs(_args(limit=10, prune_days=0, compact=False))
    assert rc == 0
    assert "x" in capsys.readouterr().out


def test_cmd_logs_with_prune(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("agent_runner.cli.RunHistory") as mh:
        mh.return_value.prune_older_than.return_value = 5
        mh.return_value.latest.return_value = []
        rc = cli._cmd_logs(_args(limit=10, prune_days=7, compact=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Pruned 5 records" in out


def test_cmd_logs_with_compact(capsys: pytest.CaptureFixture[str]) -> None:
    from agent_runner.runs import RunRecord

    rec = RunRecord(run_id="x", ts=1.0, event="run_end", task="t")
    with patch("agent_runner.cli.RunHistory") as mh:
        mh.return_value.compact.return_value = 12
        mh.return_value.latest.return_value = [rec]
        rc = cli._cmd_logs(_args(limit=10, prune_days=0, compact=True))
    assert rc == 0
    out = capsys.readouterr().out
    assert "Compacted 12 stage records" in out


def test_cmd_config_outputs_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli._cmd_config(_args(repo=str(tmp_path)))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default_agent"] == "claude"
    assert "claude" in payload["agents"]


def test_cmd_clean_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli._cmd_clean(_args(dry_run=True))
    assert rc == 0
    assert "Would truncate" in capsys.readouterr().out


def test_cmd_clean_no_file(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("agent_runner.cli.RunHistory") as mh:
        mh.return_value.path.exists.return_value = False
        rc = cli._cmd_clean(_args(dry_run=False))
    assert rc == 0
    assert "Nothing to clean" in capsys.readouterr().out


def test_main_no_command_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["agent-runner"]):
        with pytest.raises(SystemExit) as e:
            cli.main()
    assert e.value.code == 1


def test_main_version(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(sys, "argv", ["agent-runner", "--version"]):
        with pytest.raises(SystemExit) as e:
            cli.main()
    assert e.value.code == 0
    assert "agent-runner" in capsys.readouterr().out


def test_main_unknown_command_exits(capsys: pytest.CaptureFixture[str]) -> None:
    # argparse exits with code 2 on invalid choices; that's the correct behavior.
    with patch.object(sys, "argv", ["agent-runner", "nope"]):
        with pytest.raises(SystemExit) as e:
            cli.main()
    assert e.value.code == 2
