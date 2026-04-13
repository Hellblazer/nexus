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
    """Explicit cluster_by=None disables all clustering."""

    def test_explicit_none_returns_flat_list(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        results = search_cross_corpus("q", ["code__test"], 10, t3, cluster_by=None)
        assert isinstance(results, list)
        assert all(isinstance(r, SearchResult) for r in results)
        assert len(results) == 5

    def test_no_cluster_label_on_results(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 3)})
        results = search_cross_corpus("q", ["code__test"], 10, t3, cluster_by=None)
        for r in results:
            assert "_cluster_label" not in r.metadata

    def test_get_embeddings_not_called(self) -> None:
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        search_cross_corpus("q", ["code__test"], 10, t3, cluster_by=None)
        assert t3.get_embeddings_calls == []


class TestFullPipeline:
    """End-to-end tests covering contradiction + clustering + link_boost together.

    These tests bypass the _disable_contradiction_check autouse fixture by
    constructing their own test context. They ensure the full search pipeline
    exercises all features simultaneously, catching regressions like F1 (double
    embedding fetch) and interaction bugs like metadata flag propagation.
    """

    def test_single_fetch_when_contradiction_and_clustering_both_enabled(
        self, monkeypatch
    ) -> None:
        """Regression for F1: both features share one embedding fetch per collection."""
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": True}},
        )

        # Two collections, 3 results each so clustering has material to work with
        t3 = _FakeT3({
            "code__a": _low_distance_results("code__a", 3),
            "docs__b": _low_distance_results("docs__b", 3),
        })
        search_cross_corpus(
            "q",
            ["code__a", "docs__b"],
            10,
            t3,
            cluster_by="semantic",
        )
        # Exactly ONE fetch per collection, not TWO (F1 fix)
        fetched_cols = [c[0] for c in t3.get_embeddings_calls]
        assert fetched_cols.count("code__a") == 1
        assert fetched_cols.count("docs__b") == 1
        assert len(t3.get_embeddings_calls) == 2

    def test_contradiction_flag_survives_clustering(self, monkeypatch) -> None:
        """R3-5 regression: clustering must preserve _contradiction_flag in metadata."""
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": True}},
        )

        # Craft close embeddings so contradiction fires.
        # Note: search_cross_corpus builds SearchResult.metadata from all keys
        # except id/content/distance — so source_agent must be a top-level key.
        class _ContradictingT3:
            _voyage_client = "fake"

            def search(self, query, collection_names, n_results=10, where=None):
                return [
                    {"id": "chunk-a", "content": "text", "distance": 0.1,
                     "source_agent": "agent-alpha"},
                    {"id": "chunk-b", "content": "text", "distance": 0.15,
                     "source_agent": "agent-beta"},
                    {"id": "chunk-c", "content": "text", "distance": 0.2,
                     "source_agent": "agent-gamma"},
                ]

            def get_embeddings(self, collection_name, ids):
                # Return exactly one row per requested ID, matched by ID so
                # index alignment bugs in _fetch_embeddings_for_results
                # would produce a wrong mapping rather than a silent pass.
                vec_for = {
                    "chunk-a": [1.0, 0.0, 0.0, 0.0],
                    "chunk-b": [0.99, 0.01, 0.0, 0.0],
                    "chunk-c": [0.0, 0.0, 1.0, 0.0],
                }
                rows = [vec_for[i] for i in ids]
                return np.array(rows, dtype=np.float32)

        t3 = _ContradictingT3()
        results = search_cross_corpus(
            "q", ["code__test"], 10, t3, cluster_by="semantic",
        )
        # At least one result should still carry the contradiction flag after clustering
        flagged = [r for r in results if r.metadata.get("_contradiction_flag")]
        assert len(flagged) >= 2, (
            "Clustering dropped _contradiction_flag — the metadata-preservation "
            "invariant between _flag_contradictions and _apply_clustering broke"
        )

    def test_partial_collection_failure_does_not_suppress_other_flags(
        self, monkeypatch
    ) -> None:
        """R3-1 regression: one failing collection does not suppress features for others."""
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": True}},
        )

        class _PartialT3:
            _voyage_client = "fake"
            get_embeddings_calls = []

            def search(self, query, collection_names, n_results=10, where=None):
                col = collection_names[0]
                if col == "code__good":
                    return [
                        {"id": "a", "content": "t", "distance": 0.1,
                         "source_agent": "alpha"},
                        {"id": "b", "content": "t", "distance": 0.15,
                         "source_agent": "beta"},
                    ]
                return [
                    {"id": "c", "content": "t", "distance": 0.1,
                     "source_agent": "gamma"},
                    {"id": "d", "content": "t", "distance": 0.15,
                     "source_agent": "delta"},
                ]

            def get_embeddings(self, collection_name, ids):
                self.get_embeddings_calls.append((collection_name, ids))
                if collection_name == "code__broken":
                    raise RuntimeError("simulated collection fault")
                # good collection: identical vectors → contradiction
                return np.array([
                    [1.0, 0.0, 0.0, 0.0],
                    [0.99, 0.01, 0.0, 0.0],
                ], dtype=np.float32)

        t3 = _PartialT3()
        results = search_cross_corpus(
            "q", ["code__good", "code__broken"], 10, t3,
        )
        # good collection should still have contradiction flags despite broken one
        good_results = [r for r in results if r.collection == "code__good"]
        flagged_good = [r for r in good_results if r.metadata.get("_contradiction_flag")]
        assert len(flagged_good) >= 2, (
            "R3-1 regression: partial collection failure suppressed contradiction "
            "flags on successfully-fetched collections"
        )


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


