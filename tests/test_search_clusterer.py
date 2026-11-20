# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for search result clustering (RDR-056 Phase 2b)."""
from __future__ import annotations

import numpy as np
import pytest

from nexus.search_clusterer import _cluster_label, _kmeans_numpy, cluster_results


def _make_results(n: int, distances: list[float] | None = None) -> list[dict]:
    """Create n fake result dicts shaped like POST-attach rows.

    ``_display_title``/``_display_path`` are written only by ``_attach_display_paths``, which
    runs AFTER clustering in ``search_cross_corpus``. These rows therefore exercise the
    clusterer's own label logic; the live-path label is pinned through the pipeline in
    ``tests/test_fv2u4_cluster_header.py``."""
    dists = distances or [0.1 * i for i in range(n)]
    return [
        {"id": f"doc-{i}", "content": f"text {i}", "distance": dists[i],
         "metadata": {"_display_title": f"Title {i}", "_display_path": f"/path/{i}.py"}}
        for i in range(n)
    ]


def _two_cluster_embeddings(rng: np.random.Generator) -> np.ndarray:
    """Create 10 embeddings in two clear clusters."""
    cluster_a = rng.normal(loc=[1, 0, 0, 0], scale=0.1, size=(5, 4))
    cluster_b = rng.normal(loc=[0, 1, 0, 0], scale=0.1, size=(5, 4))
    return np.vstack([cluster_a, cluster_b]).astype(np.float32)


class TestClusterResults:
    def test_empty_returns_empty(self) -> None:
        assert cluster_results([], np.array([])) == []

    def test_single_result_returns_singleton(self) -> None:
        results = _make_results(1)
        out = cluster_results(results, np.array([[1.0, 0.0]]))
        assert len(out) == 1
        assert len(out[0]) == 1
        assert out[0][0]["id"] == "doc-0"

    def test_two_results_returns_two_singletons(self) -> None:
        results = _make_results(2)
        emb = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        out = cluster_results(results, emb)
        assert len(out) == 2
        assert all(len(c) == 1 for c in out)

    def test_ward_produces_correct_clusters(self) -> None:
        rng = np.random.default_rng(42)
        emb = _two_cluster_embeddings(rng)
        results = _make_results(10)
        out = cluster_results(results, emb, k=2)
        assert len(out) == 2
        # Each cluster should have 5 elements (from the two groups)
        sizes = sorted(len(c) for c in out)
        assert sizes == [5, 5]

    def test_k_heuristic(self) -> None:
        """Default k = max(2, ceil(n/5))."""
        import math
        for n, expected_k in [(5, 2), (10, 2), (11, 3), (25, 5)]:
            k = max(2, math.ceil(n / 5))
            assert k == expected_k, f"n={n}: expected {expected_k}, got {k}"

    def test_intra_cluster_sorted_by_distance(self) -> None:
        rng = np.random.default_rng(42)
        emb = _two_cluster_embeddings(rng)
        distances = [0.5, 0.3, 0.4, 0.2, 0.1, 0.9, 0.8, 0.7, 0.6, 0.55]
        results = _make_results(10, distances)
        out = cluster_results(results, emb, k=2)
        for cluster in out:
            dists = [r["distance"] for r in cluster]
            assert dists == sorted(dists)

    def test_cluster_label_assigned(self) -> None:
        """Unit-level only: the rows carry post-attach keys (see ``_make_results``). The live
        header is relabelled after the attach and pinned in ``test_fv2u4_cluster_header.py``."""
        rng = np.random.default_rng(42)
        emb = _two_cluster_embeddings(rng)
        results = _make_results(10)
        out = cluster_results(results, emb, k=2)
        for cluster in out:
            for r in cluster:
                assert "_cluster_label" in r
            # Label should match the title of the lowest-distance result
            best = min(cluster, key=lambda r: r["distance"])
            assert cluster[0]["_cluster_label"] == best["metadata"]["_display_title"]

    def test_clusters_sorted_by_best_distance(self) -> None:
        rng = np.random.default_rng(42)
        emb = _two_cluster_embeddings(rng)
        results = _make_results(10)
        out = cluster_results(results, emb, k=2)
        best_dists = [c[0]["distance"] for c in out]
        assert best_dists == sorted(best_dists)

    def test_deterministic(self) -> None:
        rng1 = np.random.default_rng(42)
        rng2 = np.random.default_rng(42)
        emb1 = _two_cluster_embeddings(rng1)
        emb2 = _two_cluster_embeddings(rng2)
        results1 = _make_results(10)
        results2 = _make_results(10)
        out1 = cluster_results(results1, emb1, k=2)
        out2 = cluster_results(results2, emb2, k=2)
        ids1 = [[r["id"] for r in c] for c in out1]
        ids2 = [[r["id"] for r in c] for c in out2]
        assert ids1 == ids2


_CODE, _DOCS = "code__1-1__voyage-code-3__v1", "docs__1-1__voyage-context-3__v1"
_FACTORS = {_CODE: 1.0, _DOCS: 0.45 / 0.65}  # the calibration scale order_by_calibrated_distance uses


