# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure scoring primitives: normalization, hybrid scoring, reranking, interleaving."""
from __future__ import annotations

import threading

import structlog

from nexus.types import SearchResult

_log = structlog.get_logger()

_EPSILON = 1e-9
_RERANK_MODEL = "rerank-2.5"
_FILE_SIZE_THRESHOLD = 30


def _file_size_factor(chunk_count: int) -> float:
    """Return a [0, 1] penalty factor for files larger than the threshold.

    Files at or below *_FILE_SIZE_THRESHOLD* chunks return 1.0 (no penalty).
    Larger files return threshold / chunk_count, linearly reducing the score.
    """
    return min(1.0, _FILE_SIZE_THRESHOLD / max(1, chunk_count))


def min_max_normalize(value: float, window: list[float]) -> float:
    """Normalize *value* into [0, 1] using the min/max of *window*.

    Computed over the combined result window (not per-corpus). Returns 1.0
    when *window* has a single element (it is trivially the maximum). Returns
    0.0 when all values are identical (denominator collapses to ε).

    Raises ValueError if *window* is empty.
    """
    if not window:
        raise ValueError("min_max_normalize: window must be non-empty")
    if len(window) == 1:
        return 1.0  # single element is trivially the best; avoid collapsing to 0.0
    lo = min(window)
    hi = max(window)
    return (value - lo) / (hi - lo + _EPSILON)


def hybrid_score(vector_norm: float, frecency_norm: float) -> float:
    """Weighted combination: 0.7 * vector_norm + 0.3 * frecency_norm."""
    return 0.7 * vector_norm + 0.3 * frecency_norm


def apply_hybrid_scoring(
    results: list[SearchResult],
    hybrid: bool,
) -> list[SearchResult]:
    """Compute hybrid scores for *results*.

    For code__ corpora (hybrid=True): score = 0.7 * vector_norm + 0.3 * frecency_norm.
    For code__ corpora (hybrid=False): score = 1.0 * vector_norm.
    For docs__/knowledge__: score = 1.0 * vector_norm (frecency_score absent).

    File-size penalty: applied to all code__ results unconditionally after the
    initial score is computed: ``score *= _file_size_factor(chunk_count)``.
    When ``chunk_count`` is absent from metadata, defaults to 1 (no penalty).

    If *hybrid* is True but no code__ collections appear in results, a warning
    is logged and all results use 1.0 * vector_norm.

    Note: Mutates ``hybrid_score`` on each SearchResult in place before
    returning the sorted list.
    """
    if not results:
        return results

    has_code = any(r.collection.startswith("code__") for r in results)

    if hybrid and not has_code:
        _log.warning("--hybrid has no effect — no code corpus in scope")

    distances = [r.distance for r in results]
    frecencies = [
        r.metadata.get("frecency_score", 0.0)
        for r in results
        if r.collection.startswith("code__")
    ]

    for r in results:
        # Invert: distances are dissimilarity (smaller = better), so best match → v_norm=1.0
        v_norm = 1.0 - min_max_normalize(r.distance, distances)
        if hybrid and r.collection.startswith("code__"):
            f_score = r.metadata.get("frecency_score", 0.0)
            f_norm = min_max_normalize(f_score, frecencies) if frecencies else 0.0
            r.hybrid_score = hybrid_score(v_norm, f_norm)
        else:
            r.hybrid_score = v_norm
        if r.collection.startswith("code__"):
            chunk_count = int(r.metadata.get("chunk_count", 1))
            r.hybrid_score *= _file_size_factor(chunk_count)

    return sorted(results, key=lambda r: r.hybrid_score, reverse=True)


_voyage_instance: object | None = None
_voyage_lock = threading.Lock()


def _voyage_client():
    """Return a cached voyageai.Client instance."""
    global _voyage_instance
    if _voyage_instance is not None:
        return _voyage_instance
    with _voyage_lock:
        if _voyage_instance is None:
            import voyageai
            from nexus.config import get_credential
            _voyage_instance = voyageai.Client(api_key=get_credential("voyage_api_key"))
    return _voyage_instance


def _reset_voyage_client() -> None:
    """Reset the cached Voyage AI client singleton (for test isolation only)."""
    global _voyage_instance
    with _voyage_lock:
        _voyage_instance = None


def rerank_results(
    results: list[SearchResult],
    query: str,
    model: str = _RERANK_MODEL,
    top_k: int | None = None,
) -> list[SearchResult]:
    """Rerank *results* using Voyage AI reranker.

    Returns results sorted by relevance_score descending.

    Note: Mutates ``hybrid_score`` on each SearchResult in place before
    returning the sorted list.
    """
    if not results:
        return results

    n = top_k or len(results)
    documents = [r.content for r in results]
    client = _voyage_client()
    try:
        rerank_response = client.rerank(
            query=query,
            documents=documents,
            model=model,
            top_k=n,
        )
    except Exception as exc:
        _log.warning("rerank failed, returning original order", error=str(exc))
        return results[:n]
    reranked: list[SearchResult] = []
    for item in rerank_response.results:
        r = results[item.index]
        r.hybrid_score = float(item.relevance_score)
        reranked.append(r)
    return reranked


def round_robin_interleave(
    grouped: list[list[SearchResult]],
) -> list[SearchResult]:
    """Interleave multiple result lists in round-robin order."""
    merged: list[SearchResult] = []
    iterators = [iter(g) for g in grouped]
    while iterators:
        next_iters = []
        for it in iterators:
            try:
                merged.append(next(it))
                next_iters.append(it)
            except StopIteration:
                pass
        iterators = next_iters
    return merged
