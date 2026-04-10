# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Telemetry — search relevance log (RDR-063 Phase 1).

Owns the ``relevance_log`` table and all methods that query or mutate
it. Extracted from the monolithic ``T2Database`` in RDR-063 Phase 1
step 6 (bead ``nexus-yjww``).

This is the last domain extraction in Phase 1; after this commit the
facade only contains the connection lifecycle, the migration guard,
the cross-domain ``expire()`` composition, and the per-domain delegate
methods. Domain DDL is fully owned by the four store modules.

The ``relevance_log`` table tracks ``(query, chunk_id, action,
session_id, collection, timestamp)`` rows whenever an MCP tool acts
on search results (``store_put``, ``catalog_link``). Used by the
RDR-061 retrieval feedback loop and future re-ranking work. The table
is intentionally append-heavy with periodic time-based purge — the
write profile is a strong candidate for the dedicated connection that
Phase 2 (``nexus-3d3k``) will give it.

Schema-creation history: prior to Phase 1 step 6, the relevance_log
table did not have a base entry in T2Database's _SCHEMA_SQL — it was
created on first construction by ``_migrate_relevance_log_if_needed``,
which was a one-shot "create if missing" disguised as a migration
because relevance_log was added in a later release than memory/plans.
This module replaces that pattern with a normal
``init_schema_unlocked`` (CREATE TABLE IF NOT EXISTS …) called from
the facade's _init_schema sequence. Behavior is identical for both
fresh and legacy databases — IF NOT EXISTS makes both cases no-op
on subsequent construction.

Lock convention (mirrors the other domain stores):
  * Public methods (``log_relevance``, ``log_relevance_batch``,
    ``get_relevance_log``, ``expire_relevance_log``) acquire
    ``self._lock`` themselves.
  * ``init_schema_unlocked`` is lock-naive — caller holds the lock.

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

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from nexus.db.t2._connection import SharedConnection

_log = structlog.get_logger()


# Per-domain migration guard placeholder — see memory_store.py.
_migrated_paths: set[str] = set()


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

    def __init__(self, shared: "SharedConnection") -> None:
        self._shared = shared
        # Legacy aliases — Phase 1 stores all share the same lock/conn,
        # so any caller that reached through .conn / ._lock continues to
        # work. Phase 2 will give each store its own pair.
        self._lock = shared.lock
        self.conn = shared.conn

    # ── Schema ────────────────────────────────────────────────────────────

    def init_schema_unlocked(self) -> None:
        """Create the ``relevance_log`` table + indexes. Caller holds the lock."""
        self.conn.executescript(_TELEMETRY_SCHEMA_SQL)

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
