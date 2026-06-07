# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres plans ETL (bead nexus-gmiaf.11, RDR-152 Phase 2.1).

COPY-NOT-MOVE: reads all rows from the SQLite ``plans`` table and writes
them through the validated HTTP seam (``HttpPlanLibrary.import_plan``) so
every write flows via Java -> jOOQ -> Postgres under RLS with tenant stamping.
The SQLite source is NEVER modified.

IDEMPOTENT: relies on the upsert ``ON CONFLICT (tenant_id, project, query)
DO UPDATE SET … = EXCLUDED.*`` that the Java service enforces via
``PlanRepository.importRow``. Re-running the ETL produces the same row
count; content changes in the source are applied on the next run; source
``created_at``, ``use_count``, ``last_used``, ``match_count``,
``match_conf_sum``, ``success_count``, and ``failure_count`` are preserved
verbatim.

FIDELITY-PRESERVING: uses ``POST /v1/plans/import`` (not ``/save``), which
writes all counter and timestamp columns from the source row VERBATIM. Using
``/save`` would reset all counters to 0 and stamp ``created_at=now()``,
destroying the observable plan performance history.

TENANT STAMPING: the ``HttpPlanLibrary`` is constructed with
``tenant=DEFAULT_TENANT`` (``"default"``); the service stamps
``tenant_id`` from the ``X-Nexus-Tenant`` request header.

FIELD MAPPING (SQLite ``plans`` columns -> ``import_plan()`` kwargs):
  project           -> project           (TEXT; '' preserved, NULL -> '')
  query             -> query             (TEXT NOT NULL)
  plan_json         -> plan_json         (TEXT NOT NULL)
  outcome           -> outcome           (TEXT; default 'success')
  tags              -> tags              (TEXT; '' preserved, NULL -> '')
  created_at        -> created_at        (ISO-8601 UTC string; REQUIRED fidelity)
  ttl               -> ttl              (INTEGER nullable)
  name              -> name              (TEXT nullable)
  verb              -> verb              (TEXT nullable)
  scope             -> scope             (TEXT nullable)
  dimensions        -> dimensions        (TEXT nullable)
  default_bindings  -> default_bindings  (TEXT nullable)
  parent_dims       -> parent_dims       (TEXT nullable)
  use_count         -> use_count         (INTEGER; copied verbatim)
  last_used         -> last_used         (ISO-8601 UTC string or None)
  match_count       -> match_count       (INTEGER; copied verbatim)
  match_conf_sum    -> match_conf_sum    (REAL; copied verbatim)
  success_count     -> success_count     (INTEGER; copied verbatim)
  failure_count     -> failure_count     (INTEGER; copied verbatim)
  scope_tags        -> scope_tags        (TEXT; '' preserved, NULL -> '')
  match_text        -> match_text        (TEXT; '' preserved, NULL -> '')
  disabled_at       -> disabled_at       (ISO-8601 UTC string or None)
  id                NOT copied           (PG uses its own BIGSERIAL)

The ``_transform_row`` function documents the full mapping logic and is
exposed for unit-testing. ``migrate_plan_rows`` is the public entry point
used by the CLI and integration tests.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# Column order matches ``_PLAN_COLUMNS`` in plan_library.py — used when reading
# rows via ``SELECT *``.
_COLUMNS = (
    "id",
    "project",
    "query",
    "plan_json",
    "outcome",
    "tags",
    "created_at",
    "ttl",
    "name",
    "verb",
    "scope",
    "dimensions",
    "default_bindings",
    "parent_dims",
    "use_count",
    "last_used",
    "match_count",
    "match_conf_sum",
    "success_count",
    "failure_count",
    "scope_tags",
    "match_text",
    "disabled_at",
)


