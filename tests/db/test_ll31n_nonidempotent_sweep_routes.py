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

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import httpx
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

    # nexus-oq5gd: three more mutating calls that retried blind on a read
    # timeout (delete / merge / ghost sweep).
    def test_delete_collection(self, client: HttpCatalogClient) -> None:
        with patch.object(client, "_post", return_value={"deleted": {}}) as m:
            client.delete_collection("code__x")
        _assert_idempotent_false(m)

    def test_merge_documents(self, client: HttpCatalogClient) -> None:
        with patch.object(client, "_post", return_value={}) as m:
            client.merge_documents("1.1.1", "1.1.2")
        _assert_idempotent_false(m)

    def test_ghost_sweep(self, client: HttpCatalogClient) -> None:
        with patch.object(client, "_post", return_value={}) as m:
            client.ghost_sweep(dry_run=False)
        _assert_idempotent_false(m)


class _StallingPostServer:
    """Real socket server: every POST is counted, then stalls past the
    client's read timeout. Lets the real ``_send`` retry loop run."""

    def __init__(self) -> None:
        self.posts: list[str] = []
        self._release = threading.Event()
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer.posts.append(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer._release.wait(timeout=5.0)

            def log_message(self, fmt, *args):  # noqa: A002
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.port: int = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_StallingPostServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()


class TestCatalogClientReadTimeoutOverRealTransport:
    """nexus-oq5gd: over a real socket, a ReadTimeout on a mutating sweep
    must cost exactly ONE POST. ``patch.object(_post)`` above proves the flag
    is threaded; this proves ``_send`` honours it end to end."""

    _CALLS = {
        "delete_collection": lambda c: c.delete_collection("code__x"),
        "merge_documents": lambda c: c.merge_documents("1.1.1", "1.1.2"),
        "ghost_sweep": lambda c: c.ghost_sweep(dry_run=False),
    }

    @staticmethod
    def _client(srv: _StallingPostServer, monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
        # Endpoint and token come from the environment, NOT constructor
        # pins: a fully pinned client raises "cannot self-heal" instead of
        # resending, which would hide a blind retry behind a count of 1.
        monkeypatch.setenv("NX_SERVICE_HOST", "127.0.0.1")
        monkeypatch.setenv("NX_SERVICE_PORT", str(srv.port))
        monkeypatch.setenv("NX_SERVICE_TOKEN", _TOKEN)
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        return HttpCatalogClient(client=httpx.Client(timeout=0.3))

    @pytest.mark.parametrize("name", sorted(_CALLS))
    def test_read_timeout_is_not_retried(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with _StallingPostServer() as srv:
            client = self._client(srv, monkeypatch)
            with pytest.raises(httpx.ReadTimeout):
                self._CALLS[name](client)
            assert len(srv.posts) == 1, srv.posts

    def test_control_idempotent_post_does_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-vacuity: the same server and client DO see two POSTs for a
        default (idempotent) call, so a count of 1 above means the flag
        worked, not that retries cannot be observed here."""
        with _StallingPostServer() as srv:
            client = self._client(srv, monkeypatch)
            with pytest.raises(httpx.ReadTimeout):
                client._post("/update", {"tumbler": "1.1.1"})
            assert len(srv.posts) == 2, srv.posts


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
