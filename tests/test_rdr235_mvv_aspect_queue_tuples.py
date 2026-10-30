# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-235 Minimum Viable Validation: the aspect queue on the ``aspects`` tuple template.

One scenario journey (``test_mvv_journey_on_the_tuple_queue``) drives the
producer, the worker, the persist fence and the readers against the real
engine substrate (``t2_service_env``), with only the extractor faked at the
documented patch points (``nexus.aspect_worker._extract_aspects`` and
``_extract_aspects_batch``). It asserts the eight bullets of the RDR's
Minimum Viable Validation, each against the ENGINE's clock or its own state
where timing matters (``claimed_at`` / ``lease_until`` on the claim rows, the
census, the claim fence), never against local monotonic time. Local
``time.monotonic`` appears only as the hang bound (``_HANG_S``, as in
``tests/test_rdr206_mvv_http_tuple_store.py``) and as pacing.

THE SCRIPT. One tenant, ``NX_ASPECT_QUEUE`` UNSET (the default is ``tuples`` since
nexus-lnwn1, so the journey reaches the tuple producer the way a user does), 50 documents registered in
a ``knowledge__`` collection and enqueued through the production batch producer
(``HttpAspectQueue.enqueue_many``, the call the indexer's flush makes). Indexing
50 real documents would add embedding time and prove nothing about the queue:
the extractor is faked and the task is an identity body, so the journey starts
at the producer. Document 7 raises a retryable error twice, then succeeds.
Document 13 crashes its worker on every claim (a ``BaseException`` out of the
extractor ends the worker thread with the claim held, so nothing nacks, acks or
releases it) until the engine dead-letters it. Leases are 2 s, the RDR-163
ladder is 2 s * 2**attempts with no jitter, both through the worker's own
configuration (``tuple_options`` and the ladder module constants).

THE BULLETS (numbering follows the RDR):
1. every other document gets its ``document_aspects`` row exactly once;
2. document 7 succeeds on its third claim, each claim at least the ladder
   interval after the last (``claimed_at`` is the engine's stamp);
3. document 13 dead-letters after the attempt cap (six lapsed leases, the engine
   clock separating the claims by at least a lease), and ``nx aspects
   requeue-failed --dry-run`` lists it;
4. a second worker started mid-run claims nothing already claimed;
5. a worker whose claim is no longer live (taken over; lapsed) writes nothing:
   its upsert and its ack answer 404, and the row holds the second worker's
   extraction;
6. a batch of 5 holds five distinct tasks, one claimant string each;
7. with the old table holding pending rows and the tuple queue holding tasks,
   both drain and neither holds the other back;
8. no document text in any task body, each under 4,096 bytes.

Scenario 24 (a parked worker is woken by an ``out`` from a second client
process) and scenario 25 (the measured backlog, 1,200 tasks against
``max_live_rows`` 10,000; ``slow`` marker, run by name) follow the journey.

MUTATIONS, each run red and then restored (recorded in T2
nexus/rdr235-mvv-2026-10-29): see that entry; in short, stub the fence in
``HttpDocumentAspectsStore.upsert`` fails bullet 5; a ladder interval of 0 fails
bullet 2; a document 13 that does not crash fails bullet 3; the per-slot
claimant removed fails bullet 6; a worker that ignores the table fails bullet 7.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

from nexus.aspect_extractor import AspectRecord
from nexus.aspect_worker import AspectExtractionWorker
from nexus.db.t2.http_tuple_store import ClaimNotFoundError

pytestmark = pytest.mark.usefixtures("t2_service_env")

#: A hang bound, not a performance assertion (nexus-61vos): the journey is a few
#: tens of seconds, so anything past this is a wedge rather than a slow box.
_HANG_S = 180.0

_SUB = "aspects"
_KEYS = {"queue": "aspects"}

N_DOCS = 50
DOC_RETRY = 7
DOC_CRASH = 13
N_TABLE_DOCS = 6

#: Lease asked of every claim in the journey. The engine's template cap is 900;
#: 2 s makes the six lapses of document 13 take seconds, not an hour (gate O6).
_LEASE_S = 2
#: The ladder, shrunk through the module's own constants: base * 2**attempts,
#: no jitter, so the intervals are 2 s then 4 s.
_RETRY_BASE_S = 2.0
#: The template's ``max_attempts`` (aspects.yaml): dead once six claims lapsed or
#: were nacked.
_MAX_ATTEMPTS = 6
#: Pacing of a normal extraction, so a second worker started mid-run finds work.
_PACE_S = 0.25

_SENTINEL = "NX235-DOCUMENT-TEXT-MUST-NOT-RIDE-THE-TASK "
_PATH_RE = re.compile(r"^/nx235/(doc|table|fence)-([a-z0-9]+)\.md$")


class _WorkerCrash(BaseException):
    """The simulated death of a worker process: not an ``Exception``, so no
    ``except Exception`` in the worker catches it and the claim is left held."""


def _ts(value: str | None) -> datetime:
    assert value, "the engine must render this timestamp"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _wait(pred, what: str, timeout: float = _HANG_S, poll: float = 0.1) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(poll)
    raise AssertionError(f"hang bound ({timeout}s) reached waiting for: {what}")


def _doc_path(i: int) -> str:
    return f"/nx235/doc-{i:02d}.md"


def _register_collection(tag: str) -> str:
    from nexus.catalog.factory import make_catalog_writer
    from nexus.corpus import collection_registration_kwargs

    coll = f"knowledge__nx235{tag}-{uuid.uuid4().hex[:6]}__bge-base-en-v15-768__v1"
    make_catalog_writer().register_collection(coll, **collection_registration_kwargs(coll))
    return coll


def _register_doc(label: str, collection: str) -> str:
    from tests._catalog_fixture_ops import register_real_doc_id

    return register_real_doc_id(
        title=f"nx235 mvv {label} {uuid.uuid4().hex[:6]}", physical_collection=collection,
    )


def _tasks(db: Any) -> list:
    return db.tuples.rd(_SUB, dict(_KEYS), n=300)


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """One T2Database per test; the worker's routed writes use it."""
    import nexus.mcp_infra as infra
    from nexus.db.t2 import T2Database

    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    # UNSET, not "tuples": the default is the thing under test (nexus-lnwn1). A test
    # that needs the table path sets it to "table" itself.
    monkeypatch.delenv("NX_ASPECT_QUEUE", raising=False)
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    d = T2Database(tmp_path / "t2.db")
    monkeypatch.setattr(infra, "t2_index_write", lambda fn, **kw: fn(d))
    yield d
    d.close()


class _Ledger:
    """Observes the wire the worker uses, without changing it.

    Wraps ``HttpTupleStore`` claim, ack, nack and release and
    ``HttpDocumentAspectsStore.upsert``: each wrapper calls the real method and
    records what came back (a claim row with the engine's ``claimed_at`` and
    ``lease_until``, or the typed error). Nothing is faked or short-circuited;
    the engine answers every call.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.claims: list[dict[str, Any]] = []
        self.ends: list[dict[str, Any]] = []
        self.upserts: list[dict[str, Any]] = []

    def reset(self) -> None:
        with self._lock:
            self.claims.clear()
            self.ends.clear()
            self.upserts.clear()

    def install(self, mp: pytest.MonkeyPatch) -> None:
        from nexus.db.t2.http_document_aspects_store import HttpDocumentAspectsStore
        from nexus.db.t2.http_tuple_store import HttpTupleStore

        led = self
        orig_inp, orig_in = HttpTupleStore.inp, HttpTupleStore.in_
        orig_ack, orig_nack, orig_release = (
            HttpTupleStore.ack, HttpTupleStore.nack, HttpTupleStore.release,
        )
        orig_upsert = HttpDocumentAspectsStore.upsert

        def _claim(subspace: str, claimant: str, got: Any) -> None:
            if subspace != _SUB or got is None:
                return
            row, claim_id = got
            with led._lock:
                led.claims.append({
                    "task_id": row.id, "claim_id": claim_id, "claimant": claimant,
                    "attempts": row.attempts, "claimed_at": _ts(row.claimed_at),
                    "lease_until": _ts(row.lease_until), "created_at": _ts(row.created_at),
                })

        def inp(self_, subspace, keys, *, claimant, lease_s):
            got = orig_inp(self_, subspace, keys, claimant=claimant, lease_s=lease_s)
            _claim(subspace, claimant, got)
            return got

        def in_(self_, subspace, keys, *, claimant, lease_s, timeout_s=0):
            got = orig_in(self_, subspace, keys, claimant=claimant, lease_s=lease_s, timeout_s=timeout_s)
            _claim(subspace, claimant, got)
            return got

        def _end(kind: str, fn, claim_id, claimant, **kw):
            rec = {"kind": kind, "claim_id": claim_id, "claimant": claimant, "error": None, **kw}
            try:
                return fn()
            except BaseException as exc:
                rec["error"] = type(exc).__name__
                raise
            finally:
                with led._lock:
                    led.ends.append(rec)

        def ack(self_, claim_id, claimant, reply=None):
            return _end("ack", lambda: orig_ack(self_, claim_id, claimant, reply), claim_id, claimant)

        def nack(self_, claim_id, claimant, *, retry_after_s=None, dead=False):
            return _end(
                "nack",
                lambda: orig_nack(self_, claim_id, claimant, retry_after_s=retry_after_s, dead=dead),
                claim_id, claimant, retry_after_s=retry_after_s, dead=dead,
            )

        def release(self_, claim_id, claimant):
            return _end("release", lambda: orig_release(self_, claim_id, claimant), claim_id, claimant)

        def upsert(self_, record, *, claim_id=None, claimant=None):
            rec = {
                "doc_id": record.doc_id, "claim_id": claim_id, "claimant": claimant, "error": None,
            }
            try:
                return orig_upsert(self_, record, claim_id=claim_id, claimant=claimant)
            except BaseException as exc:
                rec["error"] = type(exc).__name__
                raise
            finally:
                with led._lock:
                    led.upserts.append(rec)

        mp.setattr(HttpTupleStore, "inp", inp)
        mp.setattr(HttpTupleStore, "in_", in_)
        mp.setattr(HttpTupleStore, "ack", ack)
        mp.setattr(HttpTupleStore, "nack", nack)
        mp.setattr(HttpTupleStore, "release", release)
        mp.setattr(HttpDocumentAspectsStore, "upsert", upsert)

    def claims_of(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (c for c in self.claims if c["task_id"] == task_id), key=lambda c: c["claimed_at"],
            )

    def ends_of(self, claim_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self.ends if e["claim_id"] == claim_id]


class _Extractor:
    """The faked extractor, installed at the documented patch points.

    Per path: document 7 raises a retryable ``httpx.ConnectError`` on its first
    two calls; document 13 raises :class:`_WorkerCrash` on every call; a
    ``fence`` path blocks its first call on a gate and answers
    ``first-worker``, then ``second-worker`` to every later call; everything
    else answers ``extracted:<path>`` after a short pace. Every call is
    recorded, and a table-side and a tuple-side call each note the OTHER
    queue's depth at that moment (bullet 7).
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self.order: list[str] = []
        self.crashes: list[str] = []
        self.table_pending_seen_by_tuple_calls: list[int] = []
        self.tuples_available_seen_by_table_calls: list[int] = []
        self.gates: dict[str, threading.Event] = {}
        self.pace = _PACE_S

    def install(self, mp: pytest.MonkeyPatch) -> None:
        mp.setattr("nexus.aspect_worker._extract_aspects", self.single)
        mp.setattr("nexus.aspect_worker._extract_aspects_batch", self.batch)

    def count(self, path: str) -> int:
        with self._lock:
            return self.calls.get(path, 0)

    def _record(self, collection: str, source_path: str, label: str) -> AspectRecord:
        return AspectRecord(
            collection=collection, source_path=source_path, problem_formulation=label,
            proposed_method="M", experimental_datasets=["d"], experimental_baselines=["b"],
            experimental_results="R", extras={}, confidence=0.9,
            extracted_at="2026-10-29T00:00:00+00:00", model_version="test", extractor_name="test",
        )

    def single(self, content, source_path, collection, **kw):
        with self._lock:
            n = self.calls[source_path] = self.calls.get(source_path, 0) + 1
            self.order.append(source_path)
        m = _PATH_RE.match(source_path)
        assert m, f"unexpected source_path {source_path!r}"
        kind, key = m.groups()
        if kind == "table":
            seen = self._db.tuples.subspace_stats(_SUB).available
            with self._lock:
                self.tuples_available_seen_by_table_calls.append(seen)
        elif kind == "doc":
            pending = self._db.aspect_queue.pending_count()
            with self._lock:
                self.table_pending_seen_by_tuple_calls.append(pending)
            if int(key) == DOC_CRASH:
                with self._lock:
                    self.crashes.append(source_path)
                raise _WorkerCrash(source_path)
            if int(key) == DOC_RETRY and n <= 2:
                raise httpx.ConnectError(f"T3 unreachable (call {n})")
        elif kind == "fence":
            if n == 1:
                gate = self.gates[source_path]
                assert gate.wait(_HANG_S), "fence gate never opened"
                return self._record(collection, source_path, "first-worker")
            return self._record(collection, source_path, "second-worker")
        time.sleep(self.pace)
        return self._record(collection, source_path, f"extracted:{source_path}")

    def batch(self, items, **kw):
        return [self.single("", s, c, **kw) for (c, s, *_rest) in items]


class _Fleet:
    """Runs workers on their own threads and respawns one whose thread died of
    a :class:`_WorkerCrash`, as a supervisor restarts a crashed daemon."""

    def __init__(self, options: dict[str, Any]) -> None:
        self._options = options
        self.workers: list[AspectExtractionWorker] = []
        self._threads: list[threading.Thread] = []
        self._crashed: list[AspectExtractionWorker] = []
        self._replaced: set[int] = set()
        self.crash_worker_ids: list[str] = []
        self._lock = threading.Lock()

    def spawn(self) -> AspectExtractionWorker:
        w = AspectExtractionWorker(poll_interval=0.2, batch_size=1, tuple_options=dict(self._options))
        t = threading.Thread(target=self._run, args=(w,), daemon=True, name=f"mvv-{w._worker_id}")
        with self._lock:
            self.workers.append(w)
            self._threads.append(t)
        t.start()
        return w

    def _run(self, w: AspectExtractionWorker) -> None:
        try:
            w._run_loop()
        except _WorkerCrash:
            with self._lock:
                self._crashed.append(w)
                self.crash_worker_ids.append(w._worker_id)

    def respawn_crashed(self) -> int:
        with self._lock:
            todo = [w for w in self._crashed if id(w) not in self._replaced]
            self._replaced.update(id(w) for w in todo)
        for _ in todo:
            self.spawn()
        return len(todo)

    def stop_all(self) -> None:
        for w in self.workers:
            w._stop_event.set()
        for t in self._threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in self._threads), "a worker thread did not stop"


def _worker_opts(**over: Any) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "lease_s": _LEASE_S, "park_timeout_s": 1, "renew_every_s": 0.5,
        "deadline_s": lambda n: 30.0,
    }
    opts.update(over)
    return opts


