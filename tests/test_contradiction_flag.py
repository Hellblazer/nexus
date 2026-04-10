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


class _FakeT3:
    """Minimal T3 stub that returns controllable embeddings."""

    def __init__(self, embeddings: dict[str, dict[str, list[float]]]):
        """embeddings: {collection: {doc_id: [float, ...]}}"""
        self._embs = embeddings

    def get_embeddings(self, collection_name: str, ids: list[str]) -> np.ndarray:
        col = self._embs.get(collection_name, {})
        rows = [col.get(did, [0.0, 0.0, 0.0]) for did in ids]
        return np.array(rows, dtype=np.float32)


class TestFlagContradictions:
    """Unit tests for _flag_contradictions()."""

    def test_same_collection_different_agent_close_distance(self) -> None:
        """Two results, same collection, different source_agent, distance < 0.3 → flagged."""
        from nexus.search_engine import _flag_contradictions

        # Nearly identical embeddings → cosine distance ≈ 0
        t3 = _FakeT3({"code__x": {
            "a": [1.0, 0.0, 0.0],
            "b": [0.99, 0.01, 0.0],
        }})
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "code__x", source_agent="agent-beta"),
        ]
        out = _flag_contradictions(results, t3)
        assert out[0].metadata.get("_contradiction_flag") is True
        assert out[1].metadata.get("_contradiction_flag") is True

    def test_same_collection_same_agent_no_flag(self) -> None:
        """Same source_agent → no flag (same provenance)."""
        from nexus.search_engine import _flag_contradictions

        t3 = _FakeT3({"code__x": {
            "a": [1.0, 0.0, 0.0],
            "b": [0.99, 0.01, 0.0],
        }})
        results = [
            _make_result("a", "code__x", source_agent="same-agent"),
            _make_result("b", "code__x", source_agent="same-agent"),
        ]
        out = _flag_contradictions(results, t3)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata

    def test_same_collection_different_agent_far_distance_no_flag(self) -> None:
        """Cosine distance >= 0.3 → no flag."""
        from nexus.search_engine import _flag_contradictions

        # Orthogonal embeddings → cosine distance = 1.0
        t3 = _FakeT3({"code__x": {
            "a": [1.0, 0.0, 0.0],
            "b": [0.0, 1.0, 0.0],
        }})
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "code__x", source_agent="agent-beta"),
        ]
        out = _flag_contradictions(results, t3)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata

    def test_different_collections_no_flag(self) -> None:
        """Cross-collection pairs → no flag (different scopes)."""
        from nexus.search_engine import _flag_contradictions

        t3 = _FakeT3({
            "code__x": {"a": [1.0, 0.0, 0.0]},
            "docs__y": {"b": [0.99, 0.01, 0.0]},
        })
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "docs__y", source_agent="agent-beta"),
        ]
        out = _flag_contradictions(results, t3)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata

    def test_single_result_no_flag(self) -> None:
        """Single result → nothing to compare."""
        from nexus.search_engine import _flag_contradictions

        t3 = _FakeT3({"code__x": {"a": [1.0, 0.0, 0.0]}})
        results = [_make_result("a", "code__x", source_agent="agent-alpha")]
        out = _flag_contradictions(results, t3)
        assert "_contradiction_flag" not in out[0].metadata

    def test_three_results_only_contradicting_pair_flagged(self) -> None:
        """A and B contradict; C does not → only A and B flagged."""
        from nexus.search_engine import _flag_contradictions

        t3 = _FakeT3({"code__x": {
            "a": [1.0, 0.0, 0.0],
            "b": [0.99, 0.01, 0.0],  # close to a
            "c": [0.0, 1.0, 0.0],    # far from both
        }})
        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "code__x", source_agent="agent-beta"),
            _make_result("c", "code__x", source_agent="agent-gamma"),
        ]
        out = _flag_contradictions(results, t3)
        assert out[0].metadata.get("_contradiction_flag") is True   # a
        assert out[1].metadata.get("_contradiction_flag") is True   # b
        assert "_contradiction_flag" not in out[2].metadata         # c

    def test_empty_source_agent_no_flag(self) -> None:
        """Empty source_agent → no provenance conflict."""
        from nexus.search_engine import _flag_contradictions

        t3 = _FakeT3({"code__x": {
            "a": [1.0, 0.0, 0.0],
            "b": [0.99, 0.01, 0.0],
        }})
        results = [
            _make_result("a", "code__x"),  # no source_agent
            _make_result("b", "code__x"),
        ]
        out = _flag_contradictions(results, t3)
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata

    def test_get_embeddings_exception_skipped(self) -> None:
        """If get_embeddings raises, that collection is skipped gracefully."""
        from nexus.search_engine import _flag_contradictions

        class _BrokenT3:
            def get_embeddings(self, col, ids):
                raise RuntimeError("simulated failure")

        results = [
            _make_result("a", "code__x", source_agent="agent-alpha"),
            _make_result("b", "code__x", source_agent="agent-beta"),
        ]
        out = _flag_contradictions(results, _BrokenT3())
        # No crash, no flags
        assert "_contradiction_flag" not in out[0].metadata
        assert "_contradiction_flag" not in out[1].metadata
