# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
import math
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

# Access policy for T2 read operations (R3-4):
# - "track": increment access_count + update last_accessed (default for user-facing reads)
# - "silent": do not touch access metadata (internal scans, consolidation)
AccessPolicy = Literal["track", "silent"]

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


# ── Per-process migration guard (RDR-062 follow-up) ─────────────────────────
# Migrations only need to run once per DB path per process. The MCP server
# opens a fresh T2Database on every tool call; without this guard, each call
# probes all 6 migrations.
_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()


# ── Database ──────────────────────────────────────────────────────────────────

class T2Database:
    """T2 SQLite memory bank with FTS5 full-text search."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
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
            # Safe here because _init_schema runs only during __init__ with no prior transaction.
            self.conn.executescript(_SCHEMA_SQL)
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
                self._migrate_fts_if_needed()
                self._migrate_plans_if_needed()
                self._migrate_plans_ttl_if_needed()
                self._migrate_access_tracking_if_needed()
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
            if row is None:
                return None
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

    def search(
        self,
        query: str,
        project: str | None = None,
        access: AccessPolicy = "track",
    ) -> list[dict[str, Any]]:
        """FTS5 keyword search. Returns rows ordered by relevance.

        Args:
            query: FTS5 query string
            project: Optional project filter
            access: Access tracking policy (R3-4):
                - ``"track"`` (default): increments access_count and
                  updates last_accessed on every returned row — normal reads.
                - ``"silent"``: does not touch access metadata — internal
                  scans (consolidation, audit) that must not contaminate
                  the staleness signal.
        """
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
            # Batch-update access_count for all returned rows (skip when access="silent")
            if rows and access == "track":
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
        # Call outside the main lock — expire_relevance_log acquires its own.
        log_deleted = 0
        log_error: str | None = None
        try:
            log_deleted = self.expire_relevance_log(days=relevance_log_days)
        except Exception as exc:
            log_error = type(exc).__name__
            _log.warning("expire_relevance_log_failed", exc_info=exc)
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

    # ── Memory consolidation (RDR-061 E6) ────────────────────────────────────

    _STOPWORDS = frozenset({
        "the", "a", "an", "in", "of", "for", "to", "and", "or", "is", "are", "was",
        "it", "that", "this", "with", "on", "at", "by", "from", "as", "be", "not",
    })

    def find_overlapping_memories(
        self,
        project: str,
        min_similarity: float = 0.7,
        limit: int = 50,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        """Return pairs of memory entries with high word-set overlap.

        Uses FTS5 to find candidates, then Jaccard similarity on word sets
        (after stopword removal) to confirm overlap.
        """
        entries = self.get_all(project)
        if len(entries) < 2:
            return []

        def _words(text: str) -> set[str]:
            return {
                w.lower() for w in text.split()
                if len(w) > 2 and w.lower() not in self._STOPWORDS
            }

        seen: set[tuple[int, int]] = set()
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []

        for e1 in entries:
            # Use first few content words for FTS5 candidate retrieval — AND-of-terms
            # means too many words kills recall. Jaccard on full content handles precision.
            words = [w for w in e1.get("content", "").split()[:5]
                     if w.lower() not in self._STOPWORDS and len(w) > 2]
            snippet = " ".join(words[:3])
            if not snippet:
                continue
            try:
                # access="silent": consolidation scan must not bump
                # access_count/last_accessed (would contaminate flag-stale)
                candidates = self.search(snippet, project=project, access="silent")
            except ValueError:
                continue
            w1 = _words(e1.get("content", ""))
            if not w1:
                continue
            for e2 in candidates:
                if e2["id"] == e1["id"]:
                    continue
                pair_key = tuple(sorted((e1["id"], e2["id"])))
                if pair_key in seen:
                    continue
                seen.add(pair_key)
                w2 = _words(e2.get("content", ""))
                if not w2:
                    continue
                jaccard = len(w1 & w2) / len(w1 | w2)
                if jaccard >= min_similarity:
                    pairs.append((e1, e2))
                    if len(pairs) >= limit:
                        return pairs
        return pairs

    def merge_memories(
        self,
        keep_id: int,
        delete_ids: list[int],
        merged_content: str,
    ) -> None:
        """Merge multiple entries into *keep_id*, delete the rest.

        Updates content of *keep_id* and deletes all *delete_ids*.
        FTS5 triggers handle index cleanup automatically.

        Raises ValueError if keep_id appears in delete_ids — that would
        silently discard the kept entry (UPDATE then DELETE on same row).
        Raises KeyError if keep_id does not exist (prevents silent data loss
        when expire() races with merge — if keep_id was deleted between
        find-overlaps and merge, the UPDATE affects 0 rows and the DELETE
        would otherwise proceed, destroying both copies of the content).

        Uses BEGIN IMMEDIATE to establish a write lock before the UPDATE,
        preventing racing writers from deleting keep_id mid-transaction.
        """
        if keep_id in delete_ids:
            raise ValueError(
                f"keep_id ({keep_id}) must not be in delete_ids — "
                "would discard the entry meant to be kept"
            )
        with self._lock:
            # BEGIN IMMEDIATE upgrades to a write lock immediately, blocking
            # other writers (including expire()) until we commit.
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self.conn.execute(
                    "UPDATE memory SET content = ? WHERE id = ?",
                    (merged_content, keep_id),
                )
                if cur.rowcount == 0:
                    # keep_id does not exist — likely deleted by a concurrent
                    # expire() or was stale when the caller selected it.
                    # ABORT the transaction to prevent destroying delete_ids.
                    self.conn.execute("ROLLBACK")
                    raise KeyError(
                        f"keep_id {keep_id} not found — aborted merge to "
                        "prevent data loss (delete_ids left intact)"
                    )
                if delete_ids:
                    placeholders = ",".join("?" * len(delete_ids))
                    self.conn.execute(
                        f"DELETE FROM memory WHERE id IN ({placeholders})",
                        delete_ids,
                    )
                self.conn.commit()
            except Exception:
                # Ensure rollback on any error (except KeyError which already
                # rolled back above).
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass  # already rolled back
                raise

    def flag_stale_memories(
        self,
        project: str,
        idle_days: int = 30,
    ) -> list[dict[str, Any]]:
        """Return memories not accessed in *idle_days*.

        Uses ``last_accessed`` when available (non-empty), falls back to
        ``timestamp`` for entries that have never been accessed.
        """
        cutoff = (datetime.now(UTC) - timedelta(days=idle_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT id, project, title, session, agent, content, tags,
                       timestamp, ttl, access_count, last_accessed
                FROM memory
                WHERE project = ?
                  AND CASE
                      WHEN last_accessed != '' THEN last_accessed < ?
                      ELSE timestamp < ?
                  END
                """,
                (project, cutoff, cutoff),
            ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]