@pytest.mark.scenario
def test_mvv_journey_on_the_tuple_queue(
    db: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RDR-235 MVV: all eight bullets in one journey (see the module docstring)."""
    from click.testing import CliRunner

    from nexus import aspect_worker as w
    from nexus.commands.aspects import aspects_group

    monkeypatch.setattr(w, "_RETRY_BASE_SECONDS", _RETRY_BASE_S)
    monkeypatch.setattr(w, "_RETRY_JITTER_FRACTION", 0.0)
    ledger = _Ledger()
    ledger.install(monkeypatch)
    ex = _Extractor(db)
    ex.install(monkeypatch)

    # ── setup: one tenant, a collection, 50 documents, and a few old-table rows ──
    coll = _register_collection("mvv")
    docs = [_register_doc(f"d{i:02d}", coll) for i in range(N_DOCS)]
    table_docs = [_register_doc(f"t{i}", coll) for i in range(N_TABLE_DOCS)]

    # The table's pending rows are written under the explicit opt-out (a producer
    # on an older box still writes rows); the tuple tasks after, with the variable
    # unset, which is the default and so the producer a user gets (nexus-lnwn1).
    monkeypatch.setenv("NX_ASPECT_QUEUE", "table")
    for i, d in enumerate(table_docs):
        db.aspect_queue.enqueue(coll, f"/nx235/table-{i}.md", content_hash="t", doc_id=d)
    assert db.aspect_queue.pending_count() == N_TABLE_DOCS

    monkeypatch.delenv("NX_ASPECT_QUEUE", raising=False)
    from nexus.db.t2.http_aspect_queue import aspect_queue_mode  # noqa: PLC0415

    assert aspect_queue_mode() == "tuples", "the unset variable is the tuple producer"
    rows = [
        {
            "collection": coll, "source_path": _doc_path(i), "doc_id": docs[i],
            "content_hash": f"h{i}", "content": _SENTINEL * 400,  # 17 KB of document text
        }
        for i in range(N_DOCS)
    ]
    assert db.aspect_queue.enqueue_many(rows) == N_DOCS
    tasks = _tasks(db)
    assert len(tasks) == N_DOCS, "one task per document, as the producer's replace guarantees"

    # ── bullet 8: identity bodies, never text ────────────────────────────────
    idx_by_task: dict[str, int] = {}
    for t in tasks:
        body = json.loads(t.body)
        assert sorted(body) == ["collection", "content_hash", "doc_id", "source_path"], body
        assert _SENTINEL not in t.body, "document text rode the task"
        assert len(t.body.encode("utf-8")) < 4096
        idx_by_task[t.id] = int(_PATH_RE.match(body["source_path"]).group(2))
    assert sorted(idx_by_task.values()) == list(range(N_DOCS))
    task7 = next(t for t, i in idx_by_task.items() if i == DOC_RETRY)
    task13 = next(t for t, i in idx_by_task.items() if i == DOC_CRASH)

    # ── bullet 6: a batch of 5 holds five distinct tasks, one claimant each ──
    probe = AspectExtractionWorker(batch_size=5, tuple_options=_worker_opts(lease_s=60))
    side = probe._tuple_side
    claims, failed = side.claim_batch(park=False)
    assert not failed
    assert sorted(c.slot for c in claims) == [0, 1, 2, 3, 4]
    assert len({c.task_id for c in claims}) == 5, "five claimants, five distinct tasks"
    assert len({c.claimant for c in claims}) == 5
    assert all(c.claimant == f"{side.worker_id}:{c.slot}" for c in claims)
    held = [t for t in _tasks(db) if t.claim_state == "claimed"]
    assert sorted(t.claimant for t in held) == sorted(f"{side.worker_id}:{s}" for s in range(5)), (
        "the engine sees five live claims, one claimant string each"
    )
    assert side.release_held_claims() == 5
    assert all(t.attempts == 0 and t.claim_state is None for t in _tasks(db)), (
        "released, not nacked: no attempt counted"
    )
    # Control (F25): one claimant string five times is the first task five times.
    got = [db.tuples.inp(_SUB, dict(_KEYS), claimant="one-claimant", lease_s=60) for _ in range(5)]
    assert len({g[0].id for g in got}) == 1
    db.tuples.release(got[0][1], "one-claimant")
    ledger.reset()

    # ── the run: worker A, a crashing document, and a second worker mid-run ───
    fleet = _Fleet(_worker_opts())
    fleet.spawn()
    b_worker: AspectExtractionWorker | None = None
    claims_before_b: set[str] = set()
    try:
        deadline = time.monotonic() + _HANG_S
        while True:
            fleet.respawn_crashed()
            census = db.tuples.subspace_stats(_SUB)
            if b_worker is None and census.consumed >= 8 and census.available > 0:
                with ledger._lock:
                    claims_before_b = {c["claim_id"] for c in ledger.claims}
                b_worker = fleet.spawn()
            if (
                b_worker is not None
                and db.aspect_queue.pending_count() == 0
                and census.available == 0 and census.claimed == 0 and census.dead >= 1
            ):
                break
            assert time.monotonic() < deadline, f"hang bound ({_HANG_S}s): {census}"
            time.sleep(0.2)
    finally:
        fleet.stop_all()
    assert b_worker is not None
    b_id = b_worker._worker_id

    # ── bullet 1: every other document gets its row exactly once ─────────────
    with ledger._lock:
        ok_upserts = [u for u in ledger.upserts if u["error"] is None]
        failed_upserts = [u for u in ledger.upserts if u["error"] is not None]
    assert not failed_upserts, f"a write met a claim that was not live: {failed_upserts}"
    by_doc: dict[str, int] = {}
    for u in ok_upserts:
        by_doc[u["doc_id"]] = by_doc.get(u["doc_id"], 0) + 1
    for i in range(N_DOCS):
        rec = db.document_aspects.get_by_doc_id(docs[i])
        if i == DOC_CRASH:
            assert rec is None and docs[i] not in by_doc, "the crashing document wrote nothing"
            continue
        assert rec is not None and rec.problem_formulation == f"extracted:{_doc_path(i)}", i
        assert by_doc.get(docs[i]) == 1, f"document {i}: {by_doc.get(docs[i])} writes, want exactly 1"
        want_calls = 3 if i == DOC_RETRY else 1
        assert ex.count(_doc_path(i)) == want_calls, (i, ex.count(_doc_path(i)))

    # ── bullet 2: document 7 succeeds on its third claim, ladder-spaced ──────
    c7 = ledger.claims_of(task7)
    assert [c["attempts"] for c in c7] == [0, 1, 2], "three claims; attempts 0, 1, 2"
    nacks7 = [e for e in ledger.ends if e["kind"] == "nack" and e["claim_id"] in {c["claim_id"] for c in c7}]
    nacks7.sort(key=lambda e: [c["claim_id"] for c in c7].index(e["claim_id"]))
    intervals = [e["retry_after_s"] for e in nacks7]
    assert intervals == [w._backoff_interval_seconds(0), w._backoff_interval_seconds(1)] == [2, 4]
    assert all(e["error"] is None and not e["dead"] for e in nacks7)
    for (prev, nxt), interval in zip(zip(c7, c7[1:]), intervals):
        gap = (nxt["claimed_at"] - prev["claimed_at"]).total_seconds()
        assert gap >= interval, f"engine-clock gap {gap}s is under the ladder interval {interval}s"
    assert [e["kind"] for e in ledger.ends_of(c7[2]["claim_id"])] == ["ack"], "the third claim is acked"

    # ── bullet 3: document 13 dead-letters after the attempt cap ─────────────
    c13 = ledger.claims_of(task13)
    assert [c["attempts"] for c in c13] == list(range(_MAX_ATTEMPTS)), (
        "six claims, each after a lapse that counted one attempt"
    )
    assert len(ex.crashes) == _MAX_ATTEMPTS == len(fleet.crash_worker_ids)
    assert len({c["claimant"].rsplit(":", 1)[0] for c in c13}) == _MAX_ATTEMPTS, (
        "each claim was a fresh worker: the previous one died holding it"
    )
    assert not any(ledger.ends_of(c["claim_id"]) for c in c13), (
        "a crashed worker never acked, nacked or released: the leases lapsed"
    )
    for prev, nxt in zip(c13, c13[1:]):
        assert nxt["claimed_at"] >= prev["lease_until"], "re-claimed only after the engine's lease lapsed"
    dead = [t for t in _tasks(db) if t.claim_state == "dead"]
    assert [t.id for t in dead] == [task13] and dead[0].attempts == _MAX_ATTEMPTS
    assert db.tuples.subspace_stats(_SUB).dead == 1
    dry = CliRunner().invoke(aspects_group, ["requeue-failed", "--dry-run", "--collection", coll])
    assert dry.exit_code == 0, dry.output
    assert "Would re-enqueue 1 dead task(s)" in dry.output, dry.output
    assert _doc_path(DOC_CRASH) in dry.output and f"attempts={_MAX_ATTEMPTS}" in dry.output, dry.output
    assert _doc_path(0) not in dry.output
    assert [t.id for t in _tasks(db) if t.claim_state == "dead"] == [task13], "a dry run revives nothing"

    # ── bullet 4: a second worker started mid-run claims nothing already claimed ─
    with ledger._lock:
        claims_all = list(ledger.claims)
        acks = [e for e in ledger.ends if e["kind"] == "ack" and e["error"] is None]
    claimant_of_claim = {c["claim_id"]: c["claimant"] for c in claims_all}
    first_id = fleet.workers[0]._worker_id
    first_acks = {e["claim_id"] for e in acks if claimant_of_claim[e["claim_id"]].startswith(first_id + ":")}
    # Every worker the journey started after the first (B, and any restart of one
    # that died on document 13) is a "second worker": it began once work was in flight.
    later_ids = [x._worker_id for x in fleet.workers[1:]]
    later_acks = {
        e["claim_id"] for e in acks
        if any(claimant_of_claim[e["claim_id"]].startswith(i + ":") for i in later_ids)
    }
    assert b_id in later_ids and any(c["claimant"].startswith(b_id + ":") for c in claims_all), (
        "the worker started mid-run claimed work"
    )
    assert first_acks and later_acks and claims_before_b, (
        "the first worker had finished tasks before the second started, and the later workers finished others"
    )
    acked_by_b, acked_by_rest = later_acks, first_acks
    task_of_claim = {c["claim_id"]: c["task_id"] for c in claims_all}
    assert not {task_of_claim[c] for c in acked_by_b} & {task_of_claim[c] for c in acked_by_rest}, (
        "no task was finished by both workers"
    )
    assert len({task_of_claim[c] for c in acked_by_b | acked_by_rest}) == N_DOCS - 1
    by_task: dict[str, list[dict[str, Any]]] = {}
    for c in claims_all:
        by_task.setdefault(c["task_id"], []).append(c)
    for task_id, cs in by_task.items():
        cs.sort(key=lambda c: c["claimed_at"])
        for prev, nxt in zip(cs, cs[1:]):
            ended_early = any(e["error"] is None for e in ledger.ends_of(prev["claim_id"]))
            assert ended_early or nxt["claimed_at"] >= prev["lease_until"], (
                f"task {task_id} was claimed again while a claim on it was live"
            )
    census = db.tuples.subspace_stats(_SUB)
    assert census.consumed == N_DOCS - 1 and census.available == 0 and census.claimed == 0

    # ── bullet 7: the old table and the tuple queue both drain, neither held back ─
    assert db.aspect_queue.pending_count() == 0 and not db.aspect_queue.list_failed(coll)
    for i, d in enumerate(table_docs):
        rec = db.document_aspects.get_by_doc_id(d)
        assert rec is not None and rec.problem_formulation == f"extracted:/nx235/table-{i}.md"
        assert by_doc.get(d) == 1
    assert any(n > 0 for n in ex.tuples_available_seen_by_table_calls), (
        "a table row was extracted while tuple tasks were waiting: the tuple queue did not hold it back"
    )
    assert any(n > 0 for n in ex.table_pending_seen_by_tuple_calls), (
        "a tuple task was extracted while table rows were waiting: the table did not hold it back"
    )

    # ── bullet 5: a worker whose claim is not live writes nothing ────────────
    fence_opts = _worker_opts(lease_s=_LEASE_S, renew_every_s=0.3, deadline_s=lambda n: 0.4)

    def stale_worker_run(path: str, doc_id: str) -> tuple[Any, threading.Thread, AspectExtractionWorker]:
        db.aspect_queue.enqueue(coll, path, content_hash="f", doc_id=doc_id)
        ex.gates[path] = threading.Event()
        s = AspectExtractionWorker(batch_size=1, tuple_options=dict(fence_opts))
        got, failed = s._tuple_side.claim_batch(park=False)
        assert len(got) == 1 and not failed
        th = threading.Thread(target=s._tuple_side.process, args=(got,), daemon=True)
        th.start()
        _wait(lambda: ex.count(path) == 1, "the stale worker's extractor call")
        return got[0], th, s

    def second_worker_takes_over(path: str) -> Any:
        t = AspectExtractionWorker(batch_size=1, tuple_options=_worker_opts(lease_s=60))
        taken: list = []

        def reclaimed() -> bool:
            got, _ = t._tuple_side.claim_batch(park=False)
            taken.extend(got)
            return bool(got)

        _wait(reclaimed, f"the engine to hand {path} to a second worker once the lease lapsed")
        assert taken[0].attempts == 1, "the lapse counted an attempt"
        t._tuple_side.process(taken)
        return taken[0]

    # (a) taken over: the second worker's extraction is what the row holds.
    fence_a = _register_doc("fence-a", coll)
    stale, th, s = stale_worker_run("/nx235/fence-a.md", fence_a)
    second_worker_takes_over("/nx235/fence-a.md")
    assert db.document_aspects.get_by_doc_id(fence_a).problem_formulation == "second-worker"
    ex.gates["/nx235/fence-a.md"].set()  # the stale worker's extraction now returns
    th.join(timeout=_HANG_S)
    assert not th.is_alive()
    assert db.document_aspects.get_by_doc_id(fence_a).problem_formulation == "second-worker", (
        "the stale worker's late result overwrote the row"
    )
    stale_writes = [u for u in ledger.upserts if u["claim_id"] == stale.claim_id]
    assert [u["error"] for u in stale_writes] == ["ClaimNotFoundError"], stale_writes
    assert not s._tuple_side.held(), "the stale worker forgot the lost claim"
    # The same two calls by hand, with the stale pair: write 404, ack 404.
    stale_fields = {
        "collection": coll, "source_path": "/nx235/fence-a.md", "problem_formulation": "stale-direct",
        "proposed_method": "M", "experimental_datasets": [], "experimental_baselines": [],
        "experimental_results": "R", "extras": {}, "confidence": 0.9,
        "extracted_at": "2026-10-29T00:00:00+00:00", "model_version": "test",
        "extractor_name": "test", "doc_id": fence_a,
    }
    with pytest.raises(ClaimNotFoundError):
        db.complete_aspect(stale_fields, claim_id=stale.claim_id, claimant=stale.claimant)
    with pytest.raises(ClaimNotFoundError):
        db.tuples.ack(stale.claim_id, stale.claimant)
    assert db.document_aspects.get_by_doc_id(fence_a).problem_formulation == "second-worker"

    # (b) lapsed, nobody took over: the stale write and ack answer 404 and write nothing.
    fence_b = _register_doc("fence-b", coll)
    stale, th, s = stale_worker_run("/nx235/fence-b.md", fence_b)
    _wait(lambda: db.tuples.subspace_stats(_SUB).lapsed >= 1, "the engine to see the lease lapse")
    ex.gates["/nx235/fence-b.md"].set()
    th.join(timeout=_HANG_S)
    assert not th.is_alive()
    assert db.document_aspects.get_by_doc_id(fence_b) is None, "a lapsed claim's write landed"
    assert [u["error"] for u in ledger.upserts if u["claim_id"] == stale.claim_id] == ["ClaimNotFoundError"]
    with pytest.raises(ClaimNotFoundError):
        db.tuples.ack(stale.claim_id, stale.claimant)
    still = [t for t in _tasks(db) if t.claim_state != "dead"]
    assert len(still) == 1, "the lapsed task is still queued, not consumed by the stale ack"
    second_worker_takes_over("/nx235/fence-b.md")
    assert db.document_aspects.get_by_doc_id(fence_b).problem_formulation == "second-worker"
    assert [t.id for t in _tasks(db)] == [task13], "only the dead task remains"


@pytest.mark.scenario
def test_scenario_24_an_out_from_a_second_process_wakes_a_parked_worker_within_two_seconds(
    db: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Test Plan scenario 24: a worker parked on the queue claims a task that
    ANOTHER client process writes, within the RDR's 2 s.

    Measured on the engine's clock: the claim row carries the task's
    ``created_at`` and the claim's ``claimed_at``, both stamped by the engine.
    The worker polls only every 30 s and parks for 8 s, so a claim inside 2 s
    can only be the park being woken.
    """
    from tests._chunk_seed import substrate_env

    monkeypatch.delenv("NX_ASPECT_QUEUE", raising=False)  # the default is tuples
    ledger = _Ledger()
    ledger.install(monkeypatch)
    ex = _Extractor(db)
    ex.pace = 0.0
    ex.install(monkeypatch)
    coll = _register_collection("s24")
    doc = _register_doc("s24", coll)
    path = "/nx235/doc-77.md"

    worker = AspectExtractionWorker(
        poll_interval=30.0, batch_size=1,
        tuple_options=_worker_opts(lease_s=60, park_timeout_s=8, renew_every_s=5),
    )
    base = db.tuples.park_stats().global_in_use
    worker.start()
    try:
        _wait(
            lambda: any(worker._worker_id in k for k in db.tuples.park_stats().per_claimant)
            and db.tuples.park_stats().global_in_use > base,
            "the worker to park its in",
        )
        time.sleep(0.5)
        child = (
            "import sys\n"
            "from nexus.db.t2.http_aspect_queue import HttpAspectQueue\n"
            "q = HttpAspectQueue()\n"
            "q.enqueue(sys.argv[1], sys.argv[2], 'h', doc_id=sys.argv[3])\n"
        )
        env = {
            **os.environ, **substrate_env(),
            "NEXUS_CONFIG_DIR": str(tmp_path / "child-config"),
            "NX_ALLOW_PROD_WRITE": "RDR-235 scenario 24 child: writes only to this test's throwaway engine",
        }
        env.pop("NX_ASPECT_QUEUE", None)  # the child reaches the tuple producer by default
        proc = subprocess.run(
            [sys.executable, "-c", child, coll, path, doc],
            env=env, capture_output=True, text=True, timeout=_HANG_S,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        _wait(lambda: ledger.claims, "the parked worker to claim the second process's task")
    finally:
        worker.stop(timeout=30)

    claim = ledger.claims[0]
    assert claim["claimant"] == f"{worker._worker_id}:0", "slot 0, the parked in"
    latency = (claim["claimed_at"] - claim["created_at"]).total_seconds()
    print(f"SCENARIO24 engine-clock wake latency: {latency:.3f}s (bound 2s)")
    assert 0 <= latency < 2.0, f"claimed {latency}s after the out (engine clock)"
    assert claim["attempts"] == 0
    _wait(lambda: db.document_aspects.get_by_doc_id(doc) is not None, "the task to be extracted")


@pytest.mark.slow
def test_scenario_25_a_bulk_index_at_the_measured_backlog_is_not_refused_and_does_not_expire(
    db: Any, monkeypatch: pytest.MonkeyPatch, record_property: Any,
) -> None:
    """Test Plan scenario 25: out the measured backlog (nexus-5r2sc.1.3 peaked at
    1,200 documents in flight; the proposed sizing is that against the
    template's ``max_live_rows`` of 10,000) through the production batch producer.

    Heavy tier: ``slow`` keeps it out of the default suite, and it runs by name,
    ``uv run pytest -m slow tests/test_rdr235_mvv_aspect_queue_tuples.py -k scenario_25``.
    The producer is called with 16-row batches, the onnx-local flush cap that
    nexus-5r2sc.1.3 measured. The documents are not in the catalog, so the
    worker acks each as dropped: this scenario is about the queue (capacity,
    retention, drain), and the journey above covers what a worker does with a
    document.
    """
    monkeypatch.delenv("NX_ASPECT_QUEUE", raising=False)  # the default is tuples
    ex = _Extractor(db)
    ex.pace = 0.0
    ex.install(monkeypatch)
    n_tasks = 1200
    coll = _register_collection("s25")

    registry = db.tuples.registry()
    template = next(t for t in registry["templates"] if t["name"] == "aspects")
    cap = template["max_live_rows"]
    assert cap == 10000, "the template this scenario is sized against"

    rows = [
        {"collection": coll, "source_path": f"/nx235/doc-{i:04d}.md", "doc_id": f"1.7.{i}", "content_hash": "h"}
        for i in range(n_tasks)
    ]
    written = 0
    t0 = time.perf_counter()
    for start in range(0, n_tasks, 16):
        written += db.aspect_queue.enqueue_many(rows[start:start + 16])  # raises on MaxLiveRowsExceeded
    producer_s = time.perf_counter() - t0
    assert written == n_tasks, "no out was refused"
    peak = db.tuples.subspace_stats(_SUB)
    live = peak.available + peak.claimed + peak.dead
    print(
        f"SCENARIO25 producer: {n_tasks} tasks in {producer_s:.2f}s "
        f"({producer_s / n_tasks * 1000:.2f} ms/task); live rows {live} of cap {cap}"
    )
    record_property("producer_wall_s", round(producer_s, 3))
    assert peak.available == n_tasks and live <= cap

    expiries: list[datetime] = []
    after: tuple[str, str] | None = None
    while True:
        page = db.tuples.rd(_SUB, dict(_KEYS), n=300, since=after)
        if not page:
            break
        expiries.extend(_ts(t.expires_at) for t in page)
        after = (page[-1].created_at, page[-1].id)
    assert len(expiries) == n_tasks

    worker = AspectExtractionWorker(
        poll_interval=0.2, batch_size=5,
        tuple_options=_worker_opts(lease_s=60, renew_every_s=10),
    )
    t1 = time.perf_counter()
    worker.start()
    try:
        _wait(lambda: db.tuples.subspace_stats(_SUB).consumed == n_tasks, "the backlog to drain", timeout=600)
    finally:
        worker.stop(timeout=30)
    drain_s = time.perf_counter() - t1
    done = db.tuples.subspace_stats(_SUB)
    drained_at = datetime.now(timezone.utc)
    print(f"SCENARIO25 drain: {n_tasks} tasks in {drain_s:.1f}s ({n_tasks / drain_s:.1f}/s)")
    record_property("drain_wall_s", round(drain_s, 1))
    assert (done.available, done.claimed, done.dead, done.expired_unpurged) == (0, 0, 0, 0), done
    assert min(expiries) > drained_at + timedelta(days=89), "a task would expire inside retention's 90 days"
    assert ex.order == [], "every task was acked as dropped: no extraction ran"
