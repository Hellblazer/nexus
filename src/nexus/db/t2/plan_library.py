# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""PlanLibrary — reusable query-plan storage (RDR-063).

Owns the ``plans`` and ``plans_fts`` tables and all methods that query
or mutate them. Extracted from the monolithic ``T2Database`` in
RDR-063 Phase 1 step 3 (bead ``nexus-kpe7``); promoted to own its
dedicated ``sqlite3.Connection`` and ``threading.Lock`` in Phase 2
(bead ``nexus-3d3k``).

Lock ownership convention mirrors :mod:`nexus.db.t2.memory_store`:
  * Public methods (``save_plan``, ``search_plans``, ``list_plans``,
    ``plan_exists``) acquire ``self._lock`` themselves.
  * ``_init_schema`` runs under ``self._lock`` during ``__init__`` and
    invokes the private migration methods
    (``_migrate_plans_if_needed`` / ``_migrate_plans_ttl_if_needed``)
    under the per-domain ``_migrated_lock`` guard.

Landmine 1 fix (audit finding F2): :func:`plan_exists` exists so that
``src/nexus/commands/catalog.py:_seed_plan_templates`` no longer needs
to reach through the facade's private ``.conn`` attribute. Phase 2
removed ``T2Database.conn``; without this method the builtin-template
seeding would break in production.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from nexus.db.t2.memory_store import _sanitize_fts5

_log = structlog.get_logger()


# Per-domain migration guard (RDR-063 Open Question 3 — Phase 2 resolution).
# Each store owns its own ``_migrated_paths`` set and ``_migrated_lock`` so
# adding a new migration to one domain never triggers re-probing of the
# others. PlanLibrary carries two migrations (``_migrate_plans_if_needed``
# for the ``project`` column and ``_migrate_plans_ttl_if_needed`` for the
# ``ttl`` column), both protected by this guard.
_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()


# ── Schema SQL ────────────────────────────────────────────────────────────────

