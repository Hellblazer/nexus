# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-156 P4 follow-on (nexus-rzqto): query() service-mode repoint tests.

Pins: service-mode catalog-param paths route through search_metadata_scoped /
search_graph_hop; fallback to search_cross_corpus in local mode or no catalog
params; exact structured key-set parity {ids, tumblers, distances, collections,
chunk_collections, chunk_text_hash}; guards (subtree depth>=3, catalog-not-init,
empty-filters) preserved.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from nexus.mcp import core


# ── Fakes ────────────────────────────────────────────────────────────────────

class _FakeServiceT3:
    """Stands in for HttpVectorClient; records combined-query calls."""

    def __init__(
        self,
        meta_rows: list[dict] | None = None,
        graph_rows: list[dict] | None = None,
    ) -> None:
        self.meta_rows = meta_rows or []
        self.graph_rows = graph_rows or []
        self.meta_calls: list[tuple] = []
        self.graph_calls: list[tuple] = []
        self.cross_calls: list[tuple] = []  # not used on service T3

    def search_metadata_scoped(
        self, query, collection_names, *, content_type=None, author=None,
        year=None, corpus=None, subtree=None, where=None, n_results=10,
    ):
        self.meta_calls.append(
            (query, list(collection_names), content_type, author,
             year, corpus, subtree, where, n_results)
        )
        return self.meta_rows

    def search_graph_hop(
        self, query, seeds, collection_names, *, link_type=None,
        depth=1, direction="both", where=None, n_results=10,
    ):
        self.graph_calls.append(
            (query, list(seeds), list(collection_names), link_type, depth, direction, where, n_results)
        )
        return self.graph_rows


class _FakeLocalT3:
    """Non-service T3; search_cross_corpus is called via search_engine module."""
    pass


class _FakeCatalogEntry:
    """Minimal CatalogEntry stand-in."""

    def __init__(
        self, tumbler_str: str, title: str, author: str = "",
        year: int = 0, physical_collection: str = "", chunk_count: int = 3,
        bib_year: int = 0, bib_authors: str = "", bib_venue: str = "",
        bib_citation_count: int = 0,
    ) -> None:
        from nexus.catalog.tumbler import Tumbler
        self.tumbler = Tumbler.parse(tumbler_str)
        self.title = title
        self.author = author
        self.year = year
        self.physical_collection = physical_collection
        self.chunk_count = chunk_count
        self.bib_year = bib_year
        self.bib_authors = bib_authors
        self.bib_venue = bib_venue
        self.bib_citation_count = bib_citation_count


class _FakeCatalog:
    """Fake Catalog; supports the subset of methods query() calls."""

    def __init__(
        self,
        entries: list[_FakeCatalogEntry] | None = None,
        descendants_out: list[dict] | None = None,
        graph_out: dict | None = None,
        manifest_len: int = 3,
    ) -> None:
        self._entries = entries or []
        self._descendants = descendants_out or []
        self._graph_out = graph_out or {"nodes": [], "edges": []}
        self._manifest_len = manifest_len
        self.find_calls: list = []
        self.descendants_calls: list = []
        self.by_content_type_calls: list = []
        self.graph_calls: list = []
        self.resolve_calls: list = []

    def find(self, query: str, *, content_type: str | None = None) -> list:
        self.find_calls.append((query, content_type))
        return list(self._entries)

    def descendants(self, prefix: str) -> list[dict]:
        self.descendants_calls.append(prefix)
        return list(self._descendants)

    def by_content_type(self, content_type: str) -> list:
        self.by_content_type_calls.append(content_type)
        return list(self._entries)

    def graph(self, tumbler, depth=1, direction="both", link_type="", **kw) -> dict:
        self.graph_calls.append((tumbler, depth, direction, link_type))
        return self._graph_out

    def resolve(self, tumbler) -> _FakeCatalogEntry | None:
        self.resolve_calls.append(tumbler)
        tumbler_str = str(tumbler)
        for e in self._entries:
            if str(e.tumbler) == tumbler_str:
                return e
        return None

    def get_manifest(self, doc_id: str) -> list:
        return [object()] * self._manifest_len  # list of length manifest_len

    def docs_for_chashes(self, chashes: list[str]) -> dict[str, list[str]]:
        return {}


# ── Wiring helpers ────────────────────────────────────────────────────────────

