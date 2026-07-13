# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Telemetry — search relevance log (RDR-063).

Owns the ``relevance_log`` table and all methods that query or mutate
it. Extracted from the monolithic ``T2Database`` in RDR-063 Phase 1
step 6 (bead ``nexus-yjww``); promoted to own its dedicated
``sqlite3.Connection`` and ``threading.Lock`` in Phase 2 (bead
``nexus-3d3k``).

The ``relevance_log`` table tracks ``(query, chunk_id, action,
session_id, collection, timestamp)`` rows whenever an MCP tool acts
on search results (``store_put``, ``catalog_link``). Used by the
RDR-061 retrieval feedback loop and future re-ranking work. The table
is intentionally append-heavy with periodic time-based purge — the
write profile is the strongest candidate for the dedicated connection
that Phase 2 gives it. High-frequency MCP hook writes no longer block
agent-paced memory reads.

Schema-creation history: prior to Phase 1 step 6, the relevance_log
table did not have a base entry in T2Database's ``_SCHEMA_SQL`` — it
was created on first construction by
``_migrate_relevance_log_if_needed``, a one-shot "create if missing"
disguised as a migration because relevance_log was added in a later
release than memory/plans. This module replaced that pattern with a
normal ``CREATE TABLE IF NOT EXISTS`` in ``_init_schema``. Behavior is
identical for fresh and legacy databases — IF NOT EXISTS makes both
cases no-op on subsequent construction. Telemetry therefore has no
migration block and no ``_migrated_lock``.

Lock convention (mirrors the other domain stores):
  * Public methods (``log_relevance``, ``log_relevance_batch``,
    ``get_relevance_log``, ``expire_relevance_log``) acquire
    ``self._lock`` themselves.
  * ``_init_schema`` runs under ``self._lock`` during ``__init__``.

Facade contract preserved (``test_structlog_events.py`` pin):
  * The facade keeps ``expire_relevance_log`` as a method that
    delegates to ``self.telemetry.expire_relevance_log(...)``. The
    facade's ``expire()`` calls ``self.expire_relevance_log(...)``
    via its own method, NOT through ``self.telemetry`` directly.
    This preserves the monkeypatch shape used by
    ``test_expire_complete_includes_error_when_log_purge_fails``,
    where the test does ``monkeypatch.setattr(t2,
    "expire_relevance_log", boom)`` to inject a fault.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2._tuning import SERVING_BUSY_TIMEOUT_MS

_log = structlog.get_logger()


# ── Schema SQL ────────────────────────────────────────────────────────────────

