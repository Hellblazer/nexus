# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search engine: cross-corpus orchestration."""
from __future__ import annotations

from typing import Any

from nexus.types import SearchResult

__all__ = [
    "search_cross_corpus",
]


# ── Cross-corpus search ───────────────────────────────────────────────────────

def search_cross_corpus(
    query: str,
    collections: list[str],
    n_results: int,
    t3: Any,
    where: dict | None = None,
) -> list[SearchResult]:
    """Query each collection independently, returning combined raw results.

    Per-corpus over-fetch: max(5, (n_results // num_corpora) * 2).

    *where* is an optional ChromaDB metadata filter forwarded to every collection.
    """
    num = len(collections) or 1
    per_k = max(5, (n_results // num) * 2)

    all_results: list[SearchResult] = []
    for col in collections:
        raw = t3.search(query, [col], n_results=per_k, where=where)
        for r in raw:
            all_results.append(SearchResult(
                id=r["id"],
                content=r["content"],
                distance=r["distance"],
                collection=col,
                metadata={k: v for k, v in r.items()
                          if k not in {"id", "content", "distance"}},
            ))
    return all_results

