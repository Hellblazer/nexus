# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

_log = structlog.get_logger()

# ── Schema SQL ────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS memory (
    id        INTEGER PRIMARY KEY,
    project   TEXT    NOT NULL,
    title     TEXT    NOT NULL,
    session   TEXT,
    agent     TEXT,
    content   TEXT    NOT NULL,
    tags      TEXT,
    timestamp TEXT    NOT NULL,
    ttl       INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_project_title ON memory(project, title);
CREATE INDEX        IF NOT EXISTS idx_memory_project       ON memory(project);
CREATE INDEX        IF NOT EXISTS idx_memory_agent         ON memory(agent);
CREATE INDEX        IF NOT EXISTS idx_memory_timestamp     ON memory(timestamp);
CREATE INDEX        IF NOT EXISTS idx_memory_ttl_timestamp ON memory(ttl, timestamp);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    content,
    tags,
    content='memory',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, tags)
        VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO memory_fts(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
"""

_COLUMNS = ("id", "project", "title", "session", "agent", "content", "tags", "timestamp", "ttl")


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
        return dict(zip(_COLUMNS, row)) if row else None

    def search(self, query: str, project: str | None = None) -> list[dict[str, Any]]:
        """FTS5 keyword search. Returns rows ordered by relevance."""
        with self._lock:
            try:
                if project:
                    sql = """
                        SELECT m.id, m.project, m.title, m.session, m.agent,
                               m.content, m.tags, m.timestamp, m.ttl
                        FROM memory m
                        JOIN memory_fts ON memory_fts.rowid = m.id
                        WHERE memory_fts MATCH ?
                          AND m.project = ?
                        ORDER BY rank
                    """
                    rows = self.conn.execute(sql, (query, project)).fetchall()
                else:
                    sql = """
                        SELECT m.id, m.project, m.title, m.session, m.agent,
                               m.content, m.tags, m.timestamp, m.ttl
                        FROM memory m
                        JOIN memory_fts ON memory_fts.rowid = m.id
                        WHERE memory_fts MATCH ?
                        ORDER BY rank
                    """
                    rows = self.conn.execute(sql, (query,)).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def list_entries(
        self,
        project: str | None = None,
        agent: str | None = None,
    ) -> list[dict[str, Any]]:
        """List entries ordered by timestamp descending. Optionally filtered.

        Returns a summary view with columns: id, title, agent, timestamp.
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
        sql = f"SELECT id, title, agent, timestamp FROM memory {where} ORDER BY timestamp DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(zip(("id", "title", "agent", "timestamp"), row)) for row in rows]

    def search_glob(self, query: str, project_glob: str) -> list[dict[str, Any]]:
        """FTS5 search scoped to projects matching a GLOB pattern (e.g. '*_pm')."""
        sql = """
            SELECT m.id, m.project, m.title, m.session, m.agent,
                   m.content, m.tags, m.timestamp, m.ttl
            FROM memory m
            JOIN memory_fts ON memory_fts.rowid = m.id
            WHERE memory_fts MATCH ?
              AND m.project GLOB ?
            ORDER BY rank
        """
        with self._lock:
            try:
                rows = self.conn.execute(sql, (query, project_glob)).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def search_by_tag(self, query: str, tag: str) -> list[dict[str, Any]]:
        """FTS5 search scoped to entries whose tags contain *tag*.

        Uses boundary matching via ``(',' || tags || ',') LIKE '%,{tag},%'``
        to avoid false positives (e.g. 'pm' matching 'pm-archived').
        """
        sql = """
            SELECT m.id, m.project, m.title, m.session, m.agent,
                   m.content, m.tags, m.timestamp, m.ttl
            FROM memory m
            JOIN memory_fts ON memory_fts.rowid = m.id
            WHERE memory_fts MATCH ?
              AND (',' || m.tags || ',') LIKE ?
            ORDER BY rank
        """
        like_pattern = f"%,{tag},%"
        with self._lock:
            try:
                rows = self.conn.execute(sql, (query, like_pattern)).fetchall()
            except sqlite3.OperationalError as exc:
                raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def decay_project(self, project: str, ttl: int) -> None:
        """Set TTL and flip pm -> pm-archived tags for all docs in *project*."""
        with self._lock, self.conn:
            self.conn.execute(
                """
                UPDATE memory
                   SET ttl  = ?,
                       tags = trim(replace(',' || tags || ',', ',pm,', ',pm-archived,'), ',')
                 WHERE project = ?
                """,
                (ttl, project),
            )

    def restore_project(self, project: str) -> list[str]:
        """Reverse decay: set ttl=NULL and restore pm tags.

        Returns the titles of all surviving entries in the project.
        Note: entries hard-deleted during the decay window cannot be detected,
        so only surviving titles are returned.
        """
        with self._lock, self.conn:
            rows = self.conn.execute(
                "SELECT title FROM memory WHERE project = ?", (project,)
            ).fetchall()
            surviving = [r[0] for r in rows]

            self.conn.execute(
                """
                UPDATE memory
                   SET ttl  = NULL,
                       tags = trim(replace(',' || tags || ',', ',pm-archived,', ',pm,'), ',')
                 WHERE project = ?
                """,
                (project,),
            )
        return surviving

    def get_all(self, project: str) -> list[dict[str, Any]]:
        """Return all entries for *project* with full column data."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM memory WHERE project = ? ORDER BY timestamp DESC",
                (project,),
            ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def delete(self, project: str, title: str) -> bool:
        """Delete an entry by (project, title). Returns True if a row was deleted."""
        with self._lock:
            cursor = self.conn.execute(
                "DELETE FROM memory WHERE project = ? AND title = ?",
                (project, title),
            )
            self.conn.commit()
        return cursor.rowcount > 0

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def expire(self) -> int:
        """Delete TTL-expired entries. Returns the count of deleted rows."""
        with self._lock:
            cursor = self.conn.execute(
                """
                DELETE FROM memory
                WHERE ttl IS NOT NULL
                  AND julianday('now') - julianday(timestamp) > ttl
                """
            )
            self.conn.commit()
        return cursor.rowcount
