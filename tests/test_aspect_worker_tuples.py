# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-235 client half (nexus-5r2sc.3.3): the aspect worker on the ``aspects`` tuple queue.

Against the real engine substrate, with real tasks on the real queue, a stubbed
extractor (never a model call, never a credential) and the worker's own
``TupleQueueSide`` driving claims, renewals and verdicts. Test Plan scenarios 6
(five claims, five tasks), 7 (renewed inside the deadline, lapsed past it), 10
(409, release, bounded), 14 worker half (an MCP note is a task with no text; the
extractor reads it through the manifest), 15 (more than four collections, one
park), 16 (a missing document is retried once, then acked as dropped), gate S3 (T3 unreachable is a
retryable nack, never a drop) and the alternation of the tuple queue with the old
table.

THREE ENGINE JOURNEYS (nexus-9edxe round 3; they were fifteen tenant-minting tests,
and every assertion names its step because a fold hides the steps after a failure):

1. ``test_slots_parks_and_leases_on_the_engine``: S6, S15, S7a, S7b.
2. ``test_each_verdict_reaches_the_engine_as_its_own_queue_state``: S3 then S16.
3. ``test_task_shapes_the_old_table_and_a_retired_collection_on_the_engine``: S14,
   the alternation (both modes), S10 (both arms).

What runs on the fake HttpTupleStore instead, in tests/test_aspect_worker.py and with
no tenant: the T3 and manifest transport failures (the REAL extractor and reader run),
the persist 5xx and its 4xx control, and the engine's park-cap refusal.

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


def _wait(pred, timeout: float = 20.0, poll: float = 0.05, detail=None) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(poll)
    raise AssertionError("condition not reached in time" + (f": {detail()}" if detail else ""))


def _by_path(db: Any) -> dict[str, Any]:
    """The queue's rows keyed by the task body's source path."""
    return {json.loads(r.body)["source_path"]: r for r in _tasks(db)}