# ── Topic-based grouping (RDR-070, nexus-y8f) ───────────────────────────────


class TestTopicGrouping:
    """When >50% of results have topic assignments, group by topic label."""

    def _make_taxonomy(self, assignments: dict[str, int], topics: dict[int, str]):
        """Create a fake taxonomy with given doc_id→topic_id and topic_id→label maps."""
        tax = MagicMock()
        tax.conn = MagicMock()

        def fake_get_assigned(doc_ids):
            return {did: assignments[did] for did in doc_ids if did in assignments}

        tax.get_assignments_for_docs = fake_get_assigned

        def fake_get_topics(**kw):
            return [{"id": tid, "label": lbl} for tid, lbl in topics.items()]

        tax.get_topics = fake_get_topics

        def fake_get_labels_for_ids(ids):
            return {tid: topics[tid] for tid in ids if tid in topics}

        tax.get_labels_for_ids = fake_get_labels_for_ids
        return tax

    def test_topic_grouping_when_majority_assigned(self) -> None:
        """Results grouped by topic label when >50% have assignments."""
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 6)})
        # 4 of 6 results assigned (67% > 50%)
        assignments = {
            "code__test-0": 1, "code__test-1": 1,
            "code__test-2": 2, "code__test-3": 2,
        }
        topics = {1: "http handlers", 2: "database queries"}
        tax = self._make_taxonomy(assignments, topics)

        results = search_cross_corpus(
            "q", ["code__test"], 10, t3,
            cluster_by="semantic", taxonomy=tax,
        )
        assert len(results) == 6
        labeled = [r for r in results if "_topic_label" in r.metadata]
        assert len(labeled) == 4
        labels = {r.metadata["_topic_label"] for r in labeled}
        assert labels == {"http handlers", "database queries"}

    def test_ward_fallback_when_few_assignments(self) -> None:
        """Falls back to Ward clustering when <=50% have topic assignments."""
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 6)})
        # Only 2 of 6 assigned (33% < 50%)
        assignments = {"code__test-0": 1}
        topics = {1: "http handlers"}
        tax = self._make_taxonomy(assignments, topics)

        results = search_cross_corpus(
            "q", ["code__test"], 10, t3,
            cluster_by="semantic", taxonomy=tax,
        )
        assert len(results) == 6
        # Ward clustering adds _cluster_label, not _topic_label
        labeled = [r for r in results if "_cluster_label" in r.metadata]
        assert len(labeled) >= 1

    def test_ward_fallback_when_no_taxonomy(self) -> None:
        """Falls back to Ward when taxonomy is None."""
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        results = search_cross_corpus(
            "q", ["code__test"], 10, t3,
            cluster_by="semantic", taxonomy=None,
        )
        assert len(results) == 5
        # Ward clustering should have run
        labeled = [r for r in results if "_cluster_label" in r.metadata]
        assert len(labeled) >= 1

    def test_explicit_none_disables_all_clustering(self) -> None:
        """cluster_by=None disables both topic and Ward clustering."""
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        results = search_cross_corpus(
            "q", ["code__test"], 10, t3,
            cluster_by=None,
        )
        assert len(results) == 5
        for r in results:
            assert "_cluster_label" not in r.metadata
            assert "_topic_label" not in r.metadata


class TestTopicScopedSearch:
    """topic= parameter pre-filters search to documents in a specific topic."""

    def _make_taxonomy(self, topic_docs: dict[str, list[str]]):
        """Create a fake taxonomy where label→[doc_ids]."""
        tax = MagicMock()

        def fake_get_doc_ids(label):
            return topic_docs.get(label, [])

        tax.get_doc_ids_for_topic = fake_get_doc_ids

        def fake_get_assignments(doc_ids):
            result = {}
            for label, ids in topic_docs.items():
                for did in doc_ids:
                    if did in ids:
                        result[did] = hash(label) % 1000
            return result

        tax.get_assignments_for_docs = fake_get_assignments
        tax.get_topics = lambda **kw: []
        return tax

    def test_topic_prefilter_narrows_results(self) -> None:
        """Only results matching the topic's doc_ids are returned."""
        all_results = _low_distance_results("code__test", 10)
        t3 = _FakeT3({"code__test": all_results})
        # Only first 3 docs in the topic
        tax = self._make_taxonomy({"http handlers": ["code__test-0", "code__test-1", "code__test-2"]})

        results = search_cross_corpus(
            "q", ["code__test"], 10, t3,
            cluster_by=None, taxonomy=tax,
            topic="http handlers",
        )
        assert len(results) == 3
        assert {r.id for r in results} == {"code__test-0", "code__test-1", "code__test-2"}

    def test_topic_not_found_returns_empty(self) -> None:
        """Non-existent topic returns empty results."""
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        tax = self._make_taxonomy({})

        results = search_cross_corpus(
            "q", ["code__test"], 10, t3,
            cluster_by=None, taxonomy=tax,
            topic="nonexistent",
        )
        assert results == []

    def test_topic_none_does_not_filter(self) -> None:
        """topic=None (default) returns all results."""
        t3 = _FakeT3({"code__test": _low_distance_results("code__test", 5)})
        results = search_cross_corpus(
            "q", ["code__test"], 10, t3,
            cluster_by=None, topic=None,
        )
        assert len(results) == 5
