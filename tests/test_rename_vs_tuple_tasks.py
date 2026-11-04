# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""A collection rename against the default (tuples) aspect queue, Python producer and
a real engine (nexus-dm617).

``tests/test_rename_lock_t1_2.py`` and ``tests/test_rename_lock_t2_race.py`` pin the
RDR-138 rename-vs-worker races on the old queue TABLE. Under RDR-235 a rename is a
task-body rewrite inside the engine (``AspectTaskRefs``), and the engine's Java
suite covers that rewrite on its own (``AspectTaskRefsRenameTest``: the three task
states through both routes, a re-enqueue after the rename, a re-embed move, a purge,
a rename racing a fenced WRITE for a claimed task). What it does not run is the
Python producer in the loop: ``HttpAspectQueue.enqueue`` checks the name, then sends
the ``out``, and a rename can land between the two. These tests drive that.

* ``test_rename_rewrites_queued_tasks_and_the_producer_follows_the_new_name`` is one
  deterministic journey over the default producer (``NX_ASPECT_QUEUE`` unset): the
  Gap-3 twin (a completed claim, then the rename), the rewrite of an available, a
  claimed and a dead task, the producer's replace under the new name, the refusal of
  the retired name, and the window itself (rename between the check and the send),
  forced through the one seam between them.
* ``test_concurrent_enqueues_across_a_rename_lose_nothing_and_duplicate_nothing`` is
  the threaded race. Its interleaving is not fixed, so it asserts only what holds in
  every interleaving: one task per accepted document, each naming the old or the
  new collection, and a re-enqueue under the new name converges all of them.

