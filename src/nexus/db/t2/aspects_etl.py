# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres aspects ETL (bead nexus-gmiaf.15, RDR-152 Phase 2.5).

COPY-NOT-MOVE: reads all rows from the four SQLite aspects tables and writes
them through the validated HTTP seam so every write flows via Java -> Postgres
under RLS with tenant stamping. The SQLite source is NEVER modified.

IDEMPOTENT: relies on the upsert conflict strategies the Java service enforces:

- ``document_aspects``:
    ON CONFLICT (tenant_id, collection, source_path) DO UPDATE
    - extracted_at = EXCLUDED.extracted_at  (most-recent wins)
    - confidence   = GREATEST(EXCLUDED.confidence, document_aspects.confidence)
    - all other fields = EXCLUDED.*

- ``document_highlights``:
    ON CONFLICT (tenant_id, doc_id) DO UPDATE
    - ingested_at = EXCLUDED.ingested_at  (most-recent wins)
    - highlights_md, mentions_md = EXCLUDED.*

- ``aspect_extraction_queue``:
    ON CONFLICT (tenant_id, collection, source_path) DO UPDATE
    - status:        CASE WHEN existing = 'in_progress' THEN existing ELSE EXCLUDED.status
    - retry_count:   GREATEST(EXCLUDED.retry_count, queue.retry_count)
    - enqueued_at:   LEAST(EXCLUDED.enqueued_at, queue.enqueued_at)   -- preserve oldest
    - last_attempt_at: EXCLUDED.last_attempt_at (more recent is better)
    - last_error:    EXCLUDED.last_error

- ``aspect_promotion_log``:
    ON CONFLICT (tenant_id, field_name, promoted_at) DO NOTHING  -- event log, no overwrite

FIDELITY-PRESERVING:
- confidence uses GREATEST (monotonic high-water mark).
- enqueued_at uses LEAST (preserve oldest for FIFO ordering).
- status never downgrades in_progress to a lesser state.
- timestamps never downgrade (extracted_at most-recent wins, enqueued_at oldest wins).
- event-log table (aspect_promotion_log) uses DO NOTHING.

FIELD MAPPING:

document_aspects (SQLite) -> importAspect body:
  collection, source_path, problem_formulation, proposed_method,
  experimental_datasets, experimental_baselines, experimental_results,
  extras, confidence, extracted_at, model_version, extractor_name,
  source_uri, doc_id, salient_sentences

document_highlights (SQLite) -> importHighlight body:
  doc_id, source_uri, collection, highlights_md, mentions_md, ingested_at

aspect_extraction_queue (SQLite) -> importQueueRow body:
  collection, source_path, doc_id, content_hash, content,
  status, retry_count, enqueued_at, last_attempt_at, last_error

aspect_promotion_log (SQLite) -> importPromotionRow body:
  field_name, sql_type, column_added, rows_backfilled, rows_pruned,
  pruned, promoted_at
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog

from nexus.db.chroma_quotas import QUOTAS
from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker

_log = structlog.get_logger(__name__)

#: HTTP batch / SQLite page size — the service write quota (300). Decoupling is
#: unnecessary here because the page read equals the batch flush; both stay at
#: the cap so transfer is exactly ceil(N / MAX_RECORDS_PER_WRITE) (RDR-176 P3,
#: review H-1: previously 200, which under-filled batches by 33%).
_BATCH: int = QUOTAS.MAX_RECORDS_PER_WRITE


# ── Helpers ────────────────────────────────────────────────────────────────────


def _nullable_ts(v: Any) -> str | None:
    """Return None for empty/None, otherwise the verbatim string."""
    if v is None or v == "":
        return None
    return str(v)


def _str(v: Any) -> str:
    return str(v) if v is not None else ""


def _int(v: Any) -> int:
    return int(v) if v is not None else 0


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    return float(v)


# ── Transform functions ────────────────────────────────────────────────────────


