# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""SQLite -> Postgres telemetry ETL (bead nexus-gmiaf.12, RDR-152 Phase 2.2).

COPY-NOT-MOVE: reads all rows from the six SQLite telemetry tables and writes
them through the validated HTTP seam (``HttpTelemetryStore``) so every write
flows via Java -> jOOQ -> Postgres under RLS with tenant stamping. The SQLite
source is NEVER modified.

IDEMPOTENT: event log tables (relevance_log, tier_writes, nx_answer_runs,
hook_failures) use ``ON CONFLICT DO NOTHING`` on the ETL dedup unique indexes.
search_telemetry has a natural composite PK ``(tenant_id, ts, query_hash,
collection)`` with the same DO NOTHING semantics. frecency uses
``GREATEST/LEAST`` conflict resolution (score/count/last_hit_at use GREATEST;
embedded_at uses LEAST to preserve the oldest embed time).

FIDELITY-PRESERVING: all six tables go through ``POST /v1/telemetry/import``
which writes timestamp columns VERBATIM from the source row. Using the live
write paths (log_relevance, log_search_batch, etc.) would stamp ``now()`` for
the timestamp columns, destroying the historical audit trail.

TABLES AND CONFLICT STRATEGIES:
  relevance_log    — DO NOTHING on (tenant_id, query, chunk_id, action,
                       COALESCE(session_id,''), timestamp)
  search_telemetry — DO NOTHING on PK (tenant_id, ts, query_hash, collection)
  tier_writes      — DO NOTHING on (tenant_id, session_id, ts, tool, tier)
  nx_answer_runs   — DO NOTHING on (tenant_id, question, created_at)
  hook_failures    — DO NOTHING on (tenant_id, doc_id, hook_name, occurred_at)
  frecency         — GREATEST(score/count/last_hit_at), LEAST(embedded_at)

FIELD MAPPING notes:
  - ``id`` columns are NOT imported (PG uses its own BIGSERIAL).
  - All timestamp TEXT columns in SQLite become TIMESTAMPTZ in PG; the Java
    ``parseTs()`` helper handles ISO-8601 -> OffsetDateTime with a ``now()``
    fallback for corrupt values.
  - frecency.embedded_at and frecency.last_hit_at may be NULL in old DBs
    (pre-migration rows had no TTL). Pass NULL-safe strings.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

# ── Column lists (explicit SELECT prevents column-order surprises) ─────────────

_RELEVANCE_LOG_COLS = (
    "id", "query", "chunk_id", "collection", "action", "session_id", "timestamp",
)
_SEARCH_TELEMETRY_COLS = (
    "ts", "query_hash", "collection", "raw_count", "kept_count",
    "top_distance", "threshold",
)
_TIER_WRITES_COLS = (
    "id", "session_id", "ts", "tool", "tier", "agent", "project", "target_title",
)
_NX_ANSWER_RUNS_COLS = (
    "id", "question", "plan_id", "matched_confidence", "step_count",
    "final_text", "cost_usd", "duration_ms", "created_at",
)
_HOOK_FAILURES_COLS = (
    "id", "doc_id", "collection", "hook_name", "error", "occurred_at",
    "batch_doc_ids", "is_batch", "chain",
)
_FRECENCY_COLS = (
    "chunk_id", "embedded_at", "ttl_days", "frecency_score", "miss_count", "last_hit_at",
)


def _open_ro(source_db_path: Path) -> sqlite3.Connection:
    """Open *source_db_path* read-only. Raises RuntimeError if the file is missing."""
    uri = f"file:{source_db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise RuntimeError(
            f"Cannot open SQLite source for reading: {source_db_path}: {exc}"
        ) from exc
    conn.row_factory = sqlite3.Row
    return conn


