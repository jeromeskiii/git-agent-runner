"""Centralized logging for agent-runner.

Provides a single ``get_logger(name)`` factory that emits one-line key=value
records to stderr (human-readable) and, when configured, to a rotating JSONL
file under ``$AGENT_RUNNER_LOG_DIR`` (default ``~/.cache/git-agent-runner/logs``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_DEFAULT_FMT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"
_LOG_DIR: Path | None = None
_INITIALIZED = False


def _log_dir() -> Path:
    global _LOG_DIR
    if _LOG_DIR is None:
        env = os.environ.get("AGENT_RUNNER_LOG_DIR")
        _LOG_DIR = (
            Path(env).expanduser() if env else Path.home() / ".cache" / "git-agent-runner" / "logs"
        )
    return _LOG_DIR


def configure(level: str | int = "INFO", log_file: bool = True) -> None:
    """Configure the root ``agent_runner`` logger.

    Safe to call multiple times — only the first call attaches handlers.
    """
    global _INITIALIZED
    root = logging.getLogger("agent_runner")
    if _INITIALIZED:
        root.setLevel(level)
        return

    root.setLevel(level)
    root.propagate = False

    stream = logging.StreamHandler(stream=sys.stderr)
    stream.setFormatter(logging.Formatter(_DEFAULT_FMT))
    root.addHandler(stream)

    if log_file:
        try:
            d = _log_dir()
            d.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(
                d / "agent-runner.log",
                maxBytes=2_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            fh.setFormatter(logging.Formatter(_DEFAULT_FMT))
            root.addHandler(fh)
        except OSError:
            # Filesystem unavailable — fall back to stderr only.
            pass

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """Return a child logger, lazily configuring the root on first use."""
    configure()
    return logging.getLogger(
        f"agent_runner.{name}" if not name.startswith("agent_runner") else name
    )


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit a structured ``event=... key=value ...`` log line."""
    parts = [f"event={event}"]
    for k, v in fields.items():
        if v is None:
            continue
        parts.append(f"{k}={v!r}" if isinstance(v, str) else f"{k}={v}")
    logger.info(" ".join(parts))


def log_jsonl_event(path: Path, event: str, **fields: Any) -> None:
    """Append a structured JSONL record. Used by the pipeline-run history store."""
    record = {"ts": time.time(), "event": event, **fields}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")
