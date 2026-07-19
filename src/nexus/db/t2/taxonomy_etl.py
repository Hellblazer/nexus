# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres taxonomy ETL (bead nexus-gmiaf.14, RDR-152 Phase 2.4).

COPY-NOT-MOVE: reads all rows from the four SQLite taxonomy tables and writes
them through the validated HTTP seam (``HttpTaxonomyStore.import_*``) so
every write flows via Java -> Postgres under RLS with tenant stamping.
The SQLite source is NEVER modified (opened ``?mode=ro``).

IDEMPOTENT: relies on the upsert conflict strategies the Java service enforces:
- ``topics``:           ON CONFLICT (tenant_id, id) DO UPDATE
    - ``doc_count``       — NOT an ON CONFLICT merge participant. RDR-154 P0
      (nexus-i7ivk) made doc_count trigger-maintained (the topic_assignments
      statement-level trigger is the SOLE writer); the INSERT branch seeds it
      for a brand-new topic and the conflict branch leaves the live value
      untouched. Do NOT re-add doc_count to the DO UPDATE clause.
    - ``review_status``   = EXCLUDED.review_status  (mutable annotation)
    - ``centroid_hash``   = EXCLUDED.centroid_hash  (mutable annotation)
    - ``terms``           = EXCLUDED.terms          (mutable annotation)
    - ``label``           = topics.label            (non-overwritable on conflict)
    - ``created_at``      = topics.created_at       (non-overwritable on conflict)
- ``topic_assignments``: ON CONFLICT (tenant_id, doc_id, topic_id) DO UPDATE
    - ``similarity`` = GREATEST(excluded.similarity, assignments.similarity)
    - ``assigned_by`` = 'projection' if incoming is 'projection', else existing (never downgrades)
    - ``assigned_at``, ``source_collection`` = EXCLUDED.*
- ``topic_links``:     ON CONFLICT (tenant_id, from_topic_id, to_topic_id)
    DO UPDATE link_count = GREATEST(EXCLUDED.link_count, topic_links.link_count)
- ``taxonomy_meta``:   ON CONFLICT (tenant_id, collection) DO UPDATE
    - ``last_discover_doc_count`` = GREATEST(EXCLUDED.*, meta.*)
    - ``last_discover_at``        = EXCLUDED.* (more recent is better)

FIDELITY-PRESERVING:
- Monotonic counters (link_count, last_discover_doc_count) use GREATEST so
  re-runs preserve the high-water mark. (doc_count is excluded — it is
  trigger-maintained, not ETL-merged; see the topics note above.)
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
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import structlog

from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker

#: SQLite read-page size (RDR-176 / nexus-lbolo). assignment/link/meta are read
#: in LIMIT/OFFSET pages so the 190k-row topic_assignments table never
#: materializes whole. topics stays whole-load (parent-before-child topo-sort;
#: bounded count).
_READ_PAGE: int = 1000

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


def _open_taxonomy_ro(source_db_path: Path) -> sqlite3.Connection:
    """Open the SQLite taxonomy source read-only. Copy-not-move guarantee."""
    uri = f"file:{source_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite source for reading: {source_db_path}: {exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }


def _load_topic_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Whole-load + topo-sort of the topics table (parent-before-child).

    This is an O(N_topics) residual (see the historical note in
    :func:`migrate_topics`'s caller); topics is small relative to
    topic_assignments so whole-loading it (even from a caller that does not
    itself migrate topics, e.g. :func:`migrate_topic_assignments`'s orphan
    filter) is not the memory concern the streaming reads elsewhere target.
    """
    if "topics" not in _table_names(conn):
        return []
    avail = _available_columns(conn, "topics")
    sel = ", ".join(c for c in _TOPIC_COLUMNS if c in avail)
    topic_rows = [
        dict(r) for r in conn.execute(f"SELECT {sel} FROM topics ORDER BY id ASC").fetchall()
    ]
    # Root nodes (parent_id IS NULL) first, then children. Stable sort
    # handles shallow SQLite hierarchies; parent.id may exceed child.id.
    topic_rows.sort(key=lambda r: (0 if r.get("parent_id") is None else 1, r.get("id", 0)))
    return topic_rows


def _valid_topic_ids(topic_rows: list[dict[str, Any]]) -> set[int]:
    return {int(r["id"]) for r in topic_rows if r.get("id") is not None}


def _available_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _count_table(conn: sqlite3.Connection, table: str) -> int:
    """Row count for *table* (cheap COUNT, for the progress total)."""
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _iter_table(
    conn: sqlite3.Connection,
    table: str,
    cols_tuple: tuple[str, ...],
    page_size: int,
) -> Iterator[dict[str, Any]]:
    """Yield *table*'s rows (projected to the available *cols_tuple*) in
    LIMIT/OFFSET pages (RDR-176 / nexus-lbolo). Stops on the first short page."""
    avail = _available_columns(conn, table)
    cols = [c for c in cols_tuple if c in avail]
    if not cols:
        return
    sel = ", ".join(cols)
    offset = 0
    while True:
        page = conn.execute(
            f"SELECT {sel} FROM {table} ORDER BY ROWID ASC "
            f"LIMIT {page_size} OFFSET {offset}"
        ).fetchall()
        if not page:
            break
        for r in page:
            yield dict(r)
        if len(page) < page_size:
            break
        offset += page_size


# ── Per-table migration functions (RDR-180 nexus-jxizy.10.7 split) ─────────────
#
# Each function below migrates exactly ONE SQLite taxonomy table and opens its
# own read-only connection (copy-not-move; mirrors aspects_etl.py's per-table
# shape). topic_assignments and topic_links both need the SOURCE topics
# loaded (read-only) to orphan-filter against — topics are NOT written by
# either of them; only migrate_topics() writes the topics table.
#
# topic_assignments is the CHASH-BEARING table (doc_id is a chash, RDR-180):
# the guided land-then-transform path lands + promotes it server-side, so
# migrate_taxonomy_without_assignments() composes the other three and
# deliberately excludes it.


def migrate_topics(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
    collector: Any = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY the topics table.

    Whole-load (parent_id self-FK needs a parent-before-child topo-sort over
    the full set). See :func:`_load_topic_rows` for the O(N_topics) memory
    note.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_taxonomy_ro(source_db_path)
    try:
        topic_rows = _load_topic_rows(conn)
        return _migrate_table(
            rows=topic_rows, transform_fn=_transform_topic, store=store,
            kind="topic", table="topics", batch_log_every=batch_log_every,
            collector=collector, total=len(topic_rows), breaker=breaker,
        )
    finally:
        conn.close()


def migrate_topic_assignments(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
    collector: Any = None,
    read_page: int = _READ_PAGE,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY topic_assignments — the CHASH-BEARING table (doc_id is a
    chash, RDR-180). Streams page-by-page (nexus-lbolo) so a 190k-row table
    never materializes whole; orphan rows (topic_id referencing a deleted
    topic) are skipped-and-recorded via *collector*, per RDR-153 §Decision 2.

    Topics are loaded (read-only) ONLY to build the orphan-filter's valid-id
    set — they are NOT written here; see :func:`migrate_topics`.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_taxonomy_ro(source_db_path)
    try:
        tables = _table_names(conn)
        topic_rows = _load_topic_rows(conn) if "topics" in tables else []
        valid_topic_ids = _valid_topic_ids(topic_rows)
        assign_total = _count_table(conn, "topic_assignments") if "topic_assignments" in tables else 0

        # ── RDR-153 §Decision 2 orphan policy, STREAMED page-by-page
        # (nexus-lbolo) so a 190k assignments table never materializes whole.
        #
        # Failure-path note: orphans are recorded DURING the stream, so if
        # _migrate_table raises mid-table (e.g. the store is down and
        # _etl_with_retry exhausts its bound), orphans on not-yet-read pages
        # are NOT recorded and the post-call count_read is skipped — the
        # orphan report for that run is partial. That is acceptable: such a
        # run fails the total_failed==0 gate (it is not "done"), and the
        # idempotent re-run re-reads from the top and records every orphan
        # once the store is back. ──
        orphans = {"assignments": 0}

        def _valid_assignments() -> Iterator[dict[str, Any]]:
            for r in _iter_table(conn, "topic_assignments", _ASSIGNMENT_COLUMNS, read_page):
                if int(r["topic_id"]) not in valid_topic_ids:
                    orphans["assignments"] += 1
                    if collector is not None:
                        collector.record(
                            "taxonomy", "topic_assignments",
                            issue_class="orphan_parent",
                            constraint="topic_assignments.topic_id -> topics.id",
                            reason="topic_id references a deleted topic; sample ids are "
                                   "<doc_id>:<topic_id>",
                            action="skipped",
                            sample_id=f"{r.get('doc_id')}:{r.get('topic_id')}",
                        )
                    continue
                yield r

        # identity_mismatch is a once-per-run DECISION record (doc_id is a chash,
        # not a tumbler — FK deliberately not registered, nexus-sa14p), not a
        # per-row anomaly. Fire it once if the source has any assignment rows.
        if collector is not None and assign_total:
            collector.record_event(
                "taxonomy", "topic_assignments",
                issue_class="identity_mismatch",
                constraint="topic_assignments.doc_id",
                reason="doc_id is a chash (opaque identity), not a catalog "
                       "tumbler — FK deliberately not registered "
                       "(schema corrected, nexus-sa14p)",
                action="schema_corrected",
            )

        result = _migrate_table(
            rows=_valid_assignments(), transform_fn=_transform_assignment,
            store=store, kind="assignment", table="topic_assignments",
            batch_log_every=batch_log_every, collector=collector, total=assign_total,
            breaker=breaker,
        )
        # The orphan count is final once _migrate_table has drained the filter
        # generator. Record it into the collector so the reported read ==
        # source cardinality (valid + orphan); count_read accumulates.
        if collector is not None and orphans["assignments"]:
            collector.count_read("taxonomy", "topic_assignments", orphans["assignments"])
        elif collector is None and orphans["assignments"]:
            _log.warning(
                "taxonomy_etl.orphans_skipped_unrecorded",
                table="topic_assignments", count=orphans["assignments"],
                hint="pass collector= to record these in the migration report",
            )
        return result
    finally:
        conn.close()


def migrate_topic_links(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
    collector: Any = None,
    read_page: int = _READ_PAGE,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY topic_links. Topics are loaded (read-only) ONLY to build
    the orphan-filter's valid-id set — they are NOT written here; see
    :func:`migrate_topics`."""
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_taxonomy_ro(source_db_path)
    try:
        tables = _table_names(conn)
        topic_rows = _load_topic_rows(conn) if "topics" in tables else []
        valid_topic_ids = _valid_topic_ids(topic_rows)
        link_total = _count_table(conn, "topic_links") if "topic_links" in tables else 0
        orphans = {"links": 0}

        def _valid_links() -> Iterator[dict[str, Any]]:
            for r in _iter_table(conn, "topic_links", _LINK_COLUMNS, read_page):
                if (int(r["from_topic_id"]) not in valid_topic_ids
                        or int(r["to_topic_id"]) not in valid_topic_ids):
                    orphans["links"] += 1
                    if collector is not None:
                        collector.record(
                            "taxonomy", "topic_links",
                            issue_class="orphan_parent",
                            constraint="topic_links.(from|to)_topic_id -> topics.id",
                            reason="from/to topic_id references a deleted topic; sample "
                                   "ids are <from_topic_id>:<to_topic_id>",
                            action="skipped",
                            sample_id=f"{r.get('from_topic_id')}:{r.get('to_topic_id')}",
                        )
                    continue
                yield r

        result = _migrate_table(
            rows=_valid_links(), transform_fn=_transform_link, store=store,
            kind="link", table="topic_links", batch_log_every=batch_log_every,
            collector=collector, total=link_total, breaker=breaker,
        )
        if collector is not None and orphans["links"]:
            collector.count_read("taxonomy", "topic_links", orphans["links"])
        elif collector is None and orphans["links"]:
            _log.warning(
                "taxonomy_etl.orphans_skipped_unrecorded",
                table="topic_links", count=orphans["links"],
                hint="pass collector= to record these in the migration report",
            )
        return result
    finally:
        conn.close()


def migrate_taxonomy_meta(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
    collector: Any = None,
    read_page: int = _READ_PAGE,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate ONLY taxonomy_meta (no parent FK to check)."""
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_taxonomy_ro(source_db_path)
    try:
        meta_total = _count_table(conn, "taxonomy_meta") if "taxonomy_meta" in _table_names(conn) else 0
        return _migrate_table(
            rows=_iter_table(conn, "taxonomy_meta", _META_COLUMNS, read_page),
            transform_fn=_transform_meta, store=store, kind="meta",
            table="taxonomy_meta", batch_log_every=batch_log_every,
            collector=collector, total=meta_total, breaker=breaker,
        )
    finally:
        conn.close()


# ── Composition entry points ────────────────────────────────────────────────


def migrate_taxonomy_without_assignments(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
    collector: Any = None,
    read_page: int = _READ_PAGE,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, Any]:
    """topics + topic_links + taxonomy_meta — NOT topic_assignments.

    RDR-180 (nexus-jxizy.10.7): topic_assignments carries chash identity
    (doc_id) and is landed + promoted server-side by the guided
    land-then-transform path; running it here too would double-write a
    stale, unresolved legacy-id copy alongside the resolved one. The guided
    ``taxonomy`` store slot calls this instead of :func:`migrate_taxonomy_rows`.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    return {
        "topics": migrate_topics(
            source_db_path, store, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
        "links": migrate_topic_links(
            source_db_path, store, batch_log_every=batch_log_every,
            collector=collector, read_page=read_page, breaker=breaker,
        ),
        "meta": migrate_taxonomy_meta(
            source_db_path, store, batch_log_every=batch_log_every,
            collector=collector, read_page=read_page, breaker=breaker,
        ),
    }


def migrate_taxonomy_rows(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
    collector: Any = None,
    read_page: int = _READ_PAGE,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, Any]:
    """Copy all rows from the four SQLite taxonomy tables into Postgres via *store*.

    Thin composition of the four per-table entry points (RDR-180
    nexus-jxizy.10.7 split): topics first (assignments/links reference
    topic.id by value; the Java service preserves ids via BIGSERIAL explicit
    insert), then assignments, then links, then meta — the exact order the
    prior monolithic implementation used, so batch call ordering and
    collector accounting are unchanged for existing callers.

    Args:
        source_db_path: Path to the SQLite T2 database file.
        store: An ``HttpTaxonomyStore`` (or duck-typed compatible) instance
               connected to the Postgres service.
        batch_log_every: Emit a progress log line every N rows.

    Returns:
        ``{"topics": {"read": N, "written": M}, "assignments": ..., "links": ..., "meta": ...}``

    Copy-not-move guarantee: each per-table function opens the SQLite source
    in ``?mode=ro``.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    results: dict[str, Any] = {
        "topics": migrate_topics(
            source_db_path, store, batch_log_every=batch_log_every,
            collector=collector, breaker=breaker,
        ),
    }
    results["assignments"] = migrate_topic_assignments(
        source_db_path, store, batch_log_every=batch_log_every,
        collector=collector, read_page=read_page, breaker=breaker,
    )
    results["links"] = migrate_topic_links(
        source_db_path, store, batch_log_every=batch_log_every,
        collector=collector, read_page=read_page, breaker=breaker,
    )
    results["meta"] = migrate_taxonomy_meta(
        source_db_path, store, batch_log_every=batch_log_every,
        collector=collector, read_page=read_page, breaker=breaker,
    )

    _log.info(
        "taxonomy_etl.complete",
        topics_read=results["topics"]["read"], topics_written=results["topics"]["written"],
        assignments_read=results["assignments"]["read"], assignments_written=results["assignments"]["written"],
        links_read=results["links"]["read"], links_written=results["links"]["written"],
        meta_read=results["meta"]["read"], meta_written=results["meta"]["written"],
    )

    return results


def _migrate_table(
    *,
    rows: Iterable[dict[str, Any]],
    transform_fn: Any,
    store: Any,
    kind: str,
    table: str,
    batch_log_every: int,
    collector: Any = None,
    total: int = 0,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Generic per-table migration loop. Returns ``{"read": N, "written": M, "skipped": K}``.

    RDR-176 P3 (Gap 1, bead nexus-t9rmg.18): batches the HTTP transport via
    ``store.import_rows_batch(kind, batch)`` so the transfer count is
    ceil(N/quota), not N — the fix for the per-row topic_assignments leg (190k
    rows = 190k requests). Each row is still TRANSFORMED per-row (a corrupt row
    is recorded individually + excluded); only the NETWORK is batched. A
    server-side batch rejection is recorded at batch granularity; the import is
    idempotent (ON CONFLICT DO UPDATE) so a re-run lands it. ``skipped`` is 0
    (no current taxonomy import returns a skip now that fk_ta_catalog_doc is gone,
    nexus-sa14p).
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — branch-local; quota constant
    bsize = QUOTAS.MAX_RECORDS_PER_WRITE

    read_count = 0
    written_count = 0
    batch: list[dict[str, Any]] = []
    keys: list[str] = []

    def _flush() -> None:
        nonlocal written_count, batch, keys
        if not batch:
            return
        try:
            written_count += _etl_batch_with_breaker(store.import_rows_batch, kind, batch, breaker=breaker)
        except Exception as exc:  # noqa: BLE001 — batch failure logged + recorded; migration continues (idempotent re-run)
            _log.error("taxonomy_etl.batch_failed", table=table, count=len(batch), error=str(exc))
            if collector is not None:
                for key in keys:
                    collector.record(
                        "taxonomy", table,
                        issue_class="unexpected", constraint=table,
                        reason=f"batch import rejected: {exc}",
                        action="failed", sample_id=key,
                    )
        batch = []
        keys = []

    for row_dict in rows:
        read_count += 1
        try:
            transformed = transform_fn(row_dict)
        except Exception as exc:  # noqa: BLE001 — per-row transform failure logged + recorded; migration continues
            _log.error(
                "taxonomy_etl.row_failed",
                table=table, row_preview=str(row_dict)[:120], error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "taxonomy", table,
                    issue_class="unexpected", constraint=table,
                    reason=f"row rejected during transform: {exc}",
                    action="failed", sample_id=f"{table}#{read_count}",
                )
            continue

        batch.append(transformed)
        keys.append(f"{table}#{read_count}")
        if len(batch) >= bsize:
            _flush()

        if read_count % batch_log_every == 0:
            _log.info(
                "taxonomy_etl.progress",
                table=table, read=read_count, written=written_count, total=total,
            )

    _flush()

    if collector is not None:
        # Records only the rows this call saw (valid rows the orphan-filter
        # generator yielded). Orphans are skipped+recorded inside the filter and
        # added to the collector's read via a SEPARATE count_read call by the
        # caller AFTER this returns (count_read accumulates), so the reported
        # read == source cardinality. ``total`` (a COUNT(*)) is cosmetic, for the
        # progress log; when orphans exist read_count tops out below total.
        collector.count_read("taxonomy", table, read_count)
        collector.count_written("taxonomy", table, written_count)

    return {"read": read_count, "written": written_count, "skipped": 0}