def _wire(
    monkeypatch,
    t3,
    broad_target: list[str],
    cat: _FakeCatalog | None,
    *,
    service: bool = True,
    cross_corpus_result=None,
):
    """Patch all the seams query() calls into."""
    monkeypatch.setattr(core, "_get_t3", lambda: t3)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3: broad_target)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_service_backed", lambda db: service,
    )
    monkeypatch.setattr(core, "_get_catalog", lambda: cat)
    # Patch _get_collection_names (needed by the old path when catalog_collections is None)
    monkeypatch.setattr(core, "_get_collection_names", lambda: broad_target)

    # Stub _t2_ctx as a context manager returning a fake T2 db
    from contextlib import contextmanager

    @contextmanager
    def _fake_t2_ctx():
        fake_t2 = MagicMock()
        fake_t2.taxonomy = None
        fake_t2.telemetry = None
        yield fake_t2

    monkeypatch.setattr(core, "_t2_ctx", _fake_t2_ctx)

    # Stub search_cross_corpus — old dance path
    if cross_corpus_result is None:
        cross_corpus_result = []

    import nexus.search_engine as se
    monkeypatch.setattr(se, "search_cross_corpus", lambda *a, **kw: cross_corpus_result)


# ── Rows helpers ──────────────────────────────────────────────────────────────

def _make_meta_rows(*items):
    """Build list of rows as returned by search_metadata_scoped."""
    rows = []
    for i, (tumbler, chash) in enumerate(items):
        rows.append({
            "id": tumbler,
            "content": f"snippet for {tumbler}",
            "distance": 0.1 * (i + 1),
            "collection": "knowledge__owner__v1",
            "chash": chash,
        })
    return rows


def _make_graph_rows(*items):
    """Build list of rows as returned by search_graph_hop."""
    rows = []
    for i, (tumbler, chash) in enumerate(items):
        rows.append({
            "id": tumbler,
            "content": f"snippet for {tumbler}",
            "distance": 0.1 * (i + 1),
            "collection": "knowledge__owner__v1",
            "chash": chash,
        })
    return rows


# ═════════════════════════════════════════════════════════════════════════════
# SERVICE MODE: metadata-scoped path (author / content_type / subtree)
# ═════════════════════════════════════════════════════════════════════════════

