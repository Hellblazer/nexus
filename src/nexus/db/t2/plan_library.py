# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""PlanLibrary — reusable query-plan storage (RDR-063 Phase 1).

Owns the ``plans`` and ``plans_fts`` tables and all methods that query
or mutate them. Extracted from the monolithic ``T2Database`` in
RDR-063 Phase 1 step 3 (bead ``nexus-kpe7``).

Lock ownership convention mirrors :mod:`nexus.db.t2.memory_store`:
  * Public methods (``save_plan``, ``search_plans``, ``list_plans``,
    ``plan_exists``) acquire ``self._lock`` themselves.
  * ``init_schema_unlocked`` and the private migration methods
    (``_migrate_plans_if_needed`` / ``_migrate_plans_ttl_if_needed``)
    are lock-naive — the caller (currently
    :meth:`T2Database._init_schema`) holds ``self._lock`` and
    ``_migrated_lock`` for the whole sequence.

Landmine 1 fix (audit finding F2): :func:`plan_exists` exists so that
``src/nexus/commands/catalog.py:_seed_plan_templates`` no longer needs
to reach through the facade's private ``.conn`` attribute. After
Phase 2 each store gets its own connection and ``T2Database.conn``
goes away; without this method the builtin-template seeding would
break in production.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from nexus.db.t2.memory_store import _sanitize_fts5

if TYPE_CHECKING:
    from nexus.db.t2._connection import SharedConnection

_log = structlog.get_logger()


# Per-domain migration guard placeholder — see memory_store.py for the
# rationale. The authoritative guard still lives at the facade level in
# Phase 1 (nexus.db.t2._migrated_paths).
_migrated_paths: set[str] = set()


# ── Schema SQL ────────────────────────────────────────────────────────────────

_PLANS_SCHEMA_SQL = """\
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

_PLAN_COLUMNS = ("id", "project", "query", "plan_json", "outcome", "tags", "created_at", "ttl")


# ── PlanLibrary ───────────────────────────────────────────────────────────────


class PlanLibrary:
    """Owns the ``plans`` and ``plans_fts`` tables.

    See module docstring for the lock-ownership convention.
    """

    def __init__(self, shared: "SharedConnection") -> None:
        self._shared = shared
        # Legacy aliases — Phase 1 stores all share the same lock/conn,
        # so any caller that reached through .conn / ._lock continues to
        # work. Phase 2 will give each store its own pair.
        self._lock = shared.lock
        self.conn = shared.conn

    # ── Schema / migrations ───────────────────────────────────────────────

    def init_schema_unlocked(self) -> None:
        """Create the ``plans`` table + FTS + triggers. Caller holds the lock."""
        self.conn.executescript(_PLANS_SCHEMA_SQL)

    def _migrate_plans_if_needed(self) -> None:
        """Add 'project' column to plans table if missing (v2.8.0 schema change).

        Safe to call multiple times — no-op when 'project' is already present
        or when the plans table doesn't exist yet.

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        """
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='plans'"
        ).fetchone()
        if row is None or "project" in row[0]:
            return

        _log.info("Migrating plans table to add project column")
        self.conn.execute("ALTER TABLE plans ADD COLUMN project TEXT NOT NULL DEFAULT ''")
        # Recreate FTS + triggers with project column
        self.conn.executescript(
            """\
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
        """
        )
        self.conn.execute("INSERT INTO plans_fts(plans_fts) VALUES('rebuild')")
        self.conn.commit()
        _log.info("plans migration complete (added project column)")

    def _migrate_plans_ttl_if_needed(self) -> None:
        """Add 'ttl' column to plans table if missing.

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(plans)").fetchall()}
        if not cols or "ttl" in cols:
            return
        _log.info("Migrating plans table to add ttl column")
        self.conn.execute("ALTER TABLE plans ADD COLUMN ttl INTEGER")
        self.conn.commit()
        _log.info("plans ttl migration complete")

    # ── Public API ────────────────────────────────────────────────────────

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

    def search_plans(
        self,
        query: str,
        limit: int = 5,
        project: str = "",
    ) -> list[dict[str, Any]]:
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

    def plan_exists(self, query: str, tag: str) -> bool:
        """Return True if any plan with *query* has *tag* as a comma-separated token.

        Used by ``commands/catalog.py:_seed_plan_templates`` to skip
        already-seeded builtin templates without reaching through the
        facade's private ``.conn`` attribute.

        Tag matching uses the comma-boundary pattern
        ``(',' || tags || ',') LIKE '%,<tag>,%'`` — matches only when
        *tag* appears as a whole token in the comma-separated ``tags``
        column. This is the same boundary-safe pattern used by
        :meth:`MemoryStore.search_by_tag` and avoids substring false
        positives like ``"builtin-template-v2"`` or
        ``"not-builtin-template"``.

        Note that this is a tighter contract than the original
        pre-split query at ``commands/catalog.py:93`` which did a raw
        substring ``LIKE '%builtin-template%'``. Both patterns give the
        same result for the 5 seeded builtin templates (their tag
        strings all have ``builtin-template`` as a comma-separated
        token), so the Landmine 1 fix remains semantically equivalent
        for the current call site. Tightening the contract here
        prevents new callers from getting unexpected false positives.

        Audit finding F2 / Landmine 1: this method exists so that
        Phase 2's separate-connection split (which removes
        ``T2Database.conn``) does not break catalog seeding in
        production.
        """
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM plans "
                "WHERE query = ? AND (',' || tags || ',') LIKE ? LIMIT 1",
                (query, f"%,{tag},%"),
            ).fetchone()
        return row is not None
