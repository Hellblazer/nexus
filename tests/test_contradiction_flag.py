# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for JIT contradiction flag in search results (RDR-057 P3-3a, nexus-vwnz)."""
from __future__ import annotations

import numpy as np
import pytest

from nexus.types import SearchResult


def _make_result(
    id: str,
    collection: str,
    source_agent: str = "",
    distance: float = 0.5,
) -> SearchResult:
    meta = {}
    if source_agent:
        meta["source_agent"] = source_agent
    return SearchResult(
        id=id,
        content=f"content of {id}",
        distance=distance,
        collection=collection,
        metadata=meta,
    )


def _embeddings(vectors: list[list[float]]) -> np.ndarray:
    return np.array(vectors, dtype=np.float32)


class TestFlagContradictions:
    """Unit tests for _flag_contradictions() (takes pre-fetched embeddings)."""

    def test_same_collection_different_agent_close_distance(self) -> None:
        """Two results, same collection, different source_agent, distance < 0.3 → flagged."""
        from nexus.search_engine import _flag_contradictions

        # Nearly identical embeddings → cosine distance ≈ 0
        embs = _embeddings([[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]])
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "code__x", source_agent="agent-beta"),
        ]
        out = _flag_contradictions(results, embs)
        assert out[0].metadata.get("_contradiction_flag") is True
        assert out[1].metadata.get("_contradiction_flag") is True

    def test_same_collection_same_agent_no_flag(self) -> None:
        """Same source_agent → no flag (same provenance)."""
        from nexus.search_engine import _flag_contradictions

        embs = _embeddings([[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]])
        results = [
            _make_result("a", "code__x", source_agent="same-agent"),
            _make_result("b", "code__x", source_agent="same-agent"),
        ]
        out = _flag_contradictions(results, embs)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata

    def test_same_collection_different_agent_far_distance_no_flag(self) -> None:
        """Cosine distance >= 0.3 → no flag."""
        from nexus.search_engine import _flag_contradictions

        # Orthogonal embeddings → cosine distance = 1.0
        embs = _embeddings([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "code__x", source_agent="agent-beta"),
        ]
        out = _flag_contradictions(results, embs)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata

    def test_different_collections_no_flag(self) -> None:
        """Cross-collection pairs → no flag (different scopes)."""
        from nexus.search_engine import _flag_contradictions

        embs = _embeddings([[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]])
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "docs__y", source_agent="agent-beta"),
        ]
        out = _flag_contradictions(results, embs)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata

    def test_single_result_no_flag(self) -> None:
        """Single result → nothing to compare."""
        from nexus.search_engine import _flag_contradictions

        embs = _embeddings([[1.0, 0.0, 0.0]])
        results = [_make_result("a", "code__x", source_agent="agent-alpha")]
        out = _flag_contradictions(results, embs)
        assert "_contradiction_flag" not in out[0].metadata

    def test_three_results_only_contradicting_pair_flagged(self) -> None:
        """A and B contradict; C does not → only A and B flagged."""
        from nexus.search_engine import _flag_contradictions

        embs = _embeddings([
            [1.0, 0.0, 0.0],
            [0.99, 0.01, 0.0],  # close to a
            [0.0, 1.0, 0.0],    # far from both
        ])
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "code__x", source_agent="agent-beta"),
            _make_result("c", "code__x", source_agent="agent-gamma"),
        ]
        out = _flag_contradictions(results, embs)
        assert out[0].metadata.get("_contradiction_flag") is True   # a
        assert out[1].metadata.get("_contradiction_flag") is True   # b
        assert "_contradiction_flag" not in out[2].metadata         # c

    def test_empty_source_agent_no_flag(self) -> None:
        """Empty source_agent → no provenance conflict."""
        from nexus.search_engine import _flag_contradictions

        embs = _embeddings([[1.0, 0.0, 0.0], [0.99, 0.01, 0.0]])
        results = [
            _make_result("a", "code__x"),  # no source_agent
            _make_result("b", "code__x"),
        ]
        out = _flag_contradictions(results, embs)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata


class TestFetchEmbeddingsForResults:
    """Tests for the shared embedding-fetch helper (F1 fix + R3-1 fail-per-collection)."""

    def test_fetch_all_fail_returns_none_with_all_indices_failed(self) -> None:
        """When every collection fetch fails, returns (None, all_indices)."""
        from nexus.search_engine import _fetch_embeddings_for_results

        class _BrokenT3:
            def get_embeddings(self, col, ids):
                raise RuntimeError("simulated failure")

        results = [_make_result("a", "code__x"), _make_result("b", "code__x")]
        embs, failed = _fetch_embeddings_for_results(results, _BrokenT3())
        assert embs is None
        assert failed == {0, 1}

    def test_fetch_shape_mismatch_marks_collection_failed(self) -> None:
        """Shape mismatch marks the collection's indices as failed, doesn't crash."""
        from nexus.search_engine import _fetch_embeddings_for_results

        class _ShortT3:
            def get_embeddings(self, col, ids):
                return np.zeros((len(ids) - 1, 4), dtype=np.float32)

        results = [_make_result("a", "code__x"), _make_result("b", "code__x")]
        embs, failed = _fetch_embeddings_for_results(results, _ShortT3())
        assert embs is None  # no successful fetches
        assert failed == {0, 1}

    def test_fetch_assembles_multi_collection_in_result_order(self) -> None:
        """Multi-collection fetch preserves result order."""
        from nexus.search_engine import _fetch_embeddings_for_results

        class _SplitT3:
            def get_embeddings(self, col, ids):
                if col == "code__x":
                    return np.array([[1.0, 0.0]], dtype=np.float32)
                if col == "docs__y":
                    return np.array([[0.0, 1.0]], dtype=np.float32)
                raise KeyError(col)

        results = [
            _make_result("a", "code__x"),
            _make_result("b", "docs__y"),
        ]
        embs, failed = _fetch_embeddings_for_results(results, _SplitT3())
        assert embs is not None
        assert embs.shape == (2, 2)
        assert embs[0].tolist() == [1.0, 0.0]
        assert embs[1].tolist() == [0.0, 1.0]
        assert failed == set()

    def test_fetch_partial_failure_returns_good_collections(self) -> None:
        """R3-1: one failing collection does not suppress successful ones."""
        from nexus.search_engine import _fetch_embeddings_for_results

        class _PartialT3:
            def get_embeddings(self, col, ids):
                if col == "code__broken":
                    raise RuntimeError("simulated failure")
                return np.array([[1.0, 0.0]] * len(ids), dtype=np.float32)

        results = [
            _make_result("a", "code__good"),
            _make_result("b", "code__broken"),
            _make_result("c", "code__good"),
        ]
        embs, failed = _fetch_embeddings_for_results(results, _PartialT3())
        assert embs is not None
        assert embs.shape == (3, 2)
        # indices 0 and 2 (code__good) have valid embeddings
        assert embs[0].tolist() == [1.0, 0.0]
        assert embs[2].tolist() == [1.0, 0.0]
        # index 1 (code__broken) is marked failed; its embedding row is zero-filled
        assert failed == {1}

    def test_flag_contradictions_skips_failed_indices(self) -> None:
        """_flag_contradictions must not compare against zero-filled failed rows."""
        from nexus.search_engine import _flag_contradictions

        # Embedding for index 1 is zero (failed fetch); embeddings for 0 and 2 are identical
        embs = _embeddings([
            [1.0, 0.0, 0.0],
            [0.0, 0.0, 0.0],  # failed, zero-filled
            [0.99, 0.01, 0.0],  # similar to index 0
        ])
        results = [
            _make_result("a", "code__x", source_agent="alpha"),
            _make_result("b", "code__x", source_agent="beta"),  # failed
            _make_result("c", "code__x", source_agent="gamma"),
        ]
        out = _flag_contradictions(results, embs, failed_indices={1})
        # a and c are close and different agents → flagged
        assert out[0].metadata.get("_contradiction_flag") is True
        assert out[2].metadata.get("_contradiction_flag") is True
        # b was failed — should NOT be flagged (skipped from comparison)
        assert "_contradiction_flag" not in out[1].metadata
