"""Tests for run history."""

from __future__ import annotations

import time
from pathlib import Path

from agent_runner.runs import (
    DEFAULT_BACKUP_COUNT,
    HistoryLimits,
    RunHistory,
    RunRecord,
    new_run_id,
)


def test_append_and_iter(tmp_path: Path) -> None:
    p = tmp_path / "runs.jsonl"
    h = RunHistory(path=p)
    h.append(RunRecord(run_id="a", ts=1.0, event="run_start", task="t"))
    h.append(RunRecord(run_id="a", ts=2.0, event="run_end", task="t"))

    records = list(h.iter_all())
    assert len(records) == 2
    assert records[0].event == "run_start"
    assert records[1].event == "run_end"


def test_iter_run_filters_by_id(tmp_path: Path) -> None:
    p = tmp_path / "runs.jsonl"
    h = RunHistory(path=p)
    h.append(RunRecord(run_id="a", ts=1.0, event="run_start"))
    h.append(RunRecord(run_id="b", ts=2.0, event="run_start"))
    h.append(RunRecord(run_id="a", ts=3.0, event="run_end"))

    a_records = h.iter_run("a")
    assert len(a_records) == 2
    assert all(r.run_id == "a" for r in a_records)


def test_latest_returns_recent_starts(tmp_path: Path) -> None:
    p = tmp_path / "runs.jsonl"
    h = RunHistory(path=p)
    for i in range(5):
        h.append(RunRecord(run_id=f"r{i}", ts=float(i), event="run_start"))
    h.append(RunRecord(run_id="r99", ts=999.0, event="run_end"))  # not a start

    latest = h.latest(3)
    # newest first (most recent run_start)
    assert [r.run_id for r in latest] == ["r4", "r3", "r2"]


def test_iter_all_skips_malformed(tmp_path: Path) -> None:
    p = tmp_path / "runs.jsonl"
    h = RunHistory(path=p)
    h.append(RunRecord(run_id="ok", ts=1.0, event="run_start"))
    p.write_text(p.read_text() + "this is not json\n")
    p.write_text(p.read_text() + '{"run_id":"x","ts":2.0,"event":"e","unknown_field":"junk"}\n')

    records = list(h.iter_all())
    # "x" gets skipped because of unknown_field, but "ok" survives
    # and the malformed line is skipped too.
    ids = {r.run_id for r in records}
    assert "ok" in ids


def test_new_run_id_is_short_hex() -> None:
    rid = new_run_id()
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)


class TestRotation:
    def test_no_rotation_when_disabled(self, tmp_path: Path) -> None:
        p = tmp_path / "runs.jsonl"
        h = RunHistory(path=p)  # limits default to 0
        for i in range(100):
            h.append(RunRecord(run_id=f"r{i}", ts=float(i), event="run_start"))
        # No rotation → single file, 100 lines.
        assert len(list(h.iter_all())) == 100
        assert not p.with_suffix(".jsonl.1").exists()

    def test_rotates_when_size_exceeded(self, tmp_path: Path) -> None:
        p = tmp_path / "runs.jsonl"
        h = RunHistory(path=p, limits=HistoryLimits(max_bytes=500, backup_count=2))
        # Each record is ~120 bytes; this should trigger at least one rotation.
        for i in range(20):
            h.append(RunRecord(run_id=f"rid-{i:04d}", ts=float(i), event="run_start"))
        assert p.with_suffix(".jsonl.1").exists()

    def test_oldest_backup_dropped(self, tmp_path: Path) -> None:
        p = tmp_path / "runs.jsonl"
        h = RunHistory(path=p, limits=HistoryLimits(max_bytes=200, backup_count=2))
        # Force many rotations by appending in chunks that exceed the cap.
        for i in range(50):
            h.append(RunRecord(run_id=f"r{i:04d}", ts=float(i), event="run_start"))
        # We should never have more than backup_count backups.
        for i in range(1, 10):
            assert not p.with_suffix(f".jsonl.{i}").exists() or i <= DEFAULT_BACKUP_COUNT


class TestPruneOlderThan:
    def test_prunes_by_run_start_age(self, tmp_path: Path) -> None:
        p = tmp_path / "runs.jsonl"
        h = RunHistory(path=p)
        old_start = time.time() - 30 * 86_400
        new_start = time.time() - 1 * 86_400
        h.append(RunRecord(run_id="old", ts=old_start, event="run_start"))
        h.append(RunRecord(run_id="old", ts=old_start + 1, event="stage_x"))
        h.append(RunRecord(run_id="old", ts=old_start + 2, event="run_end"))
        h.append(RunRecord(run_id="new", ts=new_start, event="run_start"))
        h.append(RunRecord(run_id="new", ts=new_start + 1, event="run_end"))

        removed = h.prune_older_than(days=7)
        assert removed == 3  # all events for the "old" run
        remaining = list(h.iter_all())
        assert len(remaining) == 2
        assert all(r.run_id == "new" for r in remaining)

    def test_zero_days_is_noop(self, tmp_path: Path) -> None:
        p = tmp_path / "runs.jsonl"
        h = RunHistory(path=p)
        h.append(RunRecord(run_id="x", ts=time.time() - 999 * 86_400, event="run_start"))
        assert h.prune_older_than(days=0) == 0
        assert len(list(h.iter_all())) == 1

    def test_no_file_returns_zero(self, tmp_path: Path) -> None:
        h = RunHistory(path=tmp_path / "nope.jsonl")
        assert h.prune_older_than(days=7) == 0


class TestCompact:
    def test_collapses_old_runs(self, tmp_path: Path) -> None:
        p = tmp_path / "runs.jsonl"
        h = RunHistory(path=p)
        # 10 runs, each with 4 events (start + 2 stages + end).
        for i in range(10):
            h.append(RunRecord(run_id=f"r{i}", ts=float(i), event="run_start"))
            h.append(RunRecord(run_id=f"r{i}", ts=float(i) + 0.1, event="stage_x"))
            h.append(RunRecord(run_id=f"r{i}", ts=float(i) + 0.2, event="stage_y"))
            h.append(RunRecord(run_id=f"r{i}", ts=float(i) + 0.3, event="run_end"))
        before = len(list(h.iter_all()))
        assert before == 40

        # keep_recent=3 → 7 old runs x 3 extra events each = 21 records removed.
        removed = h.compact(keep_recent=3)
        assert removed == 21

        after = list(h.iter_all())
        # Recent 3 runs keep all events (12); old 7 runs keep only run_end (7).
        assert len(after) == 19
        old_runs = [r for r in after if r.run_id in {f"r{i}" for i in range(7)}]
        assert all(r.event == "run_end" for r in old_runs)

    def test_no_run_end_keeps_all(self, tmp_path: Path) -> None:
        p = tmp_path / "runs.jsonl"
        h = RunHistory(path=p)
        h.append(RunRecord(run_id="in_flight", ts=1.0, event="run_start"))
        h.append(RunRecord(run_id="in_flight", ts=1.1, event="stage_x"))
        # No run_end → compact must preserve everything.
        h.compact(keep_recent=0)
        assert len(list(h.iter_all())) == 2


def test_missing_file_yields_nothing(tmp_path: Path) -> None:
    h = RunHistory(path=tmp_path / "nope.jsonl")
    assert list(h.iter_all()) == []
    assert h.latest() == []