def _two_far_clusters() -> np.ndarray:
    """Rows 0-2 sit near one corner, rows 3-5 near another: Ward at k=2 splits them there."""
    return np.array([[1, 0, 0, 0], [1, 0.01, 0, 0], [1, 0, 0.01, 0],
                     [0, 1, 0, 0], [0, 1, 0.01, 0], [0, 1, 0, 0.01]], dtype=np.float32)


def _row(rid: str, collection: str, distance: float) -> dict:
    return {"id": rid, "content": "", "distance": distance, "collection": collection, "metadata": {}}


class TestClusterResultsCalibrated:
    """GH #1667 follow-up C: voyage-code-3 distances run tighter than voyage-context-3, so a raw sort
    over a pool that spans both puts code first however near the prose row is."""

    def test_clusters_are_ordered_by_calibrated_best_distance(self) -> None:
        rows = [_row("c0", _CODE, 0.38), _row("c1", _CODE, 0.40), _row("c2", _CODE, 0.42),
                _row("d0", _DOCS, 0.50), _row("d1", _DOCS, 0.52), _row("d2", _DOCS, 0.54)]
        raw = cluster_results([dict(r) for r in rows], _two_far_clusters(), k=2)
        assert [c[0]["id"] for c in raw] == ["c0", "d0"], "no factors: the raw order, as before"
        out = cluster_results(rows, _two_far_clusters(), k=2, calibration_factors=_FACTORS)
        assert [c[0]["id"] for c in out] == ["d0", "c0"]
        assert [r["distance"] for c in out for r in c] == [0.50, 0.52, 0.54, 0.38, 0.40, 0.42], (
            "the reported distances are not rescaled"
        )

    def test_rows_inside_a_cluster_are_ordered_by_calibrated_distance(self) -> None:
        rows = [_row("code", _CODE, 0.40), _row("doc-mid", _DOCS, 0.60), _row("doc-best", _DOCS, 0.50),
                _row("far0", _DOCS, 0.90), _row("far1", _DOCS, 0.91), _row("far2", _DOCS, 0.92)]
        out = cluster_results(rows, _two_far_clusters(), k=2, calibration_factors=_FACTORS)
        assert [r["id"] for r in out[0]] == ["doc-best", "code", "doc-mid"]
        assert out[0][0]["_cluster_label"] == "doc-best", "the label follows the calibrated best row"

    def test_a_single_model_pool_is_unchanged(self) -> None:
        rows = [_row(f"r{i}", _DOCS, d) for i, d in enumerate([0.5, 0.3, 0.4, 0.9, 0.8, 0.7])]
        plain = cluster_results([dict(r) for r in rows], _two_far_clusters(), k=2)
        factored = cluster_results(rows, _two_far_clusters(), k=2, calibration_factors={_DOCS: 1.0})
        assert [[r["id"] for r in c] for c in plain] == [[r["id"] for r in c] for c in factored]


class TestKmeansNumpy:
    def test_produces_valid_labels(self) -> None:
        rng = np.random.default_rng(42)
        emb = _two_cluster_embeddings(rng)
        labels = _kmeans_numpy(emb, k=2, seed=42)
        assert labels.shape == (10,)
        assert set(labels.tolist()).issubset({0, 1})

    def test_deterministic(self) -> None:
        rng = np.random.default_rng(42)
        emb = _two_cluster_embeddings(rng)
        l1 = _kmeans_numpy(emb, k=2, seed=42)
        l2 = _kmeans_numpy(emb, k=2, seed=42)
        np.testing.assert_array_equal(l1, l2)

    def test_fallback_when_scipy_unavailable(self) -> None:
        """cluster_results falls back to kmeans when scipy import fails."""
        from unittest.mock import patch
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if "scipy" in name:
                raise ImportError("mocked")
            return real_import(name, *args, **kwargs)

        rng = np.random.default_rng(42)
        emb = _two_cluster_embeddings(rng)
        results = _make_results(10)

        with patch("builtins.__import__", side_effect=mock_import):
            out = cluster_results(results, emb, k=2)

        assert len(out) == 2
        total = sum(len(c) for c in out)
        assert total == 10


class TestClusterLabel:
    def test_uses_title(self) -> None:
        r = {"id": "x", "metadata": {"_display_title": "My Title", "_display_path": "/a.py"}}
        assert _cluster_label(r) == "My Title"

    def test_falls_back_to_display_path(self) -> None:
        r = {"id": "x", "metadata": {"_display_path": "/a.py"}}
        assert _cluster_label(r) == "/a.py"

    def test_never_labels_with_the_chunk_rows_title_or_source(self) -> None:
        """nexus-fv2u4: the row's title and source are the last writer's, another document's on a shared chunk."""
        r = {"id": "x", "metadata": {"title": "other/doc.py:40-60", "source": "/other.py"}}
        assert _cluster_label(r) == "x"

    def test_falls_back_to_id(self) -> None:
        r = {"id": "x", "metadata": {}}
        assert _cluster_label(r) == "x"

    def test_falls_back_to_unknown(self) -> None:
        r: dict = {"metadata": {}}
        assert _cluster_label(r) == "unknown"
