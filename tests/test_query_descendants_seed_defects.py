# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""query()'s two descendants()-unmasked defects (ab7907fb follow-on).

``ab7907fb`` fixed ``HttpCatalogClient.descendants()`` itself (it used to
silently truncate a subtree to one unfiltered 500-row ``/list`` page). That
fix unmasks two pre-existing bugs in ``descendants()``'s consumers inside
``query()``, both of which previously only ever saw <=500 rows (usually 0):

1. The SERVICE-mode graph-hop seed list (``subtree`` -> ``search_graph_hop``)
   was unbounded and undisclosed.
2. The FALLBACK dance's seed-entry resolution was one ``cat.resolve()`` (and,
   downstream, one ``cat.graph()``) per descendant -- an N+1 HTTP
   round-trip shape. Reached whenever a subtree query carries an
   operator-shaped ``where``, or in local/Chroma mode.

These tests pin the fixes: a disclosed, capped seed list for (1), and a
batched (O(1 + aliases)) resolution + a single ``graph_many()`` BFS for (2).
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import nexus.search_engine as se
from nexus.catalog.tumbler import Tumbler
from nexus.mcp import core


# ═════════════════════════════════════════════════════════════════════════
# Fakes
# ═════════════════════════════════════════════════════════════════════════

class _FakeServiceT3:
    """Stands in for HttpVectorClient in the SERVICE-mode branch (defect 1)."""

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


class _FakeEntry:
    def __init__(self, tumbler_str: str, alias_of: str = ""):
        self.tumbler = Tumbler.parse(tumbler_str)
        self.title = tumbler_str
        self.author = ""
        self.year = 0
        self.physical_collection = "c1"
        self.chunk_count = 1
        self.bib_year = 0
        self.bib_authors = ""
        self.bib_venue = ""
        self.bib_citation_count = 0
        self.alias_of = alias_of


class _FakeCatalogSpy:
    """Distinguishes batched calls (resolve_many/graph_many) from the
    per-doc calls (resolve/graph) they replace, so a regression back to
    the N+1 shape shows up as a call-count assertion failure."""

    def __init__(
        self, descendants_out, entries_by_tumbler=None, canonical_by_alias=None,
        graph_many_nodes_by_call=None,
    ):
        self._descendants = descendants_out
        self._entries = entries_by_tumbler or {}     # tumbler -> _FakeEntry (resolve_many hits)
        self._canonical = canonical_by_alias or {}    # alias tumbler str -> canonical _FakeEntry
        # call-index (0-based, in graph_many() call order) -> list of node
        # objects to return for THAT call — lets a test simulate one batch
        # landing at the node-cap heuristic without affecting the others.
        self._graph_many_nodes_by_call = graph_many_nodes_by_call or {}
        self.descendants_calls: list[str] = []
        self.resolve_calls: list[str] = []            # per-doc resolve()
        self.resolve_many_calls: list[list[str]] = []  # batched resolve_many()
        self.graph_calls: list[str] = []              # per-doc graph()
        self.graph_many_calls: list[list[str]] = []    # batched graph_many()

    def descendants(self, prefix):
        self.descendants_calls.append(prefix)
        return list(self._descendants)

    def resolve_many(self, doc_ids):
        self.resolve_many_calls.append(list(doc_ids))
        return {t: self._entries[t] for t in doc_ids if t in self._entries}

    def resolve(self, tumbler, *, follow_alias=True):
        self.resolve_calls.append(str(tumbler))
        return self._canonical.get(str(tumbler))

    def graph(self, tumbler, depth=1, direction="both", link_type="", **kw):
        self.graph_calls.append(str(tumbler))
        return {"nodes": [], "edges": []}

    def graph_many(self, seeds, depth=1, direction="both", link_type="", **kw):
        call_index = len(self.graph_many_calls)
        self.graph_many_calls.append([str(s) for s in seeds])
        nodes = self._graph_many_nodes_by_call.get(call_index, [])
        return {"nodes": nodes, "edges": []}

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