def _available_cols(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    """Return the set of column names for *table* (empty set when table absent)."""
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return frozenset(r[1] for r in rows)
    except sqlite3.OperationalError:
        return frozenset()


def _fetch(
    conn: sqlite3.Connection,
    table: str,
    desired_cols: tuple[str, ...],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Fetch all rows from *table*, projecting only columns present in the DB.

    Returns ``(actual_col_list, rows_as_dicts)``. When the table does not
    exist, returns ``([], [])``.
    """
    avail = _available_cols(conn, table)
    if not avail:
        _log.info("telemetry_etl.table_absent", table=table)
        return [], []
    select_cols = [c for c in desired_cols if c in avail]
    if not select_cols:
        return [], []
    sql = f"SELECT {', '.join(select_cols)} FROM {table} ORDER BY ROWID ASC"
    rows = conn.execute(sql).fetchall()
    return select_cols, [dict(r) for r in rows]


# ── Per-table transform helpers ───────────────────────────────────────────────

def _str_or_empty(v: Any) -> str:
    return v if v is not None else ""


def _nullable_str(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return str(v)


def _int_or_zero(v: Any) -> int:
    return int(v) if v is not None else 0


def _nullable_int(v: Any) -> int | None:
    """Coerce to int, preserving NULL. For INTEGER columns (e.g. nx_answer_runs.plan_id,
    a BIGINT on the service side) that must NOT be stringified — sending a string trips
    the service's String->Number cast (nexus-5gaj7)."""
    if v is None or v == "":
        return None
    return int(v)


def _float_or_zero(v: Any) -> float:
    return float(v) if v is not None else 0.0


# ── Public entry points ───────────────────────────────────────────────────────

def count_source_rows(source_db_path: Path) -> dict[str, int]:
    """Return row counts for all six telemetry tables (read-only).

    Used by the ``--dry-run`` CLI path. Returns a dict:
    ``{"relevance_log": N, "search_telemetry": M, ...}``.
    """
    conn = _open_ro(source_db_path)
    try:
        result: dict[str, int] = {}
        for table in ("relevance_log", "search_telemetry", "tier_writes",
                      "nx_answer_runs", "hook_failures", "frecency"):
            avail = _available_cols(conn, table)
            if not avail:
                result[table] = 0
            else:
                result[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
        return result
    finally:
        conn.close()


def migrate_telemetry_rows(
    source_db_path: Path,
    store: Any,
    *,
    batch_log_every: int = 100,
    collector: Any = None,
) -> dict[str, Any]:
    """Copy all telemetry rows from SQLite into Postgres via *store*.

    Calls ``POST /v1/telemetry/import`` for each table, which writes timestamp
    columns VERBATIM (fidelity-preserving import path). Do NOT substitute the
    live write methods (``log_relevance``, etc.) here — they stamp ``now()``
    for the timestamp columns.

    Args:
        source_db_path:  Path to the SQLite T2 database file.
        store:           An ``HttpTelemetryStore`` (or compatible) instance
                         connected to the Postgres service.
        batch_log_every: Emit a progress log line every N rows per table.

    Returns:
        ``{"table": {"read": N, "written": M}, ...}`` for each of the six tables.
    """
    conn = _open_ro(source_db_path)
    try:
        return _migrate_all(
            conn, store, batch_log_every=batch_log_every, collector=collector,
        )
    finally:
        conn.close()


def _migrate_all(
    conn: sqlite3.Connection,
    store: Any,
    *,
    batch_log_every: int,
    collector: Any = None,
) -> dict[str, Any]:
    results: dict[str, Any] = {}

    results["relevance_log"]    = _migrate_relevance_log(
        conn, store, batch_log_every, collector=collector,
    )
    results["search_telemetry"] = _migrate_search_telemetry(
        conn, store, batch_log_every, collector=collector,
    )
    results["tier_writes"]      = _migrate_tier_writes(
        conn, store, batch_log_every, collector=collector,
    )
    results["nx_answer_runs"]   = _migrate_nx_answer_runs(
        conn, store, batch_log_every, collector=collector,
    )
    results["hook_failures"]    = _migrate_hook_failures(
        conn, store, batch_log_every, collector=collector,
    )
    results["frecency"]         = _migrate_frecency(
        conn, store, batch_log_every, collector=collector,
    )

    if collector is not None:
        for table, counts in results.items():
            collector.count_read("telemetry", table, counts["read"])
            collector.count_written("telemetry", table, counts["written"])

    total_read    = sum(v["read"]    for v in results.values())
    total_written = sum(v["written"] for v in results.values())
    _log.info(
        "telemetry_etl.complete",
        total_read=total_read,
        total_written=total_written,
        by_table={t: v for t, v in results.items()},
    )
    return results


def _migrate_relevance_log(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None,
) -> dict[str, int]:
    _, rows = _fetch(conn, "relevance_log", _RELEVANCE_LOG_COLS)
    read_n = written_n = 0
    total = len(rows)
    _log.info("telemetry_etl.relevance_log.start", total=total)

    for row in rows:
        read_n += 1
        try:
            store.import_relevance_row(
                query=row.get("query", ""),
                chunk_id=row.get("chunk_id", ""),
                collection=_str_or_empty(row.get("collection")),
                action=row.get("action", ""),
                session_id=_str_or_empty(row.get("session_id")),
                timestamp=row.get("timestamp", ""),
            )
            written_n += 1
        except Exception as exc:
            _log.error(
                "telemetry_etl.relevance_log.row_failed",
                query_prefix=str(row.get("query", ""))[:40],
                error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "telemetry", "relevance_log",
                    issue_class="unexpected",
                    constraint="relevance_log",
                    reason=f"row rejected during import: {exc}",
                    action="failed",
                    sample_id=f"relevance_log#{read_n}",
                )
        if read_n % batch_log_every == 0:
            _log.info("telemetry_etl.relevance_log.progress",
                      read=read_n, written=written_n, total=total)

    return {"read": read_n, "written": written_n}


def _migrate_search_telemetry(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None,
) -> dict[str, int]:
    _, rows = _fetch(conn, "search_telemetry", _SEARCH_TELEMETRY_COLS)
    read_n = written_n = 0
    total = len(rows)
    _log.info("telemetry_etl.search_telemetry.start", total=total)

    for row in rows:
        read_n += 1
        try:
            store.import_search_row(
                ts=row.get("ts", ""),
                query_hash=row.get("query_hash", ""),
                collection=row.get("collection", ""),
                raw_count=_int_or_zero(row.get("raw_count")),
                kept_count=_int_or_zero(row.get("kept_count")),
                top_distance=row.get("top_distance"),
                threshold=row.get("threshold"),
            )
            written_n += 1
        except Exception as exc:
            _log.error(
                "telemetry_etl.search_telemetry.row_failed",
                ts=str(row.get("ts", ""))[:30],
                error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "telemetry", "search_telemetry",
                    issue_class="unexpected",
                    constraint="search_telemetry",
                    reason=f"row rejected during import: {exc}",
                    action="failed",
                    sample_id=f"search_telemetry#{read_n}",
                )
        if read_n % batch_log_every == 0:
            _log.info("telemetry_etl.search_telemetry.progress",
                      read=read_n, written=written_n, total=total)

    return {"read": read_n, "written": written_n}


def _migrate_tier_writes(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None,
) -> dict[str, int]:
    _, rows = _fetch(conn, "tier_writes", _TIER_WRITES_COLS)
    read_n = written_n = 0
    total = len(rows)
    _log.info("telemetry_etl.tier_writes.start", total=total)

    for row in rows:
        read_n += 1
        try:
            store.import_tier_write(
                session_id=_str_or_empty(row.get("session_id")),
                ts=row.get("ts", ""),
                tool=_str_or_empty(row.get("tool")),
                tier=_str_or_empty(row.get("tier")),
                # agent/project/target_title are nullable in SQLite — preserve NULL so PG
                # aggregations (GROUP BY agent, WHERE agent IS NOT NULL) are not corrupted
                # by empty-string coercion.  Use _nullable_str (returns None for None/"").
                agent=_nullable_str(row.get("agent")),
                project=_nullable_str(row.get("project")),
                target_title=_nullable_str(row.get("target_title")),
            )
            written_n += 1
        except Exception as exc:
            _log.error(
                "telemetry_etl.tier_writes.row_failed",
                ts=str(row.get("ts", ""))[:30],
                error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "telemetry", "tier_writes",
                    issue_class="unexpected",
                    constraint="tier_writes",
                    reason=f"row rejected during import: {exc}",
                    action="failed",
                    sample_id=f"tier_writes#{read_n}",
                )
        if read_n % batch_log_every == 0:
            _log.info("telemetry_etl.tier_writes.progress",
                      read=read_n, written=written_n, total=total)

    return {"read": read_n, "written": written_n}


def _migrate_nx_answer_runs(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None,
) -> dict[str, int]:
    _, rows = _fetch(conn, "nx_answer_runs", _NX_ANSWER_RUNS_COLS)
    read_n = written_n = 0
    total = len(rows)
    _log.info("telemetry_etl.nx_answer_runs.start", total=total)

    # RDR-153 soft-dangler policy: plan_id has NO enforced FK — rows with a
    # deleted plan IMPORT (preserving event history) and an advisory records
    # the dangling reference. Valid plan ids come from the same source db.
    valid_plan_ids: set[int] = set()
    try:
        valid_plan_ids = {
            int(r[0]) for r in conn.execute("SELECT id FROM plans").fetchall()
        }
    except sqlite3.OperationalError:
        pass  # no plans table in source — every plan_id is then a dangler

    for row in rows:
        read_n += 1
        plan_id = _nullable_int(row.get("plan_id"))
        if (
            collector is not None
            and plan_id is not None
            and plan_id not in valid_plan_ids
        ):
            collector.record(
                "telemetry", "nx_answer_runs",
                issue_class="soft_dangler",
                constraint="nx_answer_runs.plan_id -> plans.id (not enforced)",
                reason="plan deleted; row imports with dangling reference; "
                       "sample ids are <run_id>:<plan_id>",
                action="flagged",
                sample_id=f"{row.get('id')}:{plan_id}",
            )
        try:
            store.import_nx_answer_run(
                question=row.get("question", ""),
                plan_id=_nullable_int(row.get("plan_id")),
                matched_confidence=row.get("matched_confidence"),
                step_count=_int_or_zero(row.get("step_count")),
                final_text=_str_or_empty(row.get("final_text")),
                cost_usd=row.get("cost_usd"),
                duration_ms=_int_or_zero(row.get("duration_ms")),
                created_at=row.get("created_at", ""),
            )
            written_n += 1
        except Exception as exc:
            _log.error(
                "telemetry_etl.nx_answer_runs.row_failed",
                question_prefix=str(row.get("question", ""))[:40],
                error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "telemetry", "nx_answer_runs",
                    issue_class="unexpected",
                    constraint="nx_answer_runs",
                    reason=f"row rejected during import: {exc}",
                    action="failed",
                    sample_id=f"nx_answer_runs#{read_n}",
                )
        if read_n % batch_log_every == 0:
            _log.info("telemetry_etl.nx_answer_runs.progress",
                      read=read_n, written=written_n, total=total)

    return {"read": read_n, "written": written_n}


def _normalize_timestamp(raw: str) -> tuple[str, bool]:
    """RDR-153 format-anomaly policy: parse lenient, emit canonical ISO-8601.

    Returns ``(canonical, was_normalized)``. The production anomaly is the
    space-form ``2026-04-23 10:47:54`` (234/234 hook_failures rows);
    ``datetime.fromisoformat`` accepts it and re-emits the ``T`` form.
    Raises ``ValueError`` for unparseable input — the caller records a
    ``failed`` issue (fail only if unparseable, never silently drop).
    """
    from datetime import datetime as _dt

    canonical = _dt.fromisoformat(raw).isoformat()
    # NOTE: any non-canonical-but-parseable form counts as normalized —
    # e.g. a 'Z' suffix becomes '+00:00', a bare date gains T00:00:00.
    # Production data had only the space form (234/234 hook_failures);
    # benign variants inflating 'handled' is acceptable and honest (the
    # stored value DID change).
    return canonical, canonical != raw


def _migrate_hook_failures(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None,
) -> dict[str, int]:
    _, rows = _fetch(conn, "hook_failures", _HOOK_FAILURES_COLS)
    read_n = written_n = 0
    total = len(rows)
    _log.info("telemetry_etl.hook_failures.start", total=total)

    for row in rows:
        read_n += 1
        raw_ts = row.get("occurred_at", "")
        try:
            occurred_at, normalized = _normalize_timestamp(raw_ts)
        except ValueError:
            _log.error(
                "telemetry_etl.hook_failures.unparseable_timestamp",
                occurred_at=raw_ts[:40],
            )
            if collector is not None:
                collector.record(
                    "telemetry", "hook_failures",
                    issue_class="format_anomaly",
                    constraint="hook_failures.occurred_at",
                    reason=f"unparseable timestamp (not ISO-8601-coercible): "
                           f"{raw_ts[:40]!r}",
                    action="failed",
                    sample_id=str(row.get("id", read_n)),
                )
            continue
        if normalized and collector is not None:
            collector.record(
                "telemetry", "hook_failures",
                issue_class="format_anomaly",
                constraint="hook_failures.occurred_at",
                reason="space-form timestamp normalized to ISO-8601 T form",
                action="handled",
                sample_id=str(row.get("id", read_n)),
            )
        try:
            store.import_hook_failure(
                doc_id=_str_or_empty(row.get("doc_id")),
                collection=_str_or_empty(row.get("collection")),
                hook_name=row.get("hook_name", ""),
                error=_str_or_empty(row.get("error")),
                occurred_at=occurred_at,
                batch_doc_ids=_nullable_str(row.get("batch_doc_ids")),
                is_batch=bool(row.get("is_batch", False)),
                chain=_nullable_str(row.get("chain")),
            )
            written_n += 1
        except Exception as exc:
            _log.error(
                "telemetry_etl.hook_failures.row_failed",
                hook_name=str(row.get("hook_name", "")),
                error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "telemetry", "hook_failures",
                    issue_class="unexpected",
                    constraint="hook_failures",
                    reason=f"row rejected during import: {exc}",
                    action="failed",
                    sample_id=str(row.get("id", read_n)),
                )
        if read_n % batch_log_every == 0:
            _log.info("telemetry_etl.hook_failures.progress",
                      read=read_n, written=written_n, total=total)

    return {"read": read_n, "written": written_n}


def _migrate_frecency(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None,
) -> dict[str, int]:
    _, rows = _fetch(conn, "frecency", _FRECENCY_COLS)
    read_n = written_n = 0
    total = len(rows)
    _log.info("telemetry_etl.frecency.start", total=total)

    for row in rows:
        read_n += 1
        try:
            store.import_frecency_row(
                chunk_id=row.get("chunk_id", ""),
                embedded_at=_nullable_str(row.get("embedded_at")),
                ttl_days=_int_or_zero(row.get("ttl_days")),
                frecency_score=_float_or_zero(row.get("frecency_score")),
                miss_count=_int_or_zero(row.get("miss_count")),
                last_hit_at=_nullable_str(row.get("last_hit_at")),
            )
            written_n += 1
        except Exception as exc:
            _log.error(
                "telemetry_etl.frecency.row_failed",
                chunk_id=str(row.get("chunk_id", ""))[:32],
                error=str(exc),
            )
            if collector is not None:
                collector.record(
                    "telemetry", "frecency",
                    issue_class="unexpected",
                    constraint="frecency",
                    reason=f"row rejected during import: {exc}",
                    action="failed",
                    sample_id=f"frecency#{read_n}",
                )
        if read_n % batch_log_every == 0:
            _log.info("telemetry_etl.frecency.progress",
                      read=read_n, written=written_n, total=total)

    return {"read": read_n, "written": written_n}
