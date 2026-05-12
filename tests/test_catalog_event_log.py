# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for nexus.catalog.event_log: append-only JSONL writer.

Coverage maps to RDR-101 §"Event log" + §Phase 1:
- ``append`` writes one envelope per line and survives ``replay``
- ``append_many`` writes a batch atomically (single flock acquisition)
- ``replay`` skips malformed lines with a warning, doesn't crash
- The file is touched on first construction (fresh-catalog case)
- Concurrent writers from threads don't tear lines (locking smoke test)
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from nexus.catalog import events as ev
from nexus.catalog.event_log import EVENTS_FILENAME, EventLog


@pytest.fixture
def event_log_dir(tmp_path) -> Path:
    """Catalog directory with no JSONL files yet."""
    d = tmp_path / "catalog"
    d.mkdir()
    return d


@pytest.fixture
def event_log(event_log_dir) -> EventLog:
    return EventLog(event_log_dir)


class TestConstruction:
    def test_creates_empty_log_on_first_use(self, event_log_dir):
        EventLog(event_log_dir)
        assert (event_log_dir / EVENTS_FILENAME).exists()
        assert (event_log_dir / EVENTS_FILENAME).read_text() == ""

    def test_does_not_truncate_existing_log(self, event_log_dir):
        path = event_log_dir / EVENTS_FILENAME
        path.write_text('{"type":"x","v":1,"payload":{},"ts":"now"}\n')
        EventLog(event_log_dir)
        assert path.read_text() == '{"type":"x","v":1,"payload":{},"ts":"now"}\n'

    def test_path_property(self, event_log):
        assert event_log.path.name == EVENTS_FILENAME


class TestAppend:
    def test_writes_one_line_per_event(self, event_log):
        e1 = ev.make_event(ev.DocumentDeletedPayload(doc_id="a", reason="r1"))
        e2 = ev.make_event(ev.DocumentDeletedPayload(doc_id="b", reason="r2"))
        event_log.append(e1)
        event_log.append(e2)
        lines = event_log.path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["payload"]["doc_id"] == "a"
        assert json.loads(lines[1])["payload"]["doc_id"] == "b"

    def test_append_then_replay_is_identity(self, event_log):
        original = [
            ev.make_event(ev.OwnerRegisteredPayload(
                owner_id="1.1", name="x", owner_type="repo", repo_root="/x", repo_hash="h",
            )),
            ev.make_event(ev.DocumentRegisteredPayload(
                doc_id="d1", owner_id="1.1", content_type="code",
                source_uri="file:///x", coll_id="c1",
            )),
            ev.make_event(ev.ChunkIndexedPayload(
                chunk_id="ch1", chash="x" * 64, doc_id="d1",
                coll_id="c1", position=0,
            )),
        ]
        for e in original:
            event_log.append(e)

        replayed = list(event_log.replay())
        assert len(replayed) == 3
        for o, r in zip(original, replayed):
            assert o.type == r.type
            assert o.v == r.v
            assert o.ts == r.ts
            assert o.payload == r.payload

    def test_append_persists_v_zero(self, event_log):
        # Synthesized events use v: 0 per RF-101-2.
        e = ev.make_event(
            ev.DocumentDeletedPayload(doc_id="x", reason="synthesized_from_tombstone"),
            v=0,
        )
        event_log.append(e)
        replayed = list(event_log.replay())
        assert len(replayed) == 1
        assert replayed[0].v == 0


