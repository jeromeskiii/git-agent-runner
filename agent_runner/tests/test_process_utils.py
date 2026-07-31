"""Tests for subprocess helper."""

from __future__ import annotations

import time
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

    # First two attempts fail (exit 2 = retryable), third succeeds.
    counter = {"i": 0}

    class _FakePopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            counter["i"] += 1
            self.returncode = 2 if counter["i"] < 3 else 0

        def communicate(self, timeout: object = None) -> tuple[str, str]:
            return "", ""

    monkeypatch.setattr("agent_runner.process_utils.subprocess.Popen", _FakePopen)
    r = run(["whatever"], retries=3, backoff=1.1, retry_on_exit=frozenset({2}))
    assert r.ok
    assert r.attempts == 3
    assert calls["n"] == 2  # slept between each of the 3 attempts


def test_run_timeout_kills_process_group(tmp_path: Path) -> None:
    """Fault injection: a timed-out command must not leave orphaned
    grandchildren running — the marker must never be written."""
    marker = tmp_path / "marker"
    # Grandchild writes the marker at t=1.0s; parent sleeps forever.
    r = run(
        ["sh", "-c", f'(sleep 1; touch "{marker}") & exec sleep 30'],
        timeout=0.2,
    )
    assert r.timed_out is True
    assert not r.ok
    time.sleep(1.5)  # well past the grandchild's scheduled write
    assert not marker.exists()


def test_run_stream_returns_empty_output(capfd: pytest.CaptureFixture[str]) -> None:
    r = run(["echo", "hi"], stream=True)
    assert r.ok
    assert r.stdout == ""
    assert r.stderr == ""
    # ...but the child's output reached our stdout (fd-level capture, since
    # the child inherits the real fd, not pytest's sys.stdout object).
    assert "hi" in capfd.readouterr().out


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


def test_run_interrupt_kills_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeInterruptPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.pid = 99999

        def communicate(self, timeout: object = None) -> tuple[str, str]:
            raise KeyboardInterrupt()

    killed_pgids = []
    monkeypatch.setattr("agent_runner.process_utils.subprocess.Popen", _FakeInterruptPopen)
    monkeypatch.setattr("os.getpgid", lambda pid: 99999)
    monkeypatch.setattr("os.killpg", lambda pgid, sig: killed_pgids.append((pgid, sig)))

    with pytest.raises(KeyboardInterrupt):
        run(["whatever"])

    assert (99999, 9) in killed_pgids
