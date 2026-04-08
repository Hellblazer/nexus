# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search engine: cross-corpus orchestration."""
from __future__ import annotations

import sqlite3
from typing import Any

import structlog

from nexus.config import load_config
from nexus.types import SearchResult

_log = structlog.get_logger(__name__)

__all__ = [
    "search_cross_corpus",
    "_overfetch_multiplier",
]

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
_PREDICATE_TO_COLUMN = {"bib_year": "year", "bib_citation_count": "year"}  # citation_count not in catalog


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

def search_cross_corpus(
    query: str,
    collections: list[str],
    n_results: int,
    t3: Any,
    where: dict | None = None,
    cluster_by: str | None = None,
    catalog: Any | None = None,
    link_boost: bool = False,
) -> list[SearchResult]:
    """Query each collection independently, returning combined raw results.

    Per-corpus over-fetch: each collection fetches ``max(5, n_results * mult)``
    candidates where *mult* is ``_overfetch_multiplier(collection)`` — 4x for
    knowledge/docs/rdr, 2x for code.  The larger pool compensates for the
    distance-threshold filtering that follows, ensuring enough survivors reach
    the caller's reranker.

    When *cluster_by* is ``"semantic"``, results are grouped by Ward
    hierarchical clustering and each result gets a ``_cluster_label`` metadata
    key.  Requires one extra ``get_embeddings`` call per collection.
    Disabled by default (``None``).

    *where* is an optional ChromaDB metadata filter forwarded to every collection.
    """
    cfg = load_config()
    if cluster_by is None:
        cluster_by = cfg.get("search", {}).get("cluster_by")

    # Thresholds are calibrated for Voyage AI embeddings.
    # Skip filtering when Voyage is not in use (local mode, test injection).
    apply_thresholds = getattr(t3, "_voyage_client", None) is not None

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
    for col in collections:
        mult = _overfetch_multiplier(col)
        per_k = max(5, n_results * mult)
        threshold = _threshold_for_collection(col, cfg) if apply_thresholds else None
        raw = t3.search(query, [col], n_results=per_k, where=effective_where)
        dropped = 0
        for r in raw:
            distance = r["distance"]
            # RDR-055 E2 quality_boost runs after hybrid scoring in the
            # CLI/MCP paths. Thresholds apply to raw distance here.
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

    # Link-aware boost (RDR-060 E3)
    if link_boost and catalog and all_results:
        from nexus.scoring import apply_link_boost
        all_results = apply_link_boost(all_results, catalog)

    if cluster_by == "semantic" and all_results:
        all_results = _apply_clustering(all_results, t3)

    return all_results


def _apply_clustering(results: list[SearchResult], t3: Any) -> list[SearchResult]:
    """Post-fetch embeddings and cluster results, returning flat list with labels."""
    import numpy as np

    from nexus.search_clusterer import cluster_results

    # Group result indices by collection for batched embedding fetch
    col_groups: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        col_groups.setdefault(r.collection, []).append(idx)

    # Fetch embeddings per collection, assemble in result order
    embeddings = np.zeros((len(results), 0), dtype=np.float32)
    for col, indices in col_groups.items():
        ids = [results[i].id for i in indices]
        col_emb = t3.get_embeddings(col, ids)
        if embeddings.shape[1] == 0:
            embeddings = np.zeros((len(results), col_emb.shape[1]), dtype=np.float32)
        for local_idx, global_idx in enumerate(indices):
            embeddings[global_idx] = col_emb[local_idx]

    # Convert SearchResults to dicts for cluster_results API
    result_dicts = [
        {"id": r.id, "content": r.content, "distance": r.distance,
         "collection": r.collection, "metadata": dict(r.metadata)}
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
            ))
    return out