_PLANS_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS plans (
    id              INTEGER PRIMARY KEY,
    project         TEXT NOT NULL DEFAULT '',
    query           TEXT NOT NULL,
    plan_json       TEXT NOT NULL,
    outcome         TEXT DEFAULT 'success',
    tags            TEXT DEFAULT '',
    created_at      TEXT NOT NULL,
    ttl             INTEGER,
    -- RDR-078 dimensional identity, currying, metrics columns. Present on
    -- fresh installs; the ``_add_plan_dimensional_identity`` migration
    -- (4.4.0) covers upgrade-in-place.
    name            TEXT,
    verb            TEXT,
    scope           TEXT,
    dimensions      TEXT,
    default_bindings TEXT,
    parent_dims     TEXT,
    use_count       INTEGER NOT NULL DEFAULT 0,
    last_used       TEXT,
    match_count     INTEGER NOT NULL DEFAULT 0,
    match_conf_sum  REAL NOT NULL DEFAULT 0.0,
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_plans_verb ON plans(verb);
CREATE INDEX IF NOT EXISTS idx_plans_scope ON plans(scope);
CREATE INDEX IF NOT EXISTS idx_plans_verb_scope ON plans(verb, scope);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_project_dimensions
    ON plans(project, dimensions) WHERE dimensions IS NOT NULL;

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

_PLAN_COLUMNS = (
    "id", "project", "query", "plan_json", "outcome", "tags",
    "created_at", "ttl",
    # RDR-078 dimensional identity, currying, and metrics columns.
    "name", "verb", "scope", "dimensions", "default_bindings", "parent_dims",
    "use_count", "last_used", "match_count", "match_conf_sum",
    "success_count", "failure_count",
)

_PLAN_SELECT_COLS = ", ".join(_PLAN_COLUMNS)


def _row_to_dict(row: tuple) -> dict[str, Any]:
    """Wrap a raw plans row in a column-keyed dict."""
    return dict(zip(_PLAN_COLUMNS, row))


# ── PlanLibrary ───────────────────────────────────────────────────────────────


class PlanLibrary:
    """Owns the ``plans`` and ``plans_fts`` tables.

    See module docstring for the lock-ownership convention.
    """

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute("PRAGMA busy_timeout=5000")
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
        """Create the plans tables and run any pending migrations.

        Runs the plans DDL under ``self._lock`` and then the two
        migrations under ``_migrated_lock`` so two constructors on the
        same path cannot both enter ALTER TABLE ADD COLUMN.
        """
        with self._lock:
            self.conn.executescript(_PLANS_SCHEMA_SQL)
            self.conn.executescript("PRAGMA journal_mode=WAL;")
            self.conn.commit()
            with _migrated_lock:
                if path_key in _migrated_paths:
                    return
                self._migrate_plans_if_needed()
                self._migrate_plans_ttl_if_needed()
                _migrated_paths.add(path_key)

    def _migrate_plans_if_needed(self) -> None:
        """Add 'project' column to plans table if missing (v2.8.0 schema change).

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        Delegates to module-level function in migrations.py (RDR-076).
        """
        from nexus.db.migrations import migrate_plan_project

        migrate_plan_project(self.conn)

    def _migrate_plans_ttl_if_needed(self) -> None:
        """Add 'ttl' column to plans table if missing.

        Lock-naive: caller must hold ``self._lock`` and ``_migrated_lock``.
        Delegates to module-level function in migrations.py (RDR-076).
        """
        from nexus.db.migrations import migrate_plan_ttl

        migrate_plan_ttl(self.conn)

    # ── Public API ────────────────────────────────────────────────────────

    def save_plan(
        self,
        query: str,
        plan_json: str,
        outcome: str = "success",
        tags: str = "",
        project: str = "",
        ttl: int | None = None,
        *,
        name: str | None = None,
        verb: str | None = None,
        scope: str | None = None,
        dimensions: str | None = None,
        default_bindings: str | None = None,
        parent_dims: str | None = None,
    ) -> int:
        """Insert a plan record. Returns the new row ID.

        Args:
            ttl: Time-to-live in days. None means permanent (no expiry).
            name, verb, scope, dimensions, default_bindings, parent_dims:
                RDR-078 dimensional-identity / currying fields. All optional.
        """
        created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            cursor = self.conn.execute(
                """
                INSERT INTO plans (
                    project, query, plan_json, outcome, tags, created_at, ttl,
                    name, verb, scope, dimensions, default_bindings, parent_dims
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project, query, plan_json, outcome, tags, created_at, ttl,
                    name, verb, scope, dimensions, default_bindings, parent_dims,
                ),
            )
            self.conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        """Return the row for *plan_id*, or ``None`` if it doesn't exist."""
        with self._lock:
            row = self.conn.execute(
                f"SELECT {_PLAN_SELECT_COLS} FROM plans WHERE id = ? LIMIT 1",
                (plan_id,),
            ).fetchone()
        return _row_to_dict(row) if row else None

    def list_active_plans(
        self,
        *,
        outcome: str = "success",
        project: str = "",
    ) -> list[dict[str, Any]]:
        """Return every non-expired plan with the given *outcome*."""
        ttl_filter = (
            "(ttl IS NULL OR julianday('now') - julianday(created_at) <= ttl)"
        )
        if project:
            sql = (
                f"SELECT {_PLAN_SELECT_COLS} FROM plans "
                f"WHERE outcome = ? AND project = ? AND {ttl_filter} "
                f"ORDER BY created_at DESC"
            )
            params: tuple = (outcome, project)
        else:
            sql = (
                f"SELECT {_PLAN_SELECT_COLS} FROM plans "
                f"WHERE outcome = ? AND {ttl_filter} "
                f"ORDER BY created_at DESC"
            )
            params = (outcome,)
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]

    def increment_match_metrics(
        self, plan_id: int, *, confidence: float | None,
    ) -> None:
        """Bump ``match_count`` and (when scored) ``match_conf_sum``.

        Called from ``plan_match`` for every returned candidate.
        ``confidence=None`` (FTS5 fallback) increments only the count
        so the running average stays a true average over scored hits.
        SC-12.
        """
        with self._lock:
            if confidence is None:
                self.conn.execute(
                    "UPDATE plans SET match_count = match_count + 1 WHERE id = ?",
                    (plan_id,),
                )
            else:
                self.conn.execute(
                    "UPDATE plans SET match_count = match_count + 1, "
                    "match_conf_sum = match_conf_sum + ? WHERE id = ?",
                    (float(confidence), plan_id),
                )
            self.conn.commit()

    def increment_run_started(self, plan_id: int) -> None:
        """Bump ``use_count`` and stamp ``last_used`` (SC-12)."""
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self.conn.execute(
                "UPDATE plans SET use_count = use_count + 1, last_used = ? WHERE id = ?",
                (now, plan_id),
            )
            self.conn.commit()

    def increment_run_outcome(self, plan_id: int, *, success: bool) -> None:
        """Bump ``success_count`` or ``failure_count`` (SC-12)."""
        column = "success_count" if success else "failure_count"
        with self._lock:
            self.conn.execute(
                f"UPDATE plans SET {column} = {column} + 1 WHERE id = ?",
                (plan_id,),
            )
            self.conn.commit()

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
        cols = ", ".join(f"p.{c}" for c in _PLAN_COLUMNS)
        if project:
            sql = f"""
                SELECT {cols}
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
                SELECT {cols}
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
        return [_row_to_dict(row) for row in rows]

    def list_plans(self, limit: int = 20, project: str = "") -> list[dict[str, Any]]:
        """Return most recent non-expired plans ordered by created_at DESC."""
        ttl_filter = "(ttl IS NULL OR julianday('now') - julianday(created_at) <= ttl)"
        if project:
            sql = (
                f"SELECT {_PLAN_SELECT_COLS} FROM plans "
                f"WHERE project = ? AND {ttl_filter} ORDER BY created_at DESC LIMIT ?"
            )
            params_l: tuple = (project, limit)
        else:
            sql = (
                f"SELECT {_PLAN_SELECT_COLS} FROM plans "
                f"WHERE {ttl_filter} ORDER BY created_at DESC LIMIT ?"
            )
            params_l = (limit,)
        with self._lock:
            rows = self.conn.execute(sql, params_l).fetchall()
        return [_row_to_dict(row) for row in rows]

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
