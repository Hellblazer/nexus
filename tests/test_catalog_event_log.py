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