def _transform_aspect(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite document_aspects row to the importAspect body.

    Fails loud on missing required fields (no silent fallbacks).
    """
    if not row.get("collection"):
        raise ValueError(f"document_aspects row missing collection: {row!r}")
    if not row.get("extracted_at"):
        raise ValueError(
            f"document_aspects row ({row.get('collection')}, "
            f"{row.get('source_path')}) missing extracted_at"
        )
    if not row.get("model_version"):
        raise ValueError(
            f"document_aspects row ({row.get('collection')}, "
            f"{row.get('source_path')}) missing model_version"
        )
    if not row.get("extractor_name"):
        raise ValueError(
            f"document_aspects row ({row.get('collection')}, "
            f"{row.get('source_path')}) missing extractor_name"
        )
    return {
        "collection":              _str(row["collection"]),
        "source_path":             _str(row.get("source_path")),
        "problem_formulation":     row.get("problem_formulation"),
        "proposed_method":         row.get("proposed_method"),
        "experimental_datasets":   _str(row.get("experimental_datasets") or "[]"),
        "experimental_baselines":  _str(row.get("experimental_baselines") or "[]"),
        "experimental_results":    row.get("experimental_results"),
        "extras":                  _str(row.get("extras") or "{}"),
        "confidence":              _float_or_none(row.get("confidence")),
        "extracted_at":            _str(row["extracted_at"]),
        "model_version":           _str(row["model_version"]),
        "extractor_name":          _str(row["extractor_name"]),
        "source_uri":              row.get("source_uri"),
        "doc_id":                  _str(row.get("doc_id") or ""),
        "salient_sentences":       _str(row.get("salient_sentences") or "[]"),
    }


def _transform_highlight(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite document_highlights row to the importHighlight body."""
    if not row.get("doc_id"):
        raise ValueError(f"document_highlights row missing doc_id: {row!r}")
    if not row.get("ingested_at"):
        raise ValueError(f"document_highlights row {row['doc_id']} missing ingested_at")
    return {
        "doc_id":        _str(row["doc_id"]),
        "source_uri":    row.get("source_uri") or "",
        "collection":    row.get("collection") or "",
        "highlights_md": row.get("highlights_md") or "",
        "mentions_md":   row.get("mentions_md") or "",
        "ingested_at":   _str(row["ingested_at"]),
    }


def _transform_queue_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite aspect_extraction_queue row to the importQueueRow body."""
    if not row.get("collection"):
        raise ValueError(f"aspect_extraction_queue row missing collection: {row!r}")
    if not row.get("source_path"):
        raise ValueError(f"aspect_extraction_queue row missing source_path: {row!r}")
    if not row.get("enqueued_at"):
        raise ValueError(
            f"aspect_extraction_queue row ({row['collection']}, "
            f"{row['source_path']}) missing enqueued_at"
        )
    return {
        "collection":      _str(row["collection"]),
        "source_path":     _str(row["source_path"]),
        "doc_id":          _str(row.get("doc_id") or ""),
        "content_hash":    _str(row.get("content_hash") or ""),
        "content":         _str(row.get("content") or ""),
        "status":          _str(row.get("status") or "pending"),
        "retry_count":     _int(row.get("retry_count")),
        "enqueued_at":     _str(row["enqueued_at"]),
        "last_attempt_at": _nullable_ts(row.get("last_attempt_at")),
        "last_error":      row.get("last_error"),
    }


def _transform_promotion_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite aspect_promotion_log row to the importPromotionRow body."""
    if not row.get("field_name"):
        raise ValueError(f"aspect_promotion_log row missing field_name: {row!r}")
    if not row.get("promoted_at"):
        raise ValueError(f"aspect_promotion_log row {row['field_name']} missing promoted_at")
    return {
        "field_name":      _str(row["field_name"]),
        "sql_type":        _str(row.get("sql_type") or "TEXT"),
        "column_added":    bool(row.get("column_added")),
        "rows_backfilled": _int(row.get("rows_backfilled")),
        "rows_pruned":     _int(row.get("rows_pruned")),
        "pruned":          bool(row.get("pruned")),
        "promoted_at":     _str(row["promoted_at"]),
    }


# ── Migration functions ────────────────────────────────────────────────────────


def _load_live_tumblers(catalog_db_path: Path) -> set[str]:
    """Document tumblers from the SQLite catalog (the valid-doc_id source
    for the RDR-153 orphan pre-check). Read-only open."""
    conn = sqlite3.connect(f"file:{catalog_db_path}?mode=ro", uri=True)
    try:
        return {
            str(r[0])
            for r in conn.execute("SELECT tumbler FROM documents").fetchall()
        }
    finally:
        conn.close()


def migrate_aspects(
    sqlite_path: Path,
    http_aspects,  # HttpDocumentAspectsStore instance
    *,
    batch_size: int = _BATCH,
    collector: Any = None,
    catalog_db_path: Path | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate document_aspects from SQLite to Postgres via the HTTP service.

    RDR-153 orphan policy: when *catalog_db_path* is provided, rows whose
    ``doc_id`` references no live catalog document (the audit's 675/675
    stale pre-rebuild tumblers) are SKIP-AND-RECORDED into *collector*
    instead of imported. Without it, behavior is unchanged (RDR-152
    callers).

    Returns a summary dict: {imported, skipped, errors}.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    live_tumblers: set[str] | None = (
        _load_live_tumblers(catalog_db_path) if catalog_db_path else None
    )
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    imported = skipped = errors = 0
    read = 0
    try:
        # Detect column presence (document_aspects schema evolves)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()}
        extra_cols = []
        if "doc_id" in cols:
            extra_cols.append("doc_id")
        if "source_uri" in cols:
            extra_cols.append("source_uri")
        if "salient_sentences" in cols:
            extra_cols.append("salient_sentences")

        base_cols = [
            "collection", "source_path", "problem_formulation", "proposed_method",
            "experimental_datasets", "experimental_baselines", "experimental_results",
            "extras", "confidence", "extracted_at", "model_version", "extractor_name",
        ]
        all_cols = base_cols + [c for c in extra_cols if c not in base_cols]
        select_sql = f"SELECT {', '.join(all_cols)} FROM document_aspects"

        offset = 0
        while True:
            rows = conn.execute(f"{select_sql} LIMIT {batch_size} OFFSET {offset}").fetchall()
            if not rows:
                break
            # RDR-176 P3 (Gap 1, bead nexus-t9rmg.18): batch this page's surviving
            # rows into ONE import_aspects_batch POST. Orphan-skip + transform
            # failures stay per-row (recorded + excluded); only the network is
            # batched. A sub-confidence row returns 0 from the batch, so
            # skipped += (batch_size - imported_this_flush).
            batch: list[dict[str, Any]] = []
            keys: list[str] = []
            for row in rows:
                row_dict = dict(row)
                read += 1
                doc_id = str(row_dict.get("doc_id") or "")
                if (
                    live_tumblers is not None
                    and doc_id
                    and doc_id not in live_tumblers
                ):
                    skipped += 1
                    if collector is not None:
                        collector.record(
                            "aspects", "document_aspects",
                            issue_class="orphan_parent",
                            constraint="document_aspects.doc_id -> "
                                       "documents.tumbler",
                            reason="doc_id references a deleted/rebuilt "
                                   "catalog document (stale pre-rebuild "
                                   "tumbler); re-extract follow-on is "
                                   "nexus-f1m8s",
                            action="skipped",
                            sample_id=doc_id,
                        )
                    continue
                try:
                    body = _transform_aspect(row_dict)
                except Exception as exc:  # noqa: BLE001 — per-row transform failure logged + recorded; continue
                    errors += 1
                    _log.warning(
                        "aspects_etl.migrate_aspects.row_error",
                        collection=row_dict.get("collection"),
                        source_path=row_dict.get("source_path"),
                        error=str(exc),
                    )
                    if collector is not None:
                        collector.record(
                            "aspects", "document_aspects",
                            issue_class="unexpected", constraint="document_aspects",
                            reason=f"row rejected during transform: {exc}",
                            action="failed", sample_id=doc_id or f"row#{read}",
                        )
                    continue
                batch.append(body)
                keys.append(doc_id or f"row#{read}")
            if batch:
                try:
                    n = _etl_batch_with_breaker(http_aspects.import_aspects_batch, batch, breaker=breaker)
                    imported += n
                    skipped += len(batch) - n
                except Exception as exc:  # noqa: BLE001 — batch failure logged + recorded; migration continues (idempotent re-run)
                    errors += len(batch)
                    _log.warning("aspects_etl.migrate_aspects.batch_error", count=len(batch), error=str(exc))
                    if collector is not None:
                        for key in keys:
                            collector.record(
                                "aspects", "document_aspects",
                                issue_class="unexpected", constraint="document_aspects",
                                reason=f"batch import rejected: {exc}",
                                action="failed", sample_id=key,
                            )
            offset += batch_size
    finally:
        conn.close()

    _log.info(
        "aspects_etl.migrate_aspects.done",
        imported=imported,
        skipped=skipped,
        errors=errors,
    )
    if collector is not None:
        collector.count_read("aspects", "document_aspects", read)
        collector.count_written("aspects", "document_aspects", imported)
    return {"imported": imported, "skipped": skipped, "errors": errors}


def migrate_highlights(
    sqlite_path: Path,
    http_highlights,  # HttpDocumentHighlightsStore instance
    *,
    batch_size: int = _BATCH,
    collector: Any = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate document_highlights from SQLite to Postgres via the HTTP service."""
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    imported = skipped = errors = 0
    read = 0
    try:
        offset = 0
        while True:
            rows = conn.execute(
                "SELECT doc_id, source_uri, collection, highlights_md, "
                "mentions_md, ingested_at "
                f"FROM document_highlights LIMIT {batch_size} OFFSET {offset}"
            ).fetchall()
            if not rows:
                break
            # RDR-176 P3 (Gap 1): batch this page into ONE import_highlights_batch.
            batch: list[dict[str, Any]] = []
            keys: list[str] = []
            for row in rows:
                row_dict = dict(row)
                read += 1
                try:
                    body = _transform_highlight(row_dict)
                except Exception as exc:  # noqa: BLE001 — per-row transform failure recorded; continue
                    errors += 1
                    _log.warning("aspects_etl.migrate_highlights.row_error",
                                 doc_id=row_dict.get("doc_id"), error=str(exc))
                    if collector is not None:
                        collector.record(
                            "aspects", "document_highlights",
                            issue_class="unexpected", constraint="document_highlights",
                            reason=f"row rejected during transform: {exc}",
                            action="failed",
                            sample_id=str(row_dict.get("doc_id") or f"row#{read}"),
                        )
                    continue
                batch.append(body)
                keys.append(str(row_dict.get("doc_id") or f"row#{read}"))
            if batch:
                try:
                    n = _etl_batch_with_breaker(http_highlights.import_highlights_batch, batch, breaker=breaker)
                    imported += n
                    skipped += len(batch) - n
                except Exception as exc:  # noqa: BLE001 — batch failure recorded; continue (idempotent re-run)
                    errors += len(batch)
                    _log.warning("aspects_etl.migrate_highlights.batch_error", count=len(batch), error=str(exc))
                    if collector is not None:
                        for key in keys:
                            collector.record(
                                "aspects", "document_highlights",
                                issue_class="unexpected", constraint="document_highlights",
                                reason=f"batch import rejected: {exc}",
                                action="failed", sample_id=key,
                            )
            offset += batch_size
    finally:
        conn.close()

    _log.info(
        "aspects_etl.migrate_highlights.done",
        imported=imported,
        skipped=skipped,
        errors=errors,
    )
    if collector is not None:
        collector.count_read("aspects", "document_highlights", read)
        collector.count_written("aspects", "document_highlights", imported)
    return {"imported": imported, "skipped": skipped, "errors": errors}


def migrate_queue(
    sqlite_path: Path,
    http_queue,  # HttpAspectQueue instance
    *,
    batch_size: int = _BATCH,
    collector: Any = None,
    catalog_db_path: Path | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate aspect_extraction_queue from SQLite to Postgres via the HTTP service.

    RDR-153 orphan policy (the audit's 3/7 shape): with *catalog_db_path*,
    rows whose ``doc_id`` references no live catalog document are
    SKIP-AND-RECORDED; the valid rows migrate.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    live_tumblers: set[str] | None = (
        _load_live_tumblers(catalog_db_path) if catalog_db_path else None
    )
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    imported = skipped = errors = 0
    read = 0
    try:
        # Detect column presence (queue schema evolves)
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(aspect_extraction_queue)"
        ).fetchall()}
        base_cols = [
            "collection", "source_path", "content_hash", "status",
            "retry_count", "enqueued_at", "last_attempt_at", "last_error",
        ]
        if "doc_id" in cols:
            base_cols.insert(2, "doc_id")
        if "content" in cols:
            base_cols.insert(base_cols.index("content_hash") + 1, "content")
        select_sql = f"SELECT {', '.join(base_cols)} FROM aspect_extraction_queue"

        offset = 0
        while True:
            rows = conn.execute(f"{select_sql} LIMIT {batch_size} OFFSET {offset}").fetchall()
            if not rows:
                break
            # RDR-176 P3 (Gap 1): batch this page into ONE import_queue_batch POST.
            batch: list[dict[str, Any]] = []
            keys: list[str] = []
            for row in rows:
                row_dict = dict(row)
                read += 1
                q_doc_id = str(row_dict.get("doc_id") or "")
                if (
                    live_tumblers is not None
                    and q_doc_id
                    and q_doc_id not in live_tumblers
                ):
                    skipped += 1
                    if collector is not None:
                        collector.record(
                            "aspects", "aspect_extraction_queue",
                            issue_class="orphan_parent",
                            constraint="aspect_extraction_queue.doc_id -> "
                                       "documents.tumbler",
                            reason="queued doc_id references a deleted/"
                                   "rebuilt catalog document",
                            action="skipped",
                            sample_id=q_doc_id,
                        )
                    continue
                try:
                    body = _transform_queue_row(row_dict)
                except Exception as exc:  # noqa: BLE001 — per-row transform failure recorded; continue
                    errors += 1
                    _log.warning(
                        "aspects_etl.migrate_queue.row_error",
                        collection=row_dict.get("collection"),
                        source_path=row_dict.get("source_path"),
                        error=str(exc),
                    )
                    if collector is not None:
                        collector.record(
                            "aspects", "aspect_extraction_queue",
                            issue_class="unexpected", constraint="aspect_extraction_queue",
                            reason=f"row rejected during transform: {exc}",
                            action="failed", sample_id=q_doc_id or f"row#{read}",
                        )
                    continue
                batch.append(body)
                keys.append(q_doc_id or f"row#{read}")
            if batch:
                try:
                    n = _etl_batch_with_breaker(http_queue.import_queue_batch, batch, breaker=breaker)
                    imported += n
                    skipped += len(batch) - n
                except Exception as exc:  # noqa: BLE001 — batch failure recorded; continue (idempotent re-run)
                    errors += len(batch)
                    _log.warning("aspects_etl.migrate_queue.batch_error", count=len(batch), error=str(exc))
                    if collector is not None:
                        for key in keys:
                            collector.record(
                                "aspects", "aspect_extraction_queue",
                                issue_class="unexpected", constraint="aspect_extraction_queue",
                                reason=f"batch import rejected: {exc}",
                                action="failed", sample_id=key,
                            )
            offset += batch_size
    finally:
        conn.close()

    _log.info(
        "aspects_etl.migrate_queue.done",
        imported=imported,
        skipped=skipped,
        errors=errors,
    )
    if collector is not None:
        collector.count_read("aspects", "aspect_extraction_queue", read)
        collector.count_written("aspects", "aspect_extraction_queue", imported)
    return {"imported": imported, "skipped": skipped, "errors": errors}


def migrate_promotion_log(
    sqlite_path: Path,
    http_aspects,  # HttpDocumentAspectsStore instance (uses /v1/aspects/promotion/import)
    *,
    batch_size: int = _BATCH,
    collector: Any = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """Migrate aspect_promotion_log from SQLite to Postgres via the HTTP service."""
    import json  # noqa: PLC0415 — deferred import; rare/branch-local path or circular-dep / startup-cost avoidance

    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    imported = skipped = errors = 0
    try:
        # Check if table exists
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='aspect_promotion_log'"
        ).fetchone()
        if not exists:
            _log.info("aspects_etl.migrate_promotion_log.table_absent")
            return {"imported": 0, "skipped": 0, "errors": 0}

        offset = 0
        while True:
            rows = conn.execute(
                "SELECT field_name, sql_type, column_added, rows_backfilled, "
                "rows_pruned, pruned, promoted_at "
                f"FROM aspect_promotion_log LIMIT {batch_size} OFFSET {offset}"
            ).fetchall()
            if not rows:
                break
            # RDR-176 P3 (Gap 1): batch this page into ONE import_promotion_batch POST.
            batch: list[dict[str, Any]] = []
            keys: list[str] = []
            for row in rows:
                row_dict = dict(row)
                try:
                    body = _transform_promotion_row(row_dict)
                except Exception as exc:  # noqa: BLE001 — per-row transform failure recorded; continue
                    errors += 1
                    _log.warning(
                        "aspects_etl.migrate_promotion_log.row_error",
                        field_name=row_dict.get("field_name"), error=str(exc),
                    )
                    if collector is not None:
                        collector.record(
                            "aspects", "aspect_promotion_log",
                            issue_class="unexpected", constraint="aspect_promotion_log",
                            reason=f"row rejected during transform: {exc}",
                            action="failed",
                            sample_id=str(row_dict.get("field_name") or f"row#{imported + skipped + errors}"),
                        )
                    continue
                batch.append(body)
                keys.append(str(row_dict.get("field_name") or ""))
            if batch:
                try:
                    n = _etl_batch_with_breaker(http_aspects.import_promotion_batch, batch, breaker=breaker)
                    imported += n
                    skipped += len(batch) - n
                except Exception as exc:  # noqa: BLE001 — batch failure recorded; continue (idempotent re-run)
                    errors += len(batch)
                    _log.warning("aspects_etl.migrate_promotion_log.batch_error", count=len(batch), error=str(exc))
                    if collector is not None:
                        for key in keys:
                            collector.record(
                                "aspects", "aspect_promotion_log",
                                issue_class="unexpected", constraint="aspect_promotion_log",
                                reason=f"batch import rejected: {exc}",
                                action="failed", sample_id=key,
                            )
            offset += batch_size
    finally:
        conn.close()

    _log.info(
        "aspects_etl.migrate_promotion_log.done",
        imported=imported,
        skipped=skipped,
        errors=errors,
    )
    return {"imported": imported, "skipped": skipped, "errors": errors}


def migrate_without_queue(
    sqlite_path: Path,
    http_aspects,
    http_highlights,
    *,
    batch_size: int = _BATCH,
    collector: Any = None,
    catalog_db_path: Path | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, dict[str, int]]:
    """Migrate document_aspects, highlights, and promotion_log — NOT the queue.

    nexus-iy5se: the aspect_extraction_queue table has a FK into
    catalog_documents (fk_aspect_queue_catalog_doc).  On a virgin target
    catalog_documents is empty when the aspects ladder slot runs, so queue
    rows with VALID doc_ids would fail the FK constraint.  Splitting the
    queue import into a post-catalog step (``migrate_queue``) fixes this.

    This function handles the three tables that carry NO FK into
    catalog_documents and therefore can safely run before catalog:

    - document_aspects (FK-free against catalog)
    - document_highlights (FK-free against catalog)
    - aspect_promotion_log (FK-free against catalog)

    Returns a summary dict with keys: aspects, highlights, promotion_log,
    each containing {imported, skipped, errors}.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    return {
        "aspects": migrate_aspects(
            sqlite_path, http_aspects, batch_size=batch_size,
            collector=collector, catalog_db_path=catalog_db_path,
            breaker=breaker,
        ),
        "highlights": migrate_highlights(
            sqlite_path, http_highlights, batch_size=batch_size,
            collector=collector, breaker=breaker,
        ),
        "promotion_log": migrate_promotion_log(
            sqlite_path, http_aspects, batch_size=batch_size,
            collector=collector, breaker=breaker,
        ),
    }


def migrate_all(
    sqlite_path: Path,
    http_aspects,
    http_highlights,
    http_queue,
    *,
    batch_size: int = _BATCH,
    collector: Any = None,
    catalog_db_path: Path | None = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, dict[str, int]]:
    """Run all four table migrations in order.

    .. deprecated::
        Prefer :func:`migrate_without_queue` (for the aspects ladder slot)
        followed by :func:`migrate_queue` (for the aspects_queue slot, which
        runs after catalog) so queue rows with valid doc_ids do not fail FK
        constraints on a virgin target.  This combined form is preserved for
        backwards-compat callers (e.g. single-store tests).

    Returns a summary dict with keys:
        aspects, highlights, queue, promotion_log
    each containing {imported, skipped, errors}.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    result = migrate_without_queue(
        sqlite_path, http_aspects, http_highlights,
        batch_size=batch_size,
        collector=collector,
        catalog_db_path=catalog_db_path,
        breaker=breaker,
    )
    result["queue"] = migrate_queue(
        sqlite_path, http_queue, batch_size=batch_size,
        collector=collector, catalog_db_path=catalog_db_path,
        breaker=breaker,
    )
    return result