KNOWN RESIDUE, pinned as such (RDR-235 F33: "a 409 needs a rename that lands between
the producer's check and the write"): a task that lands after the rename, from a
producer that checked before it, names the retired collection. The worker's write for
it answers 409 and releases the claim (``tests/test_aspect_worker_tuples.py``, S10);
the producer's next enqueue under the new name replaces it. Both are asserted here.
"""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

import nexus.corpus as corpus
from nexus.collection_errors import SupersededCollectionWriteError
from nexus.db.t2.http_aspect_queue import (
    ASPECT_QUEUE_ENV,
    ASPECTS_KEYS,
    ASPECTS_SUBSPACE,
    HttpAspectQueue,
)
from nexus.db.t2.http_tuple_store import ClaimNotFoundError, HttpTupleStore
from tests._catalog_fixture_ops import register_real_doc_id

pytestmark = pytest.mark.usefixtures("t2_service_env")

_BGE = "bge-base-en-v15-768"
_WORKER = "W:0"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    monkeypatch.delenv(ASPECT_QUEUE_ENV, raising=False)  # the default producer: tuples
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


def _names(label: str) -> tuple[str, str]:
    """(old, new): one owner, the model token and the version of a rename that stays on
    the model (``test_aspect_worker_tuples.py`` S10 uses the same shape)."""
    owner = f"nxdm617{label}-{uuid.uuid4().hex[:8]}"
    return (f"knowledge__{owner}__{_BGE}__v1", f"knowledge__{owner}__{_BGE}__v2")


def _register(name: str) -> None:
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred import

    make_catalog_writer().register_collection(name, **corpus.collection_registration_kwargs(name))


def _rename(old: str, new: str) -> dict[str, int]:
    from nexus.catalog.http_catalog_client import HttpCatalogClient  # noqa: PLC0415 — deferred import

    with HttpCatalogClient() as cat:
        return cat.rename_collection_cascade(old, new)


def _forget_registrations() -> None:
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


def _tumbler() -> str:
    return f"1.{uuid.uuid4().int % 10**6}.{uuid.uuid4().int % 10**6}"


def _tasks(store: HttpTupleStore) -> dict[str, dict[str, Any]]:
    """doc_id -> task body, asserting that no document holds two tasks."""
    out: dict[str, dict[str, Any]] = {}
    for t in store.rd(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), n=300):
        body = json.loads(t.body)
        assert body["doc_id"] not in out, f"two tasks for document {body['doc_id']}"
        out[body["doc_id"]] = body
    return out


def _fields(collection: str, source_path: str, doc_id: str) -> dict[str, Any]:
    return {
        "collection": collection, "source_path": source_path,
        "problem_formulation": "pf", "proposed_method": None,
        "experimental_datasets": [], "experimental_baselines": [], "experimental_results": None,
        "extras": {}, "confidence": 0.9, "extracted_at": "2026-11-04T00:00:00+00:00",
        "model_version": "test-model", "extractor_name": "test", "source_uri": None,
        "doc_id": doc_id, "salient_sentences": [],
    }


def test_rename_rewrites_queued_tasks_and_the_producer_follows_the_new_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One engine state, seven steps, each assertion naming its step."""
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — deferred import

    old, new = _names("a")
    _register(old)
    queue = HttpAspectQueue()
    store = HttpTupleStore()
    db = T2Database(tmp_path / "t2.db")
    try:
        # 1. A completed claim (the Gap-3 twin: complete_aspect holds RENAME_LOCK across the
        # fenced write and the ack), then an available, a claimed and a dead task.
        done = register_real_doc_id(title=f"nxdm617 done {uuid.uuid4().hex[:6]}", physical_collection=old)
        queue.enqueue(old, "/p/done.md", content_hash="h", doc_id=done)
        (_row, claim_id) = store.inp(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), claimant=_WORKER, lease_s=600)
        assert db.complete_aspect(_fields(old, "/p/done.md", done), claim_id=claim_id, claimant=_WORKER) is True
        assert _tasks(store) == {}, "step 1: the completed task is consumed"

        docs = {"claimed": _tumbler(), "dead": _tumbler(), "available": _tumbler()}
        held: dict[str, str] = {}
        for state in ("claimed", "dead"):
            queue.enqueue(old, f"/p/{state}.md", content_hash="h", doc_id=docs[state])
            # one claimant per claim: a claimant that claims again gives its first claim up
            (_row, held[state]) = store.inp(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), claimant=f"{_WORKER}:{state}", lease_s=600)
        store.nack(held["dead"], f"{_WORKER}:dead", dead=True)
        queue.enqueue(old, "/p/available.md", content_hash="h", doc_id=docs["available"])
        census = queue.tuple_counts()
        assert (census.available, census.claimed, census.dead) == (1, 1, 1), f"step 1: {census}"

        # 2. The rename rewrites all three, reports them, and moves the completed aspect row.
        counts = _rename(old, new)
        assert counts.get("aspects_tasks") == 3, f"step 2: the engine reports the rewritten tasks: {counts}"
        held_now = _tasks(store)
        assert {d: b["collection"] for d, b in held_now.items()} == {d: new for d in docs.values()}, (
            "step 2: every unconsumed task names the new collection"
        )
        assert db.document_aspects.get(old, "/p/done.md") is None, "step 2: nothing left under the old name"
        assert db.document_aspects.get(new, "/p/done.md") is not None, "step 2: the aspect row moved"
        census = queue.tuple_counts()
        assert (census.available, census.claimed, census.dead) == (1, 1, 1), (
            f"step 2: the rewrite keeps each task's state: {census}"
        )
        store.ack(held["claimed"], f"{_WORKER}:claimed")  # the holder's claim survived the rewrite
        with pytest.raises(ClaimNotFoundError):
            store.ack(held["claimed"], f"{_WORKER}:claimed")

        # 3. The producer under the new name REPLACES: the document is the identity, not the collection.
        outcomes: list[str] = []
        real = HttpAspectQueue._out_task

        def spy(self: HttpAspectQueue, *a: Any) -> str:
            outcome = real(self, *a)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(HttpAspectQueue, "_out_task", spy)
        for state in ("available", "dead"):
            queue.enqueue(new, f"/p/{state}.md", content_hash="h2", doc_id=docs[state])
        assert outcomes == ["replaced", "replaced"], f"step 3: {outcomes}"
        after = _tasks(store)
        assert sorted(after) == sorted(d for s, d in docs.items() if s != "claimed"), (
            "step 3: still one task per document (the claimed one was acked away in step 2)"
        )
        assert {b["content_hash"] for b in after.values()} == {"h2"}, "step 3: the new body"

        # 4. The retired name is refused before any send.
        _forget_registrations()
        sent: list[tuple] = []
        monkeypatch.setattr(HttpAspectQueue, "_out_task", lambda self, *a: sent.append(a) or "created")
        with pytest.raises(SupersededCollectionWriteError) as refused:
            queue.enqueue(old, "/p/late.md", content_hash="h", doc_id=_tumbler())
        assert refused.value.successor == new
        assert sent == [], "step 4: a retired name sends nothing"
        monkeypatch.setattr(HttpAspectQueue, "_out_task", real)

        # 5. The window: the producer has passed its registered-and-not-retired check, the
        # rename lands, then the out goes. No error reaches the caller, the document holds
        # one task, and it names the retired collection (the pinned residue).
        old2, new2 = _names("b")
        _register(old2)
        racer = _tumbler()
        fired: list[bool] = []

        def rename_then_send(self: HttpAspectQueue, *a: Any) -> str:
            if not fired:
                fired.append(True)
                _rename(old2, new2)
            return real(self, *a)

        monkeypatch.setattr(HttpAspectQueue, "_out_task", rename_then_send)
        queue.enqueue(old2, "/p/racer.md", content_hash="h", doc_id=racer)
        monkeypatch.setattr(HttpAspectQueue, "_out_task", real)
        assert fired == [True], "step 5: the rename ran inside the window"
        assert _tasks(store)[racer]["collection"] == old2, (
            "step 5: residue: the task landed after the rename and names the retired collection"
        )

        # 6. The producer's replace under the new name heals it: one task, the new name, the
        # same id. (A worker that claims it before the heal is released by the 409; that arm is
        # tests/test_aspect_worker_tuples.py S10.)
        queue.enqueue(new2, "/p/racer.md", content_hash="h", doc_id=racer)
        healed = _tasks(store)[racer]
        assert healed["collection"] == new2, "step 6: the replace under the new name converges the task"

        # 7. The whole of it left exactly the tasks the steps account for.
        assert sorted(_tasks(store)) == sorted([docs["available"], docs["dead"], racer]), "step 7"
    finally:
        db.close()
        store.close()
        queue.close()


