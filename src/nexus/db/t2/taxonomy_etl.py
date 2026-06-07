# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres taxonomy ETL (bead nexus-gmiaf.14, RDR-152 Phase 2.4).

COPY-NOT-MOVE: reads all rows from the four SQLite taxonomy tables and writes
them through the validated HTTP seam (``HttpTaxonomyStore.import_*``) so
every write flows via Java -> Postgres under RLS with tenant stamping.
The SQLite source is NEVER modified (opened ``?mode=ro``).

IDEMPOTENT: relies on the upsert conflict strategies the Java service enforces:
- ``topics``:           ON CONFLICT (tenant_id, id) DO UPDATE
    - ``doc_count``       = GREATEST(excluded.doc_count, topics.doc_count)
    - ``review_status``   = EXCLUDED.review_status  (mutable annotation)
    - ``centroid_hash``   = EXCLUDED.centroid_hash  (mutable annotation)
    - ``terms``           = EXCLUDED.terms          (mutable annotation)
    - ``label``           = topics.label            (non-overwritable on conflict)
    - ``created_at``      = topics.created_at       (non-overwritable on conflict)
- ``topic_assignments``: ON CONFLICT (tenant_id, doc_id, topic_id) DO UPDATE
    - ``similarity`` = GREATEST(excluded.similarity, assignments.similarity)
    - ``assigned_by``, ``assigned_at``, ``source_collection`` = EXCLUDED.*
- ``topic_links``:     ON CONFLICT (tenant_id, from_topic_id, to_topic_id)
    DO UPDATE link_count = GREATEST(EXCLUDED.link_count, topic_links.link_count)
- ``taxonomy_meta``:   ON CONFLICT (tenant_id, collection) DO UPDATE
    - ``last_discover_doc_count`` = GREATEST(EXCLUDED.*, meta.*)
    - ``last_discover_at``        = EXCLUDED.* (more recent is better)

FIDELITY-PRESERVING:
- Monotonic counters (doc_count, link_count, last_discover_doc_count) use
  GREATEST so re-runs preserve the high-water mark.
- Timestamps and labels are NOT overwritten on conflict — original creation
  state survives re-runs.
- review_status, centroid_hash, terms ARE overwritten on conflict so
  annotation changes replicate correctly.

CHROMA INTERACTION NOTE:
- This ETL handles only the four *relational* taxonomy tables.
- The ``taxonomy__centroids`` ChromaDB collection is NOT migrated here.
  Centroid vectors live in Chroma (Python-side); migrating them is out of
  scope for this bead (Seam B, Phase 3).

FIELD MAPPING (SQLite columns -> HttpTaxonomyStore.import_*() kwargs):

topics:
  id              -> src_id       (BIGSERIAL allows explicit write; no OVERRIDING SYSTEM VALUE)
  label           -> label
  parent_id       -> parent_id    (nullable INTEGER)
  collection      -> collection
  centroid_hash   -> centroid_hash (nullable TEXT)
  doc_count       -> doc_count
  created_at      -> created_at   (ISO-8601 string verbatim)
  review_status   -> review_status
  terms           -> terms        (nullable JSON TEXT)

topic_assignments:
  doc_id          -> doc_id
  topic_id        -> topic_id
  assigned_by     -> assigned_by
  similarity      -> similarity   (nullable REAL)
  assigned_at     -> assigned_at  (nullable TEXT)
  source_collection -> source_collection (nullable TEXT)

topic_links:
  from_topic_id   -> from_topic_id
  to_topic_id     -> to_topic_id
  link_count      -> link_count
  link_types      -> link_types   (JSON TEXT, default '[]')

taxonomy_meta:
  collection      -> collection
  last_discover_doc_count -> last_discover_doc_count
  last_discover_at        -> last_discover_at (nullable TEXT)
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# ── Column lists (match SQLite schema + migrations.py column additions) ────────

_TOPIC_COLUMNS = (
    "id",
    "label",
    "parent_id",
    "collection",
    "centroid_hash",
    "doc_count",
    "created_at",
    "review_status",
    "terms",
)

_ASSIGNMENT_COLUMNS = (
    "doc_id",
    "topic_id",
    "assigned_by",
    "similarity",
    "assigned_at",
    "source_collection",
)

_LINK_COLUMNS = (
    "from_topic_id",
    "to_topic_id",
    "link_count",
    "link_types",
)