def _wire_dance(monkeypatch, cat, target=None):
    """Force query() onto the FALLBACK dance branch (defect 2's location):
    local/Chroma (non-service) mode always reaches it regardless of `where`.
    """
    target = target or ["c1"]
    monkeypatch.setattr(core, "_get_t3", lambda: object())
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3: target)
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda db: False)
    monkeypatch.setattr(core, "_get_catalog", lambda: cat)
    monkeypatch.setattr(core, "_get_collection_names", lambda: target)

    @contextmanager
    def _fake_t2_ctx():
        fake_t2 = MagicMock()
        fake_t2.taxonomy = None
        fake_t2.telemetry = None
        yield fake_t2

    monkeypatch.setattr(core, "_t2_ctx", _fake_t2_ctx)
    monkeypatch.setattr(se, "search_cross_corpus", lambda *a, **kw: [])


def _wire_service(monkeypatch, t3, cat, target=None):
    """Force query() onto the SERVICE-mode branch (defect 1's location)."""
    target = target or ["c1"]
    monkeypatch.setattr(core, "_get_t3", lambda: t3)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, _t3: target)
    monkeypatch.setattr("nexus.db.http_vector_client.is_service_backed", lambda db: True)
    monkeypatch.setattr(core, "_get_catalog", lambda: cat)


# ═════════════════════════════════════════════════════════════════════════
# Defect 2 — N+1 round trips in the fallback dance
# ═════════════════════════════════════════════════════════════════════════

class TestDescendantsSeedResolutionRoundTrips:
    """The property under test is ROUND-TRIP COUNT, not entry correctness —
    the entries were already correct pre-fix; only the call shape regressed
    to N+1 once descendants() stopped truncating at 500 rows."""

    def test_large_subtree_resolves_via_one_batched_call(self, monkeypatch):
        n = 50
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        cat = _FakeCatalogSpy(desc, entries_by_tumbler=entries)
        _wire_dance(monkeypatch, cat)

        core.query("q", subtree="1.2")

        assert cat.resolve_many_calls == [tumblers], (
            "expected exactly one resolve_many() call carrying all tumblers"
        )
        assert cat.resolve_calls == [], (
            f"expected ZERO per-doc resolve() calls for non-alias rows, got "
            f"{len(cat.resolve_calls)}"
        )

    def test_large_subtree_graph_traversal_via_one_batched_call(self, monkeypatch):
        n = 50
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        cat = _FakeCatalogSpy(desc, entries_by_tumbler=entries)
        _wire_dance(monkeypatch, cat)

        core.query("q", subtree="1.2", follow_links="cites")

        assert len(cat.graph_many_calls) == 1, (
            f"expected exactly one graph_many() call, got {len(cat.graph_many_calls)}"
        )
        assert sorted(cat.graph_many_calls[0]) == sorted(tumblers)
        assert cat.graph_calls == [], (
            f"expected ZERO per-doc graph() calls, got {len(cat.graph_calls)}"
        )

    def test_alias_row_resolves_to_canonical_via_bounded_followup(self, monkeypatch):
        """Alias semantics: resolve_many() does NOT follow alias_of (a flat
        identity lookup), so an alias row must get a per-doc resolve()
        follow-up to reach its canonical entry -- but ONLY for the alias
        subset, not the whole batch."""
        tumblers = ["1.2.0", "1.2.1", "1.2.2"]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        canonical = _FakeEntry("9.9.9")  # outside the subtree, by design
        entries = {
            "1.2.0": _FakeEntry("1.2.0"),
            "1.2.1": _FakeEntry("1.2.1", alias_of="9.9.9"),  # alias row
            "1.2.2": _FakeEntry("1.2.2"),
        }
        cat = _FakeCatalogSpy(
            desc, entries_by_tumbler=entries,
            canonical_by_alias={"1.2.1": canonical},
        )
        _wire_dance(monkeypatch, cat, target=["c1"])

        core.query("q", subtree="1.2", follow_links="cites")

        # Batched identity lookup covers everything in one call.
        assert cat.resolve_many_calls == [tumblers]
        # Only the ONE alias row pays a per-doc follow-up.
        assert cat.resolve_calls == ["1.2.1"], (
            f"expected exactly one alias follow-up resolve() call, got {cat.resolve_calls}"
        )
        # The canonical (not the raw alias) tumbler must be what gets
        # seeded into graph traversal.
        assert len(cat.graph_many_calls) == 1
        seeded = cat.graph_many_calls[0]
        assert "9.9.9" in seeded, f"canonical tumbler missing from graph seeds: {seeded}"
        assert "1.2.1" not in seeded, "raw alias tumbler must not be seeded directly"


