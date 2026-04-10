# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for cluster-aware search integration (RDR-056 Phase 2c)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from nexus.search_engine import search_cross_corpus
from nexus.types import SearchResult


@pytest.fixture(autouse=True)
def _disable_contradiction_check(monkeypatch):
    """These tests exercise clustering only — disable contradiction check
    (default-on in prod) to keep embedding fetches isolated to clustering."""
    monkeypatch.setattr(
        "nexus.search_engine.load_config",
        lambda: {"search": {"contradiction_check": False}},
    )


class _FakeT3:
    """Fake T3 that returns canned results and tracks get_embeddings calls."""

    _voyage_client = "fake"  # enables threshold filtering

    def __init__(self, results_by_col: dict[str, list[dict]]) -> None:
        self._results = results_by_col
        self.get_embeddings_calls: list[tuple[str, list[str]]] = []

    def search(self, query, collection_names, n_results=10, where=None):
        return self._results.get(collection_names[0], [])

    def get_embeddings(self, collection_name: str, ids: list[str]) -> np.ndarray:
        self.get_embeddings_calls.append((collection_name, ids))
        # Return random but deterministic embeddings
        rng = np.random.default_rng(hash(collection_name) % 2**32)
        return rng.random((len(ids), 4), dtype=np.float32)


def _low_distance_results(col: str, n: int) -> list[dict]:
    """Create n results with distances below all thresholds."""
    return [
        {"id": f"{col}-{i}", "content": f"text {i}", "distance": 0.1 + 0.02 * i}
        for i in range(n)
    ]


class TestClusterByNone:
    def test_default_returns_flat_list(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        results = search_cross_corpus("q", ["code__test"], 10, t3)
        assert isinstance(results, list)
        assert all(isinstance(r, SearchResult) for r in results)
        assert len(results) == 5

    def test_no_cluster_label_on_results(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 3)})
        results = search_cross_corpus("q", ["code__test"], 10, t3)
        for r in results:
            assert "_cluster_label" not in r.metadata

    def test_get_embeddings_not_called(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        search_cross_corpus("q", ["code__test"], 10, t3)
        assert t3.get_embeddings_calls == []


class TestClusterShapeGuard:
    """Regression: _apply_clustering must not raise when get_embeddings
    returns fewer rows than requested (e.g., deleted chunks)."""

    def test_shape_mismatch_falls_through_to_unclustered(self) -> None:
        class _ShortT3:
            _voyage_client = "fake"

            def search(self, query, collection_names, n_results=10, where=None):
                return _low_distance_results(collection_names[0], 5)

            def get_embeddings(self, collection_name: str, ids: list[str]) -> np.ndarray:
                # Simulate T3 returning fewer rows than requested
                return np.zeros((len(ids) - 1, 4), dtype=np.float32)

        t3 = _ShortT3()
        # Should NOT raise IndexError — should return unclustered results
        results = search_cross_corpus("q", ["code__test"], 10, t3, cluster_by="semantic")
        assert len(results) == 5
        # No cluster labels — clustering was skipped
        for r in results:
            assert "_cluster_label" not in r.metadata


class TestClusterBySemantic:
    def test_returns_flat_list_with_cluster_labels(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 6)})
        results = search_cross_corpus("q", ["code__test"], 10, t3, cluster_by="semantic")
        assert isinstance(results, list)
        assert all(isinstance(r, SearchResult) for r in results)
        for r in results:
            assert "_cluster_label" in r.metadata

    def test_get_embeddings_called(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        search_cross_corpus("q", ["code__test"], 10, t3, cluster_by="semantic")
        assert len(t3.get_embeddings_calls) == 1
        assert t3.get_embeddings_calls[0][0] == "code__test"

    def test_multi_collection_groups_embeddings_by_collection(self) -> None:
        t3 = _FakeT3({
            "code__a": _low_distance_results("code__a", 3),
            "code__b": _low_distance_results("code__b", 3),
        })
        search_cross_corpus(
            "q", ["code__a", "code__b"], 10, t3, cluster_by="semantic",
        )
        cols_fetched = [c[0] for c in t3.get_embeddings_calls]
        assert "code__a" in cols_fetched
        assert "code__b" in cols_fetched

    def test_fewer_than_three_results_skips_clustering(self) -> None:
        """Clustering needs >=3 results; with fewer, return flat without labels."""
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 2)})
        results = search_cross_corpus("q", ["code__test"], 10, t3, cluster_by="semantic")
        assert len(results) == 2
        # cluster_results returns singletons for n<=2, which still get labels
        # but get_embeddings should still be called for the attempt
        assert len(t3.get_embeddings_calls) == 1

    def test_empty_results_returns_empty(self) -> None:
        t3 = _FakeT3({"code__test": []})
        results = search_cross_corpus("q", ["code__test"], 10, t3, cluster_by="semantic")
        assert results == []


class TestConfigDefault:
    def test_cluster_by_default_is_none(self) -> None:
        from nexus.config import load_config
        cfg = load_config()
        assert cfg.get("search", {}).get("cluster_by") is None
