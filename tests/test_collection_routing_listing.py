# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-mz9jv: the routing listing, ``GET /v1/vectors/stats?fields=routing``.

A router (corpus resolution, sibling lookup, the collection-row cache, ``nx search``'s
``--corpus`` resolver) reads a collection's name and registry row, never its size. The full stats
route makes the engine count every collection's live chunks; the routing form reads the catalog
alone. These pin the client half: the parameter reaches the wire only when asked, the rows come
back without counts, an engine that ignores the parameter still gives a complete answer, and the
process cache that sizes a fan-out asks for counts lazily.

The engine half (population, filters, the route) is ``CollectionListingRoutingIntegrationTest`` (Java).
"""
from __future__ import annotations

from typing import Any

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.db.http_vector_client import (
    HttpVectorClient,
    VectorServiceError,
    live_collection_rows,
)

_LIVE = "code__own__bge-base-en-v15-768__v1"
_QUAR = "quarantine-code__own__bge-base-en-v15-768__v1"
_ATTRS = {"content_type": "code", "owner_id": "own", "embedding_model": "bge-base-en-v15-768"}

#: What the new engine answers to fields=routing (no liveness figure), unfiltered.
_ROUTING_ROWS = [
    {"name": _LIVE, **_ATTRS, "lifecycle_state": "live"},
    {"name": _QUAR, **_ATTRS, "lifecycle_state": "quarantine"},
]
#: What the full stats route answers (and an old engine answers to fields=routing).
_FULL_ROWS = [
    {"name": _LIVE, "dim": 768, "count": 10, "stored_count": 12, **_ATTRS, "lifecycle_state": "live"},
    {"name": _QUAR, "dim": 768, "count": 0, "stored_count": 4, **_ATTRS, "lifecycle_state": "quarantine"},
]


def _engine(monkeypatch, *, serves_routing: bool) -> list[str]:
    seen: list[str] = []

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        seen.append(path)
        routing = "fields=routing" in path
        rows = _ROUTING_ROWS if (routing and serves_routing) else _FULL_ROWS
        if "lifecycle_state=live" in path:
            rows = [r for r in rows if r["lifecycle_state"] == "live"]
        return [dict(r) for r in rows]

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    return seen


@pytest.fixture(autouse=True)
def _clean_cache():
    mcp_infra.invalidate_collections_cache()
    yield
    mcp_infra.invalidate_collections_cache()


class TestTheClient:
    def test_routing_is_on_the_wire_only_when_asked(self, monkeypatch):
        seen = _engine(monkeypatch, serves_routing=True)
        c = HttpVectorClient()
        c.collection_stats()
        c.collection_stats("live")
        c.collection_stats(routing=True)
        c.collection_stats("live", routing=True)
        assert seen == [
            "/v1/vectors/stats",
            "/v1/vectors/stats?lifecycle_state=live",
            "/v1/vectors/stats?fields=routing",
            "/v1/vectors/stats?fields=routing&lifecycle_state=live",
        ]

    def test_routing_rows_carry_the_registry_row_and_no_size(self, monkeypatch):
        _engine(monkeypatch, serves_routing=True)
        monkeypatch.setattr(mcp_infra, "prime_collections_cache", lambda rows: None)
        rows = HttpVectorClient().list_collections(routing=True)
        assert [r["name"] for r in rows] == [_LIVE, _QUAR]
        for r in rows:
            assert {"content_type", "owner_id", "embedding_model", "lifecycle_state"} <= set(r)
            assert not {"count", "stored_count", "dim", "last_write"} & set(r), "no size is invented"

    def test_an_engine_that_ignores_the_parameter_gives_the_full_rows_with_counts(self, monkeypatch):
        seen = _engine(monkeypatch, serves_routing=False)
        monkeypatch.setattr(mcp_infra, "prime_collections_cache", lambda rows: None)
        rows = HttpVectorClient().list_collections(routing=True)
        assert [(r["name"], r["count"], r["stored_count"]) for r in rows] == [(_LIVE, 10, 12), (_QUAR, 0, 4)]
        assert len(seen) == 1, "answered in one request, no second round trip"

    def test_the_full_listing_is_unchanged(self, monkeypatch):
        seen = _engine(monkeypatch, serves_routing=True)
        monkeypatch.setattr(mcp_infra, "prime_collections_cache", lambda rows: None)
        rows = HttpVectorClient().list_collections()
        assert seen == ["/v1/vectors/stats"]
        assert [(r["name"], r["count"]) for r in rows] == [(_LIVE, 10), (_QUAR, 0)]

    def test_the_live_routing_view_filters_and_asks_for_the_routing_form(self, monkeypatch):
        seen = _engine(monkeypatch, serves_routing=True)
        monkeypatch.setattr(mcp_infra, "prime_collections_cache", lambda rows: None)
        rows = live_collection_rows(HttpVectorClient(), routing=True)
        assert [r["name"] for r in rows] == [_LIVE]
        assert seen == ["/v1/vectors/stats?fields=routing&lifecycle_state=live"]
        # Without the flag the live listing keeps its counts: taxonomy discovery's size floor reads them.
        seen.clear()
        full = live_collection_rows(HttpVectorClient())
        assert seen == ["/v1/vectors/stats?lifecycle_state=live"]
        assert full[0]["count"] == 10

    def test_a_404_is_the_full_listings_own_fallback(self, monkeypatch):
        calls: list[str] = []

        def get(path: str, *, tenant: str = "default") -> Any:
            calls.append(path)
            if path.startswith("/v1/vectors/stats"):
                raise VectorServiceError("gone", code=404)
            return [{"name": _LIVE}]

        monkeypatch.setattr("nexus.db.http_vector_client._get", get)
        c = HttpVectorClient()
        monkeypatch.setattr(c, "count", lambda name: 3)
        monkeypatch.setattr(mcp_infra, "prime_collections_cache", lambda rows: None)
        assert [r["name"] for r in c.list_collections(routing=True)] == [_LIVE]
        assert calls == ["/v1/vectors/stats?fields=routing", "/v1/vectors/collections"]

    def test_a_non_404_failure_degrades_to_empty_unless_strict(self, monkeypatch):
        def get(path: str, *, tenant: str = "default") -> Any:
            raise VectorServiceError("boom", code=503)

        monkeypatch.setattr("nexus.db.http_vector_client._get", get)
        c = HttpVectorClient()
        assert c.list_collections(routing=True) == []
        with pytest.raises(VectorServiceError):
            c.list_collections(routing=True, strict=True)


class TestTheProcessCache:
    """``mcp_infra``'s collection cache is filled from the routing form and fetches counts on demand."""

    def _client(self, monkeypatch, *, serves_routing: bool = True):
        seen = _engine(monkeypatch, serves_routing=serves_routing)
        client = HttpVectorClient()
        monkeypatch.setattr(mcp_infra, "get_t3", lambda: client)
        # list_collections() primes the cache itself for the process-default tenant; the
        # assertions here are about what the REFRESH asks for, so keep that out of the way.
        monkeypatch.setattr("nexus.db.http_vector_client._process_default_tenant", lambda: "not-this-tenant")
        return seen

    def test_names_and_rows_come_from_the_routing_listing_with_no_counts_fetched(self, monkeypatch):
        seen = self._client(monkeypatch)
        assert mcp_infra.get_collection_names() == [_LIVE, _QUAR]
        row = mcp_infra.get_collection_row(_QUAR)
        assert row is not None and row["lifecycle_state"] == "quarantine"
        assert mcp_infra.get_live_collection_names() == [_LIVE]
        assert seen == ["/v1/vectors/stats?fields=routing"]

    def test_counts_are_fetched_once_when_asked_and_the_cache_becomes_complete(self, monkeypatch):
        seen = self._client(monkeypatch)
        mcp_infra.get_collection_names()
        assert mcp_infra.get_collection_counts() == {_LIVE: 10, _QUAR: 0}
        assert seen == ["/v1/vectors/stats?fields=routing", "/v1/vectors/stats"]
        # Served from the cache now: no third request for either reader.
        assert mcp_infra.get_collection_counts() == {_LIVE: 10, _QUAR: 0}
        assert mcp_infra.get_collection_names() == [_LIVE, _QUAR]
        assert len(seen) == 2

    def test_counts_asked_first_on_a_cold_cache_cost_one_full_request(self, monkeypatch):
        """nexus-mz9jv review M3: a cold cache that is about to be asked for sizes goes straight to the
        full listing (one request, names and rows included), not routing first and full second."""
        seen = self._client(monkeypatch)
        assert mcp_infra.get_collection_counts() == {_LIVE: 10, _QUAR: 0}
        assert seen == ["/v1/vectors/stats"]
        assert mcp_infra.get_collection_names() == [_LIVE, _QUAR]
        assert len(seen) == 1, "the full listing filled the names too"

    def test_the_default_bare_prefix_fanout_is_one_stats_request_on_a_cold_cache(self, monkeypatch):
        """The caller of the above (``_resolve_corpus_target``, a bare-prefix corpus such as the default
        knowledge,code,docs): it reads names first, then sizes per bare prefix, and used to pay routing + full.
        (``code`` is the prefix the fake tenant holds; an unmatched prefix takes resolve_corpus's own bounded
        refresh, which is not what this pins.)"""
        from nexus.mcp import core

        seen = self._client(monkeypatch)
        core._resolve_corpus_target("code,code", mcp_infra.get_t3())
        assert seen == ["/v1/vectors/stats"]

    def test_the_cache_and_its_counts_flag_are_installed_together_under_the_lock(self):
        """review L4: the (cache, counts-loaded) pair is one atomic assignment. A writer that does not
        take ``_collections_cache_lock`` completes while another thread holds it."""
        import threading

        done = threading.Event()

        def writer():
            mcp_infra.prime_collections_cache(_FULL_ROWS)
            done.set()

        with mcp_infra._collections_cache_lock:
            t = threading.Thread(target=writer)
            t.start()
            assert not done.wait(0.3), "the cache install did not wait for the lock"
        t.join(5)
        assert done.is_set()

    def test_an_engine_that_ignores_the_parameter_fills_the_counts_in_the_first_request(self, monkeypatch):
        seen = self._client(monkeypatch, serves_routing=False)
        assert mcp_infra.get_collection_counts() == {_LIVE: 10, _QUAR: 0}
        assert seen == ["/v1/vectors/stats"], "counts were asked for, so the first request is the full one"

    def test_an_engine_that_ignores_the_parameter_serves_names_then_counts_in_one_request(self, monkeypatch):
        seen = self._client(monkeypatch, serves_routing=False)
        assert mcp_infra.get_collection_names() == [_LIVE, _QUAR]
        assert mcp_infra.get_collection_counts() == {_LIVE: 10, _QUAR: 0}
        assert seen == ["/v1/vectors/stats?fields=routing"], "the full rows already carried the counts"

    def test_a_handle_that_is_not_the_real_client_gets_the_full_listing(self, monkeypatch):
        class _Double:
            calls = 0

            def list_collections(self, *args, **kwargs):
                _Double.calls += 1
                assert not args and not kwargs, "a double has no routing form to ask for"
                return [{"name": "x", "count": 4}]

        monkeypatch.setattr(mcp_infra, "get_t3", lambda: _Double())
        assert mcp_infra.get_collection_counts() == {"x": 4}
        assert _Double.calls == 1
