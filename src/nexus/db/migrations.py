# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Centralised T2 schema migration registry.

Extracts all existing ``ALTER TABLE`` / FTS-rebuild migrations from domain
stores into module-level functions, each accepting ``sqlite3.Connection``.
A version-gated runner (``apply_pending``) executes only the migrations
introduced between the last-seen CLI version and the current version.

RDR-076 (nexus-6cn).
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Callable

import structlog

_log = structlog.get_logger()

# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_version(ver: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple.

    Falls back to ``(0, 0, 0)`` for pre-release tags or malformed input.
    """
    try:
        return tuple(int(x) for x in ver.split("."))
    except ValueError:
        return (0, 0, 0)


# ── Dataclass ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Migration:
    """A single T2 schema migration, tagged with the version that introduced it."""

    introduced: str  # package version that introduced this migration
    name: str  # human-readable description
    fn: Callable[[sqlite3.Connection], None]  # idempotent, module-level function


# ── Module-level migration functions ────────────────────────────────────────
# Extracted from domain store instance methods.  Each accepts a plain
# ``sqlite3.Connection`` and is idempotent (column/table-existence guards).


def migrate_memory_fts(conn: sqlite3.Connection) -> None:
    """Upgrade FTS5 index to include ``title`` column.

    Uses ``sqlite_master`` guard — NOT PRAGMA (FTS5 virtual tables are not
    visible to ``PRAGMA table_info``).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()
    if row is None or "title" in row[0]:
        return

    _log.info("Migrating memory_fts to include title column")
    conn.executescript(
        """\
        DROP TRIGGER IF EXISTS memory_ai;
        DROP TRIGGER IF EXISTS memory_ad;
        DROP TRIGGER IF EXISTS memory_au;
        DROP TABLE  IF EXISTS memory_fts;
    """
    )
    # Recreate with title column + triggers
    conn.executescript(
        """\
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            title, content, tags, content='memory', content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS memory_ai AFTER INSERT ON memory BEGIN
            INSERT INTO memory_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS memory_ad AFTER DELETE ON memory BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
        END;

        CREATE TRIGGER IF NOT EXISTS memory_au AFTER UPDATE ON memory BEGIN
            INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
                VALUES ('delete', old.id, old.title, old.content, old.tags);
            INSERT INTO memory_fts(rowid, title, content, tags)
                VALUES (new.id, new.title, new.content, new.tags);
        END;
    """
    )
    conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
    conn.commit()
    _log.info("memory_fts migration complete")


def migrate_plan_project(conn: sqlite3.Connection) -> None:
    """Add ``project`` column to plans table + FTS rebuild with project.

    Uses ``sqlite_master`` guard.  This is a compound migration — includes
    FTS rebuild and trigger recreation, not just ALTER TABLE.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone()
    if row is None or "project" in row[0]:
        return

    _log.info("Migrating plans table to add project column")
    conn.execute("ALTER TABLE plans ADD COLUMN project TEXT NOT NULL DEFAULT ''")
    conn.executescript(
        """\
        DROP TRIGGER IF EXISTS plans_ai;
        DROP TRIGGER IF EXISTS plans_ad;
        DROP TRIGGER IF EXISTS plans_au;
        DROP TABLE  IF EXISTS plans_fts;

        CREATE VIRTUAL TABLE IF NOT EXISTS plans_fts USING fts5(
            query, tags, project, content=plans, content_rowid='id'
        );

        CREATE TRIGGER IF NOT EXISTS plans_ai AFTER INSERT ON plans BEGIN
            INSERT INTO plans_fts(rowid, query, tags, project)
                VALUES (new.id, new.query, new.tags, new.project);
        END;
        CREATE TRIGGER IF NOT EXISTS plans_ad AFTER DELETE ON plans BEGIN
            INSERT INTO plans_fts(plans_fts, rowid, query, tags, project)
                VALUES ('delete', old.id, old.query, old.tags, old.project);
        END;
        CREATE TRIGGER IF NOT EXISTS plans_au AFTER UPDATE ON plans BEGIN
            INSERT INTO plans_fts(plans_fts, rowid, query, tags, project)
                VALUES ('delete', old.id, old.query, old.tags, old.project);
            INSERT INTO plans_fts(rowid, query, tags, project)
                VALUES (new.id, new.query, new.tags, new.project);
        END;
    """
    )
    conn.execute("INSERT INTO plans_fts(plans_fts) VALUES('rebuild')")
    conn.commit()
    _log.info("plans migration complete (added project column)")


def migrate_access_tracking(conn: sqlite3.Connection) -> None:
    """Add ``access_count`` and ``last_accessed`` columns to memory."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory)").fetchall()}
    changed = False
    if "access_count" not in cols:
        conn.execute(
            "ALTER TABLE memory ADD COLUMN access_count INTEGER DEFAULT 0 NOT NULL"
        )
        changed = True
    if "last_accessed" not in cols:
        conn.execute("ALTER TABLE memory ADD COLUMN last_accessed TEXT DEFAULT ''")
        changed = True
    if changed:
        conn.commit()
        _log.info("access_tracking migration complete")


