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
    assert mcp_infra.join_fanout_counts_refresh(5)
    # nexus-vpa9q: the cold search leaves the one full listing to a background refresh; its own
    # names read is the routing listing, unless the refresh already installed fresher rows.
    assert seen.count(_FULL) == 1 and set(seen) <= {_ROUTING, _FULL}
    seen.clear()

    clock.now += 5 * 60  # five minutes: far past the names clock, far inside the counts clock
    _search_routing(client)
    assert _FULL not in seen, "counts are served from the cache for fifteen minutes"
    assert seen == [_ROUTING], "names and rows still refresh on their own clock, from the catalog-only listing"


def test_a_search_inside_sixty_seconds_makes_no_request_at_all(world):
    seen, clock, _ = world
    client = mcp_infra.get_t3()
    _search_routing(client)
    assert mcp_infra.join_fanout_counts_refresh(5)
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
    assert mcp_infra.join_fanout_counts_refresh(5)
    assert seen.count(_FULL) == 1, "one background refresh"
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


def test_a_fetch_that_began_before_a_write_does_not_install_its_pre_write_counts(world, monkeypatch):
    # Review of 1d57bb6cd, minor 1: a search's full listing in flight while a store_put
    # invalidates must not install its (pre-write) counts for the next 15 minutes.
    seen, clock, state = world
    client = mcp_infra.get_t3()
    real = client.list_collections

    def racing_list_collections(*args, **kwargs):
        rows = real(*args, **kwargs)              # the engine answered with the pre-write count
        if not kwargs.get("routing"):
            state["count"] = 11                   # a write lands and invalidates mid-fetch
            mcp_infra.invalidate_collections_cache()
        return rows

    monkeypatch.setattr(client, "list_collections", racing_list_collections)
    mcp_infra.get_collection_counts()
    monkeypatch.setattr(client, "list_collections", real)
    assert mcp_infra.get_collection_counts()[_LIVE] == 11


def test_reset_singletons_leaves_no_fresh_counts_behind(world):
    # Review of 1d57bb6cd, minor 2: reset_singletons used to mark the counts loaded with an
    # empty map, which the 15 minute clock would then serve as fresh.
    seen, clock, state = world
    assert mcp_infra.get_collection_counts()[_LIVE] == 10
    mcp_infra.reset_singletons()
    assert not mcp_infra._counts_are_fresh(mcp_infra._COLLECTION_COUNTS_TTL)


# ── nexus-vpa9q: the fan-out floor never waits on the full listing ───────────────────────────

_THIN = "code__thin__bge-base-en-v15-768__v1"
_THIN_ATTRS = {**_ATTRS, "owner_id": "thin"}


@pytest.fixture
def floor_world(monkeypatch):
    """Two code collections, one healthy (10 chunks) and one thin (1). The full listing blocks
    until ``release`` is set, so a caller that waited on it would be visible."""
    import threading

    seen: list[str] = []
    release = threading.Event()
    full_started = threading.Event()

    def fake_get(path: str, *, tenant: str = "default") -> Any:
        seen.append(path)
        rows = [{"name": _LIVE, **_ATTRS}, {"name": _THIN, **_THIN_ATTRS}]
        if "fields=routing" in path:
            return rows
        full_started.set()
        assert release.wait(5), "test never released the full listing"
        return [{**rows[0], "dim": 768, "count": 10, "stored_count": 10},
                {**rows[1], "dim": 768, "count": 1, "stored_count": 1}]

    monkeypatch.setattr("nexus.db.http_vector_client._get", fake_get)
    clock = _Clock()
    monkeypatch.setattr(mcp_infra, "_now", clock)
    client = HttpVectorClient()
    monkeypatch.setattr(mcp_infra, "get_t3", lambda: client)
    monkeypatch.setattr("nexus.db.http_vector_client._process_default_tenant", lambda: "not-this-tenant")
    mcp_infra.invalidate_collections_cache()
    yield seen, clock, release, full_started, client
    release.set()
    mcp_infra.join_fanout_counts_refresh(5)
    mcp_infra.invalidate_collections_cache()


