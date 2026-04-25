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

-- nexus-ntbg: hook duration_ms telemetry (Claude Code v2.1.119+).
-- One row per slow PostToolUse firing (above threshold) — written by
-- nx/hooks/scripts/hook_telemetry.py. Surfaced via ``nx doctor --check-hooks``.
-- Schema duplicated in migrations.migrate_hook_telemetry for upgrades.
CREATE TABLE IF NOT EXISTS hook_telemetry (
    ts               TEXT    NOT NULL,
    hook_event_name  TEXT    NOT NULL,
    tool_name        TEXT,
    duration_ms      INTEGER NOT NULL,
    session_id       TEXT,
    cwd              TEXT
);

CREATE INDEX IF NOT EXISTS idx_hook_tel_ts
    ON hook_telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_hook_tel_duration
    ON hook_telemetry(duration_ms);
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
        self.conn.execute("PRAGMA busy_timeout=5000")
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

    # ── hook_telemetry (nexus-ntbg) ─────────────────────────────────────

    def log_hook_event(
        self,
        hook_event_name: str,
        tool_name: str,
        duration_ms: int,
        session_id: str = "",
        cwd: str = "",
    ) -> None:
        """Record a slow hook firing (above caller's threshold).

        The hook script is responsible for thresholding — this method writes
        unconditionally. Append-only; periodic ``trim_hook_telemetry`` keeps
        the table bounded.
        """
        ts = datetime.now(UTC).isoformat()
        with self._lock:
            self.conn.execute(
                "INSERT INTO hook_telemetry "
                "(ts, hook_event_name, tool_name, duration_ms, session_id, cwd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (ts, hook_event_name, tool_name, duration_ms, session_id, cwd),
            )
            self.conn.commit()

    def query_slow_hooks(
        self,
        threshold_ms: int = 0,
        days: int = 7,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return slow-hook records, newest first.

        ``threshold_ms`` filters in addition to whatever the writer used —
        defaults to 0 (return everything stored). ``days`` bounds the lookback
        window. ``limit`` caps result count.
        """
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            rows = self.conn.execute(
                "SELECT ts, hook_event_name, tool_name, duration_ms, session_id, cwd "
                "FROM hook_telemetry "
                "WHERE ts >= ? AND duration_ms >= ? "
                "ORDER BY ts DESC LIMIT ?",
                (cutoff, threshold_ms, limit),
            ).fetchall()
        cols = ("ts", "hook_event_name", "tool_name", "duration_ms", "session_id", "cwd")
        return [dict(zip(cols, row)) for row in rows]

    def trim_hook_telemetry(self, days: int = 30) -> int:
        """Delete ``hook_telemetry`` rows older than *days* days."""
        if days < 1:
            raise ValueError(f"days must be >= 1; got {days}")
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM hook_telemetry WHERE ts < ?",
                (cutoff,),
            )
            self.conn.commit()
        return cur.rowcount

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