_META_COLUMNS = (
    "collection",
    "last_discover_doc_count",
    "last_discover_at",
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _nullable_ts(v: Any) -> str | None:
    """Return None for empty/None, otherwise the string verbatim."""
    if v is None or v == "":
        return None
    return str(v)


def _int_or_zero(v: Any) -> int:
    return int(v) if v is not None else 0


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    return float(v)


# ── Transform functions ────────────────────────────────────────────────────────


def _transform_topic(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite topics row to HttpTaxonomyStore.import_topic() kwargs.

    Fails loud if required fields (id, label, collection, created_at) are
    absent — no silent fallbacks for correctness (RDR-152 constraint).
    """
    if row.get("id") is None:
        raise ValueError(f"topics row missing id: {row!r}")
    if not row.get("label"):
        raise ValueError(f"topics row {row['id']} missing label")
    if not row.get("collection"):
        raise ValueError(f"topics row {row['id']} missing collection")
    if not row.get("created_at"):
        raise ValueError(f"topics row {row['id']} missing created_at")

    return {
        "src_id":        int(row["id"]),
        "label":         str(row["label"]),
        "parent_id":     int(row["parent_id"]) if row.get("parent_id") is not None else None,
        "collection":    str(row["collection"]),
        "centroid_hash": row.get("centroid_hash"),
        "doc_count":     _int_or_zero(row.get("doc_count")),
        "created_at":    str(row["created_at"]),
        "review_status": row.get("review_status") or "pending",
        "terms":         row.get("terms"),
    }


def _transform_assignment(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite topic_assignments row to HttpTaxonomyStore.import_assignment() kwargs."""
    return {
        "doc_id":            str(row["doc_id"]),
        "topic_id":          int(row["topic_id"]),
        "assigned_by":       row.get("assigned_by") or "hdbscan",
        "similarity":        _float_or_none(row.get("similarity")),
        "assigned_at":       _nullable_ts(row.get("assigned_at")),
        "source_collection": row.get("source_collection"),
    }


def _transform_link(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite topic_links row to HttpTaxonomyStore.import_topic_link() kwargs."""
    return {
        "from_topic_id": int(row["from_topic_id"]),
        "to_topic_id":   int(row["to_topic_id"]),
        "link_count":    _int_or_zero(row.get("link_count")),
        "link_types":    row.get("link_types") or "[]",
    }


def _transform_meta(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite taxonomy_meta row to HttpTaxonomyStore.import_taxonomy_meta() kwargs."""
    return {
        "collection":               str(row["collection"]),
        "last_discover_doc_count":  _int_or_zero(row.get("last_discover_doc_count")),
        "last_discover_at":         _nullable_ts(row.get("last_discover_at")),
    }


# ── Source count ───────────────────────────────────────────────────────────────


def count_source_rows(source_db_path: Path) -> dict[str, int]:
    """Return row counts per table from the SQLite source (read-only).

    Returns a dict: ``{"topics": N, "assignments": M, "links": L, "meta": K}``.
    Used by the ``--dry-run`` CLI path.

    Opens the source in ``uri=True mode=ro`` — never modifies the source.
    """
    uri = f"file:{source_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite source for reading: {source_db_path}: {exc}"
        ) from exc

    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts: dict[str, int] = {}
        for tbl, key in [
            ("topics", "topics"),
            ("topic_assignments", "assignments"),
            ("topic_links", "links"),
            ("taxonomy_meta", "meta"),
        ]:
            if tbl in tables:
                counts[key] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
            else:
                counts[key] = 0
        return counts
    finally:
        conn.close()


def _available_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ── Main migration function ────────────────────────────────────────────────────


def migrate_taxonomy_rows(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
) -> dict[str, Any]:
    """Copy all rows from the four SQLite taxonomy tables into Postgres via *store*.

    Migration order: topics first (assignments/links reference topic.id by
    value; the Java service preserves ids via BIGSERIAL explicit insert),
    then assignments, then links, then meta.

    Args:
        source_db_path: Path to the SQLite T2 database file.
        store: An ``HttpTaxonomyStore`` (or duck-typed compatible) instance
               connected to the Postgres service.
        batch_log_every: Emit a progress log line every N rows.

    Returns:
        ``{"topics": {"read": N, "written": M}, "assignments": ..., "links": ..., "meta": ...}``

    Copy-not-move guarantee: opens the SQLite source in ``?mode=ro``.
    """
    uri = f"file:{source_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite source for reading: {source_db_path}: {exc}"
        ) from exc

    try:
        conn.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        # Load all tables while the connection is open.
        topic_rows: list[dict[str, Any]] = []
        assign_rows: list[dict[str, Any]] = []
        link_rows: list[dict[str, Any]] = []
        meta_rows: list[dict[str, Any]] = []

        if "topics" in tables:
            avail = _available_columns(conn, "topics")
            sel = ", ".join(c for c in _TOPIC_COLUMNS if c in avail)
            topic_rows = [dict(r) for r in conn.execute(
                f"SELECT {sel} FROM topics ORDER BY id ASC"
            ).fetchall()]

        if "topic_assignments" in tables:
            avail = _available_columns(conn, "topic_assignments")
            sel = ", ".join(c for c in _ASSIGNMENT_COLUMNS if c in avail)
            assign_rows = [dict(r) for r in conn.execute(
                f"SELECT {sel} FROM topic_assignments"
            ).fetchall()]

        if "topic_links" in tables:
            avail = _available_columns(conn, "topic_links")
            sel = ", ".join(c for c in _LINK_COLUMNS if c in avail)
            link_rows = [dict(r) for r in conn.execute(
                f"SELECT {sel} FROM topic_links"
            ).fetchall()]

        if "taxonomy_meta" in tables:
            avail = _available_columns(conn, "taxonomy_meta")
            sel = ", ".join(c for c in _META_COLUMNS if c in avail)
            meta_rows = [dict(r) for r in conn.execute(
                f"SELECT {sel} FROM taxonomy_meta"
            ).fetchall()]
    finally:
        conn.close()

    _log.info(
        "taxonomy_etl.start",
        source=str(source_db_path),
        topics=len(topic_rows),
        assignments=len(assign_rows),
        links=len(link_rows),
        meta=len(meta_rows),
    )

    results: dict[str, Any] = {}

    # 1. Topics — must go first; assignments/links reference topic.id
    results["topics"] = _migrate_table(
        rows=topic_rows,
        transform_fn=_transform_topic,
        import_fn=store.import_topic,
        table="topics",
        batch_log_every=batch_log_every,
    )

    # 2. Assignments
    results["assignments"] = _migrate_table(
        rows=assign_rows,
        transform_fn=_transform_assignment,
        import_fn=store.import_assignment,
        table="topic_assignments",
        batch_log_every=batch_log_every,
    )

    # 3. Links
    results["links"] = _migrate_table(
        rows=link_rows,
        transform_fn=_transform_link,
        import_fn=store.import_topic_link,
        table="topic_links",
        batch_log_every=batch_log_every,
    )

    # 4. Meta
    results["meta"] = _migrate_table(
        rows=meta_rows,
        transform_fn=_transform_meta,
        import_fn=store.import_taxonomy_meta,
        table="taxonomy_meta",
        batch_log_every=batch_log_every,
    )

    _log.info(
        "taxonomy_etl.complete",
        topics_read=results["topics"]["read"],
        topics_written=results["topics"]["written"],
        assignments_read=results["assignments"]["read"],
        assignments_written=results["assignments"]["written"],
        links_read=results["links"]["read"],
        links_written=results["links"]["written"],
        meta_read=results["meta"]["read"],
        meta_written=results["meta"]["written"],
    )

    return results


def _migrate_table(
    *,
    rows: list[dict[str, Any]],
    transform_fn: Any,
    import_fn: Any,
    table: str,
    batch_log_every: int,
) -> dict[str, int]:
    """Generic per-table migration loop. Returns ``{"read": N, "written": M}``."""
    read_count = 0
    written_count = 0
    total = len(rows)

    for row_dict in rows:
        read_count += 1
        try:
            transformed = transform_fn(row_dict)
            import_fn(**transformed)
            written_count += 1
        except Exception as exc:
            _log.error(
                "taxonomy_etl.row_failed",
                table=table,
                row_preview=str(row_dict)[:120],
                error=str(exc),
            )
            # Continue processing remaining rows — a single failure does not
            # abort the migration.

        if read_count % batch_log_every == 0:
            _log.info(
                "taxonomy_etl.progress",
                table=table,
                read=read_count,
                written=written_count,
                total=total,
            )

    return {"read": read_count, "written": written_count}