def test_a_cold_search_does_not_wait_for_the_full_listing_and_excludes_nothing(floor_world):
    seen, _clock, release, full_started, client = floor_world
    target = _search_routing(client)
    assert full_started.wait(5), "the background refresh asked for the full listing"
    assert not release.is_set(), "the search returned while the full listing was still in flight"
    assert sorted(target) == sorted([_LIVE, _THIN]), "unknown counts fail open"
    release.set()
    assert mcp_infra.join_fanout_counts_refresh(5)
    assert _search_routing(client) == [_LIVE], "the next search has the counts and drops the thin sibling"


def test_stale_counts_are_used_while_the_refresh_runs(floor_world):
    seen, clock, release, full_started, client = floor_world
    release.set()
    _search_routing(client)
    assert mcp_infra.join_fanout_counts_refresh(5)
    release.clear()
    full_started.clear()
    seen.clear()

    clock.now += 15 * 60 + 1
    target = _search_routing(client)
    assert target == [_LIVE], "the floor ran on the stale counts"
    assert full_started.wait(5), "and a refresh was started"
    assert not release.is_set()
    release.set()
    assert mcp_infra.join_fanout_counts_refresh(5)


def test_only_one_background_refresh_runs_at_a_time(floor_world):
    seen, _clock, release, full_started, client = floor_world
    _search_routing(client)
    assert full_started.wait(5)
    _search_routing(client)
    _search_routing(client)
    release.set()
    assert mcp_infra.join_fanout_counts_refresh(5)
    assert seen.count(_FULL) == 1


def test_one_search_request_resolves_its_target_once(monkeypatch):
    """The search() wrapper renders twice (text, then structured) and the second render is served
    by the page cache, keyed on the resolved target. When the background counts land between the
    two renders the floor changes the target; measured live (2026-10-09 06:59Z) that made every
    cold first search run the whole fan-out twice. Both renders now share one resolution."""
    from nexus.mcp import core

    targets = iter([["knowledge__a", "knowledge__thin"], ["knowledge__a"]])
    resolved: list[list[str]] = []

    def resolve(corpus, t3, *, excluded_out=None):
        target = next(targets)
        resolved.append(target)
        return target

    searched: list[list[str]] = []

    def fake_search(query, target, **kw):
        searched.append(list(target))
        return []

    monkeypatch.setattr(core, "_get_t3", lambda: object())
    monkeypatch.setattr(core, "_resolve_corpus_target", resolve)
    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake_search)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    core._page_cache_invalidate()
    try:
        core.search(query="anything", corpus="knowledge")
    finally:
        core._page_cache_invalidate()
    assert resolved == [["knowledge__a", "knowledge__thin"]]
    assert searched == [["knowledge__a", "knowledge__thin"]]


def test_mcp_search_and_query_ask_for_capped_row_text(monkeypatch):
    """nexus-tao37: the MCP tools show at most 300 characters of a row, so both ask the engine for
    no more; the CLI never passes content_chars and keeps the full text."""
    from nexus.mcp import core

    seen: list = []

    def fake_search(query, target, **kw):
        seen.append(kw.get("content_chars"))
        return []

    monkeypatch.setattr(core, "_get_t3", lambda: object())
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **kw: ["knowledge__a"])
    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake_search)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    core._page_cache_invalidate()
    try:
        core.search(query="anything", corpus="knowledge")
        core.query(question="anything", corpus="knowledge")
    finally:
        core._page_cache_invalidate()
    assert seen == [core._MCP_CONTENT_CHARS, core._MCP_CONTENT_CHARS]
    assert core._MCP_CONTENT_CHARS >= 300, "query shows 300-character snippets"


def test_the_cli_never_caps_row_text():
    import inspect

    from nexus.commands import search_cmd

    assert "content_chars" not in inspect.getsource(search_cmd)
