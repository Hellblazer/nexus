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
    """Parse a dotted version string into a comparable 3-component tuple.

    Normalises to exactly 3 components so ``(3, 7)`` doesn't compare
    less than ``(3, 7, 0)``.  Falls back to ``(0, 0, 0)`` for
    pre-release tags or malformed input.
    """
    try:
        parts = tuple(int(x) for x in ver.split(".")[:3])
        return parts + (0,) * (3 - len(parts))
    except ValueError:
        return (0, 0, 0)


# ── Dataclass ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Migration:
    """A single T2 schema migration, tagged with the version that introduced it.

    **Idempotency contract**: every ``fn`` MUST be idempotent — guarded by
    ``PRAGMA table_info()`` or ``sqlite_master`` checks so re-running on a
    DB that already has the migration applied is a no-op.  The retry-on-failure
    design of ``apply_pending`` depends on this invariant.
    """

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

    **Precondition**: ``_create_base_tables`` (or ``_MEMORY_SCHEMA_SQL``)
    must run before this function.  If ``memory_fts`` was dropped by a
    prior partial failure, ``_create_base_tables`` heals it via
    ``CREATE VIRTUAL TABLE IF NOT EXISTS`` with the ``title`` column,
    making the ``row is None`` early-return correct.
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
    # executescript implicitly commits the preceding ALTER TABLE before
    # running the DROP/CREATE script below.
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
    """Create topic-related tables if missing.

    Uses ``sqlite_master`` guard — NOT PRAGMA.  Creates all four
    taxonomy tables (matching ``_TAXONOMY_SCHEMA_SQL``) so standalone
    callers don't end up with a partial schema.
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
        CREATE TABLE IF NOT EXISTS taxonomy_meta (
            collection              TEXT PRIMARY KEY,
            last_discover_doc_count INTEGER NOT NULL DEFAULT 0,
            last_discover_at        TEXT
        );
        CREATE TABLE IF NOT EXISTS topic_assignments (
            doc_id    TEXT NOT NULL,
            topic_id  INTEGER NOT NULL REFERENCES topics(id),
            PRIMARY KEY (doc_id, topic_id)
        );
        CREATE TABLE IF NOT EXISTS topic_links (
            from_topic_id INTEGER NOT NULL REFERENCES topics(id),
            to_topic_id   INTEGER NOT NULL REFERENCES topics(id),
            link_count    INTEGER NOT NULL DEFAULT 0,
            link_types    TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (from_topic_id, to_topic_id)
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
    if not cols or "assigned_by" in cols:
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
    if not cols:
        return
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


def backfill_projection(t3_db: Any, taxonomy: Any) -> None:
    """Backfill cross-collection projection for all existing collections.

    For each collection with existing topics, projects against all other
    collections' centroids and stores assignments with ``assigned_by='projection'``.
    Then generates co-occurrence topic links.

    **Heavy operation** — scales O(collections²) with each pair requiring
    paginated ChromaDB fetches.  For a repo with N collections, expect
    N*(N-1) projection calls.  Prints per-collection progress to stderr.

    RDR-075 RF-11.  Idempotent via ``INSERT OR IGNORE`` in ``assign_topic``.
    """
    import sys
    import time

    import structlog

    log = structlog.get_logger()

    collections = taxonomy.get_distinct_collections()
    if not collections:
        log.info("backfill_projection_skip", reason="no topics discovered yet")
        return

    n = len(collections)
    print(
        f"  Backfilling projection across {n} collections "
        f"(~{n * (n - 1)} projection calls).",
        file=sys.stderr,
    )
    log.info("backfill_projection_start", collections=n)
    total_assigned = 0
    total_start = time.monotonic()

    for i, src in enumerate(collections, 1):
        targets = [c for c in collections if c != src]
        if not targets:
            continue
        t0 = time.monotonic()
        try:
            result = taxonomy.project_against(
                src, targets, t3_db._client, threshold=0.85,
            )
            assignments = result.get("chunk_assignments", [])
            for doc_id, topic_id in assignments:
                taxonomy.assign_topic(doc_id, topic_id, assigned_by="projection")
            total_assigned += len(assignments)
            elapsed = time.monotonic() - t0
            print(
                f"  [{i}/{n}] {src}: "
                f"{result.get('total_chunks', 0)} chunks, "
                f"{len(result.get('matched_topics', []))} matches, "
                f"{len(assignments)} assignments ({elapsed:.1f}s)",
                file=sys.stderr,
            )
        except Exception as e:
            log.warning("backfill_projection_collection_failed",
                        collection=src, exc_info=True)
            print(f"  [{i}/{n}] {src}: SKIPPED ({type(e).__name__})",
                  file=sys.stderr)

    # Generate co-occurrence links from the new projection assignments
    print("  Generating co-occurrence topic links...", file=sys.stderr)
    try:
        link_count = taxonomy.generate_cooccurrence_links()
        print(f"  Generated {link_count} co-occurrence links.", file=sys.stderr)
    except Exception:
        log.warning("backfill_cooccurrence_failed", exc_info=True)
        print("  Co-occurrence link generation: SKIPPED", file=sys.stderr)

    total_elapsed = time.monotonic() - total_start
    print(
        f"  Backfill complete: {total_assigned} total assignments in "
        f"{total_elapsed:.1f}s across {n} collections.",
        file=sys.stderr,
    )
    log.info("backfill_projection_complete",
             total_assigned=total_assigned, elapsed_s=round(total_elapsed, 1))


T3_UPGRADES: list[T3UpgradeStep] = [
    T3UpgradeStep("4.2.0", "Backfill cross-collection projection", backfill_projection),
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

_upgrade_lock = threading.RLock()
"""Serialises the check-then-add on ``_upgrade_done``.

