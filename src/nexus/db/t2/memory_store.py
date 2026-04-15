# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""MemoryStore — agent memory table, FTS5 search, TTL expire, consolidation.

Owns the ``memory`` and ``memory_fts`` tables plus all methods that
query or mutate them. Extracted from the monolithic ``T2Database`` in
RDR-063 Phase 1 step 2 (bead ``nexus-vx3c``).

As of Phase 2 (bead ``nexus-3d3k``) the store owns its own
``sqlite3.Connection`` and ``threading.Lock`` against the SQLite file.
The former :class:`SharedConnection` indirection has been removed —
each domain store is now a self-contained owner of its table(s) and
can be locked, migrated, and closed independently of its siblings.

Lock ownership convention:
  * Public mutators / readers (``put``, ``get``, ``search``, etc.)
    acquire ``self._lock`` themselves.
  * ``_init_schema`` runs under ``self._lock`` during ``__init__``
    and invokes the private migration methods
    (``_migrate_fts_if_needed`` / ``_migrate_access_tracking_if_needed``)
    under the per-domain ``_migrated_lock`` guard.

``_sanitize_fts5`` and ``_FTS5_SPECIAL`` live here (they are only used
by memory search methods and the catalog FTS helper); the facade
re-exports ``_sanitize_fts5`` so ``from nexus.db.t2 import _sanitize_fts5``
still resolves.
"""

from __future__ import annotations

import math
import os
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import structlog

from nexus.session import read_session_id as _read_session_id

_log = structlog.get_logger()


# Access policy for T2 read operations (R3-4):
# - "track": increment access_count + update last_accessed (default for user-facing reads)
# - "silent": do not touch access metadata (internal scans, consolidation)
AccessPolicy = Literal["track", "silent"]


# Per-domain migration guard (RDR-063 Open Question 3 — Phase 2 resolution).
# Each store owns its own ``_migrated_paths`` set and ``_migrated_lock`` so
# adding a new migration to one domain never triggers re-probing of unrelated
# domains. The MCP server opens a fresh ``T2Database`` per tool call; without
# this guard, each call would re-probe both migrations on every construction.
_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()


# ── FTS5 helpers ──────────────────────────────────────────────────────────────

# FTS5 special characters that cause OperationalError when unquoted:
#   -  (column filter: "col-name" → look in column "col" for "name")
#   :  (explicit column filter: "col:term")
#   (  )  ^  "  (grouping / phrase / boost — crash if unbalanced)
#   ,  ;  (statement separators in FTS5 query grammar — surface as
#           "syntax error near ','" when a tokeniser leaves them bare.
#           Hit by RDR-079 P7 on seed descriptions like ``entries,``)
# Note: trailing * is a valid FTS5 prefix wildcard (e.g. auth*) — NOT included here.
_FTS5_SPECIAL = set('-:()"^~.*+/,;')


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


# ── Schema SQL (memory + memory_fts + triggers) ───────────────────────────────

_MEMORY_SCHEMA_SQL = """\
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
"""

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

_COLUMNS = (
    "id",
    "project",
    "title",
    "session",
    "agent",
    "content",
    "tags",
    "timestamp",
    "ttl",
    "access_count",
    "last_accessed",
)


# Default busy_timeout for the memory store's connection. 5 seconds is
# well beyond any schema-creation or migration window and absorbs normal
# cross-domain write-lock contention.
_DEFAULT_BUSY_MS = 5000

# Disabled busy_timeout used only around the best-effort access-count
# UPDATE in ``search(access="track")`` and ``get()``. The access counter
# is a statistical signal and must fail-fast rather than block the
# caller on the write lock when another store is writing.
# ``busy_timeout = 0`` disables the busy handler, so SQLITE_BUSY fires
# on the first contention and we skip the update in the except branch.
_ACCESS_TRACK_BUSY_MS = 0


def _is_sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    """Return True iff ``exc`` is a SQLITE_BUSY (database-locked) error.

    The project targets Python 3.12+, which populates
    ``exc.sqlite_errorcode`` on any ``OperationalError`` raised by
    the CPython C layer during a real database operation. That is the
    primary production path: compare the numeric errorcode to
    ``sqlite3.SQLITE_BUSY`` (5) for a precise match, so we don't
    swallow unrelated ``OperationalError`` subclasses like
    SQLITE_CORRUPT (11), SQLITE_IOERR (10), or SQLITE_CANTOPEN (14).

    The substring fallback exists for tests that monkey-patch a
    raise with ``sqlite3.OperationalError("database is locked")``
    constructed from Python — those synthetic exceptions do not have
    ``sqlite_errorcode`` populated because the C layer never touched
    them. The fallback keeps test-injection patterns working without
    forcing tests to synthesize a full C-layer errorcode.

    Extended codes — ``SQLITE_BUSY_SNAPSHOT`` (517),
    ``SQLITE_BUSY_RECOVERY`` (261), ``SQLITE_BUSY_TIMEOUT`` (773) —
    are **intentionally NOT swallowed** by this helper. They indicate
    a different class of failure (snapshot staleness, recovery in
    progress, statement-level timeout) where silently skipping the
    access-tracking UPDATE is not necessarily safe; let them
    propagate so the caller sees the real failure mode. The
    access-tracking fast-fail path only absorbs pure write-lock
    contention, which is always bare SQLITE_BUSY in this codebase's
    WAL access pattern.
    """
    errorcode = getattr(exc, "sqlite_errorcode", None)
    if errorcode is not None:
        return errorcode == sqlite3.SQLITE_BUSY
    # Fallback for test-injected exceptions without errorcode.
    return "locked" in str(exc).lower()


# ── MemoryStore ───────────────────────────────────────────────────────────────


class MemoryStore:
    """Owns the ``memory`` and ``memory_fts`` tables.

    The store opens its own ``sqlite3.Connection`` and guards it with
    its own ``threading.Lock``. All four T2 stores open the same SQLite
    file; WAL mode + ``busy_timeout`` let the per-domain connections
    coordinate at the SQLite layer without sharing Python-level state.
    """

    # Common English stopwords — used by find_overlapping_memories' Jaccard
    # similarity to avoid trivial matches on filler words.
    _STOPWORDS = frozenset(
        {
            "the", "a", "an", "in", "of", "for", "to", "and", "or", "is", "are", "was",
            "it", "that", "this", "with", "on", "at", "by", "from", "as", "be", "not",
        }
    )

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        # busy_timeout lets concurrent writers (e.g. a second T2Database
        # constructor racing this one) wait on SQLite's file-level write
        # lock instead of raising OperationalError. 5 seconds is well
        # beyond any schema-creation or migration window observed in the
        # migration-guard regression tests.
        self.conn.execute(f"PRAGMA busy_timeout = {_DEFAULT_BUSY_MS}")
        try:
            canonical_key = str(path.resolve())
        except OSError:
            canonical_key = str(path)
        self._init_schema(canonical_key)

    def close(self) -> None:
        """Close the dedicated connection (idempotent under ``self._lock``)."""
        with self._lock:
            self.conn.close()

    # ── Schema / migrations ───────────────────────────────────────────────

    def _init_schema(self, path_key: str) -> None:
        """Create the memory tables and run any pending migrations.

        Runs under ``self._lock`` so concurrent writers wait on schema
        setup; the migration block additionally runs under
        ``_migrated_lock`` so two constructors on the same path cannot
        both enter the ``_migrate_*_if_needed`` methods (ALTER TABLE ADD
        COLUMN is NOT idempotent — double-application raises
        OperationalError).
        """
        with self._lock:
            self.conn.executescript(_MEMORY_SCHEMA_SQL)
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.commit()
            result = self.conn.execute("PRAGMA journal_mode").fetchone()
            if result and result[0].lower() != "wal":
                _log.warning("WAL mode not available", actual_mode=result[0])
            with _migrated_lock:
                if path_key in _migrated_paths:
                    return
                self._migrate_fts_if_needed()
                self._migrate_access_tracking_if_needed()
                _migrated_paths.add(path_key)

    def _migrate_fts_if_needed(self) -> None:
        """Upgrade FTS5 index to include 'title' column if the DB uses the old schema.

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        Delegates to module-level function in migrations.py (RDR-076).
        """
        from nexus.db.migrations import migrate_memory_fts

        migrate_memory_fts(self.conn)

    def _migrate_access_tracking_if_needed(self) -> None:
        """Add access_count and last_accessed columns to memory if missing.

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        Delegates to module-level function in migrations.py (RDR-076).
        """
        from nexus.db.migrations import migrate_access_tracking

        migrate_access_tracking(self.conn)

    # ── Write ─────────────────────────────────────────────────────────────

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

    # ── Read ──────────────────────────────────────────────────────────────

    def get(
        self,
        project: str | None = None,
        title: str | None = None,
        id: int | None = None,
    ) -> dict[str, Any] | None:
        """Retrieve a single entry by (project, title) or by numeric ID.

        Access tracking (``access_count`` + ``last_accessed``) runs as a
        best-effort side-effect after the read — see ``search()`` for the
        rationale. Under sustained cross-domain write load the UPDATE is
        allowed to fail-fast on ``SQLITE_BUSY`` (logged as
        ``memory.access_tracking.skipped``) rather than block the caller
        on the full 5-second ``busy_timeout``. The row itself is always
        returned; only the counter side-effect may be skipped.
        """
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
            tracked = False
            try:
                self.conn.execute(
                    f"PRAGMA busy_timeout = {_ACCESS_TRACK_BUSY_MS}"
                )
                self.conn.execute(
                    "UPDATE memory SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                    (now, row[0]),
                )
                self.conn.commit()
                tracked = True
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_busy(exc):
                    raise
                # SQLITE_BUSY: best-effort side-effect skipped. Roll back
                # the implicit write transaction so the connection is
                # clean, log, and return the row without the counter
                # increment reflected in the returned dict.
                try:
                    self.conn.rollback()
                except sqlite3.OperationalError:  # pragma: no cover
                    pass
                _log.warning(
                    "memory.access_tracking.skipped",
                    reason=str(exc),
                    row_id=row[0],
                    method="get",
                )
            finally:
                self.conn.execute(
                    f"PRAGMA busy_timeout = {_DEFAULT_BUSY_MS}"
                )
            result = dict(zip(_COLUMNS, row))
            if tracked:
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
            # Batch-update access_count for all returned rows (skip when
            # access="silent"). Access tracking is a best-effort statistical
            # signal — missing a few increments under heavy cross-domain
            # write contention is acceptable. We run the UPDATE with a short
            # busy_timeout so SQLITE_BUSY fails fast (and we skip the update)
            # instead of hanging the caller on the full 5-second write-lock
            # wait, then restore the long timeout for subsequent operations.
            # Only SQLITE_BUSY ("database is locked") is swallowed — other
            # OperationalError subclasses (SQLITE_CORRUPT, SQLITE_IOERR,
            # SQLITE_CANTOPEN) indicate real problems and must propagate.
            if rows and access == "track":
                now = datetime.now(UTC).isoformat()
                ids = [r[0] for r in rows]
                try:
                    self.conn.execute(
                        f"PRAGMA busy_timeout = {_ACCESS_TRACK_BUSY_MS}"
                    )
                    self.conn.executemany(
                        "UPDATE memory SET access_count = access_count + 1, last_accessed = ? WHERE id = ?",
                        [(now, rid) for rid in ids],
                    )
                    self.conn.commit()
                except sqlite3.OperationalError as exc:
                    if not _is_sqlite_busy(exc):
                        raise
                    # SQLITE_BUSY: the fast-fail retry was skipped.
                    # Roll back the implicit write transaction so the
                    # connection is clean for the next search, log at
                    # warning so the signal isn't lost, and return the
                    # rows we already fetched.
                    try:
                        self.conn.rollback()
                    except sqlite3.OperationalError:  # pragma: no cover
                        pass
                    _log.warning(
                        "memory.access_tracking.skipped",
                        reason=str(exc),
                        row_count=len(ids),
                        method="search",
                    )
                finally:
                    self.conn.execute(
                        f"PRAGMA busy_timeout = {_DEFAULT_BUSY_MS}"
                    )
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

    # ── Housekeeping ──────────────────────────────────────────────────────

    def expire(self) -> list[int]:
        """Delete TTL-expired memory entries using heat-weighted effective TTL.

        effective_ttl = base_ttl * (1 + log(access_count + 1))
        Highly accessed entries survive longer. Unaccessed entries
        (access_count=0) expire at base rate (log(1) = 0, so multiplier = 1).

        Returns the list of deleted row IDs so the facade can aggregate
        metrics across domain stores (memory + telemetry).
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
        return expired_ids

    # ── Memory consolidation (RDR-061 E6) ─────────────────────────────────

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
            words = [
                w
                for w in e1.get("content", "").split()[:5]
                if w.lower() not in self._STOPWORDS and len(w) > 2
            ]
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
        Raises KeyError if keep_id does not exist — prevents silent data
        loss when expire() races with merge. If keep_id was deleted between
        the caller's find-overlaps and this call, the UPDATE affects 0 rows
        and we raise BEFORE the DELETE runs, preserving the delete_ids.

        Atomicity: UPDATE and DELETE run inside a single `with self.conn:`
        block — the connection context manager commits on success and
        rolls back on any exception. Under SQLite's default DEFERRED
        isolation (which Python's sqlite3 uses), the UPDATE acquires a
        write lock when it executes; that lock blocks other writers
        (including expire()) until the block exits, so the DELETE runs
        while still holding the lock. If a concurrent writer held the
        lock when we started, the UPDATE waits (or raises OperationalError
        on busy timeout) — either way, no interleaving is possible.
        """
        if keep_id in delete_ids:
            raise ValueError(
                f"keep_id ({keep_id}) must not be in delete_ids — "
                "would discard the entry meant to be kept"
            )
        # Ordering: self._lock first (in-process serialization), then
        # self.conn (SQLite transaction). On exception the connection
        # context manager rolls back BEFORE the lock is released.
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE memory SET content = ? WHERE id = ?",
                (merged_content, keep_id),
            )
            if cur.rowcount == 0:
                # keep_id does not exist — likely deleted by a concurrent
                # expire() or was stale when the caller selected it. Raise
                # BEFORE running DELETE so delete_ids survive the race.
                # The with-block rolls back the (no-op) UPDATE.
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
