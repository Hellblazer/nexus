# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
import math
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()

# ── FTS5 helpers ──────────────────────────────────────────────────────────────

# FTS5 special characters that cause OperationalError when unquoted:
#   -  (column filter: "col-name" → look in column "col" for "name")
#   :  (explicit column filter: "col:term")
#   (  )  ^  "  (grouping / phrase / boost — crash if unbalanced)
# Note: trailing * is a valid FTS5 prefix wildcard (e.g. auth*) — NOT included here.
_FTS5_SPECIAL = set('-:()"^~.*+/')


def _sanitize_fts5(query: str) -> str:
    """Escape a user-supplied query for FTS5 MATCH.

    Splits on whitespace and wraps any token that contains FTS5 special
    characters in double quotes, with internal double-quotes escaped as '""'.
    Plain tokens (letters and digits only) are passed through unchanged so
    that FTS5 AND-of-terms semantics and boolean operators (AND, OR, NOT)
    still work for well-formed queries.
    """
    tokens = query.split()
    parts: list[str] = []
    for token in tokens:
        if any(ch in _FTS5_SPECIAL for ch in token):
            escaped = token.replace('"', '""')
            parts.append(f'"{escaped}"')
        else:
            parts.append(token)
    return " ".join(parts)


# ── Schema SQL ────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS memory (
    id            INTEGER PRIMARY KEY,
    project       TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    session       TEXT,
    agent         TEXT,
    content       TEXT    NOT NULL,
    tags          TEXT,
    timestamp     TEXT    NOT NULL,
    ttl           INTEGER,
    access_count  INTEGER DEFAULT 0 NOT NULL,
    last_accessed TEXT    DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_project_title ON memory(project, title);
