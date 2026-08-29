# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx collection health`` composite report — RDR-087 Phase 3.4.

One row per collection with 9 columns folding catalog, T2 telemetry,
and topic-assignment signals into a single per-collection view:

    name, chunk_count, last_indexed, zero_hit_rate_30d,
    median_query_distance_30d, cross_projection_rank,
    orphan_catalog_rows, stale_source_ratio, hub_domination_score

``stale_source_ratio`` is the fraction of a collection's documents last indexed
more than 30 days ago (``catalog._STALE_SOURCE_AGE_DAYS``; nexus-agsq7) — an INDEX-AGE proxy for
staleness, computed DB-only from ``indexed_at``. A true "source modified since
indexing" signal is structurally impossible from stored columns
(``source_mtime`` is captured at index time, so it is always <= ``indexed_at``;
RDR-154 P2 found this) and would need a live filesystem stat. ``None`` (rendered
``"—"``) when no document in the collection carries a parseable ``indexed_at``.

Every data source is dependency-injected via module-level callables
so tests can monkeypatch without standing up live T2/T3/catalog.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable


_STALE_PLACEHOLDER = "—"
_SORT_COLUMNS = (
    "name",
    "chunk_count",
    "last_indexed",
    "zero_hit_rate_30d",
    "median_query_distance_30d",
    "cross_projection_rank",
    "orphan_catalog_rows",
    "stale_source_ratio",
    "hub_domination_score",
)


@dataclass(frozen=True)
class CollectionHealthRow:
    name: str
    chunk_count: int
    last_indexed: str | None
    zero_hit_rate_30d: float | None
    median_query_distance_30d: float | None
    cross_projection_rank: int | None
    orphan_catalog_rows: int | None
    hub_domination_score: float | None = None
    # nexus-agsq7: fraction of docs indexed more than _STALE_AGE_DAYS ago
    # (index-age staleness proxy); None when no doc has a parseable indexed_at.
    stale_source_ratio: float | None = None


# ── Default production runners (dep-injected) ───────────────────────────────


def _default_enumerate_collections() -> list[str]:
    from nexus.db import make_t3  # noqa: PLC0415 — function-local import defers heavy db/T3 init until called

    return [c["name"] for c in make_t3().list_collections()]


def _open_catalog():
    """Return an initialised :class:`Catalog` or ``None`` when absent.

    The catalog lives outside the T2 DB; an uninitialised catalog just
    means the collection has no documents on record yet — health rows
    for such collections show zero chunks / no links / no orphans.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — function-local import avoids catalog<->health circular dependency

    return make_catalog_reader()


def _default_catalog_stats_fn(col: str) -> dict[str, Any]:
    """Return ``{last_indexed, orphan_count}`` for *col* from the catalog.

    ``last_indexed`` is the MAX of ``indexed_at`` across documents in
    the collection; ``orphan_count`` is the count of documents that
    have zero incoming links. Both properties are purely catalog-side
    (the catalog is authoritative for its own rows and for the link
    graph).

    ``chunk_count`` is deliberately NOT returned here — the catalog's
    ``chunk_count`` column drifts from T3 reality whenever a write
    path skips the catalog registration step (direct ``store_put``,
    cloud-side operations, tenants that predate the catalog column).
    Ground-truth chunk counts come from T3 via :func:`_default_chunk_count_fn`
    so ``nx collection health`` and ``nx collection list`` cannot
    disagree (nexus-39zi).

    nexus-dsu5z: delegates to ``cat.collection_health_meta(col)`` which is
    implemented on both Catalog (SQLite) and HttpCatalogClient (service mode).
    The former ``hasattr(cat, '_db')`` guard and inline SQL are removed.
    """
    cat = _open_catalog()
    if cat is None:
        return {"last_indexed": None, "orphan_count": 0}
    try:
        return cat.collection_health_meta(col)
    except Exception:  # noqa: BLE001 — best-effort diagnostic stat; degrade to safe defaults rather than crash the health report
        return {"last_indexed": None, "orphan_count": 0}


def _default_chunk_count_fn(col: str) -> int:
    """Return the T3 chunk count for *col* — the ground-truth number.

    Queries ``col.count()`` on the live T3 ChromaDB collection. This is
    the same source ``nx collection list`` reads, so the health report
    and the list command cannot disagree (nexus-39zi, 2026-04-18 live
    shakeout finding: catalog-sourced count drifted to 0 for 129/143
    collections on production).

    Returns 0 on any failure — T3 unreachable, collection missing,
    transient network error. The catalog-sourced ``last_indexed``
    remains meaningful even when the T3 number is stale, so a 0 here
    renders as an operator-visible "nothing to count" without
    cascading errors elsewhere in the report.
    """
    try:
        from nexus.db import make_t3  # noqa: PLC0415 — function-local import defers heavy db/T3 init until called
        t3 = make_t3()
        try:
            coll = t3.get_or_create_collection(col)
            return int(coll.count())
        except Exception:  # noqa: BLE001 — collection missing/transient T3 error; docstring mandates 0 fallback, not propagation
            return 0
    except Exception:  # noqa: BLE001 — T3 unreachable; docstring mandates 0 fallback so the report renders without cascading errors
        return 0


def _open_t2():
    """Open a ``T2Database`` rooted at the default path, or ``None``
    when the DB file doesn't exist yet."""
    from nexus.config import default_db_path  # noqa: PLC0415 — function-local import avoids config/db import cost at module load
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — function-local import defers T2 db init until called

    db_path = default_db_path()
    if not db_path.exists():
        return None
    return T2Database(db_path)  # boundary-allow: read-only T2 access, no WAL writer contention (RDR-128 P3)


