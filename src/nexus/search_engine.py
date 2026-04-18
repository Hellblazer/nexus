# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search engine: cross-corpus orchestration."""
from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import structlog

from nexus.config import get_telemetry_config, load_config
from nexus.types import SearchResult

_log = structlog.get_logger(__name__)

__all__ = [
    "search_cross_corpus",
    "_overfetch_multiplier",
    "SearchDiagnostics",
]


@dataclass
class SearchDiagnostics:
    """Per-call threshold-filter telemetry (RDR-087 Phase 1.2).

    ``per_collection`` maps name → ``(raw, dropped, threshold, top_distance)``
    where *top_distance* is the minimum (best-ranked, i.e. closest-to-query)
    distance among dropped candidates for that collection, or ``None`` when
    nothing was dropped. The name ``top_distance`` matches the RDR-087 stderr
    format (the "top of the ranking"); internally it is a ``min`` over dropped
    distances because smaller cosine distance = higher rank.

    The struct is populated by ``search_cross_corpus`` when the caller passes
    ``diagnostics_out=[]``; the CLI reads ``worst_offender()`` to emit the
    silent-zero stderr note. The engine itself never emits stderr.
    """

    per_collection: dict[str, tuple[int, int, float | None, float | None]] = field(
        default_factory=dict,
    )
    total_dropped: int = 0
    total_raw: int = 0

    def collections_with_drops(self) -> int:
        return sum(
            1 for _, dropped, _, _ in self.per_collection.values() if dropped > 0
        )

    def worst_offender(self) -> tuple[str, float | None, float] | None:
        """Return ``(name, threshold, top_distance)`` for the worst offender.

        Worst offender = collection where every candidate was dropped, with
        the highest ``top_distance`` among those. ``None`` when no collection
        had every candidate filtered.
        """
        candidates = [
            (name, threshold, top_dist)
            for name, (raw, dropped, threshold, top_dist) in self.per_collection.items()
            if raw > 0 and dropped == raw and top_dist is not None
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[2])

# Maximum ID-set size for ChromaDB $in filter — cap to avoid payload bloat.
_MAX_PREFILTER_IDS = 500

# Selectivity threshold — only pre-filter when <5% of docs match.
_SELECTIVITY_THRESHOLD = 0.05

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


def _overfetch_multiplier(collection_name: str) -> int:
    """Return the over-fetch multiplier for *collection_name*.

    4x for knowledge/docs/rdr (high noise ratio that benefits from a larger
    candidate pool before threshold filtering); 2x for code and everything else.
    """
    for prefix in ("knowledge__", "docs__", "rdr__"):
        if collection_name.startswith(prefix):
            return 4
    return 2


# ── Catalog pre-filtering (Compass, RF-3) ────────────────────────────────────

# ChromaDB where-clause operators mapped to SQL operators.
_OP_MAP = {"$eq": "=", "$gte": ">=", "$lte": "<=", "$gt": ">", "$lt": "<", "$ne": "!="}

# Predicates we can route to catalog SQLite for pre-filtering.
_PREDICATE_TO_COLUMN = {"bib_year": "year"}


def _catalog_ids_for_predicates(db: sqlite3.Connection, predicates: dict) -> list[str]:
    """Query catalog SQLite for file_paths matching *predicates*.

    Only handles predicates that map to catalog columns (bib_year → year).
    Returns file_path list (used as source_path filter in ChromaDB).
    """
    clauses: list[str] = []
    params: list[Any] = []

    for key, value in predicates.items():
        col = _PREDICATE_TO_COLUMN.get(key)
        if col is None:
            return []  # unsupported predicate — can't pre-filter
        if isinstance(value, dict):
            for op_key, op_val in value.items():
                sql_op = _OP_MAP.get(op_key)
                if sql_op is None:
                    return []
                clauses.append(f"{col} {sql_op} ?")
                params.append(op_val)
        else:
            clauses.append(f"{col} = ?")
            params.append(value)

    if not clauses:
        return []

    where = " AND ".join(clauses)
    rows = db.execute(
        f"SELECT file_path FROM documents WHERE {where} AND file_path IS NOT NULL",
        params,
    ).fetchall()
    return [r[0] for r in rows]