class TestAppendMany:
    def test_writes_batch(self, event_log):
        events = [
            ev.make_event(ev.DocumentRegisteredPayload(
                doc_id=f"d{i}", owner_id="1.1", content_type="code",
                source_uri=f"file:///f{i}", coll_id="c1",
            ))
            for i in range(5)
        ]
        event_log.append_many(events)

        replayed = list(event_log.replay())
        assert len(replayed) == 5
        for i, r in enumerate(replayed):
            assert r.payload.doc_id == f"d{i}"

    def test_empty_batch_is_noop(self, event_log):
        event_log.append_many([])
        assert event_log.path.read_text() == ""

    def test_batch_after_singletons(self, event_log):
        event_log.append(ev.make_event(
            ev.DocumentDeletedPayload(doc_id="a", reason="x")
        ))
        event_log.append_many([
            ev.make_event(ev.DocumentDeletedPayload(doc_id="b", reason="y")),
            ev.make_event(ev.DocumentDeletedPayload(doc_id="c", reason="z")),
        ])
        replayed = list(event_log.replay())
        assert [e.payload.doc_id for e in replayed] == ["a", "b", "c"]


class TestAppendUnlocked:
    """nexus-lrhg (RDR-108 audit finding 5): unlocked variants exist
    for callers that already hold the catalog directory flock. The
    write path matches the locked variants byte-for-byte; only the
    flock acquisition is skipped."""

    def test_append_unlocked_writes_one_line(self, event_log):
        e = ev.make_event(ev.DocumentDeletedPayload(doc_id="x", reason="t"))
        event_log.append_unlocked(e)
        replayed = list(event_log.replay())
        assert len(replayed) == 1
        assert replayed[0].payload.doc_id == "x"

    def test_append_many_unlocked_writes_batch(self, event_log):
        events = [
            ev.make_event(ev.DocumentDeletedPayload(doc_id=f"d{i}", reason="t"))
            for i in range(3)
        ]
        event_log.append_many_unlocked(events)
        replayed = list(event_log.replay())
        assert [r.payload.doc_id for r in replayed] == ["d0", "d1", "d2"]

    def test_append_many_unlocked_empty_is_noop(self, event_log):
        event_log.append_many_unlocked([])
        assert event_log.path.read_text() == ""

    def test_unlocked_and_locked_interleave_correctly(self, event_log):
        """A caller holding the dir flock can mix unlocked writes
        with subsequent locked writes (after releasing) — the file
        is append-only and replay sees all events in order."""
        event_log.append_unlocked(
            ev.make_event(ev.DocumentDeletedPayload(doc_id="u1", reason="t"))
        )
        event_log.append(
            ev.make_event(ev.DocumentDeletedPayload(doc_id="l1", reason="t"))
        )
        replayed = list(event_log.replay())
        assert [r.payload.doc_id for r in replayed] == ["u1", "l1"]


class TestReplay:
    def test_empty_log_yields_nothing(self, event_log):
        assert list(event_log.replay()) == []

    def test_skips_malformed_lines(self, event_log, caplog):
        path = event_log.path
        path.write_text(
            "{not json}\n"
            '{"type":"DocumentDeleted","v":1,"payload":{"doc_id":"x","reason":"r"},"ts":"t"}\n'
            "\n"
            '{"v":1,"payload":{},"ts":"t"}\n'  # missing "type"
        )
        replayed = list(event_log.replay())
        # Two bad lines + one blank skipped; one good line yielded
        assert len(replayed) == 1
        assert replayed[0].type == ev.TYPE_DOCUMENT_DELETED
        assert replayed[0].payload.doc_id == "x"

    def test_unknown_type_is_yielded_as_dict_payload(self, event_log):
        # The projector relies on this: it dispatches on (type, v) and emits
        # the RF-101-2 unknown-(type, v) warning rather than crashing.
        path = event_log.path
        path.write_text(
            '{"type":"FutureEvent","v":1,"payload":{"a":1},"ts":"t"}\n'
        )
        replayed = list(event_log.replay())
        assert len(replayed) == 1
        assert replayed[0].type == "FutureEvent"
        assert replayed[0].payload == {"a": 1}