def _default_telemetry_stats_fn(col: str) -> dict[str, Any]:
    t2 = _open_t2()
    if t2 is None:
        return {"row_count": 0, "zero_hit_rate": None, "median_top_distance": None}
    try:
        return t2.telemetry.query_collection_stats(col)
    finally:
        t2.close()


def _default_projection_rank_fn(cols: list[str]) -> dict[str, int]:
    """Projection-rank enrichment column — currently always empty.

    Ranked collections by DISTINCT incoming projection sources via raw SQL
    over the local SQLite ``topic_assignments``; that store was deleted in
    the RDR-158 P4 retirement and the engine's taxonomy API does not expose
    the aggregate yet (nexus-i711w.1 GAP). nexus-9613q.4: this is a
    diagnostic ENRICHMENT column, so degrade-to-empty is the right contract
    (the display renders absence) — contrast merge_candidates, whose raw
    read WAS the command's output and which reports itself unavailable.
    """
    return {}


def _default_hub_score_fn(col: str) -> float | None:
    """Hub-score enrichment column — currently always ``None``.

    Computed the top-10-hub assignment ratio via raw SQL over the local
    SQLite ``topic_assignments``; same disposition as
    :func:`_default_projection_rank_fn` (store deleted, no engine
    aggregate yet, degrade-to-absent per nexus-9613q.4).
    """
    return None


# Module-level runner bindings — tests monkeypatch these directly.
_enumerate_collections = _default_enumerate_collections
_catalog_stats_fn = _default_catalog_stats_fn
_chunk_count_fn = _default_chunk_count_fn
_telemetry_stats_fn = _default_telemetry_stats_fn
_projection_rank_fn = _default_projection_rank_fn
_hub_score_fn = _default_hub_score_fn


# ── Orchestrator ────────────────────────────────────────────────────────────


