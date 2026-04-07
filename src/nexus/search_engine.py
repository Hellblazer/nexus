# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search engine: cross-corpus orchestration."""
from __future__ import annotations

from typing import Any

import structlog

from nexus.config import load_config
from nexus.types import SearchResult

_log = structlog.get_logger(__name__)

__all__ = [
    "search_cross_corpus",
]

# Known collection prefixes mapped to their threshold config key.
_PREFIX_TO_KEY: list[tuple[str, str]] = [
    ("code__", "code"),
    ("knowledge__", "knowledge"),
    ("docs__", "docs"),
    ("rdr__", "rdr"),
]


def _threshold_for_collection(name: str, cfg: dict) -> float | None:
    """Return the distance threshold for *name*, or ``None`` if unconfigured."""
    thresholds = cfg.get("search", {}).get("distance_threshold")
    if not thresholds:
        return None
    for prefix, key in _PREFIX_TO_KEY:
        if name.startswith(prefix):
            return thresholds.get(key)
    return thresholds.get("default")


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
    Results exceeding per-corpus distance thresholds are filtered out.

    *where* is an optional ChromaDB metadata filter forwarded to every collection.
    """
    cfg = load_config()
    # Thresholds are calibrated for Voyage AI embeddings.
    # Skip filtering when Voyage is not in use (local mode, test injection).
    apply_thresholds = getattr(t3, "_voyage_client", None) is not None
    num = len(collections) or 1
    per_k = max(5, (n_results // num) * 2)

    all_results: list[SearchResult] = []
    for col in collections:
        threshold = _threshold_for_collection(col, cfg) if apply_thresholds else None
        raw = t3.search(query, [col], n_results=per_k, where=where)
        dropped = 0
        for r in raw:
            distance = r["distance"]
            # RDR-055 interaction: when E2 quality_score reranking
            # (nexus-3idt/nexus-rg6x) is active, thresholds must be applied
            # AFTER reranking, not here. For now thresholds apply to raw
            # distance. Re-validate after RDR-055 E1 re-indexing.
            if threshold is not None and distance > threshold:
                dropped += 1
                continue
            all_results.append(SearchResult(
                id=r["id"],
                content=r["content"],
                distance=distance,
                collection=col,
                metadata={k: v for k, v in r.items()
                          if k not in {"id", "content", "distance"}},
            ))
        if dropped:
            _log.debug(
                "threshold_filtered",
                collection=col,
                dropped=dropped,
                threshold=threshold,
            )
    return all_results