class TestQueryRepointMetadataScoped:
    """Service mode + catalog params → search_metadata_scoped is called."""

    def test_author_routes_to_search_metadata_scoped(self, monkeypatch):
        """Author filter in service mode calls search_metadata_scoped with broad target."""
        rows = _make_meta_rows(("1.2.3", "a" * 32), ("1.2.4", "b" * 32))
        t3 = _FakeServiceT3(meta_rows=rows)
        entry = _FakeCatalogEntry("1.2.3", "Paper A", author="Alice", year=2023,
                                  physical_collection="knowledge__owner__v1")
        cat = _FakeCatalog(entries=[entry])
        _wire(monkeypatch, t3, ["knowledge__owner__v1"], cat, service=True)

        result = core.query("test question", author="Alice", structured=True)

        assert isinstance(result, dict)
        assert t3.meta_calls, "search_metadata_scoped must be called in service mode"
        call = t3.meta_calls[0]
        assert call[0] == "test question"
        assert "knowledge__owner__v1" in call[1]  # broad target passed
        assert call[3] == "Alice"  # author param forwarded

    def test_structured_chunk_text_hash_from_row_chash(self, monkeypatch):
        """HIGH-1: chunk_text_hash must come from row['chash'], not manifest guess."""
        chash_a = "a" * 32
        chash_b = "b" * 32
        rows = _make_meta_rows(("1.2.3", chash_a), ("1.2.4", chash_b))
        t3 = _FakeServiceT3(meta_rows=rows)
        entry = _FakeCatalogEntry("1.2.3", "Paper A", physical_collection="knowledge__owner__v1")
        cat = _FakeCatalog(entries=[entry])
        _wire(monkeypatch, t3, ["knowledge__owner__v1"], cat, service=True)

        result = core.query("test", author="Alice", structured=True)

        assert result["chunk_text_hash"] == [chash_a, chash_b]

    def test_structured_exact_key_set(self, monkeypatch):
        """Structured output must have EXACTLY: ids, tumblers, distances, collections,
        chunk_collections, chunk_text_hash — matching the existing dance path."""
        rows = _make_meta_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(meta_rows=rows)
        cat = _FakeCatalog(entries=[_FakeCatalogEntry("1.2.3", "T",
                                                      physical_collection="c1")])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="X", structured=True)

        assert set(result.keys()) == {
            "ids", "tumblers", "distances", "collections",
            "chunk_collections", "chunk_text_hash",
        }

    def test_structured_ids_are_tumblers(self, monkeypatch):
        rows = _make_meta_rows(("1.2.3", "a" * 32), ("1.2.4", "b" * 32))
        t3 = _FakeServiceT3(meta_rows=rows)
        cat = _FakeCatalog()
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", content_type="paper", structured=True)

        assert result["ids"] == ["1.2.3", "1.2.4"]
        assert result["tumblers"] == ["1.2.3", "1.2.4"]

    def test_dedup_rows_keeps_best_distance(self, monkeypatch):
        """Multi-chunk doc: dedup to one per tumbler at best (lowest) distance."""
        rows = [
            {"id": "1.2.3", "content": "near", "distance": 0.1, "collection": "c1", "chash": "a" * 32},
            {"id": "1.2.4", "content": "other", "distance": 0.2, "collection": "c1", "chash": "b" * 32},
            {"id": "1.2.3", "content": "far", "distance": 0.9, "collection": "c1", "chash": "c" * 32},
        ]
        t3 = _FakeServiceT3(meta_rows=rows)
        cat = _FakeCatalog()
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="X", structured=True)

        assert result["ids"] == ["1.2.3", "1.2.4"]
        assert result["distances"] == [0.1, 0.2]
        assert result["chunk_text_hash"] == ["a" * 32, "b" * 32]  # best-distance chash

    def test_collections_sorted_distinct(self, monkeypatch):
        """structured['collections'] is sorted distinct across result rows."""
        rows = [
            {"id": "1.2.3", "content": "x", "distance": 0.1, "collection": "z_col", "chash": "a" * 32},
            {"id": "1.2.4", "content": "y", "distance": 0.2, "collection": "a_col", "chash": "b" * 32},
        ]
        t3 = _FakeServiceT3(meta_rows=rows)
        cat = _FakeCatalog()
        _wire(monkeypatch, t3, ["a_col", "z_col"], cat, service=True)

        result = core.query("q", author="X", structured=True)

        assert result["collections"] == sorted({"z_col", "a_col"})

    def test_chunk_collections_per_row_aligned(self, monkeypatch):
        """chunk_collections is per-row aligned (like the existing dance path)."""
        rows = [
            {"id": "1.2.3", "content": "x", "distance": 0.1, "collection": "c1", "chash": "a" * 32},
            {"id": "1.2.4", "content": "y", "distance": 0.2, "collection": "c2", "chash": "b" * 32},
        ]
        t3 = _FakeServiceT3(meta_rows=rows)
        cat = _FakeCatalog()
        _wire(monkeypatch, t3, ["c1", "c2"], cat, service=True)

        result = core.query("q", content_type="paper", structured=True)

        assert result["chunk_collections"] == ["c1", "c2"]

    def test_text_form_includes_rehydrated_title(self, monkeypatch):
        """Text form: title comes from catalog.resolve(tumbler).title."""
        rows = _make_meta_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(meta_rows=rows)
        entry = _FakeCatalogEntry("1.2.3", "The Real Title", author="Bob",
                                  year=2022, physical_collection="c1", chunk_count=5)
        cat = _FakeCatalog(entries=[entry])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="Bob", structured=False)

        assert isinstance(result, str)
        assert "The Real Title" in result

    def test_text_form_includes_chunk_count_from_manifest(self, monkeypatch):
        """Text form: chunk_count comes from len(cat.get_manifest(tumbler))."""
        rows = _make_meta_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(meta_rows=rows)
        entry = _FakeCatalogEntry("1.2.3", "Doc", physical_collection="c1", chunk_count=99)
        cat = _FakeCatalog(entries=[entry], manifest_len=7)
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="X", structured=False)

        assert "7 chunks" in result

    def test_empty_result_returns_no_documents_message(self, monkeypatch):
        """MEDIUM-3: empty result under catalog filters → 'No documents found matching catalog filters'."""
        t3 = _FakeServiceT3(meta_rows=[])
        cat = _FakeCatalog()
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="Nobody", structured=False)

        assert "No documents found matching catalog filters" in result
        assert "author=" in result

    def test_empty_result_structured_returns_empty_dict(self, monkeypatch):
        """Empty result in structured mode returns the 6-key empty dict."""
        t3 = _FakeServiceT3(meta_rows=[])
        cat = _FakeCatalog()
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="Nobody", structured=True)

        assert result == {
            "ids": [], "tumblers": [], "distances": [],
            "collections": [], "chunk_collections": [],
            "chunk_text_hash": [],
        }

    def test_where_forwarded_to_search_metadata_scoped(self, monkeypatch):
        """L2: where=KEY=VALUE is parsed and forwarded to search_metadata_scoped."""
        rows = _make_meta_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(meta_rows=rows)
        cat = _FakeCatalog()
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        core.query("q", content_type="paper", where="lang=python", structured=True)

        assert t3.meta_calls
        where_arg = t3.meta_calls[0][7]  # where is the 8th positional arg
        assert where_arg == {"lang": "python"}

    def test_text_form_includes_bib_authors_venue_citation_count(self, monkeypatch):
        """nexus-rzqto bead requirement: the service text form preserves the full bib
        richness (bib_authors/bib_venue/bib_citation_count), re-hydrated from CatalogEntry
        (the Java catalog already serializes these columns via docRowFromRecord; they are
        now surfaced onto CatalogEntry). Mirrors the dance path's bib line."""
        rows = _make_meta_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(meta_rows=rows)
        entry = _FakeCatalogEntry("1.2.3", "Rich Paper", physical_collection="c1",
                                  bib_year=2023, bib_authors="Smith, Jones",
                                  bib_venue="NeurIPS", bib_citation_count=42)
        cat = _FakeCatalog(entries=[entry])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="Smith", structured=False)

        assert "2023" in result
        assert "Smith, Jones" in result      # bib_authors
        assert "NeurIPS" in result           # bib_venue
        assert "42 citations" in result      # bib_citation_count