def migrate_topics(conn: sqlite3.Connection) -> None:
    """Create ``topics`` and ``topic_assignments`` tables if missing.

    Uses ``sqlite_master`` guard — NOT PRAGMA.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='topics'"
    ).fetchone()
    if row is not None:
        return
    _log.info("Migrating T2 schema to add topics tables")
    conn.executescript(
        """\
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
    )
    conn.commit()
    _log.info("topics migration complete")


def migrate_plan_ttl(conn: sqlite3.Connection) -> None:
    """Add ``ttl`` column to plans table if missing."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if not cols or "ttl" in cols:
        return
    _log.info("Migrating plans table to add ttl column")
    conn.execute("ALTER TABLE plans ADD COLUMN ttl INTEGER")
    conn.commit()
    _log.info("plans ttl migration complete")


def migrate_assigned_by(conn: sqlite3.Connection) -> None:
    """Add ``assigned_by`` column to ``topic_assignments`` if missing."""
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
    }
    if "assigned_by" in cols:
        return
    _log.info("Migrating topic_assignments: adding assigned_by column")
    conn.execute(
        "ALTER TABLE topic_assignments ADD COLUMN assigned_by TEXT NOT NULL DEFAULT 'hdbscan'"
    )
    conn.commit()


def migrate_review_columns(conn: sqlite3.Connection) -> None:
    """Add ``review_status`` and ``terms`` columns to ``topics`` if missing."""
    cols = {
        row[1] for row in conn.execute("PRAGMA table_info(topics)").fetchall()
    }
    changed = False
    if "review_status" not in cols:
        _log.info("Migrating topics: adding review_status column")
        conn.execute(
            "ALTER TABLE topics ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"
        )
        changed = True
    if "terms" not in cols:
        _log.info("Migrating topics: adding terms column")
        conn.execute("ALTER TABLE topics ADD COLUMN terms TEXT")
        changed = True
    if changed:
        conn.commit()


# ── Migration registry ──────────────────────────────────────────────────────
# Ordered by introduced version.  Tags verified via ``git tag --contains``
# for each migration commit.

MIGRATIONS: list[Migration] = [
    Migration("1.10.0", "Memory FTS rebuild with title", migrate_memory_fts),
    Migration("2.8.0", "Add plan project column", migrate_plan_project),
    Migration("3.7.0", "Add memory access tracking", migrate_access_tracking),
    Migration("3.7.0", "Add topics tables", migrate_topics),
    Migration("3.8.0", "Add plan ttl column", migrate_plan_ttl),
    Migration("4.0.0", "Add assigned_by column", migrate_assigned_by),
    Migration("4.0.0", "Add review columns", migrate_review_columns),
]

# ── T3 upgrade steps ────────────────────────────────────────────────────────
# Separate from T2 migrations: these require a ChromaDB client, not sqlite3.


@dataclass(frozen=True)
class T3UpgradeStep:
    """A single T3 upgrade step, requiring a ChromaDB client."""

    introduced: str
    name: str
    fn: Callable  # Callable[[T3Database, CatalogTaxonomy], None]


T3_UPGRADES: list[T3UpgradeStep] = [
    # Future T3 upgrades append here
    # T3UpgradeStep("4.2.0", "Backfill cross-collection projection", backfill_projection),
]


# ── Constants ───────────────────────────────────────────────────────────────

PRE_REGISTRY_VERSION = "4.1.2"
"""Last release before the migration registry shipped.

Existing installs are bootstrapped to this version so retroactive
migrations (which already applied) are not spuriously re-run.
"""

# ── Process-level fast path ─────────────────────────────────────────────────

_upgrade_done: set[str] = set()
"""Tracks DB paths that have already been upgraded this process.