CREATE INDEX        IF NOT EXISTS idx_memory_project       ON memory(project);
CREATE INDEX        IF NOT EXISTS idx_memory_agent         ON memory(agent);
CREATE INDEX        IF NOT EXISTS idx_memory_timestamp     ON memory(timestamp);
CREATE INDEX        IF NOT EXISTS idx_memory_ttl_timestamp ON memory(ttl, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title,
    content,
    tags,
    content='memory',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
        VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
        VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO memory_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;

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
"""

_COLUMNS = ("id", "project", "title", "session", "agent", "content", "tags", "timestamp", "ttl", "access_count", "last_accessed")
_PLAN_COLUMNS = ("id", "project", "query", "plan_json", "outcome", "tags", "created_at", "ttl")

# ── FTS5 rebuild SQL (used for migration from old schema lacking 'title') ─────
# These statements recreate only the FTS5 virtual table and its triggers after
# the old (title-less) table has been dropped during _migrate_fts_if_needed().
_FTS_REBUILD_SQL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    title,
    content,
    tags,
    content='memory',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
        VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
        VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO memory_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);
END;
"""


# ── Session discovery ─────────────────────────────────────────────────────────

from nexus.session import read_session_id as _read_session_id


# ── Database ──────────────────────────────────────────────────────────────────

class T2Database:
    """T2 SQLite memory bank with FTS5 full-text search."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            # Note: executescript() implicitly COMMITs any open transaction.
            # Safe here because _init_schema runs only during __init__ with no prior transaction.
            self.conn.executescript(_SCHEMA_SQL)
            self.conn.commit()
            result = self.conn.execute("PRAGMA journal_mode").fetchone()
            if result and result[0].lower() != "wal":
                _log.warning("WAL mode not available", actual_mode=result[0])
            self._migrate_fts_if_needed()
            self._migrate_plans_if_needed()
            self._migrate_plans_ttl_if_needed()
            self._migrate_access_tracking_if_needed()

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

    def _migrate_access_tracking_if_needed(self) -> None:
        """Add access_count and last_accessed columns to memory if missing."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(memory)").fetchall()}
        changed = False
        if "access_count" not in cols:
            self.conn.execute(
                "ALTER TABLE memory ADD COLUMN access_count INTEGER DEFAULT 0 NOT NULL"
            )
            changed = True
        if "last_accessed" not in cols:
            self.conn.execute(
                "ALTER TABLE memory ADD COLUMN last_accessed TEXT DEFAULT ''"
            )
            changed = True
        if changed:
            self.conn.commit()
            _log.info("access_tracking migration complete")

    def _migrate_fts_if_needed(self) -> None:
        """Upgrade FTS5 index to include 'title' column if the DB uses the old schema.

        Safe to call multiple times — no-op when 'title' is already present.
        FTS5 content tables are pure indexes; the authoritative data lives in
        the ``memory`` table and is unaffected by dropping/recreating the FTS table.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
        if row is None or "title" in row[0]:
            # Table missing (fresh DB handled by _SCHEMA_SQL) or already up to date
            return

        _log.info("Migrating memory_fts to include title column")
        # Drop old triggers first (they reference the old column list)
        self.conn.executescript("""\
            DROP TRIGGER IF EXISTS memory_ai;
            DROP TRIGGER IF EXISTS memory_ad;
            DROP TRIGGER IF EXISTS memory_au;
            DROP TABLE  IF EXISTS memory_fts;
        """)
        # Recreate with new schema + triggers
        self.conn.executescript(_FTS_REBUILD_SQL)
        # Rebuild the FTS index from the authoritative memory table
        self.conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
        self.conn.commit()
        _log.info("memory_fts migration complete")

    def __enter__(self) -> "T2Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ── Write ─────────────────────────────────────────────────────────────────

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
        """Upsert a memory entry keyed by (project, title). Returns the row ID."""
        if agent is None:
            agent = os.environ.get("NX_AGENT")
        if session is None:
            session = _read_session_id()
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO memory (project, title, session, agent, content, tags, timestamp, ttl)
                VALUES (:project, :title, :session, :agent, :content, :tags, :timestamp, :ttl)
                ON CONFLICT(project, title) DO UPDATE SET
                    session   = excluded.session,
                    agent     = excluded.agent,
                    content   = excluded.content,
                    tags      = excluded.tags,
                    timestamp = excluded.timestamp,
                    ttl       = excluded.ttl
                """,
                {
                    "project": project,
                    "title": title,
                    "session": session,
                    "agent": agent,
                    "content": content,
                    "tags": tags,
                    "timestamp": timestamp,
                    "ttl": ttl,
                },
            )
            self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    # ── Read ──────────────────────────────────────────────────────────────────

    def get(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a single entry by (project, title) or by numeric ID."""
        with self._lock:
            if id is not None:
                row = self.conn.execute("SELECT * FROM memory WHERE id = ?", (id,)).fetchone()
            elif project is not None and title is not None:
                row = self.conn.execute(
                    "SELECT * FROM memory WHERE project = ? AND title = ?", (project, title)
                ).fetchone()
            else:
                raise ValueError("Provide either id or both project and title.")
            if row:
                now = datetime.now(UTC).isoformat()
                self.conn.execute(
                    "UPDATE memory SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                    (now, row[0]),
                )
                self.conn.commit()
                result = dict(zip(_COLUMNS, row))
                result["access_count"] += 1
                result["last_accessed"] = now
                return result
        return dict(zip(_COLUMNS, row)) if row else None

    def search(self, query: str, project: str | None = None) -> list[dict[str, Any]]:
        """FTS5 keyword search. Returns rows ordered by relevance."""
        safe = _sanitize_fts5(query)
        with self._lock:
            try:
                if project:
                    sql = """
                        SELECT m.id, m.project, m.title, m.session, m.agent,
                               m.content, m.tags, m.timestamp, m.ttl,
                               m.access_count, m.last_accessed
                        FROM memory m
                        JOIN memory_fts ON memory_fts.rowid = m.id
                        WHERE memory_fts MATCH ?
                          AND m.project = ?
                        ORDER BY rank
                    """
                    rows = self.conn.execute(sql, (safe, project)).fetchall()
                else:
                    sql = """
                        SELECT m.id, m.project, m.title, m.session, m.agent,
                               m.content, m.tags, m.timestamp, m.ttl,
                               m.access_count, m.last_accessed
                        FROM memory m
                        JOIN memory_fts ON memory_fts.rowid = m.id
                        WHERE memory_fts MATCH ?
                        ORDER BY rank
                    """
                    rows = self.conn.execute(sql, (safe,)).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
            # Batch-update access_count for all returned rows
            if rows:
                now = datetime.now(UTC).isoformat()
                ids = [r[0] for r in rows]
                self.conn.executemany(
                    "UPDATE memory SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                    [(now, rid) for rid in ids],
                )
                self.conn.commit()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def list_entries(
        self,
        project: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        """List entries ordered by timestamp descending. Optionally filtered.

        Returns a summary view with columns: id, project, title, agent, timestamp.
        Use get() or get_all() for full row content including the text body.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if project:
            conditions.append("project = ?")
            params.append(project)
        if agent:
            conditions.append("agent = ?")
            params.append(agent)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT id, project, title, agent, timestamp FROM memory {where} ORDER BY timestamp DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(zip(("id", "project", "title", "agent", "timestamp"), row)) for row in rows]

    def get_projects_with_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Return all distinct project namespaces whose name starts with *prefix*.

        Each row has ``project`` and ``last_updated`` (MAX timestamp for that namespace).
        Results are ordered by ``last_updated`` DESC — most-recently-updated first.

        LIKE metacharacters (``%``, ``_``, ``\\``) in *prefix* are escaped so they are
        matched literally — a repo named ``my_project`` will not match ``myXproject``.
        """
        if not prefix:
            return []
        escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql = """
            SELECT project, MAX(timestamp) AS last_updated
            FROM memory
            WHERE project LIKE ? ESCAPE '\\'
            GROUP BY project
            ORDER BY MAX(timestamp) DESC
        """
        with self._lock:
            rows = self.conn.execute(sql, (f"{escaped}%",)).fetchall()
        return [{"project": row[0], "last_updated": row[1]} for row in rows]

    def search_glob(self, query: str, project_glob: str) -> list[dict[str, Any]]:
        """FTS5 search scoped to projects matching a GLOB pattern (e.g. '*_rdr')."""
        sql = """
            SELECT m.id, m.project, m.title, m.session, m.agent,
                   m.content, m.tags, m.timestamp, m.ttl,
                   m.access_count, m.last_accessed
            FROM memory m
            JOIN memory_fts ON memory_fts.rowid = m.id
            WHERE memory_fts MATCH ?
              AND m.project GLOB ?
            ORDER BY rank
        """
        safe = _sanitize_fts5(query)
        with self._lock:
            try:
                rows = self.conn.execute(sql, (safe, project_glob)).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def search_by_tag(self, query: str, tag: str) -> list[dict[str, Any]]:
        """FTS5 search scoped to entries whose tags contain *tag*.

        Uses boundary matching via ``(',' || tags || ',') LIKE '%,{tag},%'``
        to avoid false positives (e.g. 'rdr' matching 'rdr-archived').
        """
        sql = """
            SELECT m.id, m.project, m.title, m.session, m.agent,
                   m.content, m.tags, m.timestamp, m.ttl,
                   m.access_count, m.last_accessed
            FROM memory m
            JOIN memory_fts ON memory_fts.rowid = m.id
            WHERE memory_fts MATCH ?
              AND (',' || m.tags || ',') LIKE ?
            ORDER BY rank
        """
        like_pattern = f"%,{tag},%"
        safe = _sanitize_fts5(query)
        with self._lock:
            try:
                rows = self.conn.execute(sql, (safe, like_pattern)).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
        return [dict(zip(_COLUMNS, row)) for row in rows]


    def get_all(self, project: str) -> list[dict[str, Any]]:
        """Return all entries for *project* with full column data."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memory WHERE project = ? ORDER BY timestamp DESC",
                (project,),
            ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def delete(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> bool:
        """Delete an entry by (project, title) or by numeric id.

        Returns True if a row was deleted.  Raises ValueError when neither
        a valid (project, title) pair nor an id is supplied.
        """
        if id is not None:
            sql = "DELETE FROM memory WHERE id = ?"
            params: tuple = (id,)
        elif project is not None and title is not None:
            sql = "DELETE FROM memory WHERE project = ? AND title = ?"
            params = (project, title)
        else:
            raise ValueError("Provide either id or both project and title.")
        with self._lock:
            cursor = self.conn.execute(sql, params)
            self.conn.commit()
        return cursor.rowcount > 0

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

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def expire(self) -> int:
        """Delete TTL-expired entries using heat-weighted effective TTL.

        effective_ttl = base_ttl * (1 + log(access_count + 1))
        Highly accessed entries survive longer. Unaccessed entries (access_count=0)
        expire at base rate (log(1) = 0, so multiplier = 1).
        """
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, access_count, ttl, timestamp
                FROM memory
                WHERE ttl IS NOT NULL
                """
            ).fetchall()
            now = datetime.now(UTC)
            expired_ids: list[int] = []
            for row_id, access_count, ttl, timestamp in rows:
                effective_ttl = ttl * (1 + math.log(access_count + 1))
                ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                age_days = (now - ts).total_seconds() / 86400.0
                if age_days > effective_ttl:
                    expired_ids.append(row_id)
            if expired_ids:
                placeholders = ",".join("?" * len(expired_ids))
                self.conn.execute(
                    f"DELETE FROM memory WHERE id IN ({placeholders})", expired_ids
                )
                self.conn.commit()
        return len(expired_ids)