# ═════════════════════════════════════════════════════════════════════════════
# SERVICE MODE: graph-hop path (follow_links)
# ═════════════════════════════════════════════════════════════════════════════

class TestQueryRepointGraphHop:
    """Service mode + follow_links → seeds resolved app-side, then search_graph_hop."""

    def test_follow_links_calls_search_graph_hop(self, monkeypatch):
        """follow_links in service mode resolves seeds then calls t3.search_graph_hop."""
        rows = _make_graph_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(graph_rows=rows)
        seed_entry = _FakeCatalogEntry("1.1.1", "Seed Doc", physical_collection="c1")
        cat = _FakeCatalog(entries=[seed_entry])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", follow_links="cites", structured=True)

        assert t3.graph_calls, "search_graph_hop must be called"
        call = t3.graph_calls[0]
        assert call[0] == "q"
        assert "c1" in call[2]  # broad target
        assert call[3] == "cites"  # link_type

    def test_follow_links_with_author_resolves_author_seeds(self, monkeypatch):
        """follow_links + author: seeds come from cat.find(author, ...) filtered by author."""
        rows = _make_graph_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(graph_rows=rows)
        entry = _FakeCatalogEntry("1.1.1", "Seed", author="Carol",
                                  physical_collection="c1")
        cat = _FakeCatalog(entries=[entry])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="carol", follow_links="cites", structured=True)

        assert t3.graph_calls
        seeds_arg = t3.graph_calls[0][1]
        assert "1.1.1" in seeds_arg

    def test_follow_links_structured_chunk_text_hash_from_chash(self, monkeypatch):
        """HIGH-1: follow_links structured path also uses row['chash'] for chunk_text_hash."""
        chash = "f" * 32
        rows = _make_graph_rows(("1.2.3", chash))
        t3 = _FakeServiceT3(graph_rows=rows)
        entry = _FakeCatalogEntry("1.1.1", "Seed", author="X", physical_collection="c1")
        cat = _FakeCatalog(entries=[entry])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", follow_links="cites", structured=True)

        assert result["chunk_text_hash"] == [chash]

    def test_follow_links_no_seeds_falls_through_to_metadata_scoped(self, monkeypatch):
        """SIGNIFICANT: follow_links-only with no seeds → broad search via search_metadata_scoped.

        The dance path falls through to broad corpus search when no seeds resolve
        (catalog_collections stays None). The service branch must mirror this:
        call search_metadata_scoped over the broad target instead of returning an
        error (the previous behaviour).
        """
        rows = _make_meta_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(graph_rows=[], meta_rows=rows)
        cat = _FakeCatalog(entries=[])  # find() returns nothing → no seeds
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", follow_links="cites", structured=True)

        assert t3.graph_calls == [], "graph-hop must NOT be called with empty seeds"
        assert t3.meta_calls, "search_metadata_scoped must be called as fallback"
        assert isinstance(result, dict)
        assert result["ids"] == ["1.2.3"]

    def test_follow_links_no_seeds_empty_fallback_returns_no_docs_message(self, monkeypatch):
        """When no seeds AND metadata_scoped fallback also returns empty → no-docs message."""
        t3 = _FakeServiceT3(graph_rows=[], meta_rows=[])
        cat = _FakeCatalog(entries=[])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", follow_links="cites", structured=False)

        assert "No documents found matching catalog filters" in result
        assert t3.graph_calls == []
        assert t3.meta_calls  # metadata_scoped still called

    def test_follow_links_where_routes_through_graph_hop(self, monkeypatch):
        """H2 RESOLVED (nexus-7ndh3): follow_links + where stays on the service branch.

        search_graph_hop carries `where` since catalog-012, so the old dance
        fallback (the _skip_service arm) is gone — the caller's where filter is
        pushed into the combined-query call, not honored via the app-side dance.
        """
        import nexus.search_engine as se

        cross_called = []

        def _fake_cross(*a, **kw):
            cross_called.append(True)
            return []

        rows = _make_graph_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(graph_rows=rows)
        # A catalog entry so cat.find(question) resolves a seed tumbler.
        cat = _FakeCatalog(entries=[_FakeCatalogEntry("1.2.3", "T")])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)
        monkeypatch.setattr(se, "search_cross_corpus", _fake_cross)

        result = core.query("q", follow_links="cites", where="lang=python",
                            corpus="c1", structured=True)

        assert not cross_called, "the dance must NOT be used (H2 arm removed)"
        assert t3.graph_calls, "search_graph_hop must be called"
        # The where filter reaches the combined-query call (position -2 = where).
        assert t3.graph_calls[0][-2] == {"lang": "python"}
        assert isinstance(result, dict)
        assert result["ids"] == ["1.2.3"]

    def test_follow_links_operator_where_still_dances(self, monkeypatch):
        """nexus-7ndh3 critique CRITICAL-1: operator-shaped where cannot be
        expressed as JSONB containment — it must keep the dance path (whose
        search_cross_corpus leg translates operators to SQL), NOT silently
        containment-fail through search_graph_hop."""
        import nexus.search_engine as se

        cross_called = []

        def _fake_cross(*a, **kw):
            cross_called.append(True)
            return []

        t3 = _FakeServiceT3()
        cat = _FakeCatalog(entries=[])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)
        monkeypatch.setattr(se, "search_cross_corpus", _fake_cross)

        core.query("q", follow_links="cites", where="bib_year>=2020", corpus="c1")

        assert cross_called, "operator where must take the dance"
        assert not t3.graph_calls, "search_graph_hop must NOT receive an operator where"
        assert not t3.meta_calls, "search_metadata_scoped must NOT receive an operator where"

    def test_follow_links_subtree_service_mode(self, monkeypatch):
        """L1: follow_links + subtree in service mode: seeds come from cat.descendants()."""
        rows = _make_graph_rows(("1.2.3", "a" * 32))
        t3 = _FakeServiceT3(graph_rows=rows)
        cat = _FakeCatalog(
            descendants_out=[{"tumbler": "1.2.3", "physical_collection": "c1"}],
        )
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", follow_links="cites", subtree="1.2", structured=True)

        assert t3.graph_calls, "search_graph_hop must be called"
        seeds_arg = t3.graph_calls[0][1]
        assert "1.2.3" in seeds_arg
        assert result["ids"] == ["1.2.3"]


