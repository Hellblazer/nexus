# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure scoring primitives: normalization, hybrid scoring, reranking, interleaving."""
from __future__ import annotations

import threading
from typing import Any

import structlog

from nexus.retry import _voyage_with_retry
from nexus.types import SearchResult

_log = structlog.get_logger()

_EPSILON = 1e-9
_RERANK_MODEL = "rerank-2.5"
_FILE_SIZE_THRESHOLD = 30
RG_FLOOR_SCORE = 0.5

# Default scoring weights (kept as module constants for backward compatibility).
# Override by passing explicit weights to hybrid_score() / apply_hybrid_scoring().
_VECTOR_WEIGHT: float = 0.7
_FRECENCY_WEIGHT: float = 0.3


def _file_size_factor(chunk_count: int, threshold: int = _FILE_SIZE_THRESHOLD) -> float:
    """Return a [0, 1] penalty factor for files larger than the threshold.

    Files at or below *threshold* chunks return 1.0 (no penalty).
    Larger files return threshold / chunk_count, linearly reducing the score.
    """
    return min(1.0, threshold / max(1, chunk_count))


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


def hybrid_score(
    vector_norm: float,
    frecency_norm: float,
    vector_weight: float = _VECTOR_WEIGHT,
    frecency_weight: float = _FRECENCY_WEIGHT,
) -> float:
    """Weighted combination of vector and frecency scores.

    Default weights (0.7 / 0.3) match the previous hard-coded values.
    Pass explicit weights from TuningConfig to override.
    """
    return vector_weight * vector_norm + frecency_weight * frecency_norm


def apply_hybrid_scoring(
    results: list[SearchResult],
    hybrid: bool,
    *,
    vector_weight: float = _VECTOR_WEIGHT,
    frecency_weight: float = _FRECENCY_WEIGHT,
    file_size_threshold: int = _FILE_SIZE_THRESHOLD,
) -> list[SearchResult]:
    """Compute hybrid scores for *results*.

    For code__ corpora (hybrid=True): score = vector_weight * vector_norm + frecency_weight * frecency_norm.
    For code__ corpora (hybrid=False): score = 1.0 * vector_norm.
    For docs__/knowledge__: score = 1.0 * vector_norm (frecency_score absent).

    File-size penalty: applied to all code__ results unconditionally after the
    initial score is computed: ``score *= _file_size_factor(chunk_count)``.
    When ``chunk_count`` is absent from metadata, defaults to 1 (no penalty).

    If *hybrid* is True but no code__ collections appear in results, a warning
    is logged and all results use 1.0 * vector_norm.

    *vector_weight*, *frecency_weight*, and *file_size_threshold* default to the
    module constants (backward-compatible).  Pass values from TuningConfig to
    honour per-repo configuration.

    Note: Mutates ``hybrid_score`` on each SearchResult in place before
    returning the sorted list.
    """
    if not results:
        return results

    has_code = any(r.collection.startswith("code__") for r in results)

    if hybrid and not has_code:
        _log.warning("--hybrid has no effect — no code corpus in scope")

    # Exclude rg__cache from normalization window — distance=0.0 from ripgrep
    # hits distorts the min-max range for real vector distances.
    distances = [r.distance for r in results if r.collection != "rg__cache"]
    frecencies = [
        r.metadata.get("frecency_score", 0.0)
        for r in results
        if r.collection.startswith("code__")
    ]

    for r in results:
        if r.collection == "rg__cache":
            r.hybrid_score = RG_FLOOR_SCORE
            continue
        # Invert: distances are dissimilarity (smaller = better), so best match → v_norm=1.0
        v_norm = 1.0 - min_max_normalize(r.distance, distances) if distances else 1.0
        if hybrid and r.collection.startswith("code__"):
            f_score = r.metadata.get("frecency_score", 0.0)
            f_norm = min_max_normalize(f_score, frecencies) if frecencies else 0.0
            r.hybrid_score = hybrid_score(v_norm, f_norm, vector_weight, frecency_weight)
        else:
            r.hybrid_score = v_norm
        if r.collection.startswith("code__"):
            chunk_count = int(r.metadata.get("chunk_count", 1))
            r.hybrid_score *= _file_size_factor(chunk_count, file_size_threshold)

    return sorted(results, key=lambda r: r.hybrid_score, reverse=True)


def quality_score(
    citation_count: int,
    age_days: float = 0.0,
    alpha: float = 0.5,
    half_life: float = 730.0,
    c_max: float = 10_000.0,
) -> float:
    """Compute quality score from bibliographic metadata (RDR-055 E2).

    Returns 0.0 when *citation_count* is 0 (unenriched) to avoid bias.

    ``quality = α × log(count+1)/log(C+1) + (1-α) × 0.5^(age/half_life)``
    """
    if citation_count <= 0:
        return 0.0
    import math
    citation_signal = min(1.0, math.log(citation_count + 1) / math.log(c_max + 1))
    age_signal = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
    return alpha * citation_signal + (1 - alpha) * age_signal


# Default boost weight — how much quality_score influences hybrid_score.
_QUALITY_BOOST_WEIGHT: float = 0.1

# Collections eligible for quality boost (bibliographic metadata expected).
_QUALITY_ELIGIBLE_PREFIXES = ("knowledge__", "docs__", "rdr__")


