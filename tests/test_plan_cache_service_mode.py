"""nexus-373jo: the semantic plan-match cache must survive a SERVICE-backed
T1 handle.

Since T1's service default (nexus-rn3wo.1), ``get_t1()`` returns an
``HttpScratchStore`` whose ``_client`` is an ``httpx.Client`` — handing that
to ``PlanSessionCache`` (chroma-shaped) AttributeError'd, flipped the
registry to ``_UNAVAILABLE``, and silently degraded EVERY production plan
match to FTS5-only: the whole RDR-078 calibrated cosine gate
(min_confidence, grown-plan floors, scope-fit ranking) never ran. The fix
detects a non-chroma client and builds an in-process substrate — since
RDR-155 P4b P0a, an ``InMemoryVectorClient`` with real per-instance
isolation (the cache is session-scoped and in-memory by contract)."""
from __future__ import annotations

from unittest.mock import patch

import pytest


class _HttpxShapedClient:
    """Shaped like HttpScratchStore._client: no chroma surface at all."""


class _ServiceShapedT1:
    session_id = "svc-session-1"
    _client = _HttpxShapedClient()


def _fresh_registry_cache(populate_from=None):
    from nexus.mcp.plan_cache_registry import PlanCacheRegistry

    registry = PlanCacheRegistry()
    with patch("nexus.mcp_infra.get_t1", return_value=(_ServiceShapedT1(), None)):
        return registry.get(populate_from=populate_from)


def test_service_backed_t1_yields_available_cache() -> None:
    """The registry must NOT settle _UNAVAILABLE on a service-backed T1 —
    the exact production regression: cosine gate silently off everywhere."""
    cache = _fresh_registry_cache()
    assert cache is not None, "service-backed T1 must not disable the plan cache"
    assert cache.is_available


def test_service_backed_fallback_substrate_is_inmemory() -> None:
    """RDR-155 P4b P0a: the service-path fallback substrate is the
    dependency-free InMemoryVectorClient, not chromadb.EphemeralClient."""
    from nexus.db.inmemory_vector_store import InMemoryVectorClient

    cache = _fresh_registry_cache()
    assert isinstance(cache._client, InMemoryVectorClient)


def test_service_backed_cache_round_trips_a_plan() -> None:
    cache = _fresh_registry_cache()
    assert cache.upsert({
        "id": 1,
        "query": "how does the catalog resolve tumblers to documents",
        "tags": "",
        "dimensions": "{}",
    })
    hits = cache.query("catalog tumbler resolution", n=3)
    assert hits, "cosine query must return the cached plan"
    assert hits[0][0] == 1


def test_chroma_shaped_client_still_used_directly() -> None:
    """Control: a chroma-shaped T1 client (local-mode T1 chroma) is handed
    through unchanged — the Ephemeral fallback is service-path only."""
    from nexus.mcp.plan_cache_registry import PlanCacheRegistry

    import chromadb

    class _ChromaT1:
        session_id = "local-session"
        _client = chromadb.EphemeralClient()

    sentinel = _ChromaT1()
    registry = PlanCacheRegistry()
    with patch("nexus.mcp_infra.get_t1", return_value=(sentinel, None)):
        cache = registry.get()
    assert cache is not None and cache.is_available
    assert cache._client is sentinel._client
