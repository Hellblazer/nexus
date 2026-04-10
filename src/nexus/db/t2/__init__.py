# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 SQLite memory bank — facade over the domain stores.

Phase 1 of RDR-063 extracts the monolithic ``T2Database`` into domain
stores (``MemoryStore``, ``PlanLibrary``, ``CatalogTaxonomy``,
``Telemetry``). This module is the facade: it opens the single
``sqlite3.Connection``, wraps it in a :class:`SharedConnection`, and
instantiates the domain stores around it. All legacy public API calls
(``put``, ``search``, ``save_plan``, ``log_relevance``, ``expire``, …)
are preserved as thin delegating methods so no caller needs to change.

Step 2 (bead ``nexus-vx3c``) moved memory-domain state and methods into
:mod:`nexus.db.t2.memory_store`. Plan, taxonomy, and telemetry code
still lives here and will move in later Phase 1 steps.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2._connection import SharedConnection
from nexus.db.t2.memory_store import (
    AccessPolicy,
    MemoryStore,
    _sanitize_fts5,  # re-exported for nexus.catalog.catalog_db
)

_log = structlog.get_logger()

# Re-export for backward compatibility — ``catalog/catalog_db.py`` and
# ``tests/test_t2.py`` still ``from nexus.db.t2 import _sanitize_fts5``.
__all__ = ["AccessPolicy", "MemoryStore", "SharedConnection", "T2Database", "_sanitize_fts5"]


# ── Residual schema (plans, topics, topic_assignments) ───────────────────────
# Memory schema lives in memory_store._MEMORY_SCHEMA_SQL.
# Plan/taxonomy schema will migrate out in the next beads.
_RESIDUAL_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY,
    project    TEXT NOT NULL DEFAULT '',
    query      TEXT NOT NULL,
    plan_json  TEXT NOT NULL,
    outcome    TEXT DEFAULT 'success',
    tags       TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    ttl        INTEGER
);