def apply_quality_boost(
    results: list[SearchResult],
    boost_weight: float = _QUALITY_BOOST_WEIGHT,
) -> list[SearchResult]:
    """Boost hybrid_score of results that have bibliographic quality metadata.

    Mutates ``hybrid_score`` in place: ``score += boost_weight × quality_score``.
    Only applies to knowledge__/docs__/rdr__ collections.  Results without
    ``bib_citation_count`` are untouched.
    """
    from datetime import date

    today = date.today()
    for r in results:
        if not r.collection.startswith(_QUALITY_ELIGIBLE_PREFIXES):
            continue
        count = int(r.metadata.get("bib_citation_count", 0))
        if count <= 0:
            continue
        bib_year = r.metadata.get("bib_year", "")
        age_days = 0.0
        if bib_year:
            try:
                pub_date = date(int(bib_year), 6, 15)  # mid-year estimate
                age_days = max(0.0, (today - pub_date).days)
            except (ValueError, TypeError):
                pass
        r.hybrid_score += boost_weight * quality_score(count, age_days=age_days)
    return results


# ── Link-aware boost (RDR-060 E3) ───────────────────────────────────────────

_LINK_BOOST_WEIGHTS: dict[str, float] = {
    "implements": 1.0,
    "relates": 0.5,
    "cites": 0.5,
    "supersedes": 0.0,
}
_DEFAULT_LINK_BOOST_WEIGHT: float = 0.15


def apply_link_boost(
    results: list[SearchResult],
    catalog: Any,
    boost_weight: float = _DEFAULT_LINK_BOOST_WEIGHT,
    type_weights: dict[str, float] | None = None,
) -> list[SearchResult]:
    """Boost hybrid_score for results whose source documents have outgoing links.

    Looks up each result's source_path in the catalog, finds outgoing links,
    and computes a link signal from type weights. Additive:
    ``score += boost_weight * min(signal, 1.0)``.

    Only processes results that have ``source_path`` metadata and a matching
    catalog entry. Results without catalog matches are untouched.
    """
    if not catalog:
        return results
    tw = type_weights if type_weights is not None else _LINK_BOOST_WEIGHTS

    # Collect unique source_paths from results
    source_paths: set[str] = set()
    for r in results:
        sp = r.metadata.get("source_path", "")
        if sp:
            source_paths.add(sp)

    if not source_paths:
        return results

    # Batch query: find all tumblers for these source_paths
    placeholders = ",".join("?" for _ in source_paths)
    rows = catalog._db.execute(
        f"SELECT file_path, tumbler FROM documents WHERE file_path IN ({placeholders})",
        list(source_paths),
    ).fetchall()
    path_to_tumbler: dict[str, str] = {row[0]: row[1] for row in rows}

    if not path_to_tumbler:
        return results

    # Batch query: get all outgoing links for these tumblers
    tumbler_strs = list(path_to_tumbler.values())
    placeholders2 = ",".join("?" for _ in tumbler_strs)
    link_rows = catalog._db.execute(
        f"SELECT from_tumbler, link_type FROM links WHERE from_tumbler IN ({placeholders2})",
        tumbler_strs,
    ).fetchall()

    # Aggregate: tumbler -> total weighted signal
    tumbler_signal: dict[str, float] = {}
    for from_t, link_type in link_rows:
        w = tw.get(link_type, 0.0)
        tumbler_signal[from_t] = tumbler_signal.get(from_t, 0.0) + w

    # Apply boost
    for r in results:
        sp = r.metadata.get("source_path", "")
        t_str = path_to_tumbler.get(sp)
        if not t_str:
            continue
        signal = min(tumbler_signal.get(t_str, 0.0), 1.0)
        if signal > 0:
            r.hybrid_score += boost_weight * signal

    return results


# ── Topic boost (RDR-070, nexus-aym) ─────────────────────────────────────

_TOPIC_SAME_BOOST: float = 0.1
_TOPIC_LINKED_BOOST: float = 0.05


def apply_topic_boost(
    results: list[SearchResult],
    topic_assignments: dict[str, int],
    *,
    topic_links: dict[tuple[int, int], int] | None = None,
) -> list[SearchResult]:
    """Boost hybrid_score for results that share or are linked by topic.

    For each result with a topic assignment:
    - If another result in the set shares the SAME topic: +_TOPIC_SAME_BOOST
    - If another result is in a LINKED topic: +_TOPIC_LINKED_BOOST

    Boost is applied once per relationship type (not per partner).
    """
    if not topic_assignments or len(results) < 2:
        return results

    links = topic_links or {}

    # Build topic_id → set of result indices
    topic_to_indices: dict[int, list[int]] = {}
    result_topics: dict[int, int] = {}  # result index → topic_id
    for i, r in enumerate(results):
        tid = topic_assignments.get(r.id)
        if tid is not None:
            result_topics[i] = tid
            topic_to_indices.setdefault(tid, []).append(i)

    # Build set of linked topic pairs (both directions)
    linked_pairs: set[tuple[int, int]] = set()
    for (a, b) in links:
        linked_pairs.add((a, b))
        linked_pairs.add((b, a))

    for i, r in enumerate(results):
        tid = result_topics.get(i)
        if tid is None:
            continue

        # Same-topic boost: at least one other result in the same topic
        same_topic_peers = topic_to_indices.get(tid, [])
        if len(same_topic_peers) > 1:
            r.hybrid_score += _TOPIC_SAME_BOOST

        # Linked-topic boost: at least one result in a linked topic
        has_linked = False
        for other_tid, indices in topic_to_indices.items():
            if other_tid == tid:
                continue
            if (tid, other_tid) in linked_pairs:
                has_linked = True
                break
        if has_linked:
            r.hybrid_score += _TOPIC_LINKED_BOOST

    return results


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
            from nexus.config import get_credential, load_config
            timeout = load_config().get("voyageai", {}).get("read_timeout_seconds", 120.0)
            _voyage_instance = voyageai.Client(
                api_key=get_credential("voyage_api_key"),
                timeout=timeout,
                max_retries=3,
            )
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
        rerank_response = _voyage_with_retry(
            client.rerank,
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
                pass  # intentional: iterator exhaustion is normal control flow
        iterators = next_iters
    return merged
