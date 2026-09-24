# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ll31n sibling (T2 review-wave2-daemon-2026-09-23): every mutating
sweep/delete/purge route whose server-side operation discovers its own
population via a predicate (age, similarity threshold, collection-name
match) rather than a caller-supplied id set must pass ``idempotent=False``
to the RefreshableHttpStoreMixin transport.

Why: the SAME misreport class the original nexus-ll31n incident found on
``gc_quarantine_orphans`` (a completed 41,032-row move read as a failure)
applies to every route below. A lost RESPONSE after a real, committed
sweep means the default ``idempotent=True`` gateway-retry loop
(``_once_with_gateway_retry``) resends the SAME request; the resend's own
predicate re-evaluates against the now-already-swept state and finds a
different, usually much smaller (often zero) population, reporting that
as the outcome instead of what attempt 1 actually did.

The underlying MECHANISM (``idempotent=False`` disables the gateway-retry
and connection-error re-resolve loops, keeping only the 401-remint
carve-out) is already exhaustively tested generically at the shared layer
in ``tests/db/test_refreshable_client.py``. These tests are narrower and
call-site-scoped: each one proves the SPECIFIC method under test actually
threads ``idempotent=False`` through to ``self._post``, using
``unittest.mock.patch.object`` on the store instance rather than a live
server or transport fake -- the flag's effect is someone else's test; its
presence at each call site is this file's job.

Routes deliberately left OUT of this sweep, and why:
- ``HttpCatalogClient.delete_many`` / ``purge_assignments_for_doc`` /
  ``purge_manifest_for_doc``: caller-supplied-id (or single-doc) scoped,
  not a discovered population -- a retry re-targets the SAME known ids,
  which is the ordinary idempotent-upsert/delete shape this mixin's
  default already exists for.
- ``HttpMemoryStore.mark_done`` / ``mark_failed`` / ``restore``,
  ``HttpAspectQueue.mark_done`` / ``mark_failed``: single-row,
  caller-known-target state transitions, not sweeps.
- ``nexus/db/t2/http_tuple_store.py`` (the RDR-205 tuple space): every
  route there is a DOCUMENTED design-of-record exception (the module's
  own top-of-file docstring) -- ``in``/``inp``'s crash-after-claim
  ambiguity is already covered by the lease + sweep mechanism, so
  retrying is deliberately safe by design, unlike ``HttpAspectQueue``'s
  claim verbs.
- ``HttpVectorClient``'s GC routes (``gc_quarantine_orphans`` /
  ``gc_restore_rereferenced`` / ``gc_expire_quarantine``): fixed in the
  original nexus-ll31n commit via a different mechanism
  (``nexus.db.gateway_backoff.is_non_idempotent_sweep_path``, a
  path-suffix check) -- that client is urllib-based, not a
  RefreshableHttpStoreMixin adopter, so it has no ``idempotent=`` kwarg
  to thread; see ``tests/db/test_http_vector_client.py::
  TestNonIdempotentSweepNeverAutoRetries`` for its own coverage.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.db.t2.http_aspect_queue import HttpAspectQueue
from nexus.db.t2.http_centroid_store import HttpCentroidStore
from nexus.db.t2.http_memory_store import HttpMemoryStore
from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore
from nexus.db.t2.http_telemetry_store import HttpTelemetryStore

_BASE_URL = "http://fake-svc"
_TOKEN = "fake-token"


def _assert_idempotent_false(mock_post) -> None:
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs.get("idempotent") is False, (
        f"expected idempotent=False, got call_args={mock_post.call_args!r}"
    )


class TestCatalogClientSweeps:
    @pytest.fixture()
    def client(self):
        c = HttpCatalogClient(base_url=_BASE_URL, _token=_TOKEN)
        yield c
        c.close()

    def test_purge_trash(self, client: HttpCatalogClient) -> None:
        with patch.object(client, "_post", return_value={}) as m:
            client.purge_trash(older_than_days=7, dry_run=False)
        _assert_idempotent_false(m)

    def test_rename_collection_cascade(self, client: HttpCatalogClient) -> None:
        with patch.object(client, "_post", return_value={"renamed": {}}) as m:
            client.rename_collection_cascade("old", "new")
        _assert_idempotent_false(m)


class TestCentroidStoreSweeps:
    @pytest.fixture()
    def store(self):
        s = HttpCentroidStore(base_url=_BASE_URL, _token=_TOKEN)
        yield s
        s.close()

    def test_purge(self, store: HttpCentroidStore) -> None:
        with patch.object(store, "_post", return_value={"deleted": 0}) as m:
            store.purge("collection-x")
        _assert_idempotent_false(m)


class TestTaxonomyStoreSweeps:
    @pytest.fixture()
    def store(self):
        s = HttpTaxonomyStore(base_url=_BASE_URL, _token=_TOKEN)
        yield s
        s.close()

    def test_prune_projection_below(self, store: HttpTaxonomyStore) -> None:
        with patch.object(store, "_post", return_value={"removed": 0}) as m:
            store.prune_projection_below("code__", 0.5)
        _assert_idempotent_false(m)

    def test_purge_collection(self, store: HttpTaxonomyStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.purge_collection("collection-x")
        _assert_idempotent_false(m)


class TestMemoryStoreSweeps:
    @pytest.fixture()
    def store(self):
        s = HttpMemoryStore(base_url=_BASE_URL, _token=_TOKEN)
        yield s
        s.close()

    def test_expire(self, store: HttpMemoryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.expire()
        _assert_idempotent_false(m)

    def test_reap(self, store: HttpMemoryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.reap()
        _assert_idempotent_false(m)


class TestTelemetryStoreSweeps:
    @pytest.fixture()
    def store(self):
        s = HttpTelemetryStore(base_url=_BASE_URL, _token=_TOKEN)
        yield s
        s.close()

    def test_expire_relevance_log(self, store: HttpTelemetryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.expire_relevance_log(days=90)
        _assert_idempotent_false(m)

    def test_trim_search_telemetry(self, store: HttpTelemetryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.trim_search_telemetry(days=30)
        _assert_idempotent_false(m)

    def test_trim_hook_failures(self, store: HttpTelemetryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.trim_hook_failures(days=30)
        _assert_idempotent_false(m)

    def test_trim_index_failures(self, store: HttpTelemetryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.trim_index_failures(days=30)
        _assert_idempotent_false(m)

    def test_trim_capability_census(self, store: HttpTelemetryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.trim_capability_census(days=30)
        _assert_idempotent_false(m)

    def test_trim_routing_events(self, store: HttpTelemetryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.trim_routing_events(days=30)
        _assert_idempotent_false(m)

    def test_rename_collection(self, store: HttpTelemetryStore) -> None:
        with patch.object(store, "_post", return_value={}) as m:
            store.rename_collection(old="old", new="new")
        _assert_idempotent_false(m)


class TestAspectQueueSweeps:
    @pytest.fixture()
    def queue(self):
        q = HttpAspectQueue(base_url=_BASE_URL, _token=_TOKEN)
        yield q
        q.close()

    def test_reclaim_stale(self, queue: HttpAspectQueue) -> None:
        with patch.object(queue, "_post", return_value={"reclaimed": 0}) as m:
            queue.reclaim_stale(timeout_seconds=300)
        _assert_idempotent_false(m)
