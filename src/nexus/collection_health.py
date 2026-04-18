# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx collection health`` composite report — RDR-087 Phase 3.4.

One row per collection with 9 columns folding catalog, T2 telemetry,
and topic-assignment signals into a single per-collection view:

    name, chunk_count, last_indexed, zero_hit_rate_30d,
    median_query_distance_30d, cross_projection_rank,
    orphan_catalog_rows, stale_source_ratio, hub_domination_score

``stale_source_ratio`` is currently a placeholder (``"—"``) because
the catalog doesn't store ``source_mtime`` yet (tracked in bead
``nexus-8luh``).

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
    "hub_domination_score",
    "chash_indexed_ratio",
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
    stale_source_ratio: str = _STALE_PLACEHOLDER  # deferred: nexus-8luh
    # RDR-087 Phase 4.6 (nexus-c2op): ratio of chash_index rows for this
    # collection to its T3 chunk_count. 1.0 → fully backfilled; < 1.0 →
    # nx collection backfill-hash has work to do. None when either the
    # chash_index is empty or the collection has 0 T3 chunks.
    chash_indexed_ratio: float | None = None


# ── Default production runners (dep-injected) ───────────────────────────────


def _default_enumerate_collections() -> list[str]:
    from nexus.db import make_t3

    return [c["name"] for c in make_t3().list_collections()]


def _open_catalog():
    """Return an initialised :class:`Catalog` or ``None`` when absent.

    The catalog lives outside the T2 DB; an uninitialised catalog just
    means the collection has no documents on record yet — health rows
    for such collections show zero chunks / no links / no orphans.
    """
    from nexus.catalog.catalog import Catalog
    from nexus.config import catalog_path

    path = catalog_path()
    if not Catalog.is_initialized(path):
        return None
    return Catalog(path, path / ".catalog.db")


def _default_catalog_stats_fn(col: str) -> dict[str, Any]:
    """Return ``{chunk_count, last_indexed, orphan_count}`` for *col*.

    Uses the catalog cache DB. ``chunk_count`` is the SUM across every
    document in the collection; ``last_indexed`` is the MAX of
    ``indexed_at``; ``orphan_count`` is the count of documents in this
    collection that have zero incoming links.
    """
    cat = _open_catalog()
    if cat is None:
        return {"chunk_count": 0, "last_indexed": None, "orphan_count": 0}
    try:
        # Catalog's SQL cache lives behind private attributes; an
        # explicit public accessor doesn't exist yet and isn't worth
        # adding just for this read-only path.
        conn = cat._db._conn  # noqa: SLF001
        row = conn.execute(
            "SELECT COALESCE(SUM(chunk_count), 0), MAX(indexed_at) "
            "FROM documents WHERE physical_collection = ?",
            (col,),
        ).fetchone()
        chunk_count = int(row[0] or 0)
        last_indexed = row[1] if row and row[1] else None
        orphan_row = conn.execute(
            "SELECT COUNT(*) FROM documents d "
            "LEFT JOIN links l ON d.tumbler = l.to_tumbler "
            "WHERE d.physical_collection = ? AND l.id IS NULL",
            (col,),
        ).fetchone()
        orphan_count = int(orphan_row[0] or 0)
        return {
            "chunk_count": chunk_count,
            "last_indexed": last_indexed,
            "orphan_count": orphan_count,
        }
    except Exception:
        return {"chunk_count": 0, "last_indexed": None, "orphan_count": 0}


def _open_t2():
    """Open a ``T2Database`` rooted at the default path, or ``None``
    when the DB file doesn't exist yet."""
    from nexus.commands._helpers import default_db_path
    from nexus.db.t2 import T2Database

    db_path = default_db_path()
    if not db_path.exists():
        return None
    return T2Database(db_path)


def _default_telemetry_stats_fn(col: str) -> dict[str, Any]:
    t2 = _open_t2()
    if t2 is None:
        return {"row_count": 0, "zero_hit_rate": None, "median_top_distance": None}
    try:
        return t2.telemetry.query_collection_stats(col)
    finally:
        t2.close()


def _default_projection_rank_fn(cols: list[str]) -> dict[str, int]:
    """Rank collections by their incoming cross-projection count.

    Rank 1 = receives from the most distinct source collections.
    Collections with no incoming projections are absent from the map;
    the orchestrator treats that as ``None``.
    """
    t2 = _open_t2()
    if t2 is None:
        return {}
    try:
        conn = t2.taxonomy.conn
        rows = conn.execute(
            "SELECT t.collection AS col, "
            "COUNT(DISTINCT ta.source_collection) AS src_count "
            "FROM topic_assignments ta "
            "JOIN topics t ON ta.topic_id = t.id "
            "WHERE t.collection IN ({}) "
            "GROUP BY t.collection "
            "ORDER BY src_count DESC".format(
                ",".join("?" * len(cols)) or "''"
            ),
            cols,
        ).fetchall()
        return {row[0]: idx + 1 for idx, row in enumerate(rows)}
    except Exception:
        return {}
    finally:
        t2.close()