def _prefilter_from_catalog(
    where: dict | None, catalog: Any | None,
) -> dict | None:
    """Build a ChromaDB source_path pre-filter from catalog when selectivity is high.

    Returns a ChromaDB ``where`` dict with ``source_path $in`` if the catalog
    match set is small enough (<5% of total docs, ≤500 IDs).  Returns ``None``
    to fall through to standard post-filtering otherwise.
    """
    if not where or catalog is None:
        return None

    db = getattr(catalog, "_db", None)
    if db is None:
        return None

    # Only attempt pre-filter for predicates we can map to catalog columns
    mappable = {k: v for k, v in where.items() if k in _PREDICATE_TO_COLUMN}
    if not mappable:
        return None

    try:
        # Get the raw sqlite3 connection from CatalogDB wrapper
        conn = getattr(db, "_conn", db)
        paths = _catalog_ids_for_predicates(conn, mappable)
    except Exception:
        _log.debug("catalog_prefilter_failed", exc_info=True)
        return None

    if not paths:
        return None
    if len(paths) > _MAX_PREFILTER_IDS:
        _log.debug("catalog_prefilter_too_many", count=len(paths))
        return None

    # Selectivity check
    total = catalog.doc_count() if hasattr(catalog, "doc_count") else 0
    if total > 0 and len(paths) / total > _SELECTIVITY_THRESHOLD:
        return None

    return {"source_path": {"$in": paths}}


# ── Cross-corpus search ───────────────────────────────────────────────────────

_CLUSTER_DEFAULT = "semantic"


