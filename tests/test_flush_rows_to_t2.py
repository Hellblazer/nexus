# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mtheb.9: the time-ordered rule of ``mcp_infra.flush_rows_to_t2``.

A flagged T1 row is written to T2 when no entry exists or the row is newer than
the entry's last update; an entry updated at or after the row's second is kept
(a user edit). Pure unit tests over a fake T2 writer: the timestamps are chosen,
not read from a clock, so every case is deterministic. The engine-backed
counterparts (real T1 ``ts`` and T2 ``timestamp`` values) live in
``tests/mcp/test_t1_handoff_flush.py``.
"""
from __future__ import annotations

from nexus.mcp_infra import FlushCounts, flush_rows_to_t2


class _Memory:
    def __init__(self, entries: dict[tuple[str, str], dict]) -> None:
        self.entries = entries
        self.puts: list[dict] = []

    def get(self, *, project: str, title: str):
        return self.entries.get((project, title))

    def put(self, **kw) -> int:
        self.puts.append(kw)
        self.entries[(kw["project"], kw["title"])] = {"content": kw["content"], "timestamp": "9999-01-01T00:00:00Z"}
        return 1


class _Db:
    def __init__(self, entries: dict[tuple[str, str], dict] | None = None) -> None:
        self.memory = _Memory(entries or {})


class _Log:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        return lambda event, **kw: self.events.append((level, event, kw))

    def of(self, event: str) -> list[tuple[str, dict]]:
        return [(lvl, kw) for lvl, e, kw in self.events if e == event]


def _row(content: str, ts: str, *, project: str = "p", title: str = "t", rid: str = "r") -> dict:
    return {
        "id": rid, "content": content, "ts": ts, "tags": "",
        "flush_project": project, "flush_title": title,
    }


def _entry(content: str, timestamp: str) -> dict:
    return {"content": content, "timestamp": timestamp}


def test_an_absent_entry_is_created() -> None:
    db, log = _Db(), _Log()
    counts = flush_rows_to_t2(db, [_row("row", "2026-11-01T12:00:00.5Z")], op="x", log=log)
    assert counts == FlushCounts(1, 0)
    assert [p["content"] for p in db.memory.puts] == ["row"]


def test_an_entry_updated_after_the_row_was_written_is_kept_and_warned() -> None:
    db, log = _Db({("p", "t"): _entry("edited", "2026-11-01T12:05:00Z")}), _Log()
    counts = flush_rows_to_t2(db, [_row("row", "2026-11-01T12:00:00.5Z")], op="t1_handoff_flush", log=log)
    assert counts == FlushCounts(0, 1)
    assert db.memory.puts == []
    (lvl, kw), = log.of("t1_flush_kept_existing_t2_entry")
    assert lvl == "warning" and kw["op"] == "t1_handoff_flush"


def test_a_row_newer_than_the_entry_replaces_it() -> None:
    db, log = _Db({("p", "t"): _entry("old session's note", "2026-11-01T09:00:00Z")}), _Log()
    counts = flush_rows_to_t2(db, [_row("new session's row", "2026-11-01T12:00:00.5Z")], op="x", log=log)
    assert counts == FlushCounts(1, 0)
    assert [p["content"] for p in db.memory.puts] == ["new session's row"]
    assert log.of("t1_flush_kept_existing_t2_entry") == []


def test_an_entry_in_the_rows_own_second_is_kept() -> None:
    """T2 stamps whole seconds, so a row put at :00.9 and an entry stamped :00
    are the same second: the entry is not older than the row."""
    db, log = _Db({("p", "t"): _entry("other", "2026-11-01T12:00:00Z")}), _Log()
    counts = flush_rows_to_t2(db, [_row("row", "2026-11-01T12:00:00.9Z")], op="x", log=log)
    assert counts == FlushCounts(0, 1) and db.memory.puts == []


def test_an_identical_entry_is_not_rewritten_and_counts_even_when_the_row_is_newer() -> None:
    db, log = _Db({("p", "t"): _entry("same", "2026-11-01T09:00:00Z")}), _Log()
    counts = flush_rows_to_t2(db, [_row("same", "2026-11-01T12:00:00Z")], op="x", log=log)
    assert counts == FlushCounts(1, 0) and db.memory.puts == []


def test_a_timestamp_that_cannot_be_read_keeps_the_existing_entry() -> None:
    for row_ts, entry_ts in (("", "2026-11-01T09:00:00Z"), ("garbage", "2026-11-01T09:00:00Z"),
                             ("2026-11-01T12:00:00Z", None), ("2026-11-01T12:00:00Z", "yesterday")):
        db, log = _Db({("p", "t"): {"content": "mine", "timestamp": entry_ts}}), _Log()
        counts = flush_rows_to_t2(db, [_row("row", row_ts)], op="x", log=log)
        assert counts == FlushCounts(0, 1) and db.memory.puts == [], (row_ts, entry_ts)


def test_two_rows_to_one_destination_the_newest_wins_whatever_the_order() -> None:
    older = _row("older", "2026-11-01T12:00:00.100000Z", rid="a")
    newer = _row("newer", "2026-11-01T12:00:00.900000Z", rid="b")  # the same second
    for rows in ([older, newer], [newer, older]):
        db, log = _Db(), _Log()
        counts = flush_rows_to_t2(db, rows, op="x", log=log)
        assert counts == FlushCounts(1, 0)
        assert [p["content"] for p in db.memory.puts] == ["newer"]
        (lvl, kw), = log.of("t1_flush_superseded_row")
        assert kw["row_id"] == "a" and kw["op"] == "x"
        assert log.of("t1_flush_kept_existing_t2_entry") == []  # the older row is not "kept"


def test_the_newest_of_two_rows_is_still_held_against_an_edited_entry() -> None:
    db, log = _Db({("p", "t"): _entry("edited", "2026-11-01T13:00:00Z")}), _Log()
    rows = [_row("older", "2026-11-01T12:00:00Z", rid="a"), _row("newer", "2026-11-01T12:30:00Z", rid="b")]
    assert flush_rows_to_t2(db, rows, op="x", log=log) == FlushCounts(0, 1)
    assert db.memory.puts == []