Checked by ``T2Database.__init__()`` before opening any connection.
Distinct from per-domain ``_migrated_paths`` sets in each store.
"""

_bootstrap_lock = threading.Lock()
"""Guards the bootstrap check in apply_pending (read + conditional insert).

Process-level only — cross-process safety relies on INSERT OR IGNORE
plus SQLite WAL serialisation.
"""


# ── Runner ──────────────────────────────────────────────────────────────────


def apply_pending(conn: sqlite3.Connection, current_version: str) -> None:
    """Run all migrations introduced between last-seen and *current_version*.

    Steps:
      1. Create base tables (lazy import from stores) via CREATE TABLE IF NOT EXISTS
      2. Create ``_nexus_version`` table if absent
      3. Bootstrap: detect existing vs fresh install, seed version accordingly
      4. Read last-seen version
      5. Filter and execute eligible migrations
      6. Update ``_nexus_version`` to *current_version*
    """
    path_key = _connection_path_key(conn)
    if path_key in _upgrade_done:
        return

    # Pre-step: detect whether this is an existing install BEFORE
    # _create_base_tables runs (which would create the memory table,
    # making it impossible to distinguish fresh from existing).
    _pre_existing = _is_existing_install(conn)

    # Step 1: ensure all base tables exist so migration guards can PRAGMA safely
    _create_base_tables(conn)

    # Step 2: version tracking table
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _nexus_version ("
        "    key   TEXT PRIMARY KEY,"
        "    value TEXT NOT NULL"
        ")"
    )
    conn.commit()

    # Step 3: bootstrap (under lock for concurrent-constructor safety)
    with _bootstrap_lock:
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        if row is None:
            seed = PRE_REGISTRY_VERSION if _pre_existing else "0.0.0"
            conn.execute(
                "INSERT OR IGNORE INTO _nexus_version (key, value) VALUES ('cli_version', ?)",
                (seed,),
            )
            conn.commit()

    # Step 4: read last-seen version
    row = conn.execute(
        "SELECT value FROM _nexus_version WHERE key='cli_version'"
    ).fetchone()
    last_seen = row[0] if row else "0.0.0"
    last_seen_t = _parse_version(last_seen)
    current_t = _parse_version(current_version)

    # Step 5: filter and execute
    for m in MIGRATIONS:
        m_ver = _parse_version(m.introduced)
        if m_ver > last_seen_t and m_ver <= current_t:
            _log.info("Running migration", name=m.name, introduced=m.introduced)
            m.fn(conn)

    # Step 6: update stored version
    conn.execute(
        "UPDATE _nexus_version SET value=? WHERE key='cli_version'",
        (current_version,),
    )
    conn.commit()

    _upgrade_done.add(path_key)


def _connection_path_key(conn: sqlite3.Connection) -> str:
    """Extract a stable key for the connection's database file.

    For ``:memory:`` connections, returns a unique key per connection
    (avoids collisions when multiple in-memory DBs exist in the same
    process).  For file-based connections, returns the database path.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    if row and row[2]:
        return row[2]
    return f":memory:{id(conn)}"


def _is_existing_install(conn: sqlite3.Connection) -> bool:
    """Check if domain tables already exist (before _create_base_tables).

    Must be called BEFORE _create_base_tables, which creates all tables
    via CREATE TABLE IF NOT EXISTS.  An existing install has at least the
    ``memory`` table from a prior domain store constructor.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory'"
    ).fetchone()
    return row is not None


def _create_base_tables(conn: sqlite3.Connection) -> None:
    """Execute all domain base-schema SQL via CREATE TABLE IF NOT EXISTS.

    Lazy imports avoid circular dependencies between migrations.py and
    domain store modules.
    """
    from nexus.db.t2.catalog_taxonomy import _TAXONOMY_SCHEMA_SQL
    from nexus.db.t2.memory_store import _MEMORY_SCHEMA_SQL
    from nexus.db.t2.plan_library import _PLANS_SCHEMA_SQL
    from nexus.db.t2.telemetry import _TELEMETRY_SCHEMA_SQL

    conn.executescript(_MEMORY_SCHEMA_SQL)
    conn.executescript(_PLANS_SCHEMA_SQL)
    conn.executescript(_TAXONOMY_SCHEMA_SQL)
    conn.executescript(_TELEMETRY_SCHEMA_SQL)
    conn.commit()
