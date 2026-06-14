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
                 topic_rows_by_col: dict[str, list[dict]] | None = None) -> None:
        self.meta_rows = meta_rows or []
        self.topic_rows_by_col = topic_rows_by_col or {}
        self.meta_calls: list[tuple] = []
        self.topic_calls: list[tuple] = []

    def search_metadata_scoped(self, query, collection_names, *, content_type=None,
                               author=None, year=None, corpus=None, n_results=10):
        self.meta_calls.append((query, list(collection_names), content_type, author,
                                year, corpus, n_results))
        return self.meta_rows

    def search_topic_scoped(self, query, topic, collection, *, n_results=10):
        self.topic_calls.append((query, topic, collection, n_results))
        return self.topic_rows_by_col.get(collection, [])


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

        out = core.search_metadata_scoped(
            "q", corpus="knowledge", content_type="paper", author="alice",
            year=2024, limit=5, structured=True)

        assert out == {
            "ids": ["1.2.3", "1.2.4"],
            "tumblers": ["1.2.3", "1.2.4"],   # document-level: tumblers == ids
            "distances": [0.1, 0.3],
            "collections": ["c1", "c1"],
        }
        # filters forwarded; year=0/"" → None handled by the tool
        assert t3.meta_calls == [("q", ["c1"], "paper", "alice", 2024, None, 5)]

    def test_empty_filters_become_none(self, monkeypatch):
        t3 = _FakeServiceT3(meta_rows=[])
        _wire(monkeypatch, t3, ["c1"])
        core.search_metadata_scoped("q", corpus="knowledge", structured=True)
        assert t3.meta_calls == [("q", ["c1"], None, None, None, None, 10)]

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
        assert [c[2] for c in t3.topic_calls] == ["c1", "c2"]

    def test_non_service_mode_errors(self, monkeypatch):
        t3 = _FakeServiceT3()
        _wire(monkeypatch, t3, ["c1"], service=False)
        out = core.search_topic_scoped("q", "T", structured=True)
        assert isinstance(out, str) and out.startswith("Error:")


class TestPlanRunnerRegistration:
    def test_tools_registered_as_retrieval(self):
        from nexus.plans.runner import _RETRIEVAL_TOOLS
        assert "search_metadata_scoped" in _RETRIEVAL_TOOLS
        assert "search_topic_scoped" in _RETRIEVAL_TOOLS

    def test_tools_resolvable_on_core(self):
        assert callable(getattr(core, "search_metadata_scoped"))
        assert callable(getattr(core, "search_topic_scoped"))
