"""Pipeline-run history (append-only JSONL with rotation + pruning).

Each ``run_pipeline`` invocation writes a start + end record plus one
record per stage. Records carry a ``run_id`` (UUID4) so a single run can be
reconstructed from the log file.

The store enforces two soft limits so long-running installs don't grow
without bound:

- **Size-based rotation** — when ``runs.jsonl`` exceeds ``max_bytes`` it's
  rotated to ``runs.jsonl.1``, ``.2``, etc. up to ``backup_count`` total files.
- **Age-based pruning** — ``prune_older_than(days=N)`` drops any records
  whose ``run_start`` is older than N days. Stage events for pruned runs
  are dropped alongside them so we never leave orphan records.

Both limits are opt-in. The defaults (no rotation, no pruning) preserve
the original behaviour.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .logging_utils import get_logger

log = get_logger("runs")

# Conservative defaults — opt-in, not aggressive.
DEFAULT_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
DEFAULT_BACKUP_COUNT: int = 3


def default_history_path() -> Path:
    raw = os.environ.get("AGENT_RUNNER_HISTORY")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cache" / "git-agent-runner" / "runs.jsonl"


@dataclass(slots=True)
class RunRecord:
    """One entry in the pipeline-run history."""

    run_id: str
    ts: float
    event: str
    task: str = ""
    repo: str = ""
    agent: str = ""
    branch: str = ""
    worktree: str = ""
    pr_url: str = ""
    duration_s: float = 0.0
    error: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        d = asdict(self)
        # Keep the on-disk line tidy: drop empty fields.
        d = {k: v for k, v in d.items() if v not in ("", {}, None)}
        return json.dumps(d, default=str, sort_keys=False)


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(slots=True)
class HistoryLimits:
    """Size + age limits for the history store.

    Set ``max_bytes`` to 0 to disable rotation; set ``max_age_days`` to 0
    to disable age-based pruning.
    """

    max_bytes: int = 0
    backup_count: int = DEFAULT_BACKUP_COUNT
    max_age_days: int = 0


class RunHistory:
    """Append-only JSONL store with read-back support.

    Args:
        path: location of ``runs.jsonl``. Defaults to
            ``~/.cache/git-agent-runner/runs.jsonl``.
        limits: optional :class:`HistoryLimits`. Defaults to no rotation
            and no pruning (original behaviour).
    """

    def __init__(
        self,
        path: Path | None = None,
        limits: HistoryLimits | None = None,
    ) -> None:
        self.path = path or default_history_path()
        self.limits = limits or HistoryLimits()

    def append(self, record: RunRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Rotate BEFORE writing if we'd exceed the size cap.
        if self.limits.max_bytes > 0 and self.path.exists():
            projected = self.path.stat().st_size + len(record.to_jsonl()) + 1
            if projected > self.limits.max_bytes:
                self._rotate()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(record.to_jsonl() + "\n")
        log.debug("history append: %s event=%s", record.run_id, record.event)

    def iter_all(self) -> Iterator[RunRecord]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("history: skipping malformed line")
                    continue
                extra = data.pop("extra", {})
                try:
                    yield RunRecord(**data, extra=extra)
                except TypeError:
                    log.warning("history: skipping record with unknown fields")

    def iter_run(self, run_id: str) -> list[RunRecord]:
        return [r for r in self.iter_all() if r.run_id == run_id]

    def latest(self, n: int = 10) -> list[RunRecord]:
        """Return the ``n`` most recent *start* events, newest first."""
        starts = [r for r in self.iter_all() if r.event == "run_start"]
        return list(reversed(starts[-n:]))

    # ------------------------------------------------------------------
    # Maintenance: rotation + pruning + compaction.
    # ------------------------------------------------------------------

    def _rotate(self) -> None:
        """Rotate ``runs.jsonl`` → ``.1`` → ``.2`` … → ``.backup_count``.

        Oldest backup is deleted. No-op if the file doesn't exist.
        """
        if not self.path.exists():
            return
        suffix = self.path.suffix
        for i in range(self.limits.backup_count - 1, 0, -1):
            src = self.path.with_suffix(suffix + f".{i}")
            dst = self.path.with_suffix(suffix + f".{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.rename(dst)
        first_backup = self.path.with_suffix(suffix + ".1")
        if first_backup.exists():
            first_backup.unlink()
        try:
            self.path.rename(first_backup)
        except OSError as e:
            log.warning("history rotate failed: %s", e)
            return
        log.info(
            "history rotated: %s -> %s (max_bytes=%d)",
            self.path.name,
            first_backup.name,
            self.limits.max_bytes,
        )

    def prune_older_than(self, days: int) -> int:
        """Remove records older than ``days`` days, grouped by run_id.

        A run is "old" if its ``run_start`` event is older than the cutoff.
        All events for old runs are dropped together so we never leave
        orphan stage records behind.

        Returns the number of records removed.
        """
        if days <= 0 or not self.path.exists():
            return 0
        cutoff = time.time() - (days * 86_400)
        old_run_ids: set[str] = set()
        for r in self.iter_all():
            if r.event == "run_start" and r.ts < cutoff:
                old_run_ids.add(r.run_id)
        if not old_run_ids:
            return 0
        kept: list[str] = []
        removed = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("run_id") in old_run_ids:
                    removed += 1
                    continue
                kept.append(line)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(kept) + ("\n" if kept else ""))
        try:
            os.replace(tmp, self.path)
        except OSError:
            shutil.copyfile(tmp, self.path)
            tmp.unlink()
        log.info("history pruned: %d records older than %d days", removed, days)
        return removed

    def compact(self, keep_recent: int = 5) -> int:
        """Collapse old runs into a single ``run_end`` record each.

        The most recent ``keep_recent`` runs keep all their stage events
        (for live debugging). Older runs are reduced to just their
        ``run_end`` summary so the JSONL stays small.

        Returns the number of stage records merged away.
        """
        runs: dict[str, list[RunRecord]] = {}
        for r in self.iter_all():
            runs.setdefault(r.run_id, []).append(r)
        run_ids_in_order = list(runs.keys())
        recent_ids = set(run_ids_in_order[-keep_recent:])
        new_lines: list[str] = []
        removed = 0
        for run_id, records in runs.items():
            end = next((r for r in records if r.event == "run_end"), None)
            if end is None or run_id in recent_ids:
                for r in records:
                    new_lines.append(r.to_jsonl())
                continue
            new_lines.append(end.to_jsonl())
            removed += len(records) - 1
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write("\n".join(new_lines) + ("\n" if new_lines else ""))
        try:
            os.replace(tmp, self.path)
        except OSError:
            shutil.copyfile(tmp, self.path)
            tmp.unlink()
        log.info("history compacted: %d stage records merged", removed)
        return removed
