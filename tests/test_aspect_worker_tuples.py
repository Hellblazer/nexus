# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-235 client half (nexus-5r2sc.3.3): the aspect worker on the ``aspects`` tuple queue.

Against the real engine substrate, with real tasks on the real queue, a stubbed
extractor (never a model call, never a credential) and the worker's own
``TupleQueueSide`` driving claims, renewals and verdicts. Test Plan scenarios 6
(five claims, five tasks), 7 (renewed inside the deadline, lapsed past it), 10
(409, release, bounded), 14 worker half (an MCP note is a task with no text; the
extractor reads it through the manifest), 15 (more than four collections, one
park), 16 (a deleted document is acked as dropped), gate S3 (T3 unreachable is a
retryable nack, never a drop) and the alternation of the tuple queue with the old
table.

VACUITY (each run red, then restored, recorded in the bead): see the hand-back of
nexus-5r2sc.3.3. In short, removing the per-slot claimant fails scenario 6; the
renewer's loop body fails 7a; the deadline guard fails 7b; the release in the 409
arm fails 10; the infra_unavailable arm fails S3; the missing-document check
fails 16; the table turn fails the alternation test.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.aspect_extractor import AspectRecord, ExtractFail
from nexus.aspect_worker import AspectExtractionWorker
from nexus.db.t2.http_tuple_store import HttpTupleStore, ParkCapExceededError
from tests._catalog_fixture_ops import register_real_doc_id

pytestmark = pytest.mark.usefixtures("t2_service_env")

_SUB = "aspects"
_KEYS = {"queue": "aspects"}
_COLL = "knowledge__delos"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One T2Database per test; the worker's routed writes use it."""
    import nexus.mcp_infra as infra
    from nexus.db.t2 import T2Database

    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.delenv("NX_ASPECT_QUEUE", raising=False)
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    d = T2Database(tmp_path / "t2.db")
    monkeypatch.setattr(infra, "t2_index_write", lambda fn, **kw: fn(d))
    yield d
    d.close()


def _doc(label: str, collection: str = _COLL) -> str:
    return register_real_doc_id(
        title=f"nx235w {label} {uuid.uuid4().hex[:6]}", physical_collection=collection,
    )


def _out(db: Any, doc_id: str, *, collection: str = _COLL, source: str = "/doc.md") -> None:
    db.tuples.out(
        _SUB, dict(_KEYS),
        body=json.dumps({
            "collection": collection, "source_path": source,
            "doc_id": doc_id, "content_hash": "h",
        }),
        nonce=doc_id,
    )


def _tasks(db: Any) -> list:
    return db.tuples.rd(_SUB, dict(_KEYS), n=300)


def _record(collection: str, source_path: str, **kw: Any) -> AspectRecord:
    return AspectRecord(
        collection=collection, source_path=source_path, problem_formulation="P",
        proposed_method="M", experimental_datasets=["d"], experimental_baselines=["b"],
        experimental_results="R", extras={}, confidence=0.9,
        extracted_at="2026-10-29T00:00:00+00:00", model_version="test",
        extractor_name="test", **kw,
    )


