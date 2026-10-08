# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Collection counts live 15 minutes, collection names stay on the 60 s clock (Sam, 2026-10-08).

A default MCP search sizes its fan-out from per-collection chunk counts, and the counts come from
the full ``GET /v1/vectors/stats`` (about 380 ms locally, 0.73 s on the managed service). With the
counts on the 60 s clock an interactive search paid that on every call spaced more than a minute
from the last (measured 724 ms cold, 209 ms warm, 588 ms after a 65 s wait). These pin: the full
listing is not requested inside the counts window, it is requested after it, the names/rows
routing refresh keeps its own 60 s clock and does not discard the counts, an in-process write
drops the counts, a non-default tenant's listing never marks the counts fresh, and a caller that
shows sizes (``store_list``) can still ask for a tighter bound.
"""
from __future__ import annotations

from typing import Any

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.db.http_vector_client import HttpVectorClient

_LIVE = "code__own__bge-base-en-v15-768__v1"
_ATTRS = {"content_type": "code", "owner_id": "own", "embedding_model": "bge-base-en-v15-768", "lifecycle_state": "live"}
_ROUTING_ROWS = [{"name": _LIVE, **_ATTRS}]
_FULL_ROWS = [{"name": _LIVE, "dim": 768, "count": 10, "stored_count": 10, **_ATTRS}]

_FULL = "/v1/vectors/stats"
_ROUTING = "/v1/vectors/stats?fields=routing"


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def world(monkeypatch):
    """A fake engine, a fake clock, and a real client as the process's T3; returns (seen, clock, state)."""
    seen: list[str] = []
    state = {"count": 10}

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        seen.append(path)
        if "fields=routing" in path:
            return [dict(r) for r in _ROUTING_ROWS]
        return [{**r, "count": state["count"]} for r in _FULL_ROWS]

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    clock = _Clock()
    monkeypatch.setattr(mcp_infra, "_now", clock)
    client = HttpVectorClient()
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: client)
    # list_collections() primes the cache itself for the process-default tenant; keep that out of the
    # way so the assertions are about what the cache's own refresh asks for.
    monkeypatch.setattr("nexus.db.http_vector_client._process_default_tenant", lambda: "not-this-tenant")
    mcp_infra.invalidate_collections_cache()
    yield seen, clock, state
    mcp_infra.invalidate_collections_cache()


def _search_routing(client: HttpVectorClient) -> list[str]:
    """What a default MCP search does first: size the fan-out of a bare prefix."""
    from nexus.mcp import core

    return core._resolve_corpus_target("code,code", client)


def test_the_counts_ttl_is_fifteen_minutes_and_names_stay_at_sixty_seconds():
    assert mcp_infra._COLLECTION_COUNTS_TTL == 15 * 60.0
    assert mcp_infra._COLLECTIONS_CACHE_TTL == 60.0


def test_a_second_search_inside_the_window_asks_for_no_full_listing(world):
    seen, clock, _ = world
    client = mcp_infra.get_t3()
    _search_routing(client)
    assert seen == [_FULL], "the cold search pays the one full listing"
    seen.clear()

    clock.now += 5 * 60  # five minutes: far past the names clock, far inside the counts clock
    _search_routing(client)
    assert _FULL not in seen, "counts are served from the cache for fifteen minutes"
    assert seen == [_ROUTING], "names and rows still refresh on their own clock, from the catalog-only listing"


def test_a_search_inside_sixty_seconds_makes_no_request_at_all(world):
    seen, clock, _ = world
    client = mcp_infra.get_t3()
    _search_routing(client)
    seen.clear()
    clock.now += 30
    _search_routing(client)
    assert seen == []


def test_a_search_after_the_window_refetches_the_full_listing_once(world):
    seen, clock, state = world
    client = mcp_infra.get_t3()
    assert mcp_infra.get_collection_counts() == {_LIVE: 10}
    seen.clear()

    state["count"] = 2  # another process shrank it; only a refetch can see that
    clock.now += 15 * 60 + 1
    _search_routing(client)
    assert seen.count(_FULL) == 1
    assert mcp_infra.get_collection_counts() == {_LIVE: 2}
    assert seen.count(_FULL) == 1, "and the fresh counts are served from the cache again"


def test_a_names_refresh_does_not_discard_the_counts(world):
    seen, clock, _ = world
    assert mcp_infra.get_collection_counts() == {_LIVE: 10}
    seen.clear()
    clock.now += 120
    assert mcp_infra.get_collection_names() == [_LIVE]
    assert seen == [_ROUTING]
    assert mcp_infra.get_collection_counts() == {_LIVE: 10}
    assert seen == [_ROUTING], "the routing refresh carried the counts forward"


def test_an_in_process_write_invalidates_the_counts(world):
    seen, clock, state = world
    assert mcp_infra.get_collection_counts() == {_LIVE: 10}
    seen.clear()

    state["count"] = 11
    mcp_infra.invalidate_collections_cache()  # what store_put, store_delete and a new registration call
    assert mcp_infra.get_collection_counts() == {_LIVE: 11}
    assert seen.count(_FULL) == 1


def test_store_put_and_store_delete_call_the_invalidator():
    """The write sites that make this safe: pin that the MCP tools reach the invalidator (the
    behavioural pins are test_store_put_invalidates_collections_cache in test_mcp_server.py)."""
    import inspect

    from nexus.mcp import core

    for fn in (core.store_put, core.store_delete):
        assert "_invalidate_collections_cache()" in inspect.getsource(fn), fn.__name__


def test_a_caller_that_shows_sizes_can_ask_for_a_tighter_bound(world):
    seen, clock, state = world
    assert mcp_infra.get_collection_counts(max_age=60) == {_LIVE: 10}
    seen.clear()
    state["count"] = 12
    clock.now += 61
    assert mcp_infra.get_collection_counts() == {_LIVE: 10}, "the default bound still serves the cache"
    assert mcp_infra.get_collection_counts(max_age=60) == {_LIVE: 12}, "a 60 s bound refetches"
    assert seen.count(_FULL) == 1


def test_store_list_asks_for_the_sixty_second_bound(monkeypatch):
    """store_list prints the sizes, so it keeps the old 60 s freshness while the fan-out floor gets 15 minutes."""
    from nexus.mcp import core

    asked: list[Any] = []
    monkeypatch.setattr(core, "_get_collection_counts", lambda **kw: asked.append(kw) or {})
    monkeypatch.setattr(core, "_get_t3", lambda: object())
    monkeypatch.setattr(core, "_read_scope", lambda t3, collection: ["knowledge__a", "knowledge__b"])
    core.store_list()
    assert asked == [{"max_age": mcp_infra._COLLECTIONS_CACHE_TTL}]


def test_a_non_default_tenants_listing_never_marks_the_counts_fresh(monkeypatch):
    """The cache has no tenant partition, so only the process's own tenant may prime it (the
    names/rows rule); the counts ride the same gate."""
    seen: list[str] = []

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        seen.append(path)
        return [{**r, "count": 10} for r in _FULL_ROWS]

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    mcp_infra.invalidate_collections_cache()
    try:
        HttpVectorClient(tenant="tenant-a").list_collections()
        assert mcp_infra._collections_cache[1] == {}
        assert mcp_infra._collections_counts_ts == 0.0

        HttpVectorClient().list_collections()  # the process-default tenant primes it
        assert mcp_infra._collections_cache[1] == {_LIVE: 10}
        assert mcp_infra._collections_counts_ts > 0.0
    finally:
        mcp_infra.invalidate_collections_cache()
