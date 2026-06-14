# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-156 P4.2b (nexus-joesk): HttpVectorClient combined-query methods.

``search_metadata_scoped`` and ``search_topic_scoped`` are the client legs for
``POST /v1/vectors/search-metadata-scoped`` and ``/search-topic-scoped`` — the
combined-query functions (catalog-006) the Python ``query`` composition repoints
onto. These tests pin the request envelope (path, body keys, NULL-filter
omission) and the flat-list passthrough; the SQL semantics are pinned engine-side
by CombinedQueryParityTest / PgVectorCombinedQueryContractTest.
"""
from __future__ import annotations

from typing import Any

from nexus.db.http_vector_client import HttpVectorClient

META_PATH = "/v1/vectors/search-metadata-scoped"
TOPIC_PATH = "/v1/vectors/search-topic-scoped"
GRAPH_PATH = "/v1/vectors/search-graph-hop"


def _patch_post(monkeypatch, handler) -> list[tuple[str, dict]]:
    """Patch module-level _post, recording (path, body) calls."""
    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, body: dict, *, tenant: str = "default",
                  timeout: int = 120) -> Any:
        calls.append((path, body))
        return handler(path, body)

    monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
    return calls


class TestSearchMetadataScoped:
    def test_posts_to_metadata_route_with_filters(self, monkeypatch):
        rows = [{"id": "1.2.3", "content": "x", "distance": 0.1,
                 "collection": "code__a__model-x__v1"}]
        calls = _patch_post(monkeypatch, lambda p, b: rows)

        got = HttpVectorClient().search_metadata_scoped(
            "how does x work", ["code__a__model-x__v1"],
            content_type="paper", author="alice", year=2024, corpus="research",
            n_results=5)

        assert got == rows
        assert len(calls) == 1
        path, body = calls[0]
        assert path == META_PATH
        assert body == {
            "query": "how does x work",
            "collections": ["code__a__model-x__v1"],
            "content_type": "paper",
            "author": "alice",
            "year": 2024,
            "corpus": "research",
            "n_results": 5,
        }

    def test_omits_none_filters_from_body(self, monkeypatch):
        calls = _patch_post(monkeypatch, lambda p, b: [])

        HttpVectorClient().search_metadata_scoped(
            "q", ["code__a__model-x__v1"], content_type="paper")

        _, body = calls[0]
        assert body == {
            "query": "q",
            "collections": ["code__a__model-x__v1"],
            "content_type": "paper",
            "n_results": 10,
        }
        assert "author" not in body
        assert "year" not in body
        assert "corpus" not in body

    def test_returns_empty_list_passthrough(self, monkeypatch):
        _patch_post(monkeypatch, lambda p, b: [])
        assert HttpVectorClient().search_metadata_scoped("q", ["code__a__model-x__v1"]) == []


class TestSearchTopicScoped:
    def test_posts_to_topic_route(self, monkeypatch):
        rows = [{"id": "abc123", "content": "y", "distance": 0.0,
                 "collection": "knowledge__b__minilm-l6-v2-384__v1"}]
        calls = _patch_post(monkeypatch, lambda p, b: rows)

        got = HttpVectorClient().search_topic_scoped(
            "vector search", "Vector Search",
            "knowledge__b__minilm-l6-v2-384__v1", n_results=3)

        assert got == rows
        path, body = calls[0]
        assert path == TOPIC_PATH
        assert body == {
            "query": "vector search",
            "topic": "Vector Search",
            "collection": "knowledge__b__minilm-l6-v2-384__v1",
            "n_results": 3,
        }

    def test_default_n_results(self, monkeypatch):
        calls = _patch_post(monkeypatch, lambda p, b: [])
        HttpVectorClient().search_topic_scoped("q", "T", "knowledge__b__minilm-l6-v2-384__v1")
        _, body = calls[0]
        assert body["n_results"] == 10


class TestSearchGraphHop:
    """nexus-houg9: graph-hop client leg for POST /v1/vectors/search-graph-hop."""

    def test_posts_to_graph_route_with_link_type(self, monkeypatch):
        rows = [{"id": "1.2.3", "content": "z", "distance": 0.1,
                 "collection": "rdr__a__model-x__v1",
                 "chash": "0" * 32}]
        calls = _patch_post(monkeypatch, lambda p, b: rows)

        got = HttpVectorClient().search_graph_hop(
            "how does x work", ["1.2.3"], ["rdr__a__model-x__v1"],
            link_type="cites", depth=2, direction="out", n_results=5)

        assert got == rows
        assert len(calls) == 1
        path, body = calls[0]
        assert path == GRAPH_PATH
        assert body == {
            "query": "how does x work",
            "seeds": ["1.2.3"],
            "collections": ["rdr__a__model-x__v1"],
            "link_type": "cites",
            "depth": 2,
            "direction": "out",
            "n_results": 5,
        }

    def test_omits_none_link_type_and_defaults(self, monkeypatch):
        calls = _patch_post(monkeypatch, lambda p, b: [])

        HttpVectorClient().search_graph_hop("q", ["1.1"], ["rdr__a__model-x__v1"])

        _, body = calls[0]
        assert body == {
            "query": "q",
            "seeds": ["1.1"],
            "collections": ["rdr__a__model-x__v1"],
            "depth": 1,
            "direction": "both",
            "n_results": 10,
        }
        assert "link_type" not in body

    def test_returns_empty_list_passthrough(self, monkeypatch):
        _patch_post(monkeypatch, lambda p, b: [])
        assert HttpVectorClient().search_graph_hop(
            "q", ["1.1"], ["rdr__a__model-x__v1"]) == []
