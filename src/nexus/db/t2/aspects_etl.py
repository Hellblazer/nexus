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

_log = structlog.get_logger(__name__)


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


def migrate_aspects(
    sqlite_path: Path,
    http_aspects,  # HttpDocumentAspectsStore instance
    *,
    batch_size: int = 200,
) -> dict[str, int]:
    """Migrate document_aspects from SQLite to Postgres via the HTTP service.

    Returns a summary dict: {imported, skipped, errors}.
    """
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    imported = skipped = errors = 0
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
            for row in rows:
                row_dict = dict(row)
                try:
                    body = _transform_aspect(row_dict)
                    n = http_aspects.import_aspect(body)
                    if n > 0:
                        imported += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    _log.warning(
                        "aspects_etl.migrate_aspects.row_error",
                        collection=row_dict.get("collection"),
                        source_path=row_dict.get("source_path"),
                        error=str(exc),
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
    return {"imported": imported, "skipped": skipped, "errors": errors}


def migrate_highlights(
    sqlite_path: Path,
    http_highlights,  # HttpDocumentHighlightsStore instance
    *,
    batch_size: int = 200,
) -> dict[str, int]:
    """Migrate document_highlights from SQLite to Postgres via the HTTP service."""
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    imported = skipped = errors = 0
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
            for row in rows:
                row_dict = dict(row)
                try:
                    body = _transform_highlight(row_dict)
                    n = http_highlights.import_highlight(body)
                    if n > 0:
                        imported += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    _log.warning(
                        "aspects_etl.migrate_highlights.row_error",
                        doc_id=row_dict.get("doc_id"),
                        error=str(exc),
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
    return {"imported": imported, "skipped": skipped, "errors": errors}


def migrate_queue(
    sqlite_path: Path,
    http_queue,  # HttpAspectQueue instance
    *,
    batch_size: int = 200,
) -> dict[str, int]:
    """Migrate aspect_extraction_queue from SQLite to Postgres via the HTTP service."""
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    imported = skipped = errors = 0
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
            for row in rows:
                row_dict = dict(row)
                try:
                    body = _transform_queue_row(row_dict)
                    n = http_queue.import_queue_row(body)
                    if n > 0:
                        imported += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    _log.warning(
                        "aspects_etl.migrate_queue.row_error",
                        collection=row_dict.get("collection"),
                        source_path=row_dict.get("source_path"),
                        error=str(exc),
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
    return {"imported": imported, "skipped": skipped, "errors": errors}


def migrate_promotion_log(
    sqlite_path: Path,
    http_aspects,  # HttpDocumentAspectsStore instance (uses /v1/aspects/promotion/import)
    *,
    batch_size: int = 200,
) -> dict[str, int]:
    """Migrate aspect_promotion_log from SQLite to Postgres via the HTTP service."""
    import json

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
            for row in rows:
                row_dict = dict(row)
                try:
                    body = _transform_promotion_row(row_dict)
                    # POST to /v1/aspects/promotion/import
                    import httpx
                    resp = http_aspects._client.post(
                        "/v1/aspects/promotion/import",
                        content=json.dumps(body),
                    )
                    resp.raise_for_status()
                    n = resp.json().get("imported", 0)
                    if n > 0:
                        imported += 1
                    else:
                        skipped += 1
                except Exception as exc:
                    errors += 1
                    _log.warning(
                        "aspects_etl.migrate_promotion_log.row_error",
                        field_name=row_dict.get("field_name"),
                        error=str(exc),
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


def migrate_all(
    sqlite_path: Path,
    http_aspects,
    http_highlights,
    http_queue,
    *,
    batch_size: int = 200,
) -> dict[str, dict[str, int]]:
    """Run all four table migrations in order.

    Returns a summary dict with keys:
        aspects, highlights, queue, promotion_log
    each containing {imported, skipped, errors}.
    """
    return {
        "aspects": migrate_aspects(sqlite_path, http_aspects, batch_size=batch_size),
        "highlights": migrate_highlights(sqlite_path, http_highlights, batch_size=batch_size),
        "queue": migrate_queue(sqlite_path, http_queue, batch_size=batch_size),
        "promotion_log": migrate_promotion_log(sqlite_path, http_aspects, batch_size=batch_size),
    }
