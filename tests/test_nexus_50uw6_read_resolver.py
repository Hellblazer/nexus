# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-210 Phase 4 Step 1 (nexus-50uw6): the single-name READ resolver spans
every live model sibling, found by catalog row, at the places that read through
it, and does it from the rows the process already holds.

The resolver's own cases (two siblings, a non-four-segment sibling, other owners
and non-live rows, an explicit name) are in ``test_rdr_109_phase2_dispatch.py``.
"""
from __future__ import annotations

import pytest

import nexus.mcp_infra as mcp_infra
from nexus.corpus import t3_collection_name, t3_collection_names_for_read
from nexus.db.http_vector_client import HttpVectorClient
from nexus.mcp import core as mcp_core

OTHER_FAMILY = "voyage-model-b"       # any non-local model: the second family
ODD = "art-notes-in-the-second-family"  # registered under a name that is not four-segment


def _row(name: str, owner: str, model: str, **extra) -> dict:
    return {
        "name": name, "content_type": "knowledge", "owner_id": owner,
        "embedding_model": model, "lifecycle_state": "live", "count": 3, **extra,
    }


def _primary() -> str:
    """The name the single-name resolver picks for ``art`` on this install."""
    return t3_collection_name("art")


@pytest.fixture
def cache(monkeypatch):
    """Restore the process collections cache after a test primes it."""
    monkeypatch.setattr(mcp_infra, "_collections_cache", mcp_infra._collections_cache)
    yield mcp_infra
    mcp_infra.invalidate_collections_cache()


class _RowT3:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def collection_exists(self, name: str) -> bool:
        return any(r["name"] == name for r in self._rows)

    def list_collections(self) -> list[dict]:
        return [dict(r) for r in self._rows]

    def collection_info(self, name: str) -> dict:
        return {"count": 3, "metadata": {}}

    def list_store(self, name: str, limit: int = 20, offset: int = 0) -> list[dict]:
        return [{"id": f"{name}-{i}".ljust(64, "0"), "title": f"{name} note {i}"} for i in range(2)]


def test_the_default_tenant_client_finds_siblings_in_the_cache_and_fetches_nothing(monkeypatch, cache):
    """The sibling lookup adds no request of its own: it scans the collections
    cache the caller listed through, for a real process-default client."""
    rows = [_row(_primary(), "art", "local-model"), _row(ODD, "art", OTHER_FAMILY),
            _row("knowledge__other__x__v1", "other", "local-model")]
    cache.prime_collections_cache(rows)

    def _no_fetch(*a, **k):
        raise AssertionError("a sibling lookup must not fetch")

    monkeypatch.setattr(HttpVectorClient, "list_collections", _no_fetch)
    monkeypatch.setattr(HttpVectorClient, "list_live_collections", _no_fetch)
    monkeypatch.setattr(mcp_infra, "_refresh_collections_cache_if_stale", lambda **k: None)
    monkeypatch.setattr(
        HttpVectorClient, "collection_exists", lambda self, n: n in {r["name"] for r in rows},
    )
    assert t3_collection_names_for_read("art", t3=HttpVectorClient()) == [_primary(), ODD]


def test_the_cache_carries_superseded_by_so_a_retired_source_is_not_read_twice(monkeypatch, cache):
    """A re-embedded or renamed source keeps lifecycle 'live' and names its
    successor; the cached row must carry that or the resolver would list both."""
    rows = [_row(_primary(), "art", "local-model"),
            _row(ODD, "art", OTHER_FAMILY, superseded_by=_primary())]
    cache.prime_collections_cache(rows)
    assert mcp_infra.get_collection_row(ODD)["superseded_by"] == _primary()
    monkeypatch.setattr(mcp_infra, "_refresh_collections_cache_if_stale", lambda **k: None)
    monkeypatch.setattr(HttpVectorClient, "collection_exists", lambda self, n: True)
    assert t3_collection_names_for_read("art", t3=HttpVectorClient()) == [_primary()]


def test_a_non_default_tenant_client_asks_for_one_routing_listing(monkeypatch):
    listings: list[dict] = []
    rows = [_row(_primary(), "art", "local-model"), _row(ODD, "art", OTHER_FAMILY)]

    def _list(self, lifecycle_state=None, *, strict=False, routing=False):
        listings.append({"lifecycle_state": lifecycle_state, "routing": routing})
        return rows

    monkeypatch.setattr(HttpVectorClient, "list_collections", _list)
    monkeypatch.setattr(HttpVectorClient, "collection_exists", lambda self, n: True)
    monkeypatch.setattr("nexus.db.http_vector_client._process_default_tenant", lambda: "default")
    client = HttpVectorClient(tenant="other")
    assert t3_collection_names_for_read("art", t3=client) == [_primary(), ODD]
    assert listings == [{"lifecycle_state": "live", "routing": True}]


def test_a_handle_whose_listing_fails_leaves_the_one_name():
    class _Broken(_RowT3):
        def list_collections(self):
            raise RuntimeError("listing unavailable")

    t3 = _Broken([_row(_primary(), "art", "local-model")])
    assert t3_collection_names_for_read("art", t3=t3) == [_primary()]


def test_store_reads_look_in_every_sibling():
    """``store_get`` / ``store_list`` / ``store_get_many`` read through
    ``_read_scope``; a shorthand name now stands for the whole corpus, and a
    fully-qualified one is still that one collection."""
    t3 = _RowT3([_row(_primary(), "art", "local-model"), _row(ODD, "art", OTHER_FAMILY)])
    assert mcp_core._read_scope(t3, "art") == [_primary(), ODD]
    assert mcp_core._read_scope(t3, _primary()) == [_primary()]


def test_search_and_query_name_every_sibling_of_a_shorthand_corpus(cache):
    """``search`` / ``query`` with ``corpus="knowledge__art"`` target both
    siblings; the same call with the fully-qualified name targets one. A sibling
    that is not live is left out, as an explicit name always was."""
    rows = [
        _row(_primary(), "art", "local-model"),
        _row(ODD, "art", OTHER_FAMILY),
        _row("art-quarantined", "art", OTHER_FAMILY, lifecycle_state="quarantine"),
    ]
    cache.prime_collections_cache(rows)
    t3 = _RowT3(rows)
    assert mcp_core._resolve_corpus_target("knowledge__art", t3) == [_primary(), ODD]
    assert mcp_core._resolve_corpus_target(_primary(), t3) == [_primary()]


def test_store_list_of_a_named_collection_pages_it_even_with_a_live_sibling(monkeypatch, cache):
    """The bare ``knowledge`` name lists subjects; a NAMED collection pages its
    own entries even when it has a live sibling, for a legacy full name and for
    a sibling registered under a non-four-segment name, and says what else is
    in the corpus."""
    legacy = "knowledge__art"
    rows = [_row(legacy, "art", "local-model"), _row(ODD, "art", OTHER_FAMILY)]
    cache.prime_collections_cache(rows)
    t3 = _RowT3(rows)
    monkeypatch.setattr(mcp_core, "_get_t3", lambda: t3)
    out = mcp_core.store_list(collection=legacy)
    assert f"{legacy}  (showing 1-2; 3 stored)" in out, out
    assert "knowledge collections" not in out, out
    assert f"also in this corpus: {ODD}" in out, out
    out_odd = mcp_core.store_list(collection=ODD)
    assert f"{ODD}  (showing 1-2; 3 stored)" in out_odd, out_odd
    assert f"also in this corpus: {legacy}" in out_odd, out_odd


def test_a_cache_without_the_picked_names_row_is_refreshed_once(monkeypatch, cache):
    """A sibling another process just minted: the resolver's pick comes from a
    fresh listing, the cache has not seen it, so the rows are re-read once and
    the older collection is not hidden. A real default-tenant client, so the
    rows come from the cache."""
    old_row = _row(_primary(), "art", "local-model")
    new_name = "knowledge__art__voyage-model-b__v1"
    new_row = _row(new_name, "art", OTHER_FAMILY)
    cache.prime_collections_cache([old_row])        # the cache knows only the first
    state = {"invalidated": 0}
    real_invalidate = mcp_infra.invalidate_collections_cache

    def _invalidate():
        state["invalidated"] += 1
        real_invalidate()

    def _refresh():                                  # the engine now answers with both
        if state["invalidated"]:
            cache.prime_collections_cache([old_row, new_row])

    monkeypatch.setattr(mcp_infra, "invalidate_collections_cache", _invalidate)
    monkeypatch.setattr(mcp_infra, "_refresh_collections_cache_if_stale", _refresh)
    monkeypatch.setattr("nexus.corpus.t3_collection_name", lambda arg, t3=None, **kw: new_name)
    assert t3_collection_names_for_read("art", t3=HttpVectorClient()) == [new_name, _primary()]
    assert state["invalidated"] == 1