# ═════════════════════════════════════════════════════════════════════════
# Substantive critique finding 2 (T2 nexus/chroma-residue-C2-durability-
# critique-2026-08-10) — the N+1 fix above collapsed N independent
# CatalogRepository.MAX_GRAPH_NODES=500 (service/**) per-seed budgets into
# ONE budget shared across ALL seeds in a single graph_many() call. These
# tests pin the batched fix: seeds chunked to core._GRAPH_MANY_BATCH_SIZE
# per call (restoring a per-batch budget), completeness (every seed reaches
# exactly one batch), and disclosure the moment a batch's response lands at
# the node-cap heuristic (core._GRAPH_MANY_NODE_CAP_HINT) — never silent.
# ═════════════════════════════════════════════════════════════════════════

class TestGraphManyBatching:
    def test_seeds_beyond_one_batch_split_into_multiple_calls(self, monkeypatch):
        """FAILS on the pre-fix single-call shape (which always sends
        exactly one graph_many() call regardless of seed count)."""
        n = core._GRAPH_MANY_BATCH_SIZE * 2 + 50
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        cat = _FakeCatalogSpy(desc, entries_by_tumbler=entries)
        _wire_dance(monkeypatch, cat)

        core.query("q", subtree="1.2", follow_links="cites")

        expected_batches = -(-n // core._GRAPH_MANY_BATCH_SIZE)  # ceil div
        assert len(cat.graph_many_calls) == expected_batches, (
            f"expected {expected_batches} batched graph_many() calls for "
            f"{n} seeds at batch size {core._GRAPH_MANY_BATCH_SIZE}, got "
            f"{len(cat.graph_many_calls)}"
        )
        assert all(len(c) <= core._GRAPH_MANY_BATCH_SIZE for c in cat.graph_many_calls), (
            "no single batch may exceed _GRAPH_MANY_BATCH_SIZE"
        )

    def test_every_seed_reaches_exactly_one_batch_call(self, monkeypatch):
        """Completeness: chunking must not silently drop or duplicate a
        seed across batches."""
        n = core._GRAPH_MANY_BATCH_SIZE * 3 + 7
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        cat = _FakeCatalogSpy(desc, entries_by_tumbler=entries)
        _wire_dance(monkeypatch, cat)

        core.query("q", subtree="1.2", follow_links="cites")

        seen = [s for batch in cat.graph_many_calls for s in batch]
        assert sorted(seen) == sorted(tumblers), (
            f"every seed must be sent in exactly one batch — sent "
            f"{len(seen)} seed-occurrences for {n} distinct seeds"
        )
        assert len(seen) == len(set(seen)), "no seed duplicated across batches"

    def test_small_subtree_stays_one_call_unchanged(self, monkeypatch):
        """Regression guard: below the batch size, behavior is unchanged
        from the original single-call shape."""
        n = 50
        assert n <= core._GRAPH_MANY_BATCH_SIZE
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        cat = _FakeCatalogSpy(desc, entries_by_tumbler=entries)
        _wire_dance(monkeypatch, cat)

        core.query("q", subtree="1.2", follow_links="cites")

        assert len(cat.graph_many_calls) == 1
        assert sorted(cat.graph_many_calls[0]) == sorted(tumblers)

    def test_batch_at_node_cap_is_disclosed_in_structured_output(self, monkeypatch):
        """FAILS on the pre-fix code: no `graph_scope` key existed at all,
        and the single shared-budget call meant truncation was invisible."""
        n = core._GRAPH_MANY_BATCH_SIZE + 10  # forces exactly 2 batches
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        capped_nodes = [_FakeEntry(f"9.9.{i}") for i in range(core._GRAPH_MANY_NODE_CAP_HINT)]
        cat = _FakeCatalogSpy(
            desc, entries_by_tumbler=entries,
            graph_many_nodes_by_call={0: capped_nodes},
        )
        _wire_dance(monkeypatch, cat)

        result = core.query("q", subtree="1.2", follow_links="cites", structured=True)

        assert isinstance(result, dict)
        assert result.get("graph_scope") == {
            "batches_total": 2, "batches_at_cap": 1, "possibly_incomplete": True,
        }, f"expected disclosed truncation, got {result.get('graph_scope')!r}"

    def test_batch_at_node_cap_is_disclosed_in_text_output(self, monkeypatch):
        n = core._GRAPH_MANY_BATCH_SIZE + 10
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        capped_nodes = [_FakeEntry(f"9.9.{i}") for i in range(core._GRAPH_MANY_NODE_CAP_HINT)]
        cat = _FakeCatalogSpy(
            desc, entries_by_tumbler=entries,
            graph_many_nodes_by_call={0: capped_nodes},
        )
        _wire_dance(monkeypatch, cat)

        result = core.query("q", subtree="1.2", follow_links="cites", structured=False)

        assert isinstance(result, str)
        assert "WARNING" in result and "INCOMPLETE" in result, result

    def test_no_cap_hit_means_no_disclosure_warning_in_text(self, monkeypatch) -> None:
        """Non-vacuity: the WARNING must NOT fire when no batch was capped —
        otherwise the disclosure mechanism is meaningless noise."""
        n = core._GRAPH_MANY_BATCH_SIZE + 10
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        cat = _FakeCatalogSpy(desc, entries_by_tumbler=entries)  # no cap-hit configured
        _wire_dance(monkeypatch, cat)

        result = core.query("q", subtree="1.2", follow_links="cites", structured=False)

        assert isinstance(result, str)
        assert "WARNING" not in result, result

    def test_nodes_deduped_across_batches(self, monkeypatch) -> None:
        """A node reachable from more than one batch's seeds must be merged,
        not duplicated, in the final linked-collection set."""
        n = core._GRAPH_MANY_BATCH_SIZE + 10
        tumblers = [f"1.2.{i}" for i in range(n)]
        desc = [{"tumbler": t, "physical_collection": "c1"} for t in tumblers]
        entries = {t: _FakeEntry(t) for t in tumblers}
        shared = _FakeEntry("9.9.9")
        shared.physical_collection = "c2"
        cat = _FakeCatalogSpy(
            desc, entries_by_tumbler=entries,
            graph_many_nodes_by_call={0: [shared], 1: [shared]},
        )
        _wire_dance(monkeypatch, cat, target=["c1", "c2"])

        result = core.query("q", subtree="1.2", follow_links="cites", structured=True)

        assert isinstance(result, dict)
        # "c2" must be routed exactly once — a dedup failure would not
        # necessarily show up as a duplicate collection name (sets already
        # collapse strings), but the graph merge itself must not error or
        # silently keep two conflicting node objects for "9.9.9".
        assert result.get("graph_scope") == {
            "batches_total": 2, "batches_at_cap": 0, "possibly_incomplete": False,
        }


# ═════════════════════════════════════════════════════════════════════════
# Defect 1 — unbounded + undisclosed seed list in the service-mode branch
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