def compute_collection_health(
    collections: list[str],
    *,
    catalog_stats_fn: Callable[[str], dict[str, Any]],
    telemetry_stats_fn: Callable[[str], dict[str, Any]],
    projection_rank_fn: Callable[[list[str]], dict[str, int]],
    hub_score_fn: Callable[[str], float | None],
    chunk_count_fn: Callable[[str], int] | None = None,
) -> list[CollectionHealthRow]:
    """Assemble per-collection health rows from the injected callables.

    Ordering follows *collections*; callers sort via ``format_health_table``.

    ``chunk_count_fn`` (nexus-39zi) returns the ground-truth chunk count
    from T3 for each collection. Optional only for backward-compat with
    pre-39zi callers that still use ``catalog_stats_fn`` as the
    chunk-count source. Production callers always pass it — the
    catalog-sourced count drifts to 0 for most collections on tenants
    that predate the catalog's ``chunk_count`` column. When both are
    present, ``chunk_count_fn`` wins.
    """
    ranks = projection_rank_fn(collections)
    rows: list[CollectionHealthRow] = []
    for col in collections:
        catalog = catalog_stats_fn(col) or {}
        tel = telemetry_stats_fn(col) or {}
        # T3 is the ground truth for chunk count. Fall back to the
        # catalog's number only when no ``chunk_count_fn`` was injected
        # (legacy callers) — NEVER mix the two sources inside a single
        # row, as that would create asymmetric reporting.
        if chunk_count_fn is not None:
            chunk_count = int(chunk_count_fn(col))
        else:
            chunk_count = int(catalog.get("chunk_count", 0))
        rows.append(
            CollectionHealthRow(
                name=col,
                chunk_count=chunk_count,
                last_indexed=catalog.get("last_indexed"),
                zero_hit_rate_30d=tel.get("zero_hit_rate"),
                median_query_distance_30d=tel.get("median_top_distance"),
                cross_projection_rank=ranks.get(col),
                orphan_catalog_rows=int(catalog.get("orphan_count", 0))
                    if catalog.get("orphan_count") is not None else None,
                stale_source_ratio=catalog.get("stale_source_ratio"),
                hub_domination_score=hub_score_fn(col),
            )
        )
    return rows


# ── Formatters ──────────────────────────────────────────────────────────────


def _fmt_cell(value: Any) -> str:
    if value is None:
        return _STALE_PLACEHOLDER
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _sort_key(col: str):
    """Return a per-row sort-key lambda for *col*."""
    if col not in _SORT_COLUMNS:
        raise ValueError(
            f"unknown sort column {col!r}; valid: {sorted(_SORT_COLUMNS)}"
        )
    # Numeric columns — descending; text columns — ascending.
    descending = col not in {"name", "last_indexed"}
    def _key(row: CollectionHealthRow):
        v = getattr(row, col)
        # None sorts last regardless of direction.
        if v is None:
            return (1, 0)
        return (0, -v if descending and isinstance(v, (int, float)) else v)
    return _key


def format_health_table(
    rows: list[CollectionHealthRow], *, sort_by: str = "name",
) -> str:
    """Render rows as a column-aligned text table."""
    ordered = sorted(rows, key=_sort_key(sort_by))
    headers = [
        "name", "chunk_count", "last_indexed", "zero_hit_rate_30d",
        "median_query_distance_30d", "cross_projection_rank",
        "orphan_catalog_rows", "stale_source_ratio", "hub_domination_score",
    ]
    data = [
        [
            r.name,
            str(r.chunk_count),
            _fmt_cell(r.last_indexed),
            _fmt_cell(r.zero_hit_rate_30d),
            _fmt_cell(r.median_query_distance_30d),
            _fmt_cell(r.cross_projection_rank),
            _fmt_cell(r.orphan_catalog_rows),
            _fmt_cell(r.stale_source_ratio),
            _fmt_cell(r.hub_domination_score),
        ]
        for r in ordered
    ]
    widths = [
        max(len(h), *(len(row[i]) for row in data)) if data else len(h)
        for i, h in enumerate(headers)
    ]
    def _row(cells):
        return "  ".join(c.ljust(w) for c, w in zip(cells, widths))
    lines = [_row(headers), _row(["─" * w for w in widths])]
    lines.extend(_row(r) for r in data)

    return "\n".join(lines)


def format_health_json(rows: list[CollectionHealthRow]) -> str:
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "collections": [asdict(r) for r in rows],
    }
    return json.dumps(payload, indent=2)


# ── CLI entry point ─────────────────────────────────────────────────────────


def run_collection_health(*, sort_by: str = "name", fmt: str = "table") -> str:
    """Render the composite report. Invoked by ``commands/collection.py``."""
    collections = _enumerate_collections()
    rows = compute_collection_health(
        collections,
        catalog_stats_fn=_catalog_stats_fn,
        chunk_count_fn=_chunk_count_fn,
        telemetry_stats_fn=_telemetry_stats_fn,
        projection_rank_fn=_projection_rank_fn,
        hub_score_fn=_hub_score_fn,
    )
    if fmt == "json":
        return format_health_json(rows)
    return format_health_table(rows, sort_by=sort_by)
