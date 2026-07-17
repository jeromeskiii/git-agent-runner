"""Tests for subprocess helper."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_runner.process_utils import CommandResult, run


def test_run_success() -> None:
    r = run(["true"])
    assert r.ok
    assert r.returncode == 0


def test_run_failure_marks_not_ok() -> None:
    r = run(["false"])
    assert not r.ok
    assert r.returncode != 0


def test_run_timeout_sets_timed_out() -> None:
    r = run(["sleep", "10"], timeout=0.1)
    assert r.timed_out is True
    assert not r.ok


def test_run_retries_transient_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    # Speed up backoff so the test is fast.
    calls = {"n": 0}

    def fake_sleep(_: float) -> None:
        calls["n"] += 1

    monkeypatch.setattr("agent_runner.process_utils.time.sleep", fake_sleep)

    # Use a shell-like test: first two calls fail (exit 2 = retryable), third succeeds.
    counter = {"i": 0}

    def fake_subprocess_run(*args: object, **kwargs: object) -> _FakeCompleted:
        counter["i"] += 1
        if counter["i"] < 3:
            return _FakeCompleted(2, "", "")
        return _FakeCompleted(0, "ok", "")

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    r = run(["whatever"], retries=3, backoff=1.1, retry_on_exit=frozenset({2}))
    assert r.ok
    assert r.attempts == 3
    assert calls["n"] == 2  # slept between each of the 3 attempts


def test_run_check_raises() -> None:
    import pytest

    with pytest.raises(RuntimeError, match="command failed"):
        run(["false"], check=True)


def test_run_uses_cwd(tmp_path: Path) -> None:
    r = run(["pwd"], cwd=tmp_path)
    # The output contains the resolved tmp_path.
    assert str(tmp_path) in r.stdout


def test_run_captures_stderr() -> None:
    r = run(["sh", "-c", "echo bad 1>&2; exit 1"])
    assert "bad" in r.stderr
    assert not r.ok


def test_command_result_short_cmd_truncates() -> None:
    r = CommandResult(
        cmd=["echo", "hello", "world", "extra", "tokens"],
        returncode=0,
        stdout="",
        stderr="",
        duration_s=0.0,
    )
    assert "echo" in r.short_cmd()
    assert "..." in r.short_cmd()


class _FakeCompleted:
    def __init__(self, rc: int, out: str, err: str) -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err