def search_cross_corpus(
    query: str,
    collections: list[str],
    n_results: int,
    t3: Any,
    where: dict | None = None,
    cluster_by: str | None = _CLUSTER_DEFAULT,
    catalog: Any | None = None,
    link_boost: bool = False,
    taxonomy: Any | None = None,
    topic: str | None = None,
    threshold_override: float | None = None,
    diagnostics_out: list[SearchDiagnostics] | None = None,
    telemetry: Any | None = None,
) -> list[SearchResult]:
    """Query each collection independently, returning combined raw results.

    Per-corpus over-fetch: each collection fetches ``max(5, n_results * mult)``
    candidates where *mult* is ``_overfetch_multiplier(collection)`` — 4x for
    knowledge/docs/rdr, 2x for code.  The larger pool compensates for the
    distance-threshold filtering that follows, ensuring enough survivors reach
    the caller's reranker.

    When *cluster_by* is ``"semantic"`` (default), results are grouped by
    topic assignments from T2 taxonomy if >50% of results have assignments.
    Otherwise falls back to Ward hierarchical clustering. Each grouped
    result gets a ``_topic_label`` or ``_cluster_label`` metadata key.

    Pass ``cluster_by=None`` to disable all clustering.

    *taxonomy* is an optional :class:`CatalogTaxonomy` instance for topic
    lookups. When ``None`` and ``cluster_by="semantic"``, falls back to
    Ward clustering.

    *threshold_override* (RDR-087 Phase 1.1 / nexus-yi4b.1.1) replaces the
    per-collection distance threshold when non-``None``. The override is
    applied uniformly across all collections and bypasses the
    Voyage-client gate — explicit user intent takes precedence over the
    local-mode skip heuristic. Use ``float('inf')`` to disable filtering
    entirely (exposed as ``--no-threshold`` in the CLI).

    *diagnostics_out* (RDR-087 Phase 1.2 / nexus-yi4b.1.2), when provided
    as an empty list, is populated with a single :class:`SearchDiagnostics`
    instance summarising per-collection raw/dropped counts and threshold
    context. Used by the CLI to emit the silent-zero stderr note; the
    engine never emits stderr itself.

    *telemetry* (RDR-087 Phase 2.2 / nexus-yi4b.2.2), when provided,
    receives one ``(ts, query_hash, collection, raw_count, kept_count,
    top_distance, threshold)`` row per collection via
    ``log_search_batch``. Opt-out via ``telemetry.search_enabled = false``
    in ``.nexus.yml`` — the engine reads the flag and silently skips the
    write even when *telemetry* is non-``None``. *query_hash* is
    ``sha256(query)[:64]`` so raw query text is never persisted.
    *top_distance* is the minimum (best-ranked) distance across ALL raw
    candidates for that collection (kept or dropped), not just the
    dropped subset — see ``SearchDiagnostics`` for the dropped-only
    variant.
    """
    cfg = load_config()
    # Config can override: search.cluster_by in .nexus.yml
    cfg_cluster = cfg.get("search", {}).get("cluster_by")
    if cfg_cluster is not None:
        cluster_by = cfg_cluster

    # Topic pre-filter (RDR-070, nexus-u2a): restrict to docs in a topic
    topic_doc_ids: set[str] | None = None
    if topic and taxonomy is not None:
        try:
            ids = taxonomy.get_doc_ids_for_topic(topic)
            if not ids:
                _log.info("topic_not_found", topic=topic)
                return []
            if len(ids) <= _MAX_PREFILTER_IDS:
                topic_doc_ids = set(ids)
            else:
                # Too many — post-filter after search
                topic_doc_ids = set(ids)
                _log.debug("topic_prefilter_post_filter", topic=topic, n_ids=len(ids))
        except Exception:
            _log.debug("topic_prefilter_failed", exc_info=True)

    # Thresholds are calibrated for Voyage AI embeddings.
    # Skip filtering when Voyage is not in use (local mode, test injection).
    # Explicit threshold_override bypasses this gate — caller intent wins.
    apply_thresholds = (
        threshold_override is not None
        or getattr(t3, "_voyage_client", None) is not None
    )

    # Catalog pre-filter: for high-selectivity predicates, narrow the search
    # space via source_path $in filter (Compass, RF-3).
    prefilter = _prefilter_from_catalog(where, catalog)
    if prefilter is not None:
        # Merge pre-filter with original where — pre-filter uses source_path,
        # original where keeps the metadata predicates for ChromaDB post-filter.
        effective_where: dict | None = {"$and": [prefilter, where]} if where else prefilter
        _log.debug("catalog_prefilter_applied", paths=len(prefilter.get("source_path", {}).get("$in", [])))
    else:
        effective_where = where

    all_results: list[SearchResult] = []
    diag_per_collection: dict[str, tuple[int, int, float | None, float | None]] = {}
    total_dropped = 0
    total_raw = 0
    # Per-collection raw min-distance accumulator for search_telemetry rows.
    # Distinct from ``min_dropped_distance`` which covers only dropped items.
    min_raw_per_collection: dict[str, float | None] = {}
    for col in collections:
        mult = _overfetch_multiplier(col)
        # Search review I-3: cap per_k at MAX_QUERY_RESULTS=300. Without
        # this, a large ``offset`` fed into ``fetch_n = offset + limit``
        # upstream multiplies by ``mult`` (up to 4×) and the per-collection
        # n_results punches through the ChromaDB Cloud quota.
        from nexus.db.chroma_quotas import QUOTAS
        per_k = min(max(5, n_results * mult), QUOTAS.MAX_QUERY_RESULTS)
        if not apply_thresholds:
            threshold = None
        elif threshold_override is not None:
            threshold = threshold_override
        else:
            threshold = _threshold_for_collection(col, cfg)
        raw = t3.search(query, [col], n_results=per_k, where=effective_where)
        dropped = 0
        # Minimum distance among dropped items — best-of-dropped, used by
        # SearchDiagnostics.worst_offender() for the "threshold bump" hint.
        min_dropped_distance: float | None = None
        # Minimum distance across ALL raw candidates — stored in
        # search_telemetry as ``top_distance`` (best-of-raw).
        min_raw_distance: float | None = None
        for r in raw:
            distance = r["distance"]
            if min_raw_distance is None or distance < min_raw_distance:
                min_raw_distance = distance
            # RDR-055 E2 quality_boost runs after hybrid scoring in the
            # CLI/MCP paths. Thresholds apply to raw distance here.
            if threshold is not None and distance > threshold:
                dropped += 1
                if min_dropped_distance is None or distance < min_dropped_distance:
                    min_dropped_distance = distance
                continue
            all_results.append(SearchResult(
                id=r["id"],
                content=r["content"],
                distance=distance,
                collection=col,
                metadata={k: v for k, v in r.items()
                          if k not in {"id", "content", "distance"}},
            ))
        diag_per_collection[col] = (len(raw), dropped, threshold, min_dropped_distance)
        min_raw_per_collection[col] = min_raw_distance
        total_raw += len(raw)
        total_dropped += dropped
        if dropped:
            _log.debug(
                "threshold_filtered",
                collection=col,
                dropped=dropped,
                threshold=threshold,
            )

    if diagnostics_out is not None:
        diagnostics_out.append(SearchDiagnostics(
            per_collection=diag_per_collection,
            total_dropped=total_dropped,
            total_raw=total_raw,
        ))

    # RDR-087 Phase 2.2: persist per-call threshold-filter telemetry.
    # Opt-out gate reads from the typed accessor so malformed
    # ``.nexus.yml`` values surface a structured warning instead of
    # silently coercing to a truthy string.
    if telemetry is not None and get_telemetry_config(cfg=cfg).search_enabled:
        import hashlib
        from datetime import UTC, datetime

        ts = datetime.now(UTC).isoformat()
        query_hash = hashlib.sha256(query.encode()).hexdigest()[:64]
        rows = [
            (
                ts, query_hash, col,
                raw_count, raw_count - dropped_count, min_raw_per_collection[col], thr,
            )
            for col, (raw_count, dropped_count, thr, _dropped_min)
            in diag_per_collection.items()
        ]
        try:
            telemetry.log_search_batch(rows)
        except Exception:
            _log.debug("search_telemetry_write_failed", exc_info=True)

    # Topic post-filter: keep only results in the requested topic
    if topic_doc_ids is not None:
        all_results = [r for r in all_results if r.id in topic_doc_ids]

    # Link-aware boost (RDR-060 E3)
    if link_boost and catalog and all_results:
        from nexus.scoring import apply_link_boost
        all_results = apply_link_boost(all_results, catalog)

    # Compute topic assignments once for both boost and grouping (RDR-070)
    _topic_assignments: dict[str, int] | None = None
    if all_results and taxonomy is not None:
        try:
            result_ids = [r.id for r in all_results]
            _topic_assignments = taxonomy.get_assignments_for_docs(result_ids)
        except Exception:
            _log.debug("topic_assignments_failed", exc_info=True)

    # Fetch embeddings once if either contradiction detection OR clustering
    # needs them — avoids double fetching (F1 fix). Per-collection failures
    # are isolated: failed indices are excluded from feature processing but
    # do not suppress the features for successfully-fetched collections (R3-1).
    contradiction_enabled = cfg.get("search", {}).get("contradiction_check", True)
    needs_embeddings = (contradiction_enabled or cluster_by == "semantic") and all_results
    fetched_embeddings = None
    failed_indices: set[int] = set()
    if needs_embeddings:
        fetched_embeddings, failed_indices = _fetch_embeddings_for_results(all_results, t3)

    # Contradiction detection (RDR-057 Phase 3a). Default-on; opt out via
    # search.contradiction_check=false in .nexus.yml.
    if contradiction_enabled and all_results and fetched_embeddings is not None:
        all_results = _flag_contradictions(all_results, fetched_embeddings, failed_indices)

    if cluster_by == "semantic" and all_results:
        topic_grouped = False
        # Try topic-based grouping first (RDR-070, nexus-y8f)
        if taxonomy is not None and _topic_assignments:
            try:
                assignments = _topic_assignments
                coverage = len(assignments) / len(all_results) if all_results else 0
                if coverage > 0.5:
                    all_results = _apply_topic_grouping(all_results, assignments, taxonomy)
                    topic_grouped = True
                else:
                    _log.debug(
                        "topic_grouping_skipped_low_coverage",
                        coverage=f"{coverage:.0%}",
                        assigned=len(assignments),
                        total=len(all_results),
                    )
            except Exception:
                _log.debug("topic_grouping_failed", exc_info=True)

        # Fall back to Ward clustering if topic grouping didn't fire
        if not topic_grouped and fetched_embeddings is not None:
            if not failed_indices:
                all_results = _apply_clustering(all_results, fetched_embeddings)
            else:
                _log.warning(
                    "clustering_skipped_partial_failure",
                    failed_indices=len(failed_indices),
                    total_results=len(all_results),
                    reason="cannot partially cluster — some collection fetches failed",
                )

    # Topic boost (RDR-070, nexus-aym) — applied AFTER grouping so
    # distance-based group ordering is not contaminated by the boost.
    if _topic_assignments and all_results:
        try:
            from nexus.scoring import apply_topic_boost

            # Read cached topic links for linked-topic boost
            topic_links: dict[tuple[int, int], int] | None = None
            if taxonomy is not None:
                relevant_ids = list(set(_topic_assignments.values()))
                topic_links = taxonomy.get_topic_link_pairs(relevant_ids) or None

            all_results = apply_topic_boost(
                all_results, _topic_assignments, topic_links=topic_links,
            )
        except Exception:
            _log.debug("topic_boost_failed", exc_info=True)

    return all_results