def _default_hub_score_fn(col: str) -> float | None:
    """Ratio of *col*'s chunks assigned to top-10 cross-collection hubs.

    ``None`` when the taxonomy tables don't exist yet or the collection
    has zero chunks.
    """
    t2 = _open_t2()
    if t2 is None:
        return None
    try:
        conn = t2.taxonomy.conn
        # Top-10 hubs: topics whose assignments span the most distinct
        # source collections. Ranks deterministically by
        # ``(src_count DESC, topic_id ASC)``.
        hub_rows = conn.execute(
            "SELECT ta.topic_id "
            "FROM topic_assignments ta "
            "GROUP BY ta.topic_id "
            "ORDER BY COUNT(DISTINCT ta.source_collection) DESC, ta.topic_id ASC "
            "LIMIT 10"
        ).fetchall()
        if not hub_rows:
            return None
        hub_ids = tuple(r[0] for r in hub_rows)
        total = conn.execute(
            "SELECT COUNT(*) FROM topic_assignments "
            "WHERE source_collection = ?",
            (col,),
        ).fetchone()[0] or 0
        if total == 0:
            return None
        in_hubs = conn.execute(
            "SELECT COUNT(*) FROM topic_assignments "
            "WHERE source_collection = ? AND topic_id IN ({})".format(
                ",".join("?" * len(hub_ids))
            ),
            (col, *hub_ids),
        ).fetchone()[0] or 0
        return in_hubs / total
    except Exception:
        return None
    finally:
        t2.close()


def _default_chash_coverage_fn(col: str) -> float | None:
    """Ratio of chash_index rows for *col* to its T3 chunk count.

    1.0 means every T3 chunk has a chash_index entry (preferred state
    for nx doc cite / resolve_chash); < 1.0 means nx collection
    backfill-hash has work to do. Returns None when the chash_index
    is empty (fresh install) or the collection has no T3 chunks.

    Introduced in RDR-087 Phase 4.6 (nexus-c2op) after RDR-086 added
    the chash_index surface. Pure SQL composition — no new primitives.
    """
    from nexus.commands._helpers import default_db_path
    from nexus.db import make_t3
    from nexus.db.t2.chash_index import ChashIndex

    db_path = default_db_path()
    if not db_path.exists():
        return None

    idx = ChashIndex(db_path)
    try:
        with idx._lock:
            row = idx.conn.execute(
                "SELECT COUNT(*) FROM chash_index "
                "WHERE physical_collection = ?",
                (col,),
            ).fetchone()
        chash_count = int(row[0] or 0)
    finally:
        idx.close()

    try:
        t3 = make_t3()
        try:
            coll = t3.get_or_create_collection(col)
            chunk_count = coll.count()
        except Exception:
            return None
    except Exception:
        return None

    if chunk_count == 0:
        return None
    return min(1.0, chash_count / chunk_count)


# Module-level runner bindings — tests monkeypatch these directly.
_enumerate_collections = _default_enumerate_collections
_catalog_stats_fn = _default_catalog_stats_fn
_telemetry_stats_fn = _default_telemetry_stats_fn
_projection_rank_fn = _default_projection_rank_fn
_hub_score_fn = _default_hub_score_fn
_chash_coverage_fn = _default_chash_coverage_fn


# ── Orchestrator ────────────────────────────────────────────────────────────


def compute_collection_health(
    collections: list[str],
    *,
    catalog_stats_fn: Callable[[str], dict[str, Any]],
    telemetry_stats_fn: Callable[[str], dict[str, Any]],
    projection_rank_fn: Callable[[list[str]], dict[str, int]],
    hub_score_fn: Callable[[str], float | None],
    chash_coverage_fn: Callable[[str], float | None] | None = None,
) -> list[CollectionHealthRow]:
    """Assemble per-collection health rows from the injected callables.

    Ordering follows *collections*; callers sort via ``format_health_table``.

    ``chash_coverage_fn`` is optional only for backward-compat with the
    pre-nexus-c2op signature; production callers always pass it.
    """
    ranks = projection_rank_fn(collections)
    rows: list[CollectionHealthRow] = []
    for col in collections:
        catalog = catalog_stats_fn(col) or {}
        tel = telemetry_stats_fn(col) or {}
        rows.append(
            CollectionHealthRow(
                name=col,
                chunk_count=int(catalog.get("chunk_count", 0)),
                last_indexed=catalog.get("last_indexed"),
                zero_hit_rate_30d=tel.get("zero_hit_rate"),
                median_query_distance_30d=tel.get("median_top_distance"),
                cross_projection_rank=ranks.get(col),
                orphan_catalog_rows=int(catalog.get("orphan_count", 0))
                    if catalog.get("orphan_count") is not None else None,
                hub_domination_score=hub_score_fn(col),
                chash_indexed_ratio=(
                    chash_coverage_fn(col) if chash_coverage_fn is not None else None
                ),
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
        "chash_indexed_ratio",
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
            r.stale_source_ratio,
            _fmt_cell(r.hub_domination_score),
            _fmt_cell(r.chash_indexed_ratio),
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

    # RDR-087 Phase 4.6 hint: if any collection has a ratio below 1.0,
    # suggest the backfill. Absent ratios (None) don't trigger the hint.
    under_covered = [
        r for r in ordered
        if r.chash_indexed_ratio is not None and r.chash_indexed_ratio < 1.0
    ]
    if under_covered:
        lines.append("")
        lines.append(
            f"note: {len(under_covered)} collection"
            f"{'s' if len(under_covered) != 1 else ''} under-indexed for "
            "chash citations. Run `nx collection backfill-hash --all` "
            "to populate (25-70 min on a 278k-chunk corpus)."
        )
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
        telemetry_stats_fn=_telemetry_stats_fn,
        projection_rank_fn=_projection_rank_fn,
        hub_score_fn=_hub_score_fn,
        chash_coverage_fn=_chash_coverage_fn,
    )
    if fmt == "json":
        return format_health_json(rows)
    return format_health_table(rows, sort_by=sort_by)
