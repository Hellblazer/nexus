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
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import structlog

from nexus.retry import EtlCircuitBreaker, _etl_batch_with_breaker

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


#: SQLite read-page size (RDR-176 / nexus-lbolo). Caps peak memory at one page
#: regardless of table size — the 190k-row case no longer materializes whole.
_READ_PAGE: int = 1000


def _select_cols(conn: sqlite3.Connection, table: str, desired_cols: tuple[str, ...]) -> list[str]:
    """The subset of *desired_cols* present in *table* (empty if table absent)."""
    avail = _available_cols(conn, table)
    if not avail:
        _log.info("telemetry_etl.table_absent", table=table)
        return []
    return [c for c in desired_cols if c in avail]


def _count_rows(conn: sqlite3.Connection, table: str) -> int:
    """Row count for *table* (0 if absent) — cheap, for the progress total."""
    if not _available_cols(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _iter_rows(
    conn: sqlite3.Connection,
    table: str,
    desired_cols: tuple[str, ...],
    *,
    page_size: int = _READ_PAGE,
) -> Iterator[dict[str, Any]]:
    """Yield *table*'s rows as projected dicts in LIMIT/OFFSET pages (RDR-176 /
    nexus-lbolo: page the read so peak memory is one page, not the whole table).

    Dicts are built from the projected columns directly, so this does not depend
    on the connection's ``row_factory``. Stops on the first short page (no
    trailing empty-page query).
    """
    cols = _select_cols(conn, table, desired_cols)
    if not cols:
        return
    cols_sql = ", ".join(cols)
    offset = 0
    while True:
        page = conn.execute(
            f"SELECT {cols_sql} FROM {table} ORDER BY ROWID ASC "
            f"LIMIT {page_size} OFFSET {offset}"
        ).fetchall()
        if not page:
            break
        for row in page:
            yield dict(zip(cols, row))
        if len(page) < page_size:
            break
        offset += page_size


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
    breaker: EtlCircuitBreaker | None = None,
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
        breaker:         Shared :class:`~nexus.retry.EtlCircuitBreaker`
                         (RDR-178 Gap 3) spanning all six tables.

    Returns:
        ``{"table": {"read": N, "written": M}, ...}`` for each of the six tables.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    conn = _open_ro(source_db_path)
    try:
        return _migrate_all(
            conn, store, batch_log_every=batch_log_every, collector=collector,
            breaker=breaker,
        )
    finally:
        conn.close()


def _migrate_all(
    conn: sqlite3.Connection,
    store: Any,
    *,
    batch_log_every: int,
    collector: Any = None,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, Any]:
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    results: dict[str, Any] = {}

    results["relevance_log"]    = _migrate_relevance_log(
        conn, store, batch_log_every, collector=collector, breaker=breaker,
    )
    results["search_telemetry"] = _migrate_search_telemetry(
        conn, store, batch_log_every, collector=collector, breaker=breaker,
    )
    results["tier_writes"]      = _migrate_tier_writes(
        conn, store, batch_log_every, collector=collector, breaker=breaker,
    )
    results["nx_answer_runs"]   = _migrate_nx_answer_runs(
        conn, store, batch_log_every, collector=collector, breaker=breaker,
    )
    results["hook_failures"]    = _migrate_hook_failures(
        conn, store, batch_log_every, collector=collector, breaker=breaker,
    )
    results["frecency"]         = _migrate_frecency(
        conn, store, batch_log_every, collector=collector, breaker=breaker,
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


class _SkipRow(Exception):  # noqa: N818 — control-flow signal, not an error condition
    """Raised by a row-builder to skip a corrupt row, recording it as failed.

    ``issue_class`` carries the policy classification (e.g. ``format_anomaly``
    for an unparseable timestamp) so the batched driver records the same class
    the per-row path did.
    """

    def __init__(self, reason: str, sample_id: str, issue_class: str = "unexpected") -> None:
        super().__init__(reason)
        self.reason = reason
        self.sample_id = sample_id
        self.issue_class = issue_class


def _run_batched(
    store: Any,
    table: str,
    rows: Iterable[dict[str, Any]],
    build: Any,
    *,
    collector: Any,
    batch_log_every: int,
    total: int = 0,
    breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    """RDR-176 P3 (Gap 1, bead nexus-t9rmg.18): shared per-table batch driver.

    Each row is TRANSFORMED per-row by ``build(row, collector)`` (which may raise
    :class:`_SkipRow` to drop+record a corrupt row, or emit a per-row advisory);
    valid rows accumulate and ship via ``store.import_rows_batch(table, batch)``
    at ceil(N/quota) — only the NETWORK is batched. A server-side batch rejection
    is recorded at batch granularity; the import is idempotent (DO NOTHING /
    GREATEST) so a re-run lands it.
    """
    breaker = breaker if breaker is not None else EtlCircuitBreaker()
    from nexus.db.chroma_quotas import QUOTAS  # noqa: PLC0415 — branch-local; quota constant
    bsize = QUOTAS.MAX_RECORDS_PER_WRITE

    read_n = written_n = 0
    batch: list[dict[str, Any]] = []
    keys: list[str] = []
    _log.info(f"telemetry_etl.{table}.start", total=total)

    def _flush() -> None:
        nonlocal written_n, batch, keys
        if not batch:
            return
        try:
            written_n += _etl_batch_with_breaker(store.import_rows_batch, table, batch, breaker=breaker)
        except Exception as exc:  # noqa: BLE001 — batch failure logged + recorded; migration continues (idempotent re-run)
            _log.error(f"telemetry_etl.{table}.batch_failed", count=len(batch), error=str(exc))
            if collector is not None:
                for key in keys:
                    collector.record(
                        "telemetry", table,
                        issue_class="unexpected", constraint=table,
                        reason=f"batch import rejected: {exc}",
                        action="failed", sample_id=key,
                    )
        batch = []
        keys = []

    for row in rows:
        read_n += 1
        try:
            row_dict, sample_id = build(row, collector)
        except _SkipRow as skip:
            _log.error(f"telemetry_etl.{table}.row_failed", error=skip.reason)
            if collector is not None:
                collector.record(
                    "telemetry", table,
                    issue_class=skip.issue_class, constraint=table,
                    reason=skip.reason, action="failed", sample_id=skip.sample_id,
                )
            continue
        batch.append(row_dict)
        keys.append(sample_id)
        if len(batch) >= bsize:
            _flush()
        if read_n % batch_log_every == 0:
            _log.info(f"telemetry_etl.{table}.progress", read=read_n, written=written_n, total=total)

    _flush()
    return {"read": read_n, "written": written_n}


def _build_relevance(row: dict[str, Any], _collector: Any) -> tuple[dict[str, Any], str]:
    return {
        "query":      row.get("query", ""),
        "chunk_id":   row.get("chunk_id", ""),
        "collection": _str_or_empty(row.get("collection")),
        "action":     row.get("action", ""),
        "session_id": _str_or_empty(row.get("session_id")),
        "timestamp":  row.get("timestamp", ""),
    }, f"relevance_log#{row.get('id', '?')}"


def _build_search(row: dict[str, Any], _collector: Any) -> tuple[dict[str, Any], str]:
    return {
        "ts":           row.get("ts", ""),
        "query_hash":   row.get("query_hash", ""),
        "collection":   row.get("collection", ""),
        "raw_count":    _int_or_zero(row.get("raw_count")),
        "kept_count":   _int_or_zero(row.get("kept_count")),
        "top_distance": row.get("top_distance"),
        "threshold":    row.get("threshold"),
    }, f"search_telemetry#{row.get('id', '?')}"


def _build_tier(row: dict[str, Any], _collector: Any) -> tuple[dict[str, Any], str]:
    return {
        "session_id":   _str_or_empty(row.get("session_id")),
        "ts":           row.get("ts", ""),
        "tool":         _str_or_empty(row.get("tool")),
        "tier":         _str_or_empty(row.get("tier")),
        "agent":        _nullable_str(row.get("agent")),
        "project":      _nullable_str(row.get("project")),
        "target_title": _nullable_str(row.get("target_title")),
    }, f"tier_writes#{row.get('id', '?')}"


def _build_nx(row: dict[str, Any], collector: Any, valid_plan_ids: set[int]) -> tuple[dict[str, Any], str]:
    plan_id = _nullable_int(row.get("plan_id"))
    # RDR-153 soft-dangler policy: plan_id has NO enforced FK — a row whose plan
    # was deleted still imports (preserving event history); an advisory records
    # the dangling reference.
    if collector is not None and plan_id is not None and plan_id not in valid_plan_ids:
        collector.record(
            "telemetry", "nx_answer_runs",
            issue_class="soft_dangler",
            constraint="nx_answer_runs.plan_id -> plans.id (not enforced)",
            reason="plan deleted; row imports with dangling reference; "
                   "sample ids are <run_id>:<plan_id>",
            action="flagged",
            sample_id=f"{row.get('id')}:{plan_id}",
        )
    return {
        "question":           row.get("question", ""),
        "plan_id":            plan_id,
        "matched_confidence": row.get("matched_confidence"),
        "step_count":         _int_or_zero(row.get("step_count")),
        "final_text":         _str_or_empty(row.get("final_text")),
        "cost_usd":           row.get("cost_usd"),
        "duration_ms":        _int_or_zero(row.get("duration_ms")),
        "created_at":         row.get("created_at", ""),
    }, f"nx_answer_runs#{row.get('id', '?')}"


def _build_hook(row: dict[str, Any], collector: Any) -> tuple[dict[str, Any], str]:
    raw_ts = row.get("occurred_at", "")
    try:
        occurred_at, normalized = _normalize_timestamp(raw_ts)
    except ValueError as exc:
        raise _SkipRow(
            f"unparseable timestamp (not ISO-8601-coercible): {raw_ts[:40]!r}",
            str(row.get("id", "?")),
            issue_class="format_anomaly",
        ) from exc
    if normalized and collector is not None:
        collector.record(
            "telemetry", "hook_failures",
            issue_class="format_anomaly",
            constraint="hook_failures.occurred_at",
            reason="space-form timestamp normalized to ISO-8601 T form",
            action="handled",
            sample_id=str(row.get("id", "?")),
        )
    return {
        "doc_id":        _str_or_empty(row.get("doc_id")),
        "collection":    _str_or_empty(row.get("collection")),
        "hook_name":     row.get("hook_name", ""),
        "error":         _str_or_empty(row.get("error")),
        "occurred_at":   occurred_at,
        "batch_doc_ids": _nullable_str(row.get("batch_doc_ids")),
        "is_batch":      bool(row.get("is_batch", False)),
        "chain":         _nullable_str(row.get("chain")),
    }, f"hook_failures#{row.get('id', '?')}"


def _build_frecency(row: dict[str, Any], _collector: Any) -> tuple[dict[str, Any], str]:
    return {
        "chunk_id":       row.get("chunk_id", ""),
        "embedded_at":    _nullable_str(row.get("embedded_at")),
        "ttl_days":       _int_or_zero(row.get("ttl_days")),
        "frecency_score": _float_or_zero(row.get("frecency_score")),
        "miss_count":     _int_or_zero(row.get("miss_count")),
        "last_hit_at":    _nullable_str(row.get("last_hit_at")),
    }, f"frecency#{row.get('chunk_id', '?')}"


def _migrate_relevance_log(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    total = _count_rows(conn, "relevance_log")
    rows = _iter_rows(conn, "relevance_log", _RELEVANCE_LOG_COLS, page_size=_READ_PAGE)
    return _run_batched(store, "relevance_log", rows, _build_relevance,
                        collector=collector, batch_log_every=batch_log_every, total=total,
                        breaker=breaker)


def _migrate_search_telemetry(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    total = _count_rows(conn, "search_telemetry")
    rows = _iter_rows(conn, "search_telemetry", _SEARCH_TELEMETRY_COLS, page_size=_READ_PAGE)
    return _run_batched(store, "search_telemetry", rows, _build_search,
                        collector=collector, batch_log_every=batch_log_every, total=total,
                        breaker=breaker)


def _migrate_tier_writes(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    total = _count_rows(conn, "tier_writes")
    rows = _iter_rows(conn, "tier_writes", _TIER_WRITES_COLS, page_size=_READ_PAGE)
    return _run_batched(store, "tier_writes", rows, _build_tier,
                        collector=collector, batch_log_every=batch_log_every, total=total,
                        breaker=breaker)


def _migrate_nx_answer_runs(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    total = _count_rows(conn, "nx_answer_runs")
    rows = _iter_rows(conn, "nx_answer_runs", _NX_ANSWER_RUNS_COLS, page_size=_READ_PAGE)
    valid_plan_ids: set[int] = set()
    try:
        valid_plan_ids = {int(r[0]) for r in conn.execute("SELECT id FROM plans").fetchall()}
    except sqlite3.OperationalError:
        pass  # no plans table in source — every plan_id is then a dangler
    return _run_batched(store, "nx_answer_runs", rows,
                        lambda row, c: _build_nx(row, c, valid_plan_ids),
                        collector=collector, batch_log_every=batch_log_every, total=total,
                        breaker=breaker)


def _normalize_timestamp(raw: str) -> tuple[str, bool]:
    """RDR-153 format-anomaly policy: parse lenient, emit canonical
    OFFSET-QUALIFIED ISO-8601.

    Returns ``(canonical, was_normalized)``. The production anomaly is the
    space-form NAIVE ``2026-04-23 10:47:54`` (234/234 hook_failures rows).
    The Java import path (``TelemetryRepository.parseTsStrict``) uses
    ``OffsetDateTime.parse`` which REQUIRES an offset — this is the actual
    root cause of nexus-9sjn3 (hook_failures imported 0/234): the rows fail
    twice over, space separator AND missing offset. Naive timestamps are
    treated as UTC (SQLite ``CURRENT_TIMESTAMP``/``datetime('now')`` is
    UTC), so canonical form is ``...T...+00:00``.

    Raises ``ValueError`` for unparseable input — the caller records a
    ``failed`` issue (fail only if unparseable, never silently drop).

    NOTE: any non-canonical-but-parseable form counts as normalized —
    a 'Z' suffix becomes '+00:00', a naive T-form gains '+00:00'. Benign
    variants inflating 'handled' is acceptable and honest (the stored
    value DID change).
    """
    from datetime import UTC as _UTC, datetime as _dt  # noqa: PLC0415 — deferred import — optional/heavy dependency, branch-local

    parsed = _dt.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_UTC)
    canonical = parsed.isoformat()
    return canonical, canonical != raw


def _migrate_hook_failures(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    total = _count_rows(conn, "hook_failures")
    rows = _iter_rows(conn, "hook_failures", _HOOK_FAILURES_COLS, page_size=_READ_PAGE)
    return _run_batched(store, "hook_failures", rows, _build_hook,
                        collector=collector, batch_log_every=batch_log_every, total=total,
                        breaker=breaker)


def _migrate_frecency(
    conn: sqlite3.Connection, store: Any, batch_log_every: int,
    collector: Any = None, breaker: EtlCircuitBreaker | None = None,
) -> dict[str, int]:
    total = _count_rows(conn, "frecency")
    rows = _iter_rows(conn, "frecency", _FRECENCY_COLS, page_size=_READ_PAGE)
    return _run_batched(store, "frecency", rows, _build_frecency,
                        collector=collector, batch_log_every=batch_log_every, total=total,
                        breaker=breaker)


# ── verify-fill P3b (nexus-s3dd4.14): conflict-key extraction + materialized
#    per-table read, for the inner identity-diff + fill loop ─────────────────
#
# HttpTelemetryStore.probe_ids (RDR-178 wave-2 P1) is a genuine membership
# probe for ALL SIX tables — the constants/helpers below are the single
# source of truth for (a) which columns compose each table's conflict key,
# in probe_ids' wire order, and (b) reading + TRANSFORMING a whole table's
# rows for the diff (reusing the exact _build_* transform _run_batched uses,
# so a filled row is byte-for-byte what a full ETL would have sent).

#: Conflict-key column order per table, IN THE SAME ORDER
#: ``HttpTelemetryStore.probe_ids`` expects — verbatim transcription of
#: ``TelemetryRepository.probeIds``'s javadoc (itself transcribed from the
#: UNIQUE indexes / PK in ``telemetry-001-baseline.xml``; see that method's
#: docstring, the single source of truth this mirrors). NOTE
#: ``relevance_log``'s key does NOT include ``collection`` (nexus-s3dd4.14
#: R1 note 1) even though ``collection`` is a real column on the row.
CONFLICT_KEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "relevance_log":    ("query", "chunk_id", "action", "session_id", "timestamp"),
    "search_telemetry": ("ts", "query_hash", "collection"),
    "tier_writes":      ("session_id", "ts", "tool", "tier"),
    "nx_answer_runs":   ("question", "created_at"),
    "hook_failures":    ("doc_id", "hook_name", "occurred_at"),
    "frecency":         ("chunk_id",),
}


def conflict_key(table: str, row: dict[str, Any]) -> tuple[Any, ...]:
    """Extract *row*'s conflict-key TUPLE for *table*, in
    ``CONFLICT_KEY_COLUMNS`` / ``probe_ids`` wire order.

    *row* MUST already be the TRANSFORMED dict shape (e.g. from
    :func:`read_rows_for_fill` / the ``_build_*`` helpers — the same shape
    ``import_rows_batch`` sends), not a raw SQLite row: ``session_id`` is
    only coalesced to ``""`` (never ``None``) after transform, and
    ``hook_failures.occurred_at`` is only in canonical ISO-8601 form after
    ``_build_hook``'s ``_normalize_timestamp`` pass.
    """
    return tuple(row[c] for c in CONFLICT_KEY_COLUMNS[table])


#: table -> (SQLite column tuple, per-row build function) for the five
#: tables whose ``_build_*`` signature is ``(row, collector) -> (dict, str)``.
#: ``nx_answer_runs`` needs an extra ``valid_plan_ids`` argument (its build
#: function differs in signature) and is handled specially in
#: :func:`read_rows_for_fill`.
_SIMPLE_BUILDERS: dict[str, tuple[tuple[str, ...], Any]] = {
    "relevance_log":    (_RELEVANCE_LOG_COLS, _build_relevance),
    "search_telemetry": (_SEARCH_TELEMETRY_COLS, _build_search),
    "tier_writes":      (_TIER_WRITES_COLS, _build_tier),
    "hook_failures":    (_HOOK_FAILURES_COLS, _build_hook),
    "frecency":         (_FRECENCY_COLS, _build_frecency),
}


def read_rows_for_fill(
    conn: sqlite3.Connection, table: str, *, collector: Any = None,
) -> list[dict[str, Any]]:
    """Read + TRANSFORM ALL of *table*'s rows for the verify-fill inner
    loop (P3b) — the SAME per-row ``_build_*`` transform ``_migrate_all``
    uses, so a filled row is byte-for-byte identical to what a full ETL
    would have sent (the conflict-key diff and the eventual
    ``import_rows_batch`` payload must both see the transformed shape, not
    the raw SQLite row).

    Unlike ``_run_batched`` (which STREAMS rows through ``_iter_rows`` and
    ships each batch immediately), this MATERIALIZES the whole table — the
    verify-fill diff needs the complete source key set to compute against
    ``HttpTelemetryStore.probe_ids`` before any row is sent. Mirrors
    chash's ``_read_chash_rows_by_collection`` (same materialize-then-diff
    shape).

    A row that fails transform (currently only ``hook_failures`` on an
    unparseable timestamp) is SKIPPED and recorded via *collector* — same
    ``_SkipRow`` policy ``_run_batched`` applies, never a crash.
    """
    if table == "nx_answer_runs":
        cols = _NX_ANSWER_RUNS_COLS
        valid_plan_ids: set[int] = set()
        try:
            valid_plan_ids = {
                int(r[0]) for r in conn.execute("SELECT id FROM plans").fetchall()
            }
        except sqlite3.OperationalError:
            pass  # no plans table in source -- every plan_id is then a dangler

        def build(row: dict[str, Any], c: Any) -> tuple[dict[str, Any], str]:
            return _build_nx(row, c, valid_plan_ids)
    else:
        cols, build = _SIMPLE_BUILDERS[table]

    rows: list[dict[str, Any]] = []
    for raw in _iter_rows(conn, table, cols, page_size=_READ_PAGE):
        try:
            row_dict, _sample_id = build(raw, collector)
        except _SkipRow as skip:
            _log.error(f"telemetry_etl.{table}.row_failed", error=skip.reason)
            if collector is not None:
                collector.record(
                    "telemetry", table,
                    issue_class=skip.issue_class, constraint=table,
                    reason=skip.reason, action="failed", sample_id=skip.sample_id,
                )
            continue
        rows.append(row_dict)
    return rows