def _fetch_embeddings_for_results(
    results: list[SearchResult],
    t3: Any,
) -> "tuple[np.ndarray | None, set[int]]":
    """Fetch embeddings for all results in one pass, grouped by collection.

    Returns ``(embeddings, failed_indices)`` where:
    - ``embeddings`` is a float32 ndarray of shape ``(len(results), emb_dim)``
      with zero rows for any position in ``failed_indices``
    - ``failed_indices`` is the set of result indices whose collection fetch
      failed or had a shape mismatch

    When ALL collections fail, returns ``(None, all_indices)``.
    Callers must skip ``failed_indices`` when processing — feature logic
    (contradiction check, clustering) continues for successfully-fetched
    collections rather than being suppressed entirely (R3-1 fix).
    """
    import numpy as np

    col_groups: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        col_groups.setdefault(r.collection, []).append(idx)

    embeddings: "np.ndarray | None" = None
    failed_indices: set[int] = set()

    # First pass: determine emb_dim from a successful fetch so we can
    # allocate the output array. Collect per-collection results as we go.
    col_fetched: dict[str, "np.ndarray"] = {}
    emb_dim: int | None = None
    for col, indices in col_groups.items():
        ids = [results[i].id for i in indices]
        try:
            col_emb = t3.get_embeddings(col, ids)
        except Exception as exc:
            _log.warning(
                "embedding_fetch_failed",
                collection=col,
                requested=len(indices),
                exc_info=exc,
            )
            failed_indices.update(indices)
            continue
        if col_emb.shape[0] != len(indices):
            _log.warning(
                "embedding_fetch_shape_mismatch",
                collection=col,
                requested=len(indices),
                got=col_emb.shape[0],
            )
            failed_indices.update(indices)
            continue
        col_fetched[col] = col_emb
        if emb_dim is None:
            emb_dim = col_emb.shape[1]

    # If nothing fetched, nothing to assemble
    if emb_dim is None:
        return None, failed_indices

    embeddings = np.zeros((len(results), emb_dim), dtype=np.float32)
    for col, col_emb in col_fetched.items():
        indices = col_groups[col]
        for local_idx, global_idx in enumerate(indices):
            embeddings[global_idx] = col_emb[local_idx]

    return embeddings, failed_indices


