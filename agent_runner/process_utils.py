"""Subprocess helpers — timeouts, retries, and structured result types.

Every external command in agent-runner should go through :func:`run` so we
get a uniform ``CommandResult`` plus consistent retry + timeout semantics.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .logging_utils import get_logger

log = get_logger("process")

# Exit codes that are worth retrying (network-flake category).
RETRYABLE_EXIT_CODES: frozenset[int] = frozenset({2, 124, 137})


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


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    retries: int = 0,
    backoff: float = 1.5,
    retry_on_exit: frozenset[int] | None = RETRYABLE_EXIT_CODES,
    check: bool = False,
) -> CommandResult:
    """Run ``cmd`` with timeout + retry support.

    Args:
        cmd: argv list (never pass a shell string).
        cwd: working directory for the command.
        timeout: per-attempt timeout in seconds; ``None`` waits forever.
        retries: additional attempts after the first failure (0 = try once).
        backoff: multiplier applied to ``timeout`` between retries (cap 60s).
        retry_on_exit: exit codes that trigger a retry; ``None`` disables.
        check: if True, raise ``RuntimeError`` on final failure.
    """
    argv = [str(c) for c in cmd]
    last: CommandResult | None = None
    current_timeout = timeout
    attempts = retries + 1

    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                cwd=str(cwd) if cwd is not None else None,
                env=env,
                timeout=current_timeout,
            )
            duration = time.monotonic() - started
            result = CommandResult(
                cmd=list(argv),
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
                duration_s=duration,
                timed_out=False,
                attempts=attempt,
            )
        except subprocess.TimeoutExpired as e:
            duration = time.monotonic() - started
            result = CommandResult(
                cmd=list(argv),
                returncode=124,
                stdout=(e.stdout or b"").decode()
                if isinstance(e.stdout, bytes)
                else (e.stdout or ""),
                stderr=(e.stderr or b"").decode()
                if isinstance(e.stderr, bytes)
                else (e.stderr or ""),
                duration_s=duration,
                timed_out=True,
                attempts=attempt,
            )
            log.warning("command timed out: %s (timeout=%ss)", result.short_cmd(), current_timeout)

        last = result

        if result.ok:
            return result

        should_retry = (
            attempt < attempts
            and retry_on_exit is not None
            and (result.timed_out or result.returncode in retry_on_exit)
        )
        if not should_retry:
            break

        sleep_for = min(60.0, current_timeout * backoff if current_timeout else backoff**attempt)
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