# ═════════════════════════════════════════════════════════════════════════════
# GUARD PRESERVATION (MEDIUM-3)
# ═════════════════════════════════════════════════════════════════════════════

class TestQueryGuards:
    """Guards from the existing dance path must be preserved in the service-mode branch."""

    def test_subtree_depth_3_returns_existing_error(self, monkeypatch):
        """MEDIUM-3: subtree with 3+ segments → existing error string."""
        t3 = _FakeServiceT3()
        entry = _FakeCatalogEntry("1.1.1", "S", physical_collection="c1")
        cat = _FakeCatalog(entries=[entry])
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", subtree="1.2.3")  # 3 segments → doc-level

        assert isinstance(result, str)
        assert "document-level address" in result
        assert "1.2.3" in result

    def test_catalog_not_initialized_returns_error(self, monkeypatch):
        """MEDIUM-3: catalog params without catalog → existing error message."""
        t3 = _FakeServiceT3()
        _wire(monkeypatch, t3, ["c1"], None, service=True)  # cat=None

        result = core.query("q", author="X")

        assert "catalog not initialized" in result

    def test_catalog_filters_match_nothing_returns_no_docs_message(self, monkeypatch):
        """MEDIUM-3: filters resolve to empty set → 'No documents found matching catalog filters'."""
        t3 = _FakeServiceT3()
        cat = _FakeCatalog(entries=[])  # find returns nothing
        _wire(monkeypatch, t3, ["c1"], cat, service=True)

        result = core.query("q", author="Ghost")

        assert "No documents found matching catalog filters" in result
        assert "author=" in result