def _flag_contradictions(
    results: list[SearchResult],
    embeddings: "np.ndarray",
    failed_indices: set[int] | None = None,
) -> list[SearchResult]:
    """Flag results where same-collection pairs have different source_agent and close distance.

    Two results from the same collection, with different non-empty source_agent
    provenance and cosine distance < 0.3, get ``_contradiction_flag=True`` in
    their metadata. Purely retrieval-time, no LLM calls.

    Takes pre-fetched embeddings (see _fetch_embeddings_for_results) to avoid
    duplicate ChromaDB round-trips when clustering also runs.

    ``failed_indices`` are excluded from the check — their embeddings are
    zero-filled placeholders from the shared fetch helper and must not be
    compared against valid rows.
    """
    import numpy as np

    failed_indices = failed_indices or set()
    col_groups: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        if idx in failed_indices:
            continue
        col_groups.setdefault(r.collection, []).append(idx)

    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    normed = embeddings / np.maximum(norms, 1e-9)

    flagged: set[int] = set()
    pairs_checked = 0
    # Search review I-8: cap the O(n²) pairwise check to keep a single
    # noisy collection (near-duplicate chunks, e.g. a knowledge__* corpus
    # with repeated boilerplate) from dominating search-engine latency.
    # At 30 indices, the pair count is 435 — above that the pairwise
    # signal is rarely informative and the cost grows quadratically.
    _CONTRADICTION_MAX_PER_COLLECTION = 30
    for col, indices in col_groups.items():
        if len(indices) < 2:
            continue
        if len(indices) > _CONTRADICTION_MAX_PER_COLLECTION:
            _log.debug(
                "contradiction_check_skipped_collection",
                collection=col,
                size=len(indices),
                cap=_CONTRADICTION_MAX_PER_COLLECTION,
            )
            continue
        for a, b in itertools.combinations(indices, 2):
            pairs_checked += 1
            dist = 1.0 - float(np.dot(normed[a], normed[b]))
            if dist >= 0.3:
                continue
            agent_a = results[a].metadata.get("source_agent", "")
            agent_b = results[b].metadata.get("source_agent", "")
            if agent_a and agent_b and agent_a != agent_b:
                flagged.add(a)
                flagged.add(b)

    _log.debug(
        "contradiction_check",
        collections=len(col_groups),
        results=len(results),
        pairs_checked=pairs_checked,
        flagged=len(flagged),
    )

    out: list[SearchResult] = []
    for idx, r in enumerate(results):
        if idx in flagged:
            meta = dict(r.metadata)
            meta["_contradiction_flag"] = True
            out.append(SearchResult(
                id=r.id, content=r.content, distance=r.distance,
                collection=r.collection, metadata=meta,
                hybrid_score=r.hybrid_score,
            ))
        else:
            out.append(r)
    return out