class TestConcurrency:
    """Locking smoke test: parallel appends from threads must not tear lines.

    Real concurrency contention happens across processes, not threads,
    but a thread test catches the obvious failure modes (interleaved
    writes producing partial JSON) at low cost.
    """

    def test_threaded_appends_do_not_tear(self, event_log):
        n_threads = 8
        events_per_thread = 25
        errors: list[BaseException] = []

        def writer(thread_id: int) -> None:
            try:
                for i in range(events_per_thread):
                    e = ev.make_event(ev.DocumentDeletedPayload(
                        doc_id=f"t{thread_id}_e{i}",
                        reason="parallel-append-test",
                    ))
                    event_log.append(e)
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(i,))
            for i in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Threaded appends raised: {errors}"

        replayed = list(event_log.replay())
        assert len(replayed) == n_threads * events_per_thread

        # Every line is valid JSON (would crash replay if torn)
        for line in event_log.path.read_text().strip().split("\n"):
            assert line.startswith("{")
            assert line.endswith("}")
            json.loads(line)


class TestTruncate:
    def test_truncate_clears_log(self, event_log):
        event_log.append(ev.make_event(
            ev.DocumentDeletedPayload(doc_id="x", reason="r")
        ))
        assert event_log.path.read_text() != ""
        event_log.truncate()
        assert event_log.path.read_text() == ""
        assert list(event_log.replay()) == []


