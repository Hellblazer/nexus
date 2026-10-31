# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-235 F18 (bead nexus-5r2sc.3.5): ``HttpAspectQueue.tuple_counts``, the one
reader of the ``aspects`` subspace census that ``nx doctor`` and the console
share.

A subspace that does not exist is zero; any other failure propagates (UNKNOWN is
the caller's call). The three-way split the diagnostics rely on (available is
pending, claimed is in flight, dead is failed) is pinned against the real engine
in step 6 of ``tests/test_http_aspect_queue_tuples_producer.py``'s producer
journey (nexus-9edxe folded it there: it needs a tenant, and the journey has one).
"""
from __future__ import annotations

import pytest

import nexus.corpus as corpus
from nexus.db.t2.http_aspect_queue import (
    ASPECT_QUEUE_ENV,
    AspectTupleCounts,
    HttpAspectQueue,
)
from nexus.db.t2.http_tuple_store import HttpTupleStore, UnknownSubspaceError

# The tenant is opt-in (tests/AGENTS.md): the two failure-shape tests stub ``subspace_stats``
# and mint none.


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


def _offline_queue() -> HttpAspectQueue:
    """A queue with an explicit endpoint, so building it resolves nothing; the
    tests that use it replace ``subspace_stats`` and send no request."""
    return HttpAspectQueue(base_url="http://mock.test", _token="test-token")


def test_a_missing_subspace_is_zero_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _offline_queue()

    def refuse(self, subspace):
        raise UnknownSubspaceError("no template for aspects")

    monkeypatch.setattr(HttpTupleStore, "subspace_stats", refuse)
    assert queue.tuple_counts() == AspectTupleCounts(0, 0, 0)


def test_any_other_failure_propagates_so_the_caller_can_say_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    queue = _offline_queue()

    def down(self, subspace):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(HttpTupleStore, "subspace_stats", down)
    with pytest.raises(httpx.ConnectError):
        queue.tuple_counts()
