# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for per-corpus distance thresholds (RDR-056 Phase 1c)."""
from __future__ import annotations

from nexus.search_engine import _threshold_for_collection, search_cross_corpus
from nexus.types import SearchResult


class TestThresholdForCollection:
    def test_code_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"code": 0.45}}}
        assert _threshold_for_collection("code__nexus", cfg) == 0.45

    def test_knowledge_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"knowledge": 0.65}}}
        assert _threshold_for_collection("knowledge__papers", cfg) == 0.65

    def test_docs_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"docs": 0.65}}}
        assert _threshold_for_collection("docs__corpus", cfg) == 0.65

    def test_rdr_collection_threshold(self) -> None:
        cfg = {"search": {"distance_threshold": {"rdr": 0.65}}}
        assert _threshold_for_collection("rdr__reviews", cfg) == 0.65

    def test_default_threshold_for_unknown_prefix(self) -> None:
        cfg = {"search": {"distance_threshold": {"default": 0.55}}}
        assert _threshold_for_collection("custom__stuff", cfg) == 0.55

    def test_none_when_no_threshold_config(self) -> None:
        cfg: dict = {"search": {}}
        assert _threshold_for_collection("code__nexus", cfg) is None

    def test_uses_default_config_when_not_overridden(self) -> None:
        from nexus.config import load_config
        cfg = load_config()
        assert _threshold_for_collection("code__nexus", cfg) == 0.45
        assert _threshold_for_collection("knowledge__x", cfg) == 0.65
        assert _threshold_for_collection("docs__x", cfg) == 0.65
        assert _threshold_for_collection("rdr__x", cfg) == 0.65
        assert _threshold_for_collection("other__x", cfg) == 0.55


class TestSearchCrossCorpusThresholdFiltering:
    """Tests that search_cross_corpus filters results exceeding thresholds."""

    class _FakeT3:
        _voyage_client = "fake-voyage"  # Enables threshold filtering

        def __init__(self, results_by_col: dict[str, list[dict]]) -> None:
            self._results = results_by_col

        def search(self, query, collection_names, n_results=10, where=None):
            return self._results.get(collection_names[0], [])

    def test_code_result_above_threshold_filtered(self) -> None:
        """code__nexus result with distance=0.50 is filtered (>0.45)."""
        t3 = self._FakeT3({
            "code__nexus": [
                {"id": "a", "content": "good", "distance": 0.30},
                {"id": "b", "content": "noise", "distance": 0.50},
            ],
        })
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert len(results) == 1
        assert results[0].id == "a"

    def test_knowledge_result_below_threshold_passes(self) -> None:
        """knowledge__papers result with distance=0.60 passes (<=0.65)."""
        t3 = self._FakeT3({
            "knowledge__papers": [
                {"id": "a", "content": "relevant", "distance": 0.60},
            ],
        })
        results = search_cross_corpus("test", ["knowledge__papers"], 10, t3)
        assert len(results) == 1

    def test_knowledge_result_above_threshold_filtered(self) -> None:
        """knowledge__papers result with distance=0.70 is filtered (>0.65)."""
        t3 = self._FakeT3({
            "knowledge__papers": [
                {"id": "a", "content": "noise", "distance": 0.70},
            ],
        })
        results = search_cross_corpus("test", ["knowledge__papers"], 10, t3)
        assert len(results) == 0

    def test_cross_corpus_default_threshold(self) -> None:
        """Unknown prefix uses default threshold (0.55)."""
        t3 = self._FakeT3({
            "custom__stuff": [
                {"id": "a", "content": "ok", "distance": 0.52},
                {"id": "b", "content": "noise", "distance": 0.60},
            ],
        })
        results = search_cross_corpus("test", ["custom__stuff"], 10, t3)
        assert len(results) == 1
        assert results[0].id == "a"

    def test_at_threshold_passes(self) -> None:
        """Result exactly at threshold passes (<=, not <)."""
        t3 = self._FakeT3({
            "code__nexus": [
                {"id": "a", "content": "edge", "distance": 0.45},
            ],
        })
        results = search_cross_corpus("test", ["code__nexus"], 10, t3)
        assert len(results) == 1

    def test_non_voyage_skips_thresholds(self) -> None:
        """Non-Voyage embeddings (ONNX MiniLM) skip Voyage-calibrated thresholds."""
        class _NonVoyageT3:
            _voyage_client = None
            def search(self, query, collection_names, n_results=10, where=None):
                return [{"id": "a", "content": "x", "distance": 0.90}]
        results = search_cross_corpus("test", ["code__nexus"], 10, _NonVoyageT3())
        assert len(results) == 1  # 0.90 > 0.45 but NOT filtered without Voyage

    def test_multi_corpus_applies_per_corpus_threshold(self) -> None:
        """Different thresholds applied per corpus in cross-corpus search."""
        t3 = self._FakeT3({
            "code__nexus": [
                {"id": "c1", "content": "code", "distance": 0.40},
                {"id": "c2", "content": "code noise", "distance": 0.48},
            ],
            "knowledge__papers": [
                {"id": "k1", "content": "knowledge", "distance": 0.60},
                {"id": "k2", "content": "know noise", "distance": 0.70},
            ],
        })
        results = search_cross_corpus(
            "test", ["code__nexus", "knowledge__papers"], 10, t3,
        )
        ids = {r.id for r in results}
        assert ids == {"c1", "k1"}
