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
    """Tests for the shared embedding-fetch helper (F1 fix)."""

    def test_fetch_returns_none_on_exception(self) -> None:
        """Fetch failure returns None so callers fall through."""
        from nexus.search_engine import _fetch_embeddings_for_results

        class _BrokenT3:
            def get_embeddings(self, col, ids):
                raise RuntimeError("simulated failure")

        results = [_make_result("a", "code__x"), _make_result("b", "code__x")]
        out = _fetch_embeddings_for_results(results, _BrokenT3())
        assert out is None

    def test_fetch_returns_none_on_shape_mismatch(self) -> None:
        """Fetch returning fewer rows than requested returns None."""
        from nexus.search_engine import _fetch_embeddings_for_results

        class _ShortT3:
            def get_embeddings(self, col, ids):
                # Return fewer rows than requested
                return np.zeros((len(ids) - 1, 4), dtype=np.float32)

        results = [_make_result("a", "code__x"), _make_result("b", "code__x")]
        out = _fetch_embeddings_for_results(results, _ShortT3())
        assert out is None

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
        out = _fetch_embeddings_for_results(results, _SplitT3())
        assert out is not None
        assert out.shape == (2, 2)
        assert out[0].tolist() == [1.0, 0.0]
        assert out[1].tolist() == [0.0, 1.0]