The first thread adds its ``path_key`` under the lock; any concurrent
thread sees it already present and returns early before calling
``bootstrap_version``.  The lock does NOT wrap migration execution
itself — serialisation relies on the set membership check.
"""

_bootstrap_lock = threading.Lock()
"""Guards the bootstrap check in apply_pending (read + conditional insert).

Process-level only — cross-process safety relies on ``INSERT OR IGNORE``
plus SQLite WAL serialisation.  If two processes race the bootstrap,
whichever writes first determines the seed value; the loser's
``INSERT OR IGNORE`` is silently discarded.  This is correct because
all migrations are idempotent regardless of seed.
"""


# ── Runner ──────────────────────────────────────────────────────────────────


def bootstrap_version(conn: sqlite3.Connection) -> str:
    """Ensure base tables and ``_nexus_version`` exist, returning the stored version.

    Creates base tables, the version tracking table, and seeds the version
    row for existing vs fresh installs.  Idempotent per-connection and
    per-thread — do not share a single connection across threads.

    Used by both ``apply_pending`` and ``nx upgrade --dry-run`` to get an
    accurate last-seen version without duplicating bootstrap logic.
    """
    # Detect existing install BEFORE _create_base_tables runs
    _pre_existing = _is_existing_install(conn)

    _create_base_tables(conn)

    conn.execute(
        "CREATE TABLE IF NOT EXISTS _nexus_version ("
        "    key   TEXT PRIMARY KEY,"
        "    value TEXT NOT NULL"
        ")"
    )
    conn.commit()

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

    row = conn.execute(
        "SELECT value FROM _nexus_version WHERE key='cli_version'"
    ).fetchone()
    return row[0] if row else "0.0.0"


def apply_pending(conn: sqlite3.Connection, current_version: str) -> None:
    """Run all migrations introduced between last-seen and *current_version*.

    Idempotent — every migration function has column/table-existence guards.
    """
    path_key = _connection_path_key(conn)
    with _upgrade_lock:
        if path_key in _upgrade_done:
            return
        # Reserve the slot under lock — prevents concurrent entry.
        _upgrade_done.add(path_key)

    try:
        last_seen = bootstrap_version(conn)
        last_seen_t = _parse_version(last_seen)
        current_t = _parse_version(current_version)

        # Filter and execute eligible migrations
        for m in MIGRATIONS:
            m_ver = _parse_version(m.introduced)
            if m_ver > last_seen_t and m_ver <= current_t:
                _log.info("Running migration", name=m.name, introduced=m.introduced)
                m.fn(conn)

        # Update stored version.  Guards:
        # - Skip pre-release/unparseable versions ((0,0,0)) to prevent
        #   spurious re-run of all migrations on next proper release.
        # - Skip downgrade (current < last_seen) to prevent a rolled-back
        #   CLI from lowering the stored version.
        if current_t > (0, 0, 0) and current_t >= last_seen_t:
            conn.execute(
                "UPDATE _nexus_version SET value=? WHERE key='cli_version'",
                (current_version,),
            )
            conn.commit()
    except Exception:
        # On failure, remove from set under lock so the next call retries.
        # Must hold _upgrade_lock to prevent a concurrent thread from
        # seeing a stale _upgrade_done state mid-discard.
        with _upgrade_lock:
            _upgrade_done.discard(path_key)
        raise


def _connection_path_key(conn: sqlite3.Connection) -> str:
    """Extract a stable key for the connection's database file.

    For ``:memory:`` connections, returns a unique key per connection
    (avoids collisions when multiple in-memory DBs exist in the same
    process).  For file-based connections, resolves symlinks via
    ``Path.resolve()`` to match the canonicalisation used by
    ``T2Database.__init__()`` and ``_run_upgrade()``.
    """
    from pathlib import Path

    row = conn.execute("PRAGMA database_list").fetchone()
    if row and row[2]:
        try:
            return str(Path(row[2]).resolve())
        except OSError:
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