# ═════════════════════════════════════════════════════════════════════════════
# FALLBACK: LOCAL MODE (non-service) must NOT call combined-query functions
# ═════════════════════════════════════════════════════════════════════════════

class TestQueryDancePathCollectionsSorted:
    """H1: dance path `collections` must also be sorted for cross-mode parity."""

    def test_dance_path_collections_sorted(self, monkeypatch):
        """H1: dance path structured['collections'] is sorted (not arbitrary set order)."""
        import nexus.search_engine as se

        class _FakeResult:
            def __init__(self, coll, dist):
                self.id = "chunk1"
                self.collection = coll
                self.distance = dist
                self.metadata = {}
                self.content = "text"

        results = [
            _FakeResult("z_col", 0.1),
            _FakeResult("a_col", 0.2),
        ]

        _wire(monkeypatch, object(), ["z_col", "a_col"], None, service=False)
        monkeypatch.setattr(se, "search_cross_corpus",
                            lambda *a, **kw: results)

        # corpus="all" causes the dance to build target from all_names=["z_col","a_col"]
        # via the "all" expansion path (resolve_corpus exact-matches each prefix).
        result = core.query("q", structured=True, corpus="all")

        # H1: must be sorted, not arbitrary iteration order
        assert result["collections"] == sorted({"z_col", "a_col"})


class TestQueryFallbackLocalMode:
    """LOCAL MODE: is_service_backed=False → existing dance + search_cross_corpus runs."""

    def test_local_mode_with_catalog_params_uses_cross_corpus(self, monkeypatch):
        """Non-service mode + catalog params: search_cross_corpus is called, not combined funcs."""
        import nexus.search_engine as se

        t3 = _FakeLocalT3()
        # Add combined-query methods to detect wrong calls
        t3.meta_calls = []
        t3.graph_calls = []

        def _bad_meta(*a, **kw):
            t3.meta_calls.append(True)
            return []

        def _bad_graph(*a, **kw):
            t3.graph_calls.append(True)
            return []

        t3.search_metadata_scoped = _bad_meta
        t3.search_graph_hop = _bad_graph

        cross_called = []

        def _fake_cross(question, target, *, n_results, t3, where, catalog, link_boost, taxonomy, telemetry, diagnostics_out=None, **kw):
            cross_called.append(True)
            return []

        entry = _FakeCatalogEntry("1.1.1", "Doc", author="Alice",
                                  physical_collection="c1")
        cat = _FakeCatalog(entries=[entry])

        _wire(monkeypatch, t3, ["c1"], cat, service=False)
        # Override search_cross_corpus with spy AFTER _wire (so it isn't overridden)
        monkeypatch.setattr(se, "search_cross_corpus", _fake_cross)

        result = core.query("q", author="Alice")

        assert cross_called, "search_cross_corpus must be called in local mode"
        assert not t3.meta_calls, "search_metadata_scoped must NOT be called in local mode"
        assert not t3.graph_calls, "search_graph_hop must NOT be called in local mode"

    def test_no_catalog_params_uses_cross_corpus_regardless_of_service_mode(self, monkeypatch):
        """No catalog params → old dance regardless of service mode."""
        import nexus.search_engine as se

        t3 = _FakeServiceT3()
        cross_called = []

        # Wire first, then override search_cross_corpus with spy AFTER
        # (order matters: _wire's cross_corpus_result would override otherwise)
        _wire(monkeypatch, t3, ["knowledge__v1"], None, service=True)

        def _fake_cross(*a, **kw):
            cross_called.append(True)
            return []

        monkeypatch.setattr(se, "search_cross_corpus", _fake_cross)

        core.query("q")  # no author/content_type/follow_links/subtree

        assert cross_called, "No catalog params → search_cross_corpus path"
        assert not t3.meta_calls
        assert not t3.graph_calls