def test_concurrent_enqueues_across_a_rename_lose_nothing_and_duplicate_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eight producers enqueue 160 documents under the old name while the main thread renames
    the collection once at least eight are queued. The interleaving varies; these hold in all
    of them: every accepted document has exactly one task, no task names a third collection,
    the only error a producer sees is the retired-name refusal, and a pass of re-enqueues
    under the new name leaves every document with one task naming it."""
    old, new = _names("c")
    _register(old)
    queue = HttpAspectQueue()
    store = HttpTupleStore()
    docs = [_tumbler() for _ in range(160)]
    accepted: list[str] = []
    refused: list[str] = []
    unexpected: list[BaseException] = []
    lock = threading.Lock()
    queued_enough = threading.Event()

    def produce(mine: list[str]) -> None:
        for doc in mine:
            try:
                queue.enqueue(old, f"/p/{doc}.md", content_hash="h", doc_id=doc)
                with lock:
                    accepted.append(doc)
                    if len(accepted) >= 8:
                        queued_enough.set()
            except SupersededCollectionWriteError:
                with lock:
                    refused.append(doc)
            except BaseException as exc:  # noqa: BLE001 — asserted empty below
                with lock:
                    unexpected.append(exc)

    threads = [threading.Thread(target=produce, args=(docs[i::8],), name=f"producer-{i}") for i in range(8)]
    try:
        for t in threads:
            t.start()
        assert queued_enough.wait(timeout=60), "no producer got eight documents queued"
        counts = _rename(old, new)
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads), "a producer is still running"

        assert unexpected == [], f"a producer saw an error other than the retired-name refusal: {unexpected}"
        assert sorted(accepted + refused) == sorted(docs), "every document was either accepted or refused"
        assert counts.get("aspects_tasks", 0) >= 8, f"the rename rewrote what had been queued: {counts}"
        held = _tasks(store)
        assert sorted(held) == sorted(accepted), "one task per accepted document, none lost, none extra"
        assert {b["collection"] for b in held.values()} <= {old, new}, "no task names a third collection"
        assert sum(b["collection"] == new for b in held.values()) >= 8, "the tasks queued before the rename follow it"

        for doc in accepted:
            queue.enqueue(new, f"/p/{doc}.md", content_hash="h", doc_id=doc)
        converged = _tasks(store)
        assert sorted(converged) == sorted(accepted), "the convergence pass adds no task"
        assert {b["collection"] for b in converged.values()} == {new}, "and every task names the new collection"
    finally:
        for t in threads:
            t.join(timeout=30)
        store.close()
        queue.close()