def _transform_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map a SQLite plans row dict to ``HttpPlanLibrary.import_plan()`` kwargs.

    Rules:
    - ``id`` is NOT included (PG uses its own BIGSERIAL).
    - ``project``, ``tags``, ``scope_tags``, ``match_text``: ``None``
      normalised to ``""``.
    - ``outcome``: ``None`` normalised to ``"success"``.
    - ``created_at``: required; falls back to epoch if absent (should
      never happen in practice).
    - ``last_used``, ``disabled_at``: empty string ``""`` normalised to
      ``None`` (PG stores these as TIMESTAMPTZ nullable; ``''`` is the SQLite
      column default but has no meaning as a timestamp).
    - Counter fields (``use_count``, ``match_count``, ``success_count``,
      ``failure_count``): ``None`` normalised to 0.
    - ``match_conf_sum``: ``None`` normalised to 0.0.

    Returns a dict suitable for unpacking into
    ``store.import_plan(**kwargs)``.
    """
    def _str_or_empty(v: Any) -> str:
        return v if v is not None else ""

    def _nullable_ts(v: Any) -> str | None:
        """Return None if empty/None, otherwise the string verbatim."""
        if v is None or v == "":
            return None
        return str(v)

    def _int_or_zero(v: Any) -> int:
        return int(v) if v is not None else 0

    return {
        "project":          _str_or_empty(row.get("project")),
        "query":            row["query"],
        "plan_json":        row["plan_json"],
        "outcome":          row.get("outcome") or "success",
        "tags":             _str_or_empty(row.get("tags")),
        "created_at":       row.get("created_at") or "1970-01-01T00:00:00Z",
        "ttl":              row.get("ttl"),
        "name":             row.get("name"),
        "verb":             row.get("verb"),
        "scope":            row.get("scope"),
        "dimensions":       row.get("dimensions"),
        "default_bindings": row.get("default_bindings"),
        "parent_dims":      row.get("parent_dims"),
        "use_count":        _int_or_zero(row.get("use_count")),
        "last_used":        _nullable_ts(row.get("last_used")),
        "match_count":      _int_or_zero(row.get("match_count")),
        "match_conf_sum":   float(row.get("match_conf_sum") or 0.0),
        "success_count":    _int_or_zero(row.get("success_count")),
        "failure_count":    _int_or_zero(row.get("failure_count")),
        "scope_tags":       _str_or_empty(row.get("scope_tags")),
        "match_text":       _str_or_empty(row.get("match_text")),
        "disabled_at":      _nullable_ts(row.get("disabled_at")),
    }


def count_source_rows(source_db_path: Path) -> int:
    """Return the number of rows in the SQLite plans table (read-only).

    Used by the ``--dry-run`` CLI path to report the row count without
    writing. Opens the source in ``uri=True mode=ro`` (read-only).
    """
    uri = f"file:{source_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite source for reading: {source_db_path}: {exc}"
        ) from exc
    try:
        return conn.execute("SELECT COUNT(*) FROM plans").fetchone()[0]
    finally:
        conn.close()


def migrate_plan_rows(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
) -> dict[str, int]:
    """Copy all rows from a SQLite plans table into Postgres via *store*.

    Uses ``store.import_plan(...)`` (``POST /v1/plans/import``) which
    writes ``created_at`` and all counter/metric columns VERBATIM from the
    source row. This preserves plan performance history across re-runs and
    is the correct write path for plans ETL. Do NOT substitute
    ``store.save_plan(...)`` here — ``save_plan`` routes through
    ``/v1/plans/save`` which stamps ``created_at=now()`` and resets all
    counters to 0 on every write.

    Args:
        source_db_path: Path to the SQLite T2 database file.
        store: An ``HttpPlanLibrary`` (or compatible duck-typed) instance
               connected to the Postgres service.
        batch_log_every: Emit a progress log line every N rows.

    Returns:
        ``{"read": N, "written": M}`` — always ``read == written`` for
        a healthy run; differences indicate per-row failures (logged).

    Copy-not-move guarantee: the SQLite connection is opened in
    ``uri=True`` mode with ``?mode=ro`` so the file is opened read-only
    at the OS level; even a bug in this function cannot modify the source.
    """
    read_count = 0
    written_count = 0

    uri = f"file:{source_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite source for reading: {source_db_path}: {exc}"
        ) from exc

    try:
        conn.row_factory = sqlite3.Row
        # Select explicit columns so row order matches _COLUMNS even if the
        # source DB was migrated and has a different column order on disk.
        # Only select columns that exist; the _transform_row function handles
        # missing optional fields gracefully.
        avail = {
            row[1]
            for row in conn.execute("PRAGMA table_info(plans)").fetchall()
        }
        select_cols = ", ".join(c for c in _COLUMNS if c in avail)
        cursor = conn.execute(
            f"SELECT {select_cols} FROM plans ORDER BY id ASC"
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    total = len(rows)
    _log.info(
        "plan_etl.start",
        source=str(source_db_path),
        total_rows=total,
    )

    for row_dict in (dict(r) for r in rows):
        read_count += 1
        transformed = _transform_row(row_dict)

        try:
            store.import_plan(**transformed)
            written_count += 1
        except Exception as exc:
            _log.error(
                "plan_etl.row_failed",
                project=transformed.get("project", ""),
                query_prefix=(
                    transformed["query"][:40]
                    if len(transformed.get("query", "")) > 40
                    else transformed.get("query", "")
                ),
                error=str(exc),
            )
            # Continue processing remaining rows so a single failure
            # doesn't abort the whole migration.

        if read_count % batch_log_every == 0:
            _log.info(
                "plan_etl.progress",
                read=read_count,
                written=written_count,
                total=total,
            )

    _log.info(
        "plan_etl.complete",
        read=read_count,
        written=written_count,
        total=total,
    )
    return {"read": read_count, "written": written_count}
