# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import logging

_log = logging.getLogger(__name__)

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

def _read_session_id() -> str | None:
    """Read session ID from the PID-scoped session file written by the SessionStart hook."""
    from nexus.session import session_file_path
    try:
        return session_file_path().read_text().strip() or None
    except FileNotFoundError:
        return None


# ── Database ──────────────────────────────────────────────────────────────────

class T2Database:
    """T2 SQLite memory bank with FTS5 full-text search."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
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
        """List entries ordered by timestamp descending. Optionally filtered."""
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
        try:
            rows = self.conn.execute(sql, (query, project_glob)).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError(f"Invalid search query {query!r}: {exc}") from exc
        return [dict(zip(_COLUMNS, row)) for row in rows]

    def decay_project(self, project: str, ttl: int) -> None:
        """Set TTL and flip pm → pm-archived tags for all docs in *project*."""
        self.conn.execute(
            """
            UPDATE memory
               SET ttl  = ?,
                   tags = replace(tags, 'pm,', 'pm-archived,')
             WHERE project = ?
            """,
            (ttl, project),
        )
        self.conn.commit()

    def restore_project(self, project: str) -> tuple[list[str], list[str]]:
        """Reverse decay: set ttl=NULL and restore pm, tags.

        Returns (restored_titles, missing_titles) where missing_titles are docs
        that were in the project at archive time but have since been hard-deleted.
        """
        rows = self.conn.execute(
            "SELECT title FROM memory WHERE project = ?", (project,)
        ).fetchall()
        surviving = [r[0] for r in rows]

        self.conn.execute(
            """
            UPDATE memory
               SET ttl  = NULL,
                   tags = replace(tags, 'pm-archived,', 'pm,')
             WHERE project = ?
            """,
            (project,),
        )
        self.conn.commit()
        return surviving, []

    def get_all(self, project: str) -> list[dict[str, Any]]:
        """Return all entries for *project* with full column data."""
        rows = self.conn.execute(
            "SELECT * FROM memory WHERE project = ? ORDER BY timestamp DESC",
            (project,),
        ).fetchall()
        return [dict(zip(_COLUMNS, row)) for row in rows]

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def expire(self) -> int:
        """Delete TTL-expired entries. Returns the count of deleted rows."""
        cursor = self.conn.execute(
            """
            DELETE FROM memory
            WHERE ttl IS NOT NULL
              AND julianday('now') - julianday(timestamp) > ttl
            """
        )
        self.conn.commit()
        return cursor.rowcount