def test_slots_parks_and_leases_on_the_engine(db: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """One journey over the claim mechanics, each step starting from an empty queue
    (nexus-9edxe round 3 folded four engine tests into this; every assertion names
    its step, and a failure in an early step hides the later ones):

    - S6: five slots return five distinct tasks, and ONE claimant claiming five times
      is its first task five times (F25), which is what the five slots must not be.
    - S15: tasks in more than four collections, then an idle queue: exactly ONE parked
      in under the slot-0 claimant (a worker that parked on every slot under five
      claimants would pass the refusal counters and fail the per-claimant set,
      nexus-5r2sc.3.10), several idle rounds never exceed the engine's park cap. The
      refusal arm (the engine answering the park with the cap) runs on the fake, in
      tests/test_aspect_worker.py.
    - S7a: a claim is renewed past its 2 s lease while an extraction runs 3.5 s inside
      its deadline; the fenced write lands and the ack consumes the task.
    - S7b: a hung extraction past a 1 s deadline stops being renewed, the lease lapses,
      a second claimant takes the task with an attempt counted, and the hung worker's
      late result writes nothing and acks nothing.
    """
    # ── S6: five claims, five tasks ─────────────────────────────────────────────
    collections = [f"knowledge__nx235c{i}-{uuid.uuid4().hex[:4]}" for i in range(5)]
    docs = [_doc(f"s6-{i}", c) for i, c in enumerate(collections)]
    for d, c in zip(docs, collections):
        _out(db, d, collection=c)

    got = [
        db.tuples.inp(_SUB, dict(_KEYS), claimant="one-claimant", lease_s=600)
        for _ in range(5)
    ]
    assert all(g is not None for g in got)
    assert len({g[0].id for g in got}) == 1, "S6: F25: a repeat claim by one claimant is its held claim"
    assert len({g[1] for g in got}) == 1
    db.tuples.release(got[0][1], "one-claimant")

    side = _worker()._tuple_side
    claims, failed = side.claim_batch(park=False)

    assert not failed
    assert sorted(c.slot for c in claims) == [0, 1, 2, 3, 4], "S6: one claim per slot"
    assert len({c.task_id for c in claims}) == 5, "S6: five claimants, five distinct tasks"
    assert len({c.claimant for c in claims}) == 5
    assert all(c.claimant == f"{side.worker_id}:{c.slot}" for c in claims)
    assert all(c.row is not None and c.row.doc_id in docs for c in claims)
    assert len({c.row.collection for c in claims}) == 5, "S15: more than four collections in the queue"
    for c in claims:  # drain, so the idle steps below are the parked case
        db.tuples.ack(c.claim_id, c.claimant)
    assert _tasks(db) == [], "S6: all five consumed"

    # ── S15: more than four collections, one park ───────────────────────────────
    # While parked (read it from the engine, mid-park): exactly ONE parked in,
    # under the slot-0 claimant.
    side_p = _worker(park_timeout_s=3)._tuple_side
    base_stats = db.tuples.park_stats()
    base = base_stats.global_in_use
    # The refusal counters are engine-process-wide and shared with every other test on this
    # substrate, so S15 asserts no INCREASE, never an absolute zero (develop red at c94cd4b00).
    base_refused_claimant = base_stats.refused_claimant
    base_refused_global = base_stats.refused_global
    holder: list = []
    t = threading.Thread(target=lambda: holder.append(side_p.claim_batch(park=True)), daemon=True)
    t.start()
    try:
        _wait(lambda: db.tuples.park_stats().per_claimant, timeout=10)
        stats = db.tuples.park_stats()
        assert set(stats.per_claimant) == {side_p.claimant(0)}, ("S15", stats.per_claimant)
        assert stats.global_in_use - base == 1, ("S15", stats.global_in_use, base)
    finally:
        t.join(timeout=15)
    assert holder and holder[0] == ([], False)

    # Idle: slots 1..4 probe and slot 0 parks, once; several rounds never
    # exceed the engine's per-claimant park cap of 4.
    side_i = _worker(park_timeout_s=1)._tuple_side
    for _ in range(3):
        got_i, failed = side_i.claim_batch(park=True)
        assert got_i == [] and not failed
    end_stats = db.tuples.park_stats()
    assert end_stats.refused_claimant == base_refused_claimant, "S15: no per-claimant refusal"
    assert end_stats.refused_global == base_refused_global, "S15: no global refusal"

    # ── S7a: renewed past its lease, inside the deadline ────────────────────────
    doc_a = _doc("s7a")
    _out(db, doc_a)

    def slow(content, source_path, collection, **kw):
        time.sleep(3.5)  # longer than the 2 s lease
        return _record(collection, source_path)

    monkeypatch.setattr("nexus.aspect_worker._extract_aspects", slow)
    side_a = _worker(lease_s=2, renew_every_s=0.5, deadline_s=lambda n: 30.0)._tuple_side
    claims_a, _ = side_a.claim_batch(park=False)
    assert len(claims_a) == 1

    side_a.process(claims_a)

    assert db.document_aspects.get_by_doc_id(doc_a) is not None, (
        "S7a: the fenced write landed: the claim was held"
    )
    assert _tasks(db) == [], "S7a: and the ack consumed it, with no lapse in between"

    # ── S7b: hung past the deadline, the lease lapses ───────────────────────────
    doc_b = _doc("s7b")
    _out(db, doc_b)
    release_hang = threading.Event()

    def hang(content, source_path, collection, **kw):
        release_hang.wait(30)
        return _record(collection, source_path)

    monkeypatch.setattr("nexus.aspect_worker._extract_aspects", hang)
    side_b = _worker(lease_s=2, renew_every_s=0.4, deadline_s=lambda n: 1.0)._tuple_side
    claims_b, _ = side_b.claim_batch(park=False)
    t = threading.Thread(target=side_b.process, args=(claims_b,), daemon=True)
    t.start()
    try:
        # The renewer stops at 1 s; the 2 s lease then lapses and a second
        # claimant takes the task with an attempt counted.
        taken: list = []

        def reclaimed() -> bool:
            got_r = db.tuples.inp(_SUB, dict(_KEYS), claimant="rescuer", lease_s=600)
            if got_r is not None:
                taken.append(got_r)
            return got_r is not None

        _wait(reclaimed, timeout=15)
        row, claim_id = taken[0]
        assert row.attempts == 1, "S7b: the lapse counted an attempt"
    finally:
        release_hang.set()
        t.join(20)
    # The hung worker's late result met a claim that is not its own: nothing
    # written, nothing acked.
    assert db.document_aspects.get_by_doc_id(doc_b) is None, "S7b: the late result wrote nothing"
    assert len(_tasks(db)) == 1, "S7b: and acked nothing"
    db.tuples.ack(claim_id, "rescuer")


def test_each_verdict_reaches_the_engine_as_its_own_queue_state(
    db: Any, extracted: list[dict], tuple_events: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One journey over the worker's verdicts as the engine's queue records them
    (nexus-9edxe round 3 folded gate S3 and scenario 16; a failure in S3 hides S16):

    - S3: four tasks, the extractor failing a different way for each (keyed on source
      path): infra_unavailable and an unreachable engine are retryable nacks and never
      a drop; a permanent read failure is acked as dropped; a non-retryable error is
      dead at once. The queue holds the retryable two back by the ladder's interval.
      (The transport failures of T3 and the manifest, and the persist 5xx, are
      verdicts of the worker alone and run on the fake, in tests/test_aspect_worker.py.)
    - S16 (nexus-1o3ft; the table's FK refused such a row at enqueue, the tuple queue
      checks nothing, and the wake is ~11 ms, so a producer can out a task before its
      catalog row commits), on the queue S3 left behind:
      ``present``, the control, extracts on the first pass so the silence of the other
      two is the verdict and not a worker that did nothing; ``late``, whose document is
      registered only after the first claim, gets a short retryable nack and is then
      extracted and persisted; ``gone``, whose document never appears, gets the same
      nack and is then acked as dropped.
    """
    from nexus import aspect_worker as w

    # ── S3: each failure class gets its own verdict ─────────────────────────────
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

    with monkeypatch.context() as m:
        m.setattr("nexus.aspect_worker._extract_aspects", fake)
        # S3's two retryable nacks must stay held back for the WHOLE of S16 below: with the
        # production 30 s base they became claimable 24-36 s in, and on a loaded shard the S16
        # first pass outlasted that, so the second claim swept them up beside gone and late and
        # the two-claim predicate never held (develop red at 18f805d8a, shard 6). 300 s keeps
        # them out of S16's 60 s window; S3 asserts only that they are held back.
        m.setattr(w, "_RETRY_BASE_SECONDS", 300.0)
        side3 = _worker()._tuple_side
        claims3, _ = side3.claim_batch(park=False)
        assert len(claims3) == 4

        for claim in claims3:  # one at a time: a lone claim takes the single-document extractor
            side3.process([claim])

    rows = _by_path(db)
    assert sorted(rows) == ["/conn.md", "/dead.md", "/infra.md"], (
        f"S3: only the permanent read failure is consumed: {sorted(rows)}"
    )
    for path in ("/infra.md", "/conn.md"):
        assert rows[path].attempts == 1, f"S3 {path}: the nack counted an attempt"
        assert rows[path].claim_state != "dead", f"S3 {path}: retryable, never dead"
    assert rows["/dead.md"].claim_state == "dead" and rows["/dead.md"].attempts == 1, "S3: dead at once"
    for doc in docs.values():
        assert db.document_aspects.get_by_doc_id(doc) is None, "S3: nothing persisted"
    # held back by retry_after_s from the RDR-163 ladder (300 s here, see above)
    assert db.tuples.inp(_SUB, dict(_KEYS), claimant="other", lease_s=60) is None, (
        "S3: the retryable two are held back by the ladder, the dead one is not claimable"
    )

    # ── S16: a missing document is retried once, then dropped; a late one is extracted ──
    monkeypatch.setattr(w, "_RETRY_BASE_SECONDS", 2.0)
    monkeypatch.setattr(w, "_RETRY_JITTER_FRACTION", 0.0)
    present = _doc("s16-present")
    anchor = _doc("s16-anchor")
    head, _, last = anchor.rpartition(".")
    late = f"{head}.{int(last) + 1}"  # the next tumbler this owner mints
    _out(db, "1.99999.99999", source="/gone.md")  # no such catalog document, ever
    _out(db, late, source="/late.md")  # not registered yet
    _out(db, present, source="/present.md")
    side = _worker()._tuple_side
    claims, _ = side.claim_batch(park=False)
    assert len(claims) == 3, "S16: only the three new tasks are claimable (S3's are held back or dead)"
    assert {c.attempts for c in claims} == {0}

    side.process(claims)

    assert [c["source_path"] for c in extracted] == ["/present.md"], (
        "S16: the first pass extracts only the document that is there"
    )
    assert tuple_events.count("aspect_task_doc_not_visible_yet") == 2, tuple_events
    assert "aspect_task_dropped_missing_doc" not in tuple_events, (
        "S16: the first gone verdict is a retry, never a drop"
    )
    held = _by_path(db)
    assert sorted(held) == ["/conn.md", "/dead.md", "/gone.md", "/infra.md", "/late.md"], (
        f"S16: present is consumed, gone and late queued beside S3's three: {sorted(held)}"
    )
    for path in ("/gone.md", "/late.md"):
        assert held[path].attempts == 1 and held[path].claim_state != "dead", (path, held[path])
        # The delay is proven by the engine's own stamp (rendered since
        # nexus-p909o), not by an immediate re-claim: under load the first pass
        # can outlast the 2 s ladder interval and a re-claim then finds the task
        # claimable again, which is the ladder working, not a defect.
        assert held[path].available_at is not None, (
            f"S16 {path}: the retried task carries the ladder's available_at stamp"
        )
    assert db.document_aspects.get_by_doc_id(late) is None

    assert _doc("s16-late") == late, "S16: the late document registers under the predicted tumbler"

    again: dict[str, Any] = {}

    def second_claim() -> bool:
        got, _ = side.claim_batch(park=False)
        for c in got:
            # keyed on the task's path: a task that is claimed, lapses and is claimed again
            # counts once, and a task from any other phase never satisfies the predicate.
            if c.row is not None:
                again[c.row.source_path] = c
        return {"/gone.md", "/late.md"} <= set(again)

    # 120 s: the retried task's ladder delay is on the engine clock and loaded CI shards reached
    # 20 s (develop red at c94cd4b00) and 60 s (b7d6c6355, 2026-10-30) with the ladder correct.
    # On a timeout the queue state is in the message, so the next red is diagnosable.
    _wait(
        second_claim, timeout=120,
        detail=lambda: (
            f"claimed={sorted(again)} rows="
            f"{[(p, r.claim_state, r.attempts, r.available_at) for p, r in sorted(_by_path(db).items())]}"
        ),
    )
    again_list = [again["/gone.md"], again["/late.md"]]
    assert {c.attempts for c in again_list} == {1}
    side.process(again_list)

    assert [c["source_path"] for c in extracted] == ["/present.md", "/late.md"]
    assert tuple_events.count("aspect_task_dropped_missing_doc") == 1, tuple_events
    assert tuple_events.count("aspect_task_doc_not_visible_yet") == 2, "S16: no second retry"
    left = _by_path(db)
    assert sorted(left) == ["/conn.md", "/dead.md", "/infra.md"], (
        f"S16: late extracted and acked, gone acked as dropped; S3's three untouched: {sorted(left)}"
    )
    assert left["/infra.md"].attempts == 1 and left["/conn.md"].attempts == 1, "S3's rows were not re-claimed"
    assert db.document_aspects.get_by_doc_id("1.99999.99999") is None
    assert db.document_aspects.get_by_doc_id(late) is not None
    assert db.document_aspects.get_by_doc_id(present) is not None


def _rename_after_claim(old: str, new: str) -> None:
    from nexus.catalog.factory import make_catalog_writer
    from nexus.catalog.http_catalog_client import HttpCatalogClient
    from nexus.corpus import collection_registration_kwargs

    make_catalog_writer().register_collection(old, **collection_registration_kwargs(old))
    with HttpCatalogClient() as cat:
        cat.rename_collection_cascade(old, new)


def test_task_shapes_the_old_table_and_a_retired_collection_on_the_engine(
    db: Any, extracted: list[dict], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One journey over the task shapes and queue interplay (nexus-9edxe round 3 folded
    scenario 14, the alternation and scenario 10; each step ends on an empty live queue,
    scenario 10 last because its second arm leaves a dead row):

    - S14 worker half: an MCP note is an identity-body task with no text; the
      extractor reads it through the catalog manifest.
    - Alternation: in tuples mode a worker drains table rows and tuple tasks both; in
      table mode (gate S1 rollback) it still drains leftover tuple tasks.
    - S10: a 409 (the collection was renamed after the claim) releases with no attempt
      counted and the next claim carries the new name; a task that keeps answering 409
      (a retired name nothing rewrote) is nacked dead on the second.
    """
    # ── S14: an MCP note is a task with no text ─────────────────────────────────
    from nexus.catalog.factory import make_catalog_reader
    from nexus.catalog.note_write import write_note
    from nexus.catalog.store_hook import catalog_store_hook_tracked, note_manifest_metadata

    collection = f"knowledge__nx235n-{uuid.uuid4().hex[:6]}__bge-base-en-v15-768__v1"
    # field presence, never strings: the body has the four identity keys and
    # no text; the extractor is handed empty content, the doc_id and a
    # manifest lookup that answers for the note.
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
    assert all(len(str(v)) < 200 for v in body.values()), "S14: no document text rides the task"

    side14 = _worker()._tuple_side
    claims14, _ = side14.claim_batch(park=False)
    side14.process(claims14)

    assert len(extracted) == 1
    call = extracted[0]
    assert call["content"] == "", "S14: the text is not in the task: the extractor fetches it"
    assert call["doc_id_lookup"](collection, chash) == doc
    manifest = call["manifest_lookup"](doc)
    assert len(manifest) == len(pieces), "S14: the manifest holds one row per position, the repeat included"
    assert len({r.chash for r in manifest}) == 2, "S14: a repeated paragraph is one chunk at two positions"
    assert make_catalog_reader().get_manifest(doc), "S14: and the catalog answers for it"
    assert _tasks(db) == [], "S14: the note task is consumed"

    # ── alternation: both queues drain in tuples mode; table mode drains leftover tasks ──
    _pending_table_rows_and_tuple_tasks_both_drain_in_tuples_mode(db, monkeypatch)
    monkeypatch.delenv("NX_ASPECT_QUEUE")
    _a_table_mode_worker_still_drains_leftover_tuple_tasks(db, monkeypatch)

    # ── S10: superseded ─────────────────────────────────────────────────────────
    _409_releases_and_the_next_claim_carries_the_new_name(db, monkeypatch)
    _a_task_that_keeps_answering_409_is_nacked_dead_on_the_second(db, monkeypatch)


def _pending_table_rows_and_tuple_tasks_both_drain_in_tuples_mode(
    db: Any, monkeypatch: pytest.MonkeyPatch,
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
    db: Any, monkeypatch: pytest.MonkeyPatch,
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


def _409_releases_and_the_next_claim_carries_the_new_name(
    db: Any, monkeypatch: pytest.MonkeyPatch,
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
            _rename_after_claim(old, new)
            renamed.set()
        return _record(collection, source_path)

    monkeypatch.setattr("nexus.aspect_worker._extract_aspects", extract_then_rename)
    side = _worker()._tuple_side
    claims, _ = side.claim_batch(park=False)
    assert claims[0].row.collection == old

    side.process(claims)

    rows = _tasks(db)
    assert len(rows) == 1 and rows[0].attempts == 0, "S10: released: no attempt counted"
    assert db.document_aspects.get_by_doc_id(doc) is None, "S10: the 409 wrote nothing"
    again, _ = side.claim_batch(park=False)
    assert [c.row.collection for c in again] == [new], "S10: the next claim carries the new name"
    side.process(again)
    assert db.document_aspects.get_by_doc_id(doc) is not None
    assert _tasks(db) == []


def _a_task_that_keeps_answering_409_is_nacked_dead_on_the_second(
    db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second arm of scenario 10: runs after the release arm, on a task of its own."""
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
    assert len(rows) == 1 and rows[0].attempts == 0 and rows[0].claim_state != "dead", "S10: first 409 releases"

    second, _ = side.claim_batch(park=False)
    assert len(second) == 1
    side.process(second)
    rows = _tasks(db)
    assert len(rows) == 1 and rows[0].claim_state == "dead", "S10: bounded: dead on the second 409"
    assert db.document_aspects.get_by_doc_id(doc) is None
