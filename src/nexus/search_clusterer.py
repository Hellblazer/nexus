# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search result clustering via Ward hierarchical linkage."""
from __future__ import annotations

import math
from typing import Any

import numpy as np


def cluster_results(
    results: list[dict[str, Any]],
    embeddings: np.ndarray,
    k: int | None = None,
) -> list[list[dict[str, Any]]]:
    """Cluster search results by semantic similarity.

    Uses Ward hierarchical linkage (scipy) with numpy k-means fallback.

    Args:
        results: Search result dicts with at least ``distance`` key.
        embeddings: ``(N, D)`` float32 array, one row per result.
        k: Number of clusters.  Defaults to ``max(2, ceil(n / 5))``.

    Returns:
        List of clusters sorted by best (lowest) distance.
        Each result dict gets ``_cluster_label`` added in-place.
    """
    n = len(results)
    if n == 0:
        return []
    if n <= 2:
        return [[r] for r in results]

    k = k or max(2, math.ceil(n / 5))
    k = min(k, n)

    try:
        from scipy.cluster.hierarchy import fcluster, linkage

        Z = linkage(embeddings, method="ward")
        labels = fcluster(Z, k, criterion="maxclust") - 1  # 0-indexed
    except ImportError:
        labels = _kmeans_numpy(embeddings, k, seed=42)

    # Group by label
    clusters: dict[int, list[dict[str, Any]]] = {}
    for r, label in zip(results, labels):
        clusters.setdefault(int(label), []).append(r)

    # Sort within each cluster by distance, assign label
    out: list[list[dict[str, Any]]] = []
    for cluster_list in clusters.values():
        sorted_cluster = sorted(cluster_list, key=lambda r: r.get("distance", 0.0))
        label_title = _cluster_label(sorted_cluster[0])
        for r in sorted_cluster:
            r["_cluster_label"] = label_title
        out.append(sorted_cluster)

    # Sort clusters by best distance
    out.sort(key=lambda c: c[0].get("distance", 0.0))
    return out


def _kmeans_numpy(
    embeddings: np.ndarray, k: int, seed: int = 42, max_iter: int = 100,
) -> np.ndarray:
    """Numpy-only k-means.  Deterministic via seeded RNG."""
    rng = np.random.default_rng(seed)
    n = embeddings.shape[0]
    indices = rng.choice(n, size=k, replace=False)
    centroids = embeddings[indices].copy()

    labels = np.zeros(n, dtype=np.intp)
    for _ in range(max_iter):
        dists = np.linalg.norm(embeddings[:, None] - centroids[None, :], axis=2)
        new_labels = np.argmin(dists, axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            mask = labels == j
            if mask.any():
                centroids[j] = embeddings[mask].mean(axis=0)
    return labels


def _cluster_label(best_result: dict[str, Any]) -> str:
    """Extract a human-readable label from the best result in a cluster."""
    meta = best_result.get("metadata", {})
    return meta.get("title") or meta.get("source") or best_result.get("id", "unknown")