@pytest.fixture
def extracted(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Stub the single-document extractor; every call is recorded."""
    calls: list[dict] = []

    def fake(content, source_path, collection, **kw):
        calls.append({"content": content, "source_path": source_path,
                      "collection": collection, **kw})
        return _record(collection, source_path)

    monkeypatch.setattr("nexus.aspect_worker._extract_aspects", fake)
    return calls


class _Events:
    """A stand-in for the module logger: records event names. (``capture_logs``
    cannot see a logger that bound itself on first use.)"""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def __getattr__(self, level: str):
        return lambda event, **kw: self._sink.append(event)


@pytest.fixture
def tuple_events(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr("nexus.aspect_worker_tuples._log", _Events(events))
    return events


def _worker(**opts: Any) -> AspectExtractionWorker:
    return AspectExtractionWorker(poll_interval=0.1, tuple_options=opts or None)


def _wait(pred, timeout: float = 20.0, poll: float = 0.05) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(poll)
    raise AssertionError("condition not reached in time")


class TestScenario6FiveClaims:
    def test_five_slots_return_five_distinct_tasks_and_one_claimant_returns_one(
        self, db: Any,
    ) -> None:
        docs = [_doc(f"s6-{i}") for i in range(5)]
        for d in docs:
            _out(db, d)

        # The control first: ONE claimant claiming five times is its first task
        # five times (F25), which is what the five slots below must not be.
        got = [
            db.tuples.inp(_SUB, dict(_KEYS), claimant="one-claimant", lease_s=600)
            for _ in range(5)
        ]
        assert all(g is not None for g in got)
        assert len({g[0].id for g in got}) == 1, "F25: a repeat claim by one claimant is its held claim"
        assert len({g[1] for g in got}) == 1
        db.tuples.release(got[0][1], "one-claimant")

        side = _worker()._tuple_side
        claims, failed = side.claim_batch(park=False)

        assert not failed
        assert sorted(c.slot for c in claims) == [0, 1, 2, 3, 4]
        assert len({c.task_id for c in claims}) == 5, "five claimants, five distinct tasks"
        assert len({c.claimant for c in claims}) == 5
        assert all(c.claimant == f"{side.worker_id}:{c.slot}" for c in claims)
        assert all(c.row is not None and c.row.doc_id in docs for c in claims)


class TestScenario15ParkCap:
    def test_more_than_four_collections_park_once_under_slot_zero(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for i in range(6):
            _out(db, _doc(f"s15-{i}"), collection=f"knowledge__nx235c{i}-{uuid.uuid4().hex[:4]}")
        # drain them, so the idle batch below is the parked case
        side = _worker(park_timeout_s=1)._tuple_side
        claims, _ = side.claim_batch(park=False)
        for c in claims:
            db.tuples.ack(c.claim_id, c.claimant)
        more, _ = side.claim_batch(park=False)
        for c in more:
            db.tuples.ack(c.claim_id, c.claimant)
        assert _tasks(db) == []

        # Idle: slots 1..4 probe and slot 0 parks, once; several rounds never
        # exceed the engine's per-claimant park cap of 4.
        for _ in range(3):
            got, failed = side.claim_batch(park=True)
            assert got == [] and not failed
        assert db.tuples.park_stats().refused_claimant == 0
        assert db.tuples.park_stats().refused_global == 0


        # A park-cap refusal from the engine is a failed claim, not a crash.
        def refuse(*a: Any, **k: Any):
            raise ParkCapExceededError("cap")

        monkeypatch.setattr(HttpTupleStore, "in_", refuse)
        got, failed = side.claim_batch(park=True)
        assert got == [] and failed, "a park-cap refusal is a failed claim"


class TestScenario16DeletedDocument:
    def test_a_task_for_a_missing_document_is_acked_as_dropped_and_a_present_one_is_extracted(
        self, db: Any, extracted: list[dict], tuple_events: list[str],
    ) -> None:
        """The present document is the control: the same pass extracts it, so the
        missing one's silence is the drop and not a worker that did nothing."""
        present = _doc("s16-present")
        _out(db, "1.99999.99999", source="/gone.md")  # no such catalog document
        _out(db, present, source="/present.md")
        side = _worker()._tuple_side
        claims, _ = side.claim_batch(park=False)
        assert len(claims) == 2

        side.process(claims)

        assert "aspect_task_dropped_missing_doc" in tuple_events, tuple_events
        assert [c["source_path"] for c in extracted] == ["/present.md"], (
            "no extraction for a document that is gone; one for the document that is there"
        )
        assert _tasks(db) == [], "acked as dropped: both tasks are consumed"
        assert db.document_aspects.get_by_doc_id("1.99999.99999") is None
        assert db.document_aspects.get_by_doc_id(present) is not None


class TestGateS3InfraUnavailable:
    def test_each_failure_class_gets_its_own_verdict(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """One pass over four tasks, the extractor failing a different way for
        each (keyed on source path): infra_unavailable and an unreachable engine
        are retryable nacks and never a drop; a permanent read failure is acked
        as dropped; a non-retryable error is dead at once."""
        docs = {name: _doc(f"s3-{name}") for name in ("infra", "conn", "perm", "dead")}
        for name, doc in docs.items():
            _out(db, doc, source=f"/{name}.md")

        def fake(content=None, source_path=None, collection=None, **kw: Any):
            if source_path == "/infra.md":
                return ExtractFail(uri="nx-note://x", reason="infra_unavailable", detail="T3 down")
            if source_path == "/conn.md":
                raise httpx.ConnectError("T3 unreachable")
            if source_path == "/perm.md":
                return ExtractFail(uri="u", reason="unreachable", detail="bad uri")
            raise ValueError("malformed")

        monkeypatch.setattr("nexus.aspect_worker._extract_aspects", fake)
        side = _worker()._tuple_side
        claims, _ = side.claim_batch(park=False)
        assert len(claims) == 4

        for claim in claims:  # one at a time: a lone claim takes the single-document extractor
            side.process([claim])

        rows = {json.loads(r.body)["source_path"]: r for r in _tasks(db)}
        assert sorted(rows) == ["/conn.md", "/dead.md", "/infra.md"], (
            f"only the permanent read failure is consumed: {sorted(rows)}"
        )
        for path in ("/infra.md", "/conn.md"):
            assert rows[path].attempts == 1, f"{path}: the nack counted an attempt"
            assert rows[path].claim_state != "dead", f"{path}: retryable, never dead"
        assert rows["/dead.md"].claim_state == "dead" and rows["/dead.md"].attempts == 1
        for doc in docs.values():
            assert db.document_aspects.get_by_doc_id(doc) is None
        # held back by retry_after_s from the RDR-163 ladder (30 s +- 20 %)
        assert db.tuples.inp(_SUB, dict(_KEYS), claimant="other", lease_s=60) is None


def _note_task(db: Any) -> tuple[str, str, str]:
    """A real MCP note in the catalog and a text-free task for it (scenario 14's shape)."""
    from nexus.catalog.note_write import write_note
    from nexus.catalog.store_hook import catalog_store_hook_tracked, note_manifest_metadata

    collection = f"knowledge__nx235t-{uuid.uuid4().hex[:6]}__bge-base-en-v15-768__v1"
    pieces = ["nx235t alpha paragraph", "nx235t beta paragraph"]
    first, _ = note_manifest_metadata(pieces)
    doc, _created = catalog_store_hook_tracked(title="nx235t note", doc_id=first, collection_name=collection)
    write_note(catalog_doc_id=doc, collection=collection, pieces=pieces)
    _out(db, doc, collection=collection, source=first)
    return collection, doc, first


class _T3Stub:
    """A T3 whose collection answers ``get`` with *behaviour* (raise or return a page)."""

    def __init__(self, behaviour: Any) -> None:
        self.calls = 0
        outer = self

        class _Coll:
            def get(self, **kw: Any) -> dict:
                outer.calls += 1
                return behaviour(**kw)

        self._coll = _Coll()

    def get_collection(self, name: str) -> Any:
        return self._coll


class TestTransportReadFailureIsRetried:
    """nexus-5r2sc.3.10 Critical 1: a transport failure reading T3 or the manifest
    is a fact about the network, not the document. It is retried on the ladder and
    never acked as dropped. The REAL extractor and reader run; only T3 is a stub."""

    def _run(self, db: Any, monkeypatch: pytest.MonkeyPatch, t3: _T3Stub) -> list:
        monkeypatch.setattr("nexus.aspect_extractor.get_t3", lambda: t3)
        side = _worker()._tuple_side
        claims, _ = side.claim_batch(park=False)
        assert len(claims) == 1
        side.process(claims)
        return _tasks(db)

    def test_a_t3_get_that_raises_a_transport_error_keeps_the_task(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _note_task(db)

        def boom(**kw: Any) -> dict:
            raise httpx.ConnectError("T3 unreachable")

        t3 = _T3Stub(boom)
        rows = self._run(db, monkeypatch, t3)
        assert t3.calls >= 1, "the read reached T3: the failure is the stub's, not an earlier skip"
        assert len(rows) == 1, "the task survives a transport failure"
        assert rows[0].attempts == 1 and rows[0].claim_state != "dead"
        assert rows[0].claim_state is None, "released back to the queue, not held, not dead"

    def test_a_t3_5xx_is_retried_too(self, db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        _note_task(db)

        def boom(**kw: Any) -> dict:
            req = httpx.Request("POST", "http://t3/v1/vectors/get")
            raise httpx.HTTPStatusError("500", request=req, response=httpx.Response(500, request=req))

        rows = self._run(db, monkeypatch, _T3Stub(boom))
        assert len(rows) == 1 and rows[0].attempts == 1 and rows[0].claim_state != "dead"

    def test_a_manifest_lookup_that_raises_a_transport_error_keeps_the_task(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _note_task(db)

        class _Cat:
            def get_manifest(self, doc_id: str) -> list:
                raise httpx.ConnectError("catalog unreachable")

        monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda *a, **k: _Cat())
        t3 = _T3Stub(lambda **kw: {"ids": [], "documents": [], "metadatas": []})
        rows = self._run(db, monkeypatch, t3)
        assert len(rows) == 1, "an empty manifest from a failed lookup is not a verdict on the document"
        assert rows[0].attempts == 1 and rows[0].claim_state != "dead"

    def test_control_an_empty_read_is_still_dropped(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """T3 answers, with nothing: a permanent verdict. The same pass consumes the task."""
        _note_task(db)
        t3 = _T3Stub(lambda **kw: {"ids": [], "documents": [], "metadatas": []})
        rows = self._run(db, monkeypatch, t3)
        assert t3.calls >= 1
        assert rows == [], "an empty read is acked as dropped"


class TestScenario7Renewal:
    def test_a_claim_is_renewed_past_its_lease_inside_the_deadline(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        doc = _doc("s7a")
        _out(db, doc)

        def slow(content, source_path, collection, **kw):
            time.sleep(3.5)  # longer than the 2 s lease
            return _record(collection, source_path)

        monkeypatch.setattr("nexus.aspect_worker._extract_aspects", slow)
        w = _worker(lease_s=2, renew_every_s=0.5, deadline_s=lambda n: 30.0)
        side = w._tuple_side
        claims, _ = side.claim_batch(park=False)
        assert len(claims) == 1

        side.process(claims)

        assert db.document_aspects.get_by_doc_id(doc) is not None, "the fenced write landed: the claim was held"
        assert _tasks(db) == [], "and the ack consumed it, with no lapse in between"

    def test_a_hung_extraction_past_the_deadline_loses_its_lease_and_the_task_is_retried(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        doc = _doc("s7b")
        _out(db, doc)
        release_hang = threading.Event()

        def hang(content, source_path, collection, **kw):
            release_hang.wait(30)
            return _record(collection, source_path)

        monkeypatch.setattr("nexus.aspect_worker._extract_aspects", hang)
        w = _worker(lease_s=2, renew_every_s=0.4, deadline_s=lambda n: 1.0)
        side = w._tuple_side
        claims, _ = side.claim_batch(park=False)
        t = threading.Thread(target=side.process, args=(claims,), daemon=True)
        t.start()
        try:
            # The renewer stops at 1 s; the 2 s lease then lapses and a second
            # claimant takes the task with an attempt counted.
            taken: list = []

            def reclaimed() -> bool:
                got = db.tuples.inp(_SUB, dict(_KEYS), claimant="rescuer", lease_s=600)
                if got is not None:
                    taken.append(got)
                return got is not None

            _wait(reclaimed, timeout=15)
            row, claim_id = taken[0]
            assert row.attempts == 1, "the lapse counted an attempt"
        finally:
            release_hang.set()
            t.join(20)
        # The hung worker's late result met a claim that is not its own: nothing
        # written, nothing acked.
        assert db.document_aspects.get_by_doc_id(doc) is None
        assert len(_tasks(db)) == 1
        db.tuples.ack(claim_id, "rescuer")


class TestScenario10Superseded:
    def _rename_after_claim(self, db: Any, old: str, new: str) -> None:
        from nexus.catalog.factory import make_catalog_writer
        from nexus.corpus import collection_registration_kwargs

        from nexus.catalog.http_catalog_client import HttpCatalogClient

        make_catalog_writer().register_collection(old, **collection_registration_kwargs(old))
        with HttpCatalogClient() as cat:
            cat.rename_collection_cascade(old, new)

    def test_409_releases_with_no_attempt_and_the_next_claim_carries_the_new_name_and_a_repeated_409_is_bounded(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._409_releases_and_the_next_claim_carries_the_new_name(db, monkeypatch)
        self._a_task_that_keeps_answering_409_is_nacked_dead_on_the_second(db, monkeypatch)

    def _409_releases_and_the_next_claim_carries_the_new_name(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        owner = uuid.uuid4().hex[:8]
        old = f"knowledge__nx235s-{owner}__bge-base-en-v15-768__v1"
        new = f"knowledge__nx235s-{owner}__bge-base-en-v15-768__v2"
        from nexus.catalog.factory import make_catalog_writer
        from nexus.corpus import collection_registration_kwargs

        make_catalog_writer().register_collection(old, **collection_registration_kwargs(old))
        doc = _doc("s10", old)
        _out(db, doc, collection=old)

        renamed = threading.Event()

        def extract_then_rename(content, source_path, collection, **kw):
            if not renamed.is_set():
                self._rename_after_claim(db, old, new)
                renamed.set()
            return _record(collection, source_path)

        monkeypatch.setattr("nexus.aspect_worker._extract_aspects", extract_then_rename)
        side = _worker()._tuple_side
        claims, _ = side.claim_batch(park=False)
        assert claims[0].row.collection == old

        side.process(claims)

        rows = _tasks(db)
        assert len(rows) == 1 and rows[0].attempts == 0, "released: no attempt counted"
        assert db.document_aspects.get_by_doc_id(doc) is None, "the 409 wrote nothing"
        again, _ = side.claim_batch(park=False)
        assert [c.row.collection for c in again] == [new], "the next claim carries the new name"
        side.process(again)
        assert db.document_aspects.get_by_doc_id(doc) is not None
        assert _tasks(db) == []

    def _a_task_that_keeps_answering_409_is_nacked_dead_on_the_second(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Second arm of the scenario: runs after the release arm, on a task of its own."""
        owner = uuid.uuid4().hex[:8]
        old = f"knowledge__nx235t-{owner}__bge-base-en-v15-768__v1"
        new = f"knowledge__nx235t-{owner}__bge-base-en-v15-768__v2"
        from nexus.catalog.factory import make_catalog_writer
        from nexus.corpus import collection_registration_kwargs

        w = make_catalog_writer()
        for name in (old, new):
            w.register_collection(name, **collection_registration_kwargs(name))
        doc = _doc("s10-repeat", old)
        w.supersede_collection(old, new)  # retired, but the task names it and nothing rewrote it
        _out(db, doc, collection=old)
        monkeypatch.setattr(
            "nexus.aspect_worker._extract_aspects",
            lambda content, source_path, collection, **kw: _record(collection, source_path),
        )
        side = _worker()._tuple_side

        first, _ = side.claim_batch(park=False)
        side.process(first)
        rows = _tasks(db)
        assert len(rows) == 1 and rows[0].attempts == 0 and rows[0].claim_state != "dead"

        second, _ = side.claim_batch(park=False)
        assert len(second) == 1
        side.process(second)
        rows = _tasks(db)
        assert len(rows) == 1 and rows[0].claim_state == "dead", "bounded: dead on the second 409"
        assert db.document_aspects.get_by_doc_id(doc) is None


class TestScenario14NoteTask:
    def test_an_mcp_note_task_is_an_identity_body_and_the_extractor_reads_it_itself(
        self, db: Any, extracted: list[dict],
    ) -> None:
        # field presence, never strings: the body has the four identity keys and
        # no text; the extractor is handed empty content, the doc_id and a
        # manifest lookup that answers for the note.
        from nexus.catalog.factory import make_catalog_reader
        from nexus.catalog.note_write import write_note
        from nexus.catalog.store_hook import catalog_store_hook_tracked, note_manifest_metadata

        collection = f"knowledge__nx235n-{uuid.uuid4().hex[:6]}__bge-base-en-v15-768__v1"
        a = "nx235n alpha paragraph: the first distinct piece of this note"
        b = "nx235n beta paragraph: the middle piece with trailing spaces   \r\n"
        pieces = [a, b, a]  # a repeated paragraph; CRLF and whitespace in the middle piece
        first, _ = note_manifest_metadata(pieces)
        doc, _created = catalog_store_hook_tracked(title="nx235n note", doc_id=first, collection_name=collection)
        write_note(catalog_doc_id=doc, collection=collection, pieces=pieces)
        chash = first
        _out(db, doc, collection=collection, source=chash)

        body = json.loads(_tasks(db)[0].body)
        assert sorted(body) == ["collection", "content_hash", "doc_id", "source_path"]
        assert all(len(str(v)) < 200 for v in body.values()), "no document text rides the task"

        side = _worker()._tuple_side
        claims, _ = side.claim_batch(park=False)
        side.process(claims)

        assert len(extracted) == 1
        call = extracted[0]
        assert call["content"] == "", "the text is not in the task: the extractor fetches it"
        assert call["doc_id_lookup"](collection, chash) == doc
        manifest = call["manifest_lookup"](doc)
        assert len(manifest) == len(pieces), "the manifest holds one row per position, the repeat included"
        assert len({r.chash for r in manifest}) == 2, "a repeated paragraph is one chunk at two positions"
        assert make_catalog_reader().get_manifest(doc), "and the catalog answers for it"


class TestAlternationWithTheTable:
    def test_both_queues_drain_in_tuples_mode_and_a_table_mode_worker_drains_leftover_tasks(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._pending_table_rows_and_tuple_tasks_both_drain_in_tuples_mode(db, monkeypatch)
        monkeypatch.delenv("NX_ASPECT_QUEUE")
        self._a_table_mode_worker_still_drains_leftover_tuple_tasks(db, monkeypatch)

    def _pending_table_rows_and_tuple_tasks_both_drain_in_tuples_mode(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        table_docs = [_doc(f"alt-t{i}") for i in range(3)]
        tuple_docs = [_doc(f"alt-u{i}") for i in range(3)]
        # Table rows first, while the flag is unset: in tuples mode the producer
        # would write tasks instead (a producer on an older box still writes rows).
        for i, d in enumerate(table_docs):
            db.aspect_queue.enqueue(_COLL, f"/t{i}.md", doc_id=d)
        assert db.aspect_queue.pending_count() == 3
        for i, d in enumerate(tuple_docs):
            _out(db, d, source=f"/u{i}.md")
        monkeypatch.setenv("NX_ASPECT_QUEUE", "tuples")
        monkeypatch.setattr(
            "nexus.aspect_worker._extract_aspects",
            lambda content, source_path, collection, **kw: _record(collection, source_path),
        )
        monkeypatch.setattr(
            "nexus.aspect_worker._extract_aspects_batch",
            lambda items, **kw: [_record(c, s) for (c, s, *_rest) in items],
        )
        w = AspectExtractionWorker(
            poll_interval=0.2, batch_size=2, tuple_options={"park_timeout_s": 1},
        )
        w.start()
        try:
            _wait(
                lambda: db.aspect_queue.pending_count() == 0
                and _tasks(db) == []
                and all(db.document_aspects.get_by_doc_id(d) for d in table_docs + tuple_docs),
                timeout=60,
            )
        finally:
            w.stop(timeout=10)

    def _a_table_mode_worker_still_drains_leftover_tuple_tasks(
        self, db: Any, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # gate S1 rollback: the flag is unset (table), tasks are on the tuple queue
        docs = [_doc(f"s1-{i}") for i in range(2)]
        for i, d in enumerate(docs):
            _out(db, d, source=f"/s1-{i}.md")
        monkeypatch.setattr(
            "nexus.aspect_worker._extract_aspects",
            lambda content, source_path, collection, **kw: _record(collection, source_path),
        )
        monkeypatch.setattr(
            "nexus.aspect_worker._extract_aspects_batch",
            lambda items, **kw: [_record(c, s) for (c, s, *_rest) in items],
        )
        w = AspectExtractionWorker(poll_interval=0.2)
        w.start()
        try:
            _wait(lambda: _tasks(db) == [] and all(db.document_aspects.get_by_doc_id(d) for d in docs), timeout=60)
        finally:
            w.stop(timeout=10)