CREATE VIRTUAL TABLE IF NOT EXISTS plans_fts USING fts5(
    query,
    tags,
    project,
    content=plans,
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS plans_ai AFTER INSERT ON plans BEGIN
    INSERT INTO plans_fts(rowid, query, tags, project) VALUES (new.id, new.query, new.tags, new.project);
END;

CREATE TRIGGER IF NOT EXISTS plans_ad AFTER DELETE ON plans BEGIN
    INSERT INTO plans_fts(plans_fts, rowid, query, tags, project)
        VALUES ('delete', old.id, old.query, old.tags, old.project);
END;

CREATE TRIGGER IF NOT EXISTS plans_au AFTER UPDATE ON plans BEGIN
    INSERT INTO plans_fts(plans_fts, rowid, query, tags, project)
        VALUES ('delete', old.id, old.query, old.tags, old.project);
    INSERT INTO plans_fts(rowid, query, tags, project) VALUES (new.id, new.query, new.tags, new.project);
END;

CREATE TABLE IF NOT EXISTS topics (
    id            INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    parent_id     INTEGER REFERENCES topics(id),
    collection    TEXT NOT NULL,
    centroid_hash TEXT,
    doc_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topic_assignments (
    doc_id    TEXT NOT NULL,
    topic_id  INTEGER NOT NULL REFERENCES topics(id),
    PRIMARY KEY (doc_id, topic_id)
);
"""

_PLAN_COLUMNS = ("id", "project", "query", "plan_json", "outcome", "tags", "created_at", "ttl")


# ── Per-process migration guard ──────────────────────────────────────────────
# Migrations only need to run once per DB path per process. The MCP server
# opens a fresh T2Database on every tool call; without this guard, each call
# probes all 6 migrations.
#
# This lives at the facade level in Phase 1; RDR-063 §Open Question 3
# (per-domain guards) will split it in a later Phase 1 step. The existing
# regression tests ``test_migration_guard_concurrent_threads`` access this
# module attribute directly.
_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()


# ── Database facade ───────────────────────────────────────────────────────────


class T2Database:
    """T2 SQLite memory bank with FTS5 full-text search.

    Phase 1 facade: holds a single :class:`SharedConnection` and delegates
    memory-domain calls to :class:`MemoryStore`. Plan, taxonomy, and
    telemetry methods remain inlined until their extraction beads land.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        # Wrap the connection + lock in a SharedConnection for the domain
        # stores. Both self._lock / self.conn and the SharedConnection's
        # fields point at the same objects — Phase 2 will split them.
        self._shared = SharedConnection(conn=self.conn, lock=self._lock)
        self.memory: MemoryStore = MemoryStore(self._shared)
        # Canonicalize path for the migration guard key: /foo/./bar and
        # /foo/bar must hash to the same entry or the guard is bypassed.
        try:
            canonical_key = str(path.resolve())
        except OSError:
            canonical_key = str(path)
        self._init_schema(canonical_key)

    def _init_schema(self, path_key: str) -> None:
        with self._lock:
            # Note: executescript() implicitly COMMITs any open transaction.
            # Safe here because _init_schema runs only during __init__ with
            # no prior transaction. Memory DDL runs first (lock-naive helper
            # on MemoryStore), then the residual plans/topics DDL.
            self.memory.init_schema_unlocked()
            self.conn.executescript(_RESIDUAL_SCHEMA_SQL)
            self.conn.commit()
            result = self.conn.execute("PRAGMA journal_mode").fetchone()
            if result and result[0].lower() != "wal":
                _log.warning("WAL mode not available", actual_mode=result[0])
            # Migration guard: hold _migrated_lock across the full check-run-add
            # sequence so two concurrent T2Database constructors on the same path
            # cannot both enter the migration functions (ALTER TABLE ADD COLUMN
            # is NOT idempotent — double-application raises OperationalError).
            with _migrated_lock:
                if path_key in _migrated_paths:
                    return
                self.memory._migrate_fts_if_needed()
                self._migrate_plans_if_needed()
                self._migrate_plans_ttl_if_needed()
                self.memory._migrate_access_tracking_if_needed()
                self._migrate_topics_if_needed()
                self._migrate_relevance_log_if_needed()
                _migrated_paths.add(path_key)

    def _migrate_plans_if_needed(self) -> None:
        """Add 'project' column to plans table if missing (v2.8.0 schema change).

        Safe to call multiple times — no-op when 'project' is already present
        or when the plans table doesn't exist yet.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plans'"
        ).fetchone()
        if row is None or "project" in row[0]:
            return

        _log.info("Migrating plans table to add project column")
        self.conn.execute("ALTER TABLE plans ADD COLUMN project TEXT NOT NULL DEFAULT ''")
        # Recreate FTS + triggers with project column
        self.conn.executescript("""\
            DROP TRIGGER IF EXISTS plans_ai;
            DROP TRIGGER IF EXISTS plans_ad;
            DROP TRIGGER IF EXISTS plans_au;
            DROP TABLE  IF EXISTS plans_fts;

            CREATE VIRTUAL TABLE IF NOT EXISTS plans_fts USING fts5(
                query, tags, project, content=plans, content_rowid='id'
            );

            CREATE TRIGGER IF NOT EXISTS plans_ai AFTER INSERT ON plans BEGIN
                INSERT INTO plans_fts(rowid, query, tags, project) VALUES (new.id, new.query, new.tags, new.project);
            END;
            CREATE TRIGGER IF NOT EXISTS plans_ad AFTER DELETE ON plans BEGIN
                INSERT INTO plans_fts(plans_fts, rowid, query, tags, project)
                    VALUES ('delete', old.id, old.query, old.tags, old.project);
            END;
            CREATE TRIGGER IF NOT EXISTS plans_au AFTER UPDATE ON plans BEGIN
                INSERT INTO plans_fts(plans_fts, rowid, query, tags, project)
                    VALUES ('delete', old.id, old.query, old.tags, old.project);
                INSERT INTO plans_fts(rowid, query, tags, project) VALUES (new.id, new.query, new.tags, new.project);
            END;
        """)
        self.conn.execute("INSERT INTO plans_fts(plans_fts) VALUES('rebuild')")
        self.conn.commit()
        _log.info("plans migration complete (added project column)")

    def _migrate_plans_ttl_if_needed(self) -> None:
        """Add 'ttl' column to plans table if missing."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(plans)").fetchall()}
        if not cols or "ttl" in cols:
            return
        _log.info("Migrating plans table to add ttl column")
        self.conn.execute("ALTER TABLE plans ADD COLUMN ttl INTEGER")
        self.conn.commit()
        _log.info("plans ttl migration complete")

    def _migrate_topics_if_needed(self) -> None:
        """Add topics and topic_assignments tables if missing."""
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='topics'"
        ).fetchone()
        if row is not None:
            return
        _log.info("Migrating T2 schema to add topics tables")
        self.conn.executescript("""\
            CREATE TABLE IF NOT EXISTS topics (
                id            INTEGER PRIMARY KEY,
                label         TEXT NOT NULL,
                parent_id     INTEGER REFERENCES topics(id),
                collection    TEXT NOT NULL,
                centroid_hash TEXT,
                doc_count     INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topic_assignments (
                doc_id    TEXT NOT NULL,
                topic_id  INTEGER NOT NULL REFERENCES topics(id),
                PRIMARY KEY (doc_id, topic_id)
            );
        """)
        self.conn.commit()
        _log.info("topics migration complete")

    def _migrate_relevance_log_if_needed(self) -> None:
        """Add relevance_log table if missing (RDR-061 E2).

        Records (query, chunk_id, action) triples when an agent acts on
        search results within a session. Used by future re-ranking.
        """
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='relevance_log'"
        ).fetchone()
        if row is not None:
            return
        _log.info("Migrating T2 schema to add relevance_log table")
        self.conn.executescript("""\
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
        """)
        self.conn.commit()
        _log.info("relevance_log migration complete")

    def __enter__(self) -> "T2Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── Memory delegation (RDR-063 Phase 1 step 2) ────────────────────────────
    # Every memory-domain method delegates to self.memory. Signatures and
    # behavior are identical to the pre-split monolithic T2Database — these
    # delegates exist solely so callers that hold a T2Database (facade) do
    # not need to change their import or call sites.

    def put(
        self,
        project: str,
        title: str,
        content: str,
        tags: str = "",
        ttl: int | None = 30,
        agent: str | None = None,
        session: str | None = None,
    ) -> int:
        return self.memory.put(
            project=project,
            title=title,
            content=content,
            tags=tags,
            ttl=ttl,
            agent=agent,
            session=session,
        )

    def get(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> dict[str, Any] | None:
        return self.memory.get(project=project, title=title, id=id)

    def search(
        self,
        query: str,
        project: str | None = None,
        access: AccessPolicy = "track",
    ) -> list[dict[str, Any]]:
        return self.memory.search(query, project=project, access=access)

    def list_entries(
        self,
        project: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        return self.memory.list_entries(project=project, agent=agent)

    def get_projects_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        return self.memory.get_projects_with_prefix(prefix)

    def search_glob(self, query: str, project_glob: str) -> list[dict[str, Any]]:
        return self.memory.search_glob(query, project_glob)

    def search_by_tag(self, query: str, tag: str) -> list[dict[str, Any]]:
        return self.memory.search_by_tag(query, tag)

    def get_all(self, project: str) -> list[dict[str, Any]]:
        return self.memory.get_all(project)

    def delete(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> bool:
        return self.memory.delete(project=project, title=title, id=id)

    def find_overlapping_memories(
        self,
        project: str,
        min_similarity: float = 0.7,
        limit: int = 50,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return self.memory.find_overlapping_memories(
            project, min_similarity=min_similarity, limit=limit
        )

    def merge_memories(
        self,
        keep_id: int,
        delete_ids: list[int],
        merged_content: str,
    ) -> None:
        return self.memory.merge_memories(keep_id, delete_ids, merged_content)

    def flag_stale_memories(
        self,
        project: str,
        idle_days: int = 30,
    ) -> list[dict[str, Any]]:
        return self.memory.flag_stale_memories(project, idle_days=idle_days)

    # ── Plan Library ──────────────────────────────────────────────────────────

    def save_plan(
        self,
        query: str,
        plan_json: str,
        outcome: str = "success",
        tags: str = "",
        project: str = "",
        ttl: int | None = None,
    ) -> int:
        """Insert a plan record. Returns the new row ID.

        Args:
            ttl: Time-to-live in days. None means permanent (no expiry).
        """
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO plans (project, query, plan_json, outcome, tags, created_at, ttl)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (project, query, plan_json, outcome, tags, created_at, ttl),
            )
            self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def search_plans(self, query: str, limit: int = 5, project: str = "") -> list[dict[str, Any]]:
        """FTS5 search over plans (query + tags). Returns plans ordered by rank.

        Expired plans (ttl set and created_at + ttl days < now) are excluded.
        """
        safe = _sanitize_fts5(query)
        ttl_filter = (
            "AND (p.ttl IS NULL OR julianday('now') - julianday(p.created_at) <= p.ttl)"
        )
        if project:
            sql = f"""
                SELECT p.id, p.project, p.query, p.plan_json, p.outcome, p.tags, p.created_at, p.ttl
                FROM plans p
                JOIN plans_fts ON plans_fts.rowid = p.id
                WHERE plans_fts MATCH ? AND p.project = ?
                {ttl_filter}
                ORDER BY rank
                LIMIT ?
            """
            params: tuple = (safe, project, limit)
        else:
            sql = f"""
                SELECT p.id, p.project, p.query, p.plan_json, p.outcome, p.tags, p.created_at, p.ttl
                FROM plans p
                JOIN plans_fts ON plans_fts.rowid = p.id
                WHERE plans_fts MATCH ?
                {ttl_filter}
                ORDER BY rank
                LIMIT ?
            """
            params = (safe, limit)
        with self._lock:
            try:
                rows = self.conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
        return [dict(zip(_PLAN_COLUMNS, row)) for row in rows]

    def list_plans(self, limit: int = 20, project: str = "") -> list[dict[str, Any]]:
        """Return most recent non-expired plans ordered by created_at DESC."""
        ttl_filter = "(ttl IS NULL OR julianday('now') - julianday(created_at) <= ttl)"
        if project:
            sql = f"""
                SELECT id, project, query, plan_json, outcome, tags, created_at, ttl
                FROM plans WHERE project = ? AND {ttl_filter} ORDER BY created_at DESC LIMIT ?
            """
            params_l: tuple = (project, limit)
        else:
            sql = f"""
                SELECT id, project, query, plan_json, outcome, tags, created_at, ttl
                FROM plans WHERE {ttl_filter} ORDER BY created_at DESC LIMIT ?
            """
            params_l = (limit,)
        with self._lock:
            rows = self.conn.execute(sql, params_l).fetchall()
        return [dict(zip(_PLAN_COLUMNS, row)) for row in rows]

    # ── Relevance log (RDR-061 E2) ────────────────────────────────────────────

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
        cols = ("id", "query", "chunk_id", "collection", "action", "session_id", "timestamp")
        return [dict(zip(cols, row)) for row in rows]

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def expire(self, relevance_log_days: int = 90) -> int:
        """Delete TTL-expired entries using heat-weighted effective TTL.

        effective_ttl = base_ttl * (1 + log(access_count + 1))
        Highly accessed entries survive longer. Unaccessed entries (access_count=0)
        expire at base rate (log(1) = 0, so multiplier = 1).

        Also purges relevance_log rows older than ``relevance_log_days`` days
        (default 90) to prevent unbounded growth of the telemetry table.
        Return value counts only memory rows deleted. Log purge count and
        errors are surfaced via structured logs (``expire_complete`` /
        ``expire_relevance_log_failed``).
        """
        # Purge relevance_log (RDR-061 E2 telemetry retention).
        # Call outside the memory lock — expire_relevance_log acquires its own.
        log_deleted = 0
        log_error: str | None = None
        try:
            log_deleted = self.expire_relevance_log(days=relevance_log_days)
        except Exception as exc:
            log_error = type(exc).__name__
            _log.warning("expire_relevance_log_failed", exc_info=exc)
        expired_ids = self.memory.expire()
        extra: dict[str, Any] = {}
        if log_error is not None:
            extra["relevance_log_error"] = log_error
        _log.info(
            "expire_complete",
            memory_deleted=len(expired_ids),
            relevance_log_deleted=log_deleted,
            **extra,
        )
        return len(expired_ids)

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
