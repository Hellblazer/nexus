# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic candidate pools for the boost-window pins (nexus-cbg2s).

One fixed, distance-ordered sequence of rows; ``pool(depth)`` is its first
*depth* rows, which is what a deeper fetch of the same query looks like (the
nearest rows are the same, the pool simply goes further). Frecency is seeded
noise, so the hybrid score has something to disagree with raw distance about
and a change in the normalisation window can swap near ties.
"""
from __future__ import annotations

import random

from nexus.types import SearchResult

DEPTHS = (40, 60, 80, 120, 300)
_MAX_DEPTH = max(DEPTHS)


def _rows(collection: str) -> list[tuple[int, float, float]]:
    rng = random.Random(20261009)
    return [
        (i, round(0.20 + 0.0015 * i + rng.uniform(0.0, 0.0008), 6), rng.uniform(0.0, 100.0))
        for i in range(_MAX_DEPTH)
    ]


def pool(depth: int, collection: str = "code__test") -> list[SearchResult]:
    """The *depth* nearest rows of the fixed sequence, fresh objects each call
    (scoring mutates ``hybrid_score`` in place)."""
    return [
        SearchResult(
            id=f"row{i:03d}", content=f"content {i}", distance=d, collection=collection,
            metadata={"frecency_score": f, "source_path": f"src/file{i:03d}.py"},
        )
        for i, d, f in _rows(collection)[:depth]
    ]
