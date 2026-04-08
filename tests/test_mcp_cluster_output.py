# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-056: MCP search output preserves cluster grouping and labels."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import chromadb
import pytest

from nexus.db.t3 import T3Database
from nexus.mcp_server import _inject_t3, _reset_singletons, search
from nexus.types import SearchResult


@pytest.fixture(autouse=True)
def _reset():
    _reset_singletons()
    yield
    _reset_singletons()


def _make_results_with_clusters() -> list[SearchResult]:
    """Build results simulating post-clustering output with _cluster_label."""
    return [
        # Cluster A — two results
        SearchResult(
            id="a1", content="HNSW tail failures in approximate search",
            distance=0.41, collection="knowledge__papers",
            metadata={"_cluster_label": "HNSW Robustness", "title": "HNSW paper"},
        ),
        SearchResult(
            id="a2", content="Graph-based index failures compound in pipelines",
            distance=0.52, collection="knowledge__papers",
            metadata={"_cluster_label": "HNSW Robustness", "title": "Pipeline paper"},
        ),
        # Cluster B — two results
        SearchResult(
            id="b1", content="Ward hierarchical clustering groups results",
            distance=0.45, collection="docs__manual",
            metadata={"_cluster_label": "Result Clustering", "title": "Clustering doc"},
        ),
        SearchResult(
            id="b2", content="Semantic grouping improves LLM comprehension",
            distance=0.55, collection="docs__manual",
            metadata={"_cluster_label": "Result Clustering", "title": "LLM doc"},
        ),
    ]


class TestClusterOutputFormat:
    """MCP search output renders cluster labels and preserves cluster order."""

    def test_cluster_labels_in_output(self):
        """When cluster_by=semantic, output includes cluster header lines."""
        mock_t3 = MagicMock()
        mock_t3.list_collections.return_value = [
            {"name": "knowledge__papers", "count": 10},
        ]
        _inject_t3(mock_t3)

        results = _make_results_with_clusters()

        def fake_search(query, collections, n_results, t3, where=None, **kwargs):
            return results

        with patch("nexus.search_engine.search_cross_corpus", fake_search):
            output = search("test query", corpus="knowledge,docs", cluster_by="semantic")

        # Cluster headers should appear
        assert "HNSW Robustness" in output
        assert "Result Clustering" in output

    def test_cluster_order_preserved(self):
        """Results within a cluster stay grouped, not re-sorted globally by distance."""
        mock_t3 = MagicMock()
        mock_t3.list_collections.return_value = [
            {"name": "knowledge__papers", "count": 10},
            {"name": "docs__manual", "count": 5},
        ]
        _inject_t3(mock_t3)

        results = _make_results_with_clusters()

        def fake_search(query, collections, n_results, t3, where=None, **kwargs):
            return results

        with patch("nexus.search_engine.search_cross_corpus", fake_search):
            output = search("test query", corpus="knowledge,docs", cluster_by="semantic")

        # a1 (0.41) and a2 (0.52) should appear before b1 (0.45) and b2 (0.55)
        # because they're in cluster A which has best distance 0.41
        pos_a1 = output.find("HNSW tail failures")
        pos_a2 = output.find("Graph-based index")
        pos_b1 = output.find("Ward hierarchical")
        pos_b2 = output.find("Semantic grouping")
        assert pos_a1 < pos_a2, "a1 before a2 within cluster A"
        assert pos_a2 < pos_b1, "cluster A before cluster B"
        assert pos_b1 < pos_b2, "b1 before b2 within cluster B"

    def test_flat_search_no_cluster_headers(self):
        """When cluster_by is empty, output has no cluster headers."""
        mock_t3 = MagicMock()
        mock_t3.list_collections.return_value = [
            {"name": "knowledge__papers", "count": 10},
        ]
        _inject_t3(mock_t3)

        results = [
            SearchResult(
                id="r1", content="some result",
                distance=0.3, collection="knowledge__papers",
                metadata={"title": "Paper"},
            ),
        ]

        def fake_search(query, collections, n_results, t3, where=None, **kwargs):
            return results

        with patch("nexus.search_engine.search_cross_corpus", fake_search):
            output = search("test query", corpus="knowledge")

        # No cluster separator lines
        assert "---" not in output.split("\n--- showing")[0]
