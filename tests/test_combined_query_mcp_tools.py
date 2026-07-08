# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-156 P4.2b (nexus-joesk): combined-query MCP tools.

``search_metadata_scoped`` / ``search_topic_scoped`` in ``nexus.mcp.core`` are
the plan-runner-consumable primitives wrapping the catalog-006 combined-query
functions (via HttpVectorClient). These tests pin: corpus→collection routing,
the service-mode gate, the client call shape, and the structured
``{ids, tumblers, distances, collections}`` envelope the plan runner consumes.
"""
from __future__ import annotations

from typing import Any

from nexus.mcp import core


class _FakeServiceT3:
    """Records combined-query client calls; stands in for HttpVectorClient."""

    def __init__(self, meta_rows: list[dict] | None = None,
                 topic_rows_by_col: dict[str, list[dict]] | None = None,
                 graph_rows: list[dict] | None = None) -> None:
        self.meta_rows = meta_rows or []
        self.topic_rows_by_col = topic_rows_by_col or {}
        self.graph_rows = graph_rows or []
        self.meta_calls: list[tuple] = []
        self.topic_calls: list[tuple] = []
        self.graph_calls: list[tuple] = []

    def search_metadata_scoped(self, query, collection_names, *, content_type=None,
                               author=None, year=None, corpus=None, subtree=None,
                               where=None, n_results=10):
        self.meta_calls.append((query, list(collection_names), content_type, author,
                                year, corpus, subtree, where, n_results))
        return self.meta_rows

    def search_topic_scoped(self, query, topic, collection, *, n_results=10):
        self.topic_calls.append((query, topic, collection, n_results))
        return self.topic_rows_by_col.get(collection, [])

    def search_graph_hop(self, query, seeds, collection_names, *, link_type=None,
                         depth=1, direction="both", where=None, n_results=10):
        self.graph_calls.append((query, list(seeds), list(collection_names),
                                 link_type, depth, direction, where, n_results))
        return self.graph_rows


def _wire(monkeypatch, t3, target, *, service=True):
    monkeypatch.setattr(core, "_get_t3", lambda: t3)
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3: target)
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_service_backed", lambda db: service)


class TestSearchMetadataScopedTool:
    def test_structured_doc_level_ids_are_tumblers(self, monkeypatch):
        rows = [
            {"id": "1.2.3", "content": "a", "distance": 0.1, "collection": "c1"},
            {"id": "1.2.4", "content": "b", "distance": 0.3, "collection": "c1"},
        ]
        t3 = _FakeServiceT3(meta_rows=rows)
        _wire(monkeypatch, t3, ["c1"])

        rows[0]["chash"] = "a" * 32
        rows[1]["chash"] = "b" * 32
        out = core.search_metadata_scoped(
            "q", corpus="knowledge", content_type="paper", author="alice",
            year=2024, subtree="1.2", where="lang=java", limit=5, structured=True)

        assert out == {
            "ids": ["1.2.3", "1.2.4"],
            "tumblers": ["1.2.3", "1.2.4"],   # document-level: tumblers == ids
            "distances": [0.1, 0.3],
            "collections": ["c1", "c1"],
            "contents": ["a", "b"],
            "chashes": ["a" * 32, "b" * 32],
        }
        # filters forwarded; year=0/"" → None; where string parsed to a dict
        assert t3.meta_calls == [
            ("q", ["c1"], "paper", "alice", 2024, None, "1.2", {"lang": "java"}, 5)]

    def test_empty_filters_become_none(self, monkeypatch):
        t3 = _FakeServiceT3(meta_rows=[])
        _wire(monkeypatch, t3, ["c1"])
        core.search_metadata_scoped("q", corpus="knowledge", structured=True)
        assert t3.meta_calls == [
            ("q", ["c1"], None, None, None, None, None, None, 10)]

    def test_where_multi_pair_parsed(self, monkeypatch):
        t3 = _FakeServiceT3(meta_rows=[])
        _wire(monkeypatch, t3, ["c1"])
        core.search_metadata_scoped("q", where="lang=java, kind=fn", structured=True)
        assert t3.meta_calls[0][7] == {"lang": "java", "kind": "fn"}

    def test_where_range_operator_rejected_loudly(self, monkeypatch):
        # Equality-only: a comparison op must error, NOT silently parse to a bogus key
        # ("bib_year>") that matches nothing and drops the filter (nexus-889ff review C2).
        t3 = _FakeServiceT3(meta_rows=[])
        _wire(monkeypatch, t3, ["c1"])
        out = core.search_metadata_scoped("q", where="bib_year>=2020", structured=True)
        assert isinstance(out, str) and out.startswith("Error:") and "equality-only" in out
        assert t3.meta_calls == []  # rejected before dispatch

    def test_where_numeric_equality_typed(self, monkeypatch):
        # parse_where_str coerces known numeric fields → JSONB number, not string.
        t3 = _FakeServiceT3(meta_rows=[])
        _wire(monkeypatch, t3, ["c1"])
        core.search_metadata_scoped("q", where="page_count=3", structured=True)
        assert t3.meta_calls[0][7] == {"page_count": 3}

    def test_non_service_mode_errors(self, monkeypatch):
        t3 = _FakeServiceT3()
        _wire(monkeypatch, t3, ["c1"], service=False)
        out = core.search_metadata_scoped("q", structured=True)
        assert isinstance(out, str) and out.startswith("Error:")
        assert t3.meta_calls == []

    def test_multichunk_doc_deduped_keeps_best_distance(self, monkeypatch):
        # The SQL function returns one row per matching chunk (distance-asc), so a
        # 2-chunk document repeats its tumbler. The tool collapses to one row per
        # id at the best distance — document-level contract.
        rows = [
            {"id": "1.2.3", "content": "near chunk", "distance": 0.1, "collection": "c1"},
            {"id": "1.2.4", "content": "other doc",  "distance": 0.2, "collection": "c1"},
            {"id": "1.2.3", "content": "far chunk",  "distance": 0.9, "collection": "c1"},
        ]
        t3 = _FakeServiceT3(meta_rows=rows)
        _wire(monkeypatch, t3, ["c1"])
        out = core.search_metadata_scoped("q", structured=True)
        assert out["ids"] == ["1.2.3", "1.2.4"]
        assert out["distances"] == [0.1, 0.2]  # 1.2.3 kept at its best (0.1), not 0.9
        assert out["contents"] == ["near chunk", "other doc"]  # best-distance row's content


class TestSearchTopicScopedTool:
    def test_structured_chunk_level_merges_collections(self, monkeypatch):
        rows = {
            "c1": [{"id": "h1", "content": "x", "distance": 0.4, "collection": "c1"}],
            "c2": [{"id": "h2", "content": "y", "distance": 0.1, "collection": "c2"}],
        }
        t3 = _FakeServiceT3(topic_rows_by_col=rows)
        _wire(monkeypatch, t3, ["c1", "c2"])

        out = core.search_topic_scoped("q", "Vector Search", corpus="all",
                                       limit=10, structured=True)

        # merged across collections, sorted by distance ascending
        assert out["ids"] == ["h2", "h1"]
        assert out["distances"] == [0.1, 0.4]
        assert out["collections"] == ["c2", "c1"]
        # chunk-level: no document tumblers
        assert out["tumblers"] == ["", ""]
        assert out["contents"] == ["y", "x"]  # inline contents, distance order
        assert [c[2] for c in t3.topic_calls] == ["c1", "c2"]

    def test_non_service_mode_errors(self, monkeypatch):
        t3 = _FakeServiceT3()
        _wire(monkeypatch, t3, ["c1"], service=False)
        out = core.search_topic_scoped("q", "T", structured=True)
        assert isinstance(out, str) and out.startswith("Error:")


class TestSearchGraphHopTool:
    """nexus-houg9: graph-hop tool — doc-level dedup + chash in the structured shape."""

    def test_operator_where_rejected_loudly(self, monkeypatch):
        # nexus-7ndh3 critique CRITICAL-1: JSONB containment is equality-only;
        # an operator where must error, never silently containment-fail to zero.
        t3 = _FakeServiceT3(graph_rows=[])
        _wire(monkeypatch, t3, ["c1"])

        out = core.search_graph_hop("q", "1.2.0", corpus="rdr", where="bib_year>=2020")

        assert "equality-only" in out
        assert not t3.graph_calls, "the combined-query call must NOT be made"

    def test_where_string_parsed_and_passed(self, monkeypatch):
        # nexus-7ndh3: the KEY=VALUE where string reaches the client as a dict.
        t3 = _FakeServiceT3(graph_rows=[])
        _wire(monkeypatch, t3, ["c1"])

        core.search_graph_hop("q", "1.2.0", corpus="rdr", where="lang=python")

        assert t3.graph_calls, "graph-hop must be called"
        assert t3.graph_calls[0][-2] == {"lang": "python"}

    def test_structured_doc_level_with_chashes(self, monkeypatch):
        rows = [
            {"id": "1.2.3", "content": "a", "distance": 0.1, "collection": "c1",
             "chash": "a" * 32},
            {"id": "1.2.4", "content": "b", "distance": 0.3, "collection": "c1",
             "chash": "b" * 32},
        ]
        t3 = _FakeServiceT3(graph_rows=rows)
        _wire(monkeypatch, t3, ["c1"])

        out = core.search_graph_hop(
            "q", ["1.2.0"], corpus="rdr", link_type="cites", depth=2,
            direction="out", limit=5, structured=True)

        assert out == {
            "ids": ["1.2.3", "1.2.4"],
            "tumblers": ["1.2.3", "1.2.4"],
            "distances": [0.1, 0.3],
            "collections": ["c1", "c1"],
            "contents": ["a", "b"],
            "chashes": ["a" * 32, "b" * 32],
        }
        assert t3.graph_calls == [("q", ["1.2.0"], ["c1"], "cites", 2, "out", None, 5)]

    def test_single_string_seed_accepted(self, monkeypatch):
        t3 = _FakeServiceT3(graph_rows=[])
        _wire(monkeypatch, t3, ["c1"])
        core.search_graph_hop("q", "1.2.0", corpus="rdr", structured=True)
        assert t3.graph_calls == [("q", ["1.2.0"], ["c1"], None, 1, "both", None, 10)]

    def test_empty_seeds_short_circuit(self, monkeypatch):
        t3 = _FakeServiceT3(graph_rows=[])
        _wire(monkeypatch, t3, ["c1"])
        out = core.search_graph_hop("q", [], corpus="rdr")
        assert isinstance(out, str) and "No seeds" in out
        assert t3.graph_calls == []

    def test_multichunk_doc_deduped_keeps_best_distance(self, monkeypatch):
        rows = [
            {"id": "1.2.3", "content": "near", "distance": 0.1, "collection": "c1",
             "chash": "a" * 32},
            {"id": "1.2.4", "content": "other", "distance": 0.2, "collection": "c1",
             "chash": "c" * 32},
            {"id": "1.2.3", "content": "far", "distance": 0.9, "collection": "c1",
             "chash": "d" * 32},
        ]
        t3 = _FakeServiceT3(graph_rows=rows)
        _wire(monkeypatch, t3, ["c1"])
        out = core.search_graph_hop("q", ["1.2.0"], structured=True)
        assert out["ids"] == ["1.2.3", "1.2.4"]
        assert out["distances"] == [0.1, 0.2]
        assert out["chashes"] == ["a" * 32, "c" * 32]  # best-distance row's chash

    def test_non_service_mode_errors(self, monkeypatch):
        t3 = _FakeServiceT3()
        _wire(monkeypatch, t3, ["c1"], service=False)
        out = core.search_graph_hop("q", ["1.2.0"], structured=True)
        assert isinstance(out, str) and out.startswith("Error:")
        assert t3.graph_calls == []


class TestPlanRunnerRegistration:
    def test_tools_registered_as_retrieval(self):
        from nexus.plans.runner import _RETRIEVAL_TOOLS
        assert "search_metadata_scoped" in _RETRIEVAL_TOOLS
        assert "search_topic_scoped" in _RETRIEVAL_TOOLS
        assert "search_graph_hop" in _RETRIEVAL_TOOLS

    def test_tools_resolvable_on_core(self):
        assert callable(getattr(core, "search_metadata_scoped"))
        assert callable(getattr(core, "search_topic_scoped"))
        assert callable(getattr(core, "search_graph_hop"))
