"""Subprocess helpers — timeouts, retries, and structured result types.

Every external command in agent-runner should go through :func:`run` so we
get a uniform ``CommandResult`` plus consistent retry + timeout semantics.

Children are started in their own process session so that a timeout can
kill the whole process group — a direct ``proc.kill()`` would orphan
grandchildren (e.g. an AI agent launched by gtr's bash subshells), which
then keeps producing side effects after we report the stage as failed.
"""

from __future__ import annotations

import contextlib
import os
import random
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .logging_utils import get_logger

log = get_logger("process")

# Exit codes that are worth retrying (timeout-kill category). Notably NOT 2:
# that is the conventional CLI usage-error exit — retrying it just sleeps and
# fails again. Callers may pass their own set via ``retry_on_exit``.
RETRYABLE_EXIT_CODES: frozenset[int] = frozenset({124, 137})


@dataclass(slots=True)
class CommandResult:
    """Structured result of an external command."""

    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def short_cmd(self) -> str:
        return " ".join(shlex.quote(p) for p in self.cmd[:4]) + (
            " ..." if len(self.cmd) > 4 else ""
        )


def _kill_process_group(proc: subprocess.Popen[Any]) -> None:
    """Kill the child's whole process group, falling back to the child only."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            proc.kill()


def _run_once(
    argv: list[str],
    *,
    cwd: Path | str | None,
    timeout: float | None,
    env: dict[str, str] | None,
    stream: bool,
    attempt: int,
    started: float,
) -> CommandResult:
    """Single attempt of :func:`run` (split out for clarity)."""
    proc = subprocess.Popen(
        argv,
        # stream=True lets the child inherit our stdout/stderr (live output);
        # otherwise capture both so callers can inspect them.
        stdout=None if stream else subprocess.PIPE,
        stderr=None if stream else subprocess.PIPE,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return CommandResult(
            cmd=list(argv),
            returncode=proc.returncode,
            stdout=out or "",
            stderr=err or "",
            duration_s=time.monotonic() - started,
            timed_out=False,
            attempts=attempt,
        )
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        out, err = proc.communicate()  # reap; collect whatever output remains
        result = CommandResult(
            cmd=list(argv),
            returncode=124,
            stdout=out or "",
            stderr=err or "",
            duration_s=time.monotonic() - started,
            timed_out=True,
            attempts=attempt,
        )
        log.warning("command timed out: %s (timeout=%ss)", result.short_cmd(), timeout)
        return result
    except BaseException:
        _kill_process_group(proc)
        with contextlib.suppress(OSError):
            proc.communicate()
        raise


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    retries: int = 0,
    backoff: float = 1.5,
    retry_on_exit: frozenset[int] | None = RETRYABLE_EXIT_CODES,
    retry_on_timeout: bool = True,
    base_delay: float = 1.0,
    check: bool = False,
    stream: bool = False,
) -> CommandResult:
    """Run ``cmd`` with timeout + retry support.

    Args:
        cmd: argv list (never pass a shell string).
        cwd: working directory for the command.
        timeout: per-attempt timeout in seconds; ``None`` waits forever. On
            timeout the child's entire process group is SIGKILLed, so
            grandchildren (agents, hooks) cannot outlive the attempt.
        retries: additional attempts after the first failure (0 = try once).
        backoff: multiplier on ``base_delay`` per attempt (cap 60s, ±20%
            jitter so concurrent callers don't stampede).
        retry_on_exit: exit codes that trigger a retry; ``None`` disables.
        retry_on_timeout: whether timeouts retry. Pass False for commands
            with non-idempotent side effects (e.g. pr-agent posting PR
            comments — a timed-out run may already have posted).
        base_delay: seconds to sleep before the first retry.
        check: if True, raise ``RuntimeError`` on final failure.
        stream: if True, the child inherits our stdout/stderr (live output)
            and ``result.stdout``/``result.stderr`` come back empty.
    """
    argv = [str(c) for c in cmd]
    last: CommandResult | None = None
    current_timeout = timeout
    attempts = retries + 1

    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        result = _run_once(
            argv,
            cwd=cwd,
            timeout=current_timeout,
            env=env,
            stream=stream,
            attempt=attempt,
            started=started,
        )
        last = result

        if result.ok:
            return result

        retriable_exit = retry_on_exit is not None and result.returncode in retry_on_exit
        retriable = (result.timed_out and retry_on_timeout) or (
            not result.timed_out and retriable_exit
        )
        if attempt >= attempts or not retriable:
            break

        sleep_for = min(60.0, base_delay * backoff ** (attempt - 1)) * random.uniform(0.8, 1.2)
        log.warning(
            "command failed (attempt %d/%d, exit=%d), retrying in %.1fs",
            attempt,
            attempts,
            result.returncode,
            sleep_for,
        )
        time.sleep(sleep_for)

    assert last is not None
    if check and not last.ok:
        raise RuntimeError(
            f"command failed after {last.attempts} attempts: {last.short_cmd()}\n"
            f"exit={last.returncode} stderr={last.stderr.strip()[:500]}"
        )
    return last
