# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 F18 (bead nexus-5r2sc.3.5): ``HttpAspectQueue.tuple_counts``, the one
reader of the ``aspects`` subspace census that ``nx doctor`` and the console
share, against the real engine substrate.

Pins the three-way split the diagnostics rely on: available is pending, claimed
is in flight (neither pending nor failed), dead is failed; a subspace that does
not exist is zero; any other failure propagates (UNKNOWN is the caller's call).
"""
from __future__ import annotations

import uuid

import pytest

import nexus.corpus as corpus
from nexus.db.t2.http_aspect_queue import (
    ASPECT_QUEUE_ENV,
    ASPECTS_KEYS,
    ASPECTS_SUBSPACE,
    AspectTupleCounts,
    HttpAspectQueue,
)
from nexus.db.t2.http_tuple_store import HttpTupleStore, UnknownSubspaceError

pytestmark = pytest.mark.usefixtures("t2_service_env")


def _coll() -> str:
    return f"docs__nx235c-{uuid.uuid4().hex[:8]}__bge-base-en-v15-768__v1"


def _tumbler() -> str:
    return f"1.{uuid.uuid4().int % 10**6}.{uuid.uuid4().int % 10**6}"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch: pytest.MonkeyPatch):
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()
    monkeypatch.setenv("NX_ASPECT_WORKER_AUTOSTART", "0")
    monkeypatch.setattr("nexus.aspect_worker.signal_aspect_wake", lambda *a, **k: None)
    monkeypatch.setenv(ASPECT_QUEUE_ENV, "tuples")
    yield
    corpus._REGISTERED_COLLECTIONS.clear()
    corpus._REGISTERED_COLLECTIONS_SCOPED.clear()


@pytest.fixture
def queue():
    q = HttpAspectQueue()
    yield q
    q.close()


def test_an_empty_queue_is_zero_on_every_side(queue: HttpAspectQueue) -> None:
    assert queue.tuple_counts() == AspectTupleCounts(0, 0, 0)


def test_available_claimed_and_dead_are_three_different_counts(queue: HttpAspectQueue) -> None:
    coll = _coll()
    for i in range(4):
        queue.enqueue(coll, f"/p/{i}.md", doc_id=_tumbler())
    store = HttpTupleStore()
    try:
        # One task in flight, one dead-lettered; two stay available.
        (_row, in_flight) = store.inp(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), claimant="c1", lease_s=600)
        (_row, doomed) = store.inp(ASPECTS_SUBSPACE, dict(ASPECTS_KEYS), claimant="c2", lease_s=600)
        store.nack(doomed, "c2", dead=True)
        counts = queue.tuple_counts()
    finally:
        store.close()
    assert counts == AspectTupleCounts(available=2, claimed=1, dead=1), (
        f"claimed is in flight, not pending; dead is failed: {counts}, claim {in_flight}"
    )


def test_a_missing_subspace_is_zero_not_a_failure(
    queue: HttpAspectQueue, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(self, subspace):
        raise UnknownSubspaceError("no template for aspects")

    monkeypatch.setattr(HttpTupleStore, "subspace_stats", refuse)
    assert queue.tuple_counts() == AspectTupleCounts(0, 0, 0)


def test_any_other_failure_propagates_so_the_caller_can_say_unknown(
    queue: HttpAspectQueue, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    def down(self, subspace):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(HttpTupleStore, "subspace_stats", down)
    with pytest.raises(httpx.ConnectError):
        queue.tuple_counts()