class TestReplayFrom:
    """RDR-104 Step 1: ``EventLog.replay_from(offset, *, limit_offset)``.

    Streams events whose start-of-line byte offset is in the half-open
    range ``[offset, limit_offset)`` (or to EOF when ``limit_offset is
    None``). Binary-mode file open + ``seek(offset)`` so byte positions
    are portable across platforms (text-mode ``tell()`` returns an
    opaque cookie on Windows under universal-newline translation).

    The bounded form is mandatory for concurrent-appender safety in
    ``Catalog._ensure_consistent``'s incremental path: a writer landing
    between the orchestrator's ``stat()`` snapshot and the iterator's
    read window must not extend the iterator past the captured offset,
    or the marker the orchestrator persists drifts below the true tail.
    """

    @staticmethod
    def _seed(event_log: EventLog, count: int) -> list[ev.Event]:
        seeded = [
            ev.make_event(ev.DocumentDeletedPayload(
                doc_id=f"doc-{i:03d}", reason="seed",
            ))
            for i in range(count)
        ]
        for e in seeded:
            event_log.append(e)
        return seeded

    def test_replay_from_zero_unbounded_equals_replay(self, event_log):
        seeded = self._seed(event_log, 5)
        full = list(event_log.replay())
        partial = list(event_log.replay_from(0))
        assert len(partial) == len(full) == len(seeded)
        for a, b in zip(full, partial):
            assert a.type == b.type
            assert a.payload == b.payload

    def test_replay_from_zero_bounded_at_eof_equals_replay(self, event_log):
        self._seed(event_log, 5)
        eof = event_log.path.stat().st_size
        full = list(event_log.replay())
        partial = list(event_log.replay_from(0, limit_offset=eof))
        assert len(partial) == len(full)
        for a, b in zip(full, partial):
            assert a.payload == b.payload

    def test_replay_from_eof_yields_zero_events(self, event_log):
        self._seed(event_log, 3)
        eof = event_log.path.stat().st_size
        assert list(event_log.replay_from(eof)) == []
        assert list(event_log.replay_from(eof, limit_offset=eof)) == []

    def test_replay_from_offset_greater_than_file_size_raises(self, event_log):
        self._seed(event_log, 2)
        eof = event_log.path.stat().st_size
        with pytest.raises(ValueError, match="exceeds file size"):
            list(event_log.replay_from(eof + 1))

    def test_replay_from_empty_file_returns_empty_iterator(self, event_log):
        # Fresh log; nothing appended.
        assert list(event_log.replay_from(0)) == []
        assert list(event_log.replay_from(0, limit_offset=0)) == []

    def test_replay_from_starts_at_event_boundary(self, event_log):
        """Capturing ``f.tell()`` after writing event N yields events N+1..end.

        Round 3 Observation: tests use ``f.tell()`` AFTER a line write
        to capture the half-open boundary value, not a computed sum.
        """
        seeded = self._seed(event_log, 5)
        # Capture the byte offset at the start of event index 2.
        with event_log.path.open("rb") as f:
            f.readline()
            f.readline()
            offset_at_event_2 = f.tell()
        replayed = list(event_log.replay_from(offset_at_event_2))
        assert len(replayed) == len(seeded) - 2
        assert [e.payload.doc_id for e in replayed] == [
            "doc-002", "doc-003", "doc-004",
        ]

    def test_replay_from_bounded_caps_iterator_at_limit(self, event_log):
        """The bounded form stops at limit_offset regardless of live EOF.

        Round 2 Critical #1: simulates the concurrent-appender race.
        Setup: log with N events; capture limit at end of event N-1.
        Append M more events (live EOF advances). Call replay_from(0,
        limit_offset=captured_limit). Iterator must yield exactly events
        0..N-1, none of the appended-after-snapshot tail.
        """
        first_batch = self._seed(event_log, 4)
        # Capture limit AFTER the 4 events. f.tell() at EOF after a
        # readline loop equals the byte position right after the 4th
        # event's '\n' — the start of where event 4 (0-indexed) WOULD
        # begin. Half-open [0, limit) covers events 0..3.
        with event_log.path.open("rb") as f:
            for _ in range(4):
                f.readline()
            captured_limit = f.tell()

        # Concurrent appender lands more events.
        self._seed(event_log, 3)
        live_eof = event_log.path.stat().st_size
        assert live_eof > captured_limit, (
            "appender must extend the file past the captured limit"
        )

        bounded = list(event_log.replay_from(0, limit_offset=captured_limit))
        assert len(bounded) == 4
        assert [e.payload.doc_id for e in bounded] == [
            seeded.payload.doc_id for seeded in first_batch
        ]

    def test_replay_from_bounded_with_limit_at_event_boundary(self, event_log):
        """Half-open semantic: a line whose start-offset == limit is excluded."""
        self._seed(event_log, 5)
        with event_log.path.open("rb") as f:
            f.readline()
            f.readline()
            f.readline()
            limit = f.tell()  # start of event 3
        bounded = list(event_log.replay_from(0, limit_offset=limit))
        assert [e.payload.doc_id for e in bounded] == [
            "doc-000", "doc-001", "doc-002",
        ]

    def test_replay_from_mid_line_offset_warns_and_skips(self, event_log, caplog):
        """Mid-line offset → warn-and-skip per existing replay() pattern.

        Round 2 Significant #3: aligns with replay()'s warn-and-skip
        rather than raising. The orchestrator detects corruption at the
        caller layer (zero events from a non-empty range) and escalates.
        """
        self._seed(event_log, 3)
        with event_log.path.open("rb") as f:
            f.readline()
            line_start = f.tell()
            second_line = f.readline()
        # Land in the middle of the second line.
        mid_line_offset = line_start + max(1, len(second_line) // 2)
        with caplog.at_level("WARNING"):
            replayed = list(event_log.replay_from(mid_line_offset))
        # The first read-from-mid-line is a partial line that fails to
        # parse and is skipped. Remaining well-formed lines yield.
        assert len(replayed) == 1
        assert replayed[0].payload.doc_id == "doc-002"

    def test_replay_from_unbounded_after_seek(self, event_log):
        """Unbounded form preserves 'everything from here' for non-orchestrator callers."""
        self._seed(event_log, 6)
        with event_log.path.open("rb") as f:
            f.readline()
            offset = f.tell()
        replayed = list(event_log.replay_from(offset))
        assert len(replayed) == 5
        assert [e.payload.doc_id for e in replayed] == [
            "doc-001", "doc-002", "doc-003", "doc-004", "doc-005",
        ]