_TELEMETRY_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS relevance_log (
    id         INTEGER PRIMARY KEY,
    query      TEXT NOT NULL,
    chunk_id   TEXT NOT NULL,
    collection TEXT,
    action     TEXT NOT NULL,
    session_id TEXT,
    timestamp  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_relevance_log_query
    ON relevance_log(query);
CREATE INDEX IF NOT EXISTS idx_relevance_log_chunk
    ON relevance_log(chunk_id);
CREATE INDEX IF NOT EXISTS idx_relevance_log_session
    ON relevance_log(session_id);

-- RDR-087 Phase 2: per-call threshold-filter telemetry.
-- Schema duplicated from migrations.migrate_search_telemetry so that
-- fresh T2Database constructions get the table even before apply_pending
-- runs. IF NOT EXISTS makes construction idempotent with the migration.
-- ``kept_count`` matches the RDR-087 spec; 4.6.0 shipped this column as
-- ``dropped_count`` — the 4.6.1 rename migration upgrades existing DBs.
CREATE TABLE IF NOT EXISTS search_telemetry (
    ts             TEXT    NOT NULL,
    query_hash     TEXT    NOT NULL,
    collection     TEXT    NOT NULL,
    raw_count      INTEGER NOT NULL,
    kept_count     INTEGER NOT NULL,
    top_distance   REAL,
    threshold      REAL,
    PRIMARY KEY (ts, query_hash, collection)
);

CREATE INDEX IF NOT EXISTS idx_search_tel_collection
    ON search_telemetry(collection);
CREATE INDEX IF NOT EXISTS idx_search_tel_ts
    ON search_telemetry(ts);

"""

_RELEVANCE_LOG_COLUMNS = (
    "id",
    "query",
    "chunk_id",
    "collection",
    "action",
    "session_id",
    "timestamp",
)


# ── Telemetry ─────────────────────────────────────────────────────────────────


class Telemetry:
    """Owns the ``relevance_log`` table.

    See module docstring for the lock convention and the facade
    contract preservation.
    """

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(f"PRAGMA busy_timeout={SERVING_BUSY_TIMEOUT_MS}")
        self._init_schema()

    def close(self) -> None:
        """Close the dedicated connection (idempotent under ``self._lock``)."""
        with self._lock:
            self.conn.close()

    # ── Schema ────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        """Create the ``relevance_log`` table + indexes under ``self._lock``.

        Telemetry has no migrations — the table is pure ``CREATE IF NOT
        EXISTS`` so repeated construction is idempotent without needing
        a migration guard.
        """
        with self._lock:
            self.conn.executescript(_TELEMETRY_SCHEMA_SQL)
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.commit()

    # ── Public API ────────────────────────────────────────────────────────

    def log_relevance(
        self,
        query: str,
        chunk_id: str,
        action: str,
        session_id: str = "",
        collection: str = "",
    ) -> int:
        """Record a (query, chunk_id, action) triple in the relevance log.

        Called by MCP tools when an agent acts on search results (store_put,
        catalog_link). Returns the new row id. Prefer ``log_relevance_batch``
        when writing multiple rows — it uses a single transaction.
        """
        now = datetime.now(UTC).isoformat()
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO relevance_log (query, chunk_id, collection, action, session_id, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (query, chunk_id, collection, action, session_id, now),
            )
            self.conn.commit()
            return cur.lastrowid

    def record_tier_write(
        self,
        *,
        session_id: str,
        ts: str,
        tool: str,
        tier: str,
        agent: str | None = None,
        project: str | None = None,
        target_title: str | None = None,
    ) -> None:
        """Append one row to ``tier_writes`` (tier-discipline audit).

        nexus-pyzk7: the canonical store owns the INSERT so the MCP consumers
        call ``db.telemetry.record_tier_write(...)`` instead of reaching for a
        raw ``.conn`` (which a service-backed store does not have).
        """
        from nexus.db.migrations import migrate_tier_writes  # noqa: PLC0415 — circular-dep avoidance (nexus.db.migrations)
        with self._lock:
            migrate_tier_writes(self.conn)
            self.conn.execute(
                "INSERT INTO tier_writes "
                "(session_id, ts, tool, tier, agent, project, target_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, ts, tool, tier, agent, project, target_title),
            )
            self.conn.commit()

    def record_consent(
        self,
        *,
        scope: str,
        ts: str,
        granted: bool,
    ) -> None:
        """Append one row to ``claude_assisted_remediation_consents`` (RDR-182
        P1.2, nexus-ykzbj.6).

        Consent AUDIT for the opt-in ``claude_assisted_remediation.enabled``
        flag: a grant OR a revoke is recorded as its own row (``granted``
        distinguishes them) so the flag's revocability is reflected in the
        audit trail, not collapsed by an upsert. ``ts`` is caller-supplied —
        this method never reads the wall clock.
        """
        from nexus.db.migrations import (  # noqa: PLC0415 — circular-dep avoidance (nexus.db.migrations)
            migrate_claude_assisted_remediation_consents,
        )
        with self._lock:
            migrate_claude_assisted_remediation_consents(self.conn)
            self.conn.execute(
                "INSERT INTO claude_assisted_remediation_consents "
                "(scope, ts, granted) VALUES (?, ?, ?)",
                (scope, ts, 1 if granted else 0),
            )
            self.conn.commit()

    def list_consents(self) -> list[dict]:
        """Read the consent-audit trail (RDR-182 read surface, nexus-ykzbj.15).

        Rows in insertion order — grants AND revokes, append-only, so the
        history of the durable flag and every per-invocation release is
        reconstructible. Returns ``[{scope, ts, granted}, ...]``.
        """
        from nexus.db.migrations import (  # noqa: PLC0415 — circular-dep avoidance (nexus.db.migrations)
            migrate_claude_assisted_remediation_consents,
        )
        with self._lock:
            migrate_claude_assisted_remediation_consents(self.conn)
            rows = self.conn.execute(
                "SELECT scope, ts, granted "
                "FROM claude_assisted_remediation_consents ORDER BY id"
            ).fetchall()
        return [
            {"scope": r[0], "ts": r[1], "granted": bool(r[2])} for r in rows
        ]

    def record_nx_answer_run(
        self,
        *,
        question: str,
        plan_id: int | None,
        matched_confidence: float | None,
        step_count: int,
        final_text: str,
        cost_usd: float,
        duration_ms: int,
    ) -> None:
        """Append one row to ``nx_answer_runs`` (RDR-080 run metrics).

        nexus-pyzk7: consumer redaction (trace=False) is applied by the caller
        before invoking this; the store just persists the given values.
        """
        from nexus.db.migrations import migrate_nx_answer_runs  # noqa: PLC0415 — circular-dep avoidance (nexus.db.migrations)
        with self._lock:
            migrate_nx_answer_runs(self.conn)
            self.conn.execute(
                "INSERT INTO nx_answer_runs "
                "(question, plan_id, matched_confidence, step_count, "
                "final_text, cost_usd, duration_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (question, plan_id, matched_confidence, step_count,
                 final_text, cost_usd, duration_ms),
            )
            self.conn.commit()

    def record_hook_failure(
        self,
        *,
        doc_id: str,
        collection: str,
        hook_name: str,
        error: str,
        chain: str,
        batch_doc_ids: str | None = None,
        is_batch: bool = False,
        occurred_at: str | None = None,
    ) -> None:
        """Append one row to ``hook_failures`` (post-store hook failure audit).

        nexus-9613q.3: the canonical store owns the INSERT so hook_registry
        calls ``db.telemetry.record_hook_failure(...)`` instead of reaching a
        raw ``.conn`` — which a service-backed store lacks, so every row was
        silently dropped in service mode (the silent-loss class nexus-pyzk7
        closed for tier_writes). Ensures the full RDR-095/RDR-089 column set
        exists, then writes a single complete row (no per-caller column
        fallback ladder — the migration guarantees the columns).
        """
        from nexus.db.migrations import (  # noqa: PLC0415 — circular-dep avoidance (nexus.db.migrations)
            migrate_hook_failures,
            migrate_hook_failures_batch_columns,
            migrate_hook_failures_chain_column,
        )
        with self._lock:
            migrate_hook_failures(self.conn)
            migrate_hook_failures_batch_columns(self.conn)
            migrate_hook_failures_chain_column(self.conn)
            cols = ["doc_id", "collection", "hook_name", "error",
                    "batch_doc_ids", "is_batch", "chain"]
            vals: list[Any] = [doc_id, collection, hook_name, error,
                               batch_doc_ids, 1 if is_batch else 0, chain]
            # Always stamp occurred_at in ISO-8601 (T separator). When the caller
            # omits it, use isoformat() rather than letting the DDL DEFAULT
            # CURRENT_TIMESTAMP fill a space-separated value — the age reaper
            # (trim_hook_failures, nexus-7365x) compares occurred_at as TEXT
            # against an isoformat() cutoff, and a space (0x20) sorts before
            # 'T' (0x54), which would skew the cutoff-day boundary. Matches the
            # search_telemetry convention (ts is always isoformat()).
            cols.append("occurred_at")
            vals.append(
                occurred_at if occurred_at is not None
                else datetime.now(UTC).isoformat()
            )
            placeholders = ", ".join(["?"] * len(vals))
            self.conn.execute(
                f"INSERT INTO hook_failures ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
            self.conn.commit()

    def log_relevance_batch(
        self,
        rows: list[tuple[str, str, str, str, str]],
    ) -> int:
        """Insert multiple (query, chunk_id, collection, action, session_id) rows.

        Single transaction for all rows. Returns the number of rows inserted.
        """
        if not rows:
            return 0
        now = datetime.now(UTC).isoformat()
        params = [(*r, now) for r in rows]
        with self._lock:
            self.conn.executemany(
                "INSERT INTO relevance_log (query, chunk_id, collection, action, session_id, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                params,
            )
            self.conn.commit()
        return len(rows)

    def get_relevance_log(
        self,
        query: str = "",
        chunk_id: str = "",
        action: str = "",
        session_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Query the relevance log by filters. All filters optional.

        Returns rows as dicts ordered by most recent first.
        """
        conditions = ["1=1"]
        params: list = []
        if query:
            conditions.append("query = ?")
            params.append(query)
        if chunk_id:
            conditions.append("chunk_id = ?")
            params.append(chunk_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        sql = (
            "SELECT id, query, chunk_id, collection, action, session_id, timestamp "
            f"FROM relevance_log WHERE {' AND '.join(conditions)} "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(zip(_RELEVANCE_LOG_COLUMNS, row)) for row in rows]

    def expire_relevance_log(self, days: int = 90) -> int:
        """Delete relevance_log entries older than *days* days (RDR-061 E2).

        The relevance_log accumulates on every store_put/catalog_link.
        Without periodic purge it grows unboundedly. Default retention:
        90 days — enough for re-ranking signal, bounded for disk use.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM relevance_log WHERE timestamp < ?",
                (cutoff,),
            )
            self.conn.commit()
        return cur.rowcount

    # ── search_telemetry (RDR-087 Phase 2) ────────────────────────────────

    def log_search_batch(
        self,
        rows: list[tuple[str, str, str, int, int, float | None, float | None]],
    ) -> int:
        """Insert per-call threshold-filter telemetry in a single transaction.

        Row tuple layout: ``(ts, query_hash, collection, raw_count,
        kept_count, top_distance, threshold)``.

        Uses ``INSERT OR IGNORE`` on the composite PK so two writers
        racing within the same ISO-second emit exactly one row. Duplicate
        same-second calls are silently discarded — the retention-trim
        (Phase 2.4) will clean up stragglers.
        """
        if not rows:
            return 0
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO search_telemetry "
                "(ts, query_hash, collection, raw_count, kept_count, "
                "top_distance, threshold) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self.conn.commit()
        return len(rows)

    def query_collection_stats(
        self, collection: str, *, days: int = 30,
    ) -> dict[str, Any]:
        """Return retrieval-health stats for *collection* over the last *days*.

        Used by ``nx collection health`` (RDR-087 Phase 3.4). Returns a
        dict with keys:

        - ``row_count``            : rows in-window for this collection.
        - ``zero_hit_rate``        : ``kept_count == 0`` fraction, or
          ``None`` when ``row_count == 0``.
        - ``median_top_distance``  : median ``top_distance`` over rows
          with ``raw_count > 0``, or ``None`` when no such rows.

        Does not write. Uses the same 30-day window the CLI flag uses
        by default; *days* is overridable for testing / audits.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            row_count, zero_count = self.conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN kept_count = 0 THEN 1 ELSE 0 END) "
                "FROM search_telemetry WHERE collection = ? AND ts >= ?",
                (collection, cutoff),
            ).fetchone()
            distances = [
                r[0] for r in self.conn.execute(
                    "SELECT top_distance FROM search_telemetry "
                    "WHERE collection = ? AND ts >= ? "
                    "AND raw_count > 0 AND top_distance IS NOT NULL "
                    "ORDER BY top_distance",
                    (collection, cutoff),
                ).fetchall()
            ]
        zero_rate: float | None = None
        if row_count:
            zero_rate = (zero_count or 0) / row_count
        median: float | None = None
        n = len(distances)
        if n:
            if n % 2 == 1:
                median = distances[n // 2]
            else:
                median = (distances[n // 2 - 1] + distances[n // 2]) / 2
        return {
            "row_count": row_count or 0,
            "zero_hit_rate": zero_rate,
            "median_top_distance": median,
        }

    def trim_search_telemetry(self, days: int = 30) -> int:
        """Delete ``search_telemetry`` rows older than *days* days (Phase 2.4).

        Exposed via ``nx doctor --trim-telemetry [--days N]``. Default 30d
        balances an analytical window long enough to detect slow-burn
        silent-threshold-drop patterns against disk use. Safe on empty
        tables. Returns the number of rows deleted.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM search_telemetry WHERE ts < ?",
                (cutoff,),
            )
            self.conn.commit()
        return cur.rowcount

    def trim_hook_failures(self, days: int = 30) -> int:
        """Delete ``hook_failures`` rows older than *days* days (nexus-7365x).

        Audit-table TTL parity with :meth:`trim_search_telemetry`: hook_failures
        is a no-cascade audit table (RDR-164 P0) reaped by age. Filters on the
        ``occurred_at`` timestamp. Default 30d, matching the search-telemetry
        reaper. Safe on an empty or not-yet-migrated table. Returns rows deleted.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            # hook_failures is created by migration; tolerate its absence.
            exists = self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='hook_failures'"
            ).fetchone()
            if not exists:
                return 0
            cur = self.conn.execute(
                "DELETE FROM hook_failures WHERE occurred_at < ?",
                (cutoff,),
            )
            self.conn.commit()
        return cur.rowcount

    def rename_collection(self, *, old: str, new: str) -> dict[str, int]:
        """Re-point ``collection`` columns from ``old`` to ``new`` in all
        telemetry tables that carry a collection name.

        nexus-nhyh / K9: ``search_telemetry.collection`` and
        ``hook_failures.collection`` (if the table exists) are both
        indexed collection-scoped lookup columns. A collection rename
        that skips these tables leaves telemetry orphaned under the old
        name, making ``nx collection health`` and ``nx doctor hooks``
        query the wrong bucket.

        Returns a dict with keys ``search_telemetry`` and
        ``hook_failures`` (each value is the row count updated). The
        ``hook_failures`` key is 0 and not an error when the table does
        not yet exist (pre-migration databases).
        """
        counts: dict[str, int] = {"search_telemetry": 0, "hook_failures": 0}
        with self._lock:
            cur = self.conn.execute(
                "UPDATE search_telemetry SET collection = ? WHERE collection = ?",
                (new, old),
            )
            counts["search_telemetry"] = cur.rowcount

            # hook_failures may not exist on pre-migration databases.
            table_exists = self.conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='hook_failures'"
            ).fetchone()[0]
            if table_exists:
                cur = self.conn.execute(
                    "UPDATE hook_failures SET collection = ? WHERE collection = ?",
                    (new, old),
                )
                counts["hook_failures"] = cur.rowcount

            self.conn.commit()
        return counts
