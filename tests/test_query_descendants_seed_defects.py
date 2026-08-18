# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""query()'s descendants()-unmasked seed-cap defect (ab7907fb follow-on).

``ab7907fb`` fixed ``HttpCatalogClient.descendants()`` itself (it used to
silently truncate a subtree to one unfiltered 500-row ``/list`` page). That
fix unmasked two pre-existing bugs in ``descendants()``'s consumers inside
``query()``:

1. The SERVICE-mode graph-hop seed list (``subtree`` -> ``search_graph_hop``)
   was unbounded and undisclosed. Fixed and pinned below.
2. The app-side FALLBACK dance's seed-entry resolution was one
   ``cat.resolve()`` (and, downstream, one ``cat.graph()``) per descendant —
   an N+1 HTTP round-trip shape, reached whenever a subtree query carried an
   operator-shaped ``where``, or in local/Chroma mode. That whole dance
   (and the ``_resolve_seed_entries_batched`` / ``_graph_many_batched``
   helpers that fixed its N+1 shape) was DELETED at RDR-156 P4.2c
   (nexus-2bqpn) — both of those combinations now loud-reject instead of
   falling back to the dance. Defect 2 is moot: there is no longer any code
   path to regress. Its round-trip-count tests (``TestDescendantsSeed
   ResolutionRoundTrips`` / ``TestGraphManyBatching``) were removed with the
   dance rather than repointed, since their subject matter no longer
   exists; the loud-reject contract that replaced the dance is pinned in
   ``tests/test_query_repoint.py`` (``TestQueryFallbackLocalMode``,
   ``test_follow_links_operator_where_is_loud_rejected``).

These tests pin the fix for (1): a disclosed, capped seed list.
"""
from __future__ import annotations

from nexus.mcp import core


# ═════════════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════════════

class _FakeServiceT3:
    """Stands in for HttpVectorClient in the SERVICE-mode branch."""

    def __init__(self, graph_rows=None):
        self.graph_rows = graph_rows or []
        self.graph_calls: list[tuple] = []

    def search_graph_hop(
        self, query, seeds, collection_names, *, link_type=None,
        depth=1, direction="both", where=None, n_results=10,
    ):
        self.graph_calls.append(
            (query, list(seeds), list(collection_names), link_type,
             depth, direction, where, n_results)
        )
        return self.graph_rows


class _FakeCatalogSpy:
    """Minimal catalog fake: query()'s service-mode subtree+follow_links
    seed resolution only calls ``descendants()`` — no per-doc resolve/graph
    round trips (those belonged to the now-deleted dance)."""

    def __init__(self, descendants_out):
        self._descendants = descendants_out
        self.descendants_calls: list[str] = []

    def descendants(self, prefix):
        self.descendants_calls.append(prefix)
        return list(self._descendants)

    # Unused by these tests but part of the surface query() may touch.
    def find(self, query, *, content_type=None):
        return []

    def by_content_type(self, content_type):
        return []

    def get_manifest(self, doc_id):
        return []


# ═════════════════════════════════════════════════════════════════════════
# Wiring
# ═════════════════════════════════════════════════════════════════════════

def _wire_service(monkeypatch, t3, cat, target=None):
    """Force query() onto the SERVICE-mode branch (the only catalog-param
    path since RDR-156 P4.2c)."""
    target = target or ["c1"]
    monkeypatch.setattr(core, "_get_t3", lambda: t3)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3: target)
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda db: True)
    monkeypatch.setattr(core, "_get_catalog", lambda: cat)


# ═════════════════════════════════════════════════════════════════════════
# Unbounded + undisclosed seed list in the service-mode branch
# ═════════════════════════════════════════════════════════════════════════

class TestGraphHopSeedCapDisclosure:
    def test_large_subtree_seed_list_is_capped_and_disclosed(self, monkeypatch):
        n = core._MAX_GRAPH_HOP_SEEDS + 500
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        t3 = _FakeServiceT3(graph_rows=[])
        cat = _FakeCatalogSpy(desc)
        _wire_service(monkeypatch, t3, cat)

        result = core.query("q", subtree="1.2", follow_links="cites", structured=True)

        assert t3.graph_calls, "search_graph_hop must still be called"
        seeds_sent = t3.graph_calls[0][1]
        assert len(seeds_sent) == core._MAX_GRAPH_HOP_SEEDS, (
            f"expected the seed list capped at {core._MAX_GRAPH_HOP_SEEDS}, "
            f"got {len(seeds_sent)} (an uncapped send reintroduces the "
            f"silent-truncation-at-scale class ab7907fb fixed)"
        )
        assert isinstance(result, dict)
        assert result.get("seed_scope") == {
            "total": n, "used": core._MAX_GRAPH_HOP_SEEDS, "truncated": True,
        }, f"cap must be disclosed in the structured envelope, got {result.get('seed_scope')!r}"

    def test_large_subtree_cap_disclosed_in_text_form(self, monkeypatch):
        n = core._MAX_GRAPH_HOP_SEEDS + 1
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        rows = [{"id": "1.2.0", "content": "x", "distance": 0.1,
                  "collection": "c1", "chash": "a" * 32}]
        t3 = _FakeServiceT3(graph_rows=rows)
        cat = _FakeCatalogSpy(desc)
        _wire_service(monkeypatch, t3, cat)

        result = core.query("q", subtree="1.2", follow_links="cites", structured=False)

        assert isinstance(result, str)
        assert "capped" in result.lower()
        assert str(core._MAX_GRAPH_HOP_SEEDS) in result
        assert str(n) in result

    def test_small_subtree_is_not_capped_and_disclosure_says_so(self, monkeypatch):
        tumblers = ["1.2.0", "1.2.1", "1.2.2"]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        t3 = _FakeServiceT3(graph_rows=[])
        cat = _FakeCatalogSpy(desc)
        _wire_service(monkeypatch, t3, cat)

        result = core.query("q", subtree="1.2", follow_links="cites", structured=True)

        assert t3.graph_calls
        seeds_sent = t3.graph_calls[0][1]
        assert sorted(seeds_sent) == sorted(tumblers), "small subtree must NOT be truncated"
        assert result.get("seed_scope") == {"total": 3, "used": 3, "truncated": False}