def _apply_clustering(
    results: list[SearchResult],
    embeddings: "np.ndarray",
) -> list[SearchResult]:
    """Cluster results using pre-fetched embeddings, returning flat list with labels.

    Takes pre-fetched embeddings (see _fetch_embeddings_for_results) to avoid
    duplicate ChromaDB round-trips when contradiction detection also runs.
    """
    from nexus.search_clusterer import cluster_results

    # Convert SearchResults to dicts for cluster_results API
    result_dicts = [
        {"id": r.id, "content": r.content, "distance": r.distance,
         "collection": r.collection, "metadata": dict(r.metadata),
         "hybrid_score": r.hybrid_score}
        for r in results
    ]

    clusters = cluster_results(result_dicts, embeddings)

    # Flatten clusters back to SearchResult list, preserving cluster labels
    out: list[SearchResult] = []
    for cluster in clusters:
        for rd in cluster:
            meta = rd.get("metadata", {})
            if "_cluster_label" in rd:
                meta["_cluster_label"] = rd["_cluster_label"]
            out.append(SearchResult(
                id=rd["id"],
                content=rd["content"],
                distance=rd["distance"],
                collection=rd["collection"],
                metadata=meta,
                hybrid_score=rd.get("hybrid_score", 0.0),
            ))
    return out


def _apply_topic_grouping(
    results: list[SearchResult],
    assignments: dict[str, int],
    taxonomy: Any,
) -> list[SearchResult]:
    """Group results by T2 topic assignment, sorted by topic then distance.

    Results with assignments get a ``_topic_label`` metadata key.
    Unassigned results are appended at the end.
    """
    # Build topic_id → label map (scoped query, not full table scan)
    topic_ids = set(assignments.values())
    id_to_label = taxonomy.get_labels_for_ids(list(topic_ids))

    # Partition: assigned (grouped by topic) vs unassigned
    grouped: dict[int, list[SearchResult]] = {}
    unassigned: list[SearchResult] = []
    for r in results:
        tid = assignments.get(r.id)
        if tid is not None and tid in id_to_label:
            grouped.setdefault(tid, []).append(r)
        else:
            unassigned.append(r)

    # Sort groups by best (lowest) distance, within group by distance
    out: list[SearchResult] = []
    for tid, group in sorted(grouped.items(), key=lambda kv: min(r.distance for r in kv[1])):
        label = id_to_label[tid]
        for r in sorted(group, key=lambda r: r.distance):
            meta = dict(r.metadata)
            meta["_topic_label"] = label
            out.append(SearchResult(
                id=r.id, content=r.content, distance=r.distance,
                collection=r.collection, metadata=meta,
                hybrid_score=r.hybrid_score,
            ))

    # Unassigned at the end, sorted by distance
    for r in sorted(unassigned, key=lambda r: r.distance):
        out.append(r)

    return out
