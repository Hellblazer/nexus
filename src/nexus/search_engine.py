# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search engine: cross-corpus orchestration and Mixedbread fan-out."""
from __future__ import annotations

import hashlib
from typing import Any

import structlog

from nexus.types import SearchResult

_log = structlog.get_logger()

__all__ = [
    "search_cross_corpus",
    "fetch_mxbai_results",
]


# ── Cross-corpus search ───────────────────────────────────────────────────────

def _t3_for_search():
    """Create a T3Database from credentials."""
    from nexus.db import make_t3
    return make_t3()


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


# ── Mixedbread fan-out ────────────────────────────────────────────────────────

def _mxbai_client(api_key: str):
    """Return a Mixedbread client."""
    try:
        from mixedbread import Mixedbread
    except ImportError as exc:
        raise ImportError(
            "The 'mixedbread' package is required for --mxbai. "
            "Install it with: pip install mixedbread"
        ) from exc
    return Mixedbread(api_key=api_key)


def fetch_mxbai_results(
    query: str,
    stores: list[str],
    per_k: int,
) -> list[SearchResult]:
    """Fan-out to Mixedbread stores. Returns [] with a warning if key is unset."""
    from nexus.config import get_credential
    api_key = get_credential("mxbai_api_key")
    if not api_key:
        _log.warning("MXBAI_API_KEY not set — skipping Mixedbread fan-out")
        return []

    client = _mxbai_client(api_key)
    results: list[SearchResult] = []
    for store_id in stores:
        try:
            response = client.stores.search(store_id=store_id, query=query, top_k=per_k)
        except Exception as exc:
            _log.warning("Mixedbread store unavailable, skipping", store_id=store_id, error=str(exc))
            continue
        for chunk in response.chunks:
            _digest = hashlib.sha256(chunk.content.text.encode()).hexdigest()[:16]
            results.append(SearchResult(
                id=f"mxbai__{store_id}__{_digest}",
                content=chunk.content.text,
                distance=1.0 - float(chunk.score),
                collection=f"mxbai__{store_id}",
                metadata={"mxbai_store": store_id, "mxbai_score": float(chunk.score)},
            ))
    return results
