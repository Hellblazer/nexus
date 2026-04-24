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


def _add_projection_quality_columns(conn: sqlite3.Connection) -> None:
    """Add ``similarity``, ``assigned_at``, ``source_collection`` columns + index.

    RDR-077 Phase 1 (nexus-nsh). New columns are all NULL-able — pre-migration
    rows keep NULLs; re-projection populates them later (no backfill here).

    The composite index on ``(source_collection, assigned_by)`` supports the
    ICF aggregation query in Phase 3 and the ``nx taxonomy audit`` /
    ``hubs`` filters in Phases 5-6.

    Idempotent: guarded by ``PRAGMA table_info`` for columns and
    ``CREATE INDEX IF NOT EXISTS`` for the index. No-op when
    ``topic_assignments`` does not yet exist (fresh installs create it via
    ``_TAXONOMY_SCHEMA_SQL`` before any migrations run; this guard only
    protects against being called on a DB without the taxonomy schema at all).
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='topic_assignments'"
    ).fetchone()
    if not has_table:
        return

    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
    }
    changed = False
    if "similarity" not in cols:
        _log.info("Migrating topic_assignments: adding similarity column")
        conn.execute("ALTER TABLE topic_assignments ADD COLUMN similarity REAL")
        changed = True
    if "assigned_at" not in cols:
        _log.info("Migrating topic_assignments: adding assigned_at column")
        conn.execute("ALTER TABLE topic_assignments ADD COLUMN assigned_at TEXT")
        changed = True
    if "source_collection" not in cols:
        _log.info("Migrating topic_assignments: adding source_collection column")
        conn.execute(
            "ALTER TABLE topic_assignments ADD COLUMN source_collection TEXT"
        )
        changed = True

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_topic_assignments_source "
        "ON topic_assignments(source_collection, assigned_by)"
    )

    if changed:
        conn.commit()


def migrate_nx_answer_runs(conn: sqlite3.Connection) -> None:
    """Create the ``nx_answer_runs`` table for RDR-080 run telemetry.

    Idempotent — no-op if the table already exists. Called lazily from
    ``_nx_answer_record_run`` so the table is created on first use.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='nx_answer_runs'"
    ).fetchone()
    if row is not None:
        return
    conn.execute("""
        CREATE TABLE nx_answer_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            question    TEXT    NOT NULL,
            plan_id     INTEGER,
            matched_confidence REAL,
            step_count  INTEGER NOT NULL DEFAULT 0,
            final_text  TEXT    NOT NULL DEFAULT '',
            cost_usd    REAL    NOT NULL DEFAULT 0.0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
        )
    """)
    conn.commit()
    _log.info("Migrated: created nx_answer_runs table (RDR-080)")


def migrate_search_telemetry(conn: sqlite3.Connection) -> None:
    """Create the ``search_telemetry`` table for RDR-087 Phase 2 persistence.

    Per-call threshold-filter telemetry. One row per (query, collection)
    pair — Phase 2.2's hot-path INSERT OR IGNORE writes a row for every
    collection touched by a ``search_cross_corpus`` call. Query text is
    hashed (sha256) rather than stored raw for privacy. Composite PK
    ``(ts, query_hash, collection)`` lets the same query re-run in a
    later second emit a fresh row while duplicate writes within the
    same ISO-second are deduped.

    Idempotent — no-op if the table already exists.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_telemetry'"
    ).fetchone()
    if row is not None:
        return
    conn.executescript("""
        CREATE TABLE search_telemetry (
            ts             TEXT    NOT NULL,
            query_hash     TEXT    NOT NULL,
            collection     TEXT    NOT NULL,
            raw_count      INTEGER NOT NULL,
            dropped_count  INTEGER NOT NULL,
            top_distance   REAL,
            threshold      REAL,
            PRIMARY KEY (ts, query_hash, collection)
        );
        CREATE INDEX idx_search_tel_collection
            ON search_telemetry(collection);
        CREATE INDEX idx_search_tel_ts
            ON search_telemetry(ts);
    """)
    conn.commit()
    _log.info("Migrated: created search_telemetry table (RDR-087)")


def migrate_rename_dropped_to_kept(conn: sqlite3.Connection) -> None:
    """Rename ``search_telemetry.dropped_count`` → ``kept_count`` and flip values.

    RDR-087 review follow-up (nexus-yi4b.2.5). The RDR spec calls for
    ``kept_count``; 4.6.0 shipped with ``dropped_count``. Rename the
    column and flip stored values via ``kept_count = raw_count - kept_count``
    so downstream Phase 3 consumers see spec-aligned column semantics.

    Idempotent — no-op when ``dropped_count`` is already absent (either
    because the column was renamed previously, or the table doesn't
    exist yet).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='search_telemetry'"
    ).fetchone()
    if row is None:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(search_telemetry)").fetchall()}
    if "dropped_count" not in cols:
        return
    conn.execute("ALTER TABLE search_telemetry RENAME COLUMN dropped_count TO kept_count")
    conn.execute("UPDATE search_telemetry SET kept_count = raw_count - kept_count")
    conn.commit()
    _log.info("Migrated: search_telemetry.dropped_count → kept_count (RDR-087 errata)")


def _add_plan_dimensional_identity(conn: sqlite3.Connection) -> None:
    """Add RDR-078 dimensional identity + currying + metrics columns to plans.

    Columns added (all nullable / zero-default so RDR-042 callers keep
    working):

    * Dimensional identity: ``verb``, ``scope``, ``dimensions``,
      ``default_bindings``, ``parent_dims``, ``name``.
    * Metrics: ``use_count``, ``last_used``, ``match_count``,
      ``match_conf_sum``, ``success_count``, ``failure_count``.

    Indexes added:

    * ``idx_plans_verb``, ``idx_plans_scope``,
      ``idx_plans_verb_scope`` — dimensional filter acceleration.
    * ``idx_plans_project_dimensions`` — partial ``UNIQUE`` index on
      ``(project, dimensions) WHERE dimensions IS NOT NULL``. Legacy
      rows with ``dimensions=NULL`` are excluded so they never collide
      with each other; new rows using :func:`nexus.plans.schema.
      canonical_dimensions_json` dedupe at write time.

    RDR-078 Phase 4c (nexus-05i.1). Idempotent via ``PRAGMA table_info``
    column guard + ``CREATE INDEX IF NOT EXISTS``. No-op on a DB that
    has no ``plans`` table.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone()
    if not has_table:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}

    changed = False
    additions: list[tuple[str, str]] = [
        ("verb", "TEXT"),
        ("scope", "TEXT"),
        ("dimensions", "TEXT"),
        ("default_bindings", "TEXT"),
        ("parent_dims", "TEXT"),
        ("name", "TEXT"),
        ("use_count", "INTEGER NOT NULL DEFAULT 0"),
        ("last_used", "TEXT"),
        ("match_count", "INTEGER NOT NULL DEFAULT 0"),
        ("match_conf_sum", "REAL NOT NULL DEFAULT 0.0"),
        ("success_count", "INTEGER NOT NULL DEFAULT 0"),
        ("failure_count", "INTEGER NOT NULL DEFAULT 0"),
    ]
    for col_name, decl in additions:
        if col_name not in cols:
            _log.info("Adding plans column", column=col_name)
            conn.execute(f"ALTER TABLE plans ADD COLUMN {col_name} {decl}")
            changed = True

    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_verb ON plans(verb)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plans_scope ON plans(scope)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plans_verb_scope ON plans(verb, scope)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_plans_project_dimensions "
        "ON plans(project, dimensions) WHERE dimensions IS NOT NULL"
    )

    if changed:
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

def migrate_hook_failures(conn: sqlite3.Connection) -> None:
    """Create the ``hook_failures`` table for GH #251.

    ``fire_post_store_hooks`` in ``mcp_infra.py`` wraps every post-store
    hook in a per-hook ``try/except`` — a failing
    ``taxonomy_assign_hook`` (e.g. missing centroids, Chroma timeout)
    logs a warning and moves on so ``store_put`` never rolls back. The
    dropped write is currently invisible outside structlog output.

    This table captures each failure with enough context for ``status``
    to surface an actionable Action line and (optional) ``nx doctor
    hooks`` to propose retries.

    Schema:
      - id           INTEGER PRIMARY KEY
      - doc_id       TEXT  (may be empty when the hook fails before the
                            doc-level identifier is known)
      - collection   TEXT
      - hook_name    TEXT  (from getattr(hook, '__name__', '?'))
      - error        TEXT  (str(exc) — the traceback stays in structlog)
      - occurred_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP

    Indexes:
      - idx_hook_failures_occurred_at — `status` reads ``occurred_at >= ?``
        with a 24h window
      - idx_hook_failures_collection — scoped reports

    Idempotent: no-op if the table already exists.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hook_failures'"
    ).fetchone()
    if row is not None:
        return
    conn.executescript("""
        CREATE TABLE hook_failures (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id      TEXT NOT NULL DEFAULT '',
            collection  TEXT NOT NULL DEFAULT '',
            hook_name   TEXT NOT NULL,
            error       TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_hook_failures_occurred_at
            ON hook_failures(occurred_at);
        CREATE INDEX idx_hook_failures_collection
            ON hook_failures(collection);
    """)
    conn.commit()
    _log.info("Migrated: created hook_failures table (GH #251)")


def migrate_chash_index(conn: sqlite3.Connection) -> None:
    """Create the ``chash_index`` table for RDR-086 Phase 1.

    Global content-addressed chunk lookup: given a ``chash:<hex>`` span,
    find which (physical_collection, doc_id) pair(s) carry that chunk.
    Phase 2 builds ``Catalog.resolve_chash(chash)`` on top of this;
    without the T2 index the serial-ChromaDB-filter alternative takes
    ~300ms/collection (RF-6 measurement: 13 min on a 136-collection prod
    DB). With the T2 JOIN, ~50µs.

    Schema:
      - chash              TEXT NOT NULL
      - physical_collection TEXT NOT NULL
      - doc_id             TEXT NOT NULL
      - created_at         TEXT NOT NULL  (ISO-8601 UTC, set at INSERT time;
                                           Phase 2 uses it for multi-match
                                           newest-wins tie-break)
      - PRIMARY KEY (chash, physical_collection)

    Compound PK rationale (RF-10 Issue 1): the same chunk text (same
    SHA-256 chash) can legitimately be indexed into multiple collections
    — e.g. ``knowledge__delos`` and ``knowledge__delos_docling`` both
    ingest the same paper, so every chunk's SHA-256 is identical. A
    single-column chash PK would FK-violate on the second write.

    Secondary index: ``idx_chash_index_collection`` on ``physical_collection``
    — the Phase 1.4 delete cascade issues ``DELETE FROM chash_index WHERE
    physical_collection = ?`` (inherited from nexus-lub), which without
    the index would be a table scan.

    Idempotent: no-op if the table already exists (re-apply safe).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chash_index'"
    ).fetchone()
    if row is not None:
        return
    conn.executescript("""
        CREATE TABLE chash_index (
            chash                TEXT NOT NULL,
            physical_collection  TEXT NOT NULL,
            doc_id               TEXT NOT NULL,
            created_at           TEXT NOT NULL,
            PRIMARY KEY (chash, physical_collection)
        );
        CREATE INDEX idx_chash_index_collection
            ON chash_index(physical_collection);
    """)
    conn.commit()


def _add_plan_scope_tags(conn: sqlite3.Connection) -> None:
    """Add the ``scope_tags`` column to ``plans`` and backfill default rows.

    RDR-091 Phase 2a (bead ``nexus-x6pr``). ``scope_tags`` captures which
    corpora / collections a plan actually touched — a comma-separated,
    sorted, deduplicated, hash-suffix-normalized string. Phase 2b consumes
    this column during match-time re-ranking; this migration only ensures
    the column exists and carries a best-effort value for every row.

    The backfill intentionally guards on ``WHERE scope_tags = ''`` so
    explicitly-authored values (passed via ``save_plan(scope_tags=...)``)
    survive process restarts. Without this guard (RDR-091 critic
    follow-up, nexus-dfok), every process start would overwrite
    explicit tags with inference output, defeating the explicit-override
    path documented in the authoring guide.

    Idempotent via a ``PRAGMA table_info`` column guard. The backfill is
    safe to re-run because inference is deterministic.

    No-op on a DB that has no ``plans`` table (fresh install without
    :mod:`nexus.db.t2.plan_library` ever instantiated).

    The ``DEFAULT ''`` at column-creation time is load-bearing: any code
    path that inserts into ``plans`` without naming ``scope_tags`` (pre
    the plan_library update in the same PR) produces ``""``, not ``NULL``,
    which Phase 2b treats as the scope-agnostic marker.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone()
    if not has_table:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if "scope_tags" not in cols:
        _log.info("Adding plans column", column="scope_tags")
        conn.execute(
            "ALTER TABLE plans ADD COLUMN scope_tags TEXT NOT NULL DEFAULT ''"
        )

    # Backfill only rows where scope_tags is still the default ''.
    # Explicit values survive; partial-failure retries pick up where
    # they left off.
    from nexus.plans.scope import _infer_scope_tags

    rows = conn.execute(
        "SELECT id, plan_json FROM plans WHERE scope_tags = ''"
    ).fetchall()
    for row_id, plan_json in rows:
        inferred = _infer_scope_tags(plan_json or "")
        if inferred:
            conn.execute(
                "UPDATE plans SET scope_tags = ? WHERE id = ? AND scope_tags = ''",
                (inferred, row_id),
            )
    conn.commit()


def _rewash_plan_scope_tags_all_sentinel(conn: sqlite3.Connection) -> None:
    """Rewash rows whose ``scope_tags`` contains ``'all'`` from pre-fix backfill.

    RDR-091 critic follow-up (bead ``nexus-dfok``). The first-cut
    ``_infer_scope_tags`` treated ``corpus: "all"`` as a concrete
    scope tag, so the 4.8.0 backfill wrote ``scope_tags='all'`` onto
    every builtin plan that uses ``corpus: all`` (7 of 9 builtins).
    At match time those plans prefix-matched no real scope and were
    filtered out of the candidate pool — inverting RDR-091's whole
    purpose for any scoped ``nx_answer`` call.

    The inference fix (``"all"`` is now a skipped sentinel) takes
    effect on new saves, but existing rows carry the broken value.
    This migration re-runs inference on any row whose ``scope_tags``
    contains the token ``all``, replacing it with the corrected value
    (typically ``""`` for agnostic plans, or the subset of real tags
    when a multi-corpus plan mixed ``all`` with specific corpora).

    No-op on fresh installs or on DBs where every row already has
    clean tags. Idempotent: a second run finds no matching rows.
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone()
    if not has_table:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if "scope_tags" not in cols:
        return

    from nexus.plans.scope import _infer_scope_tags

    rows = conn.execute(
        """
        SELECT id, plan_json FROM plans
        WHERE scope_tags = 'all'
           OR scope_tags LIKE 'all,%'
           OR scope_tags LIKE '%,all'
           OR scope_tags LIKE '%,all,%'
        """
    ).fetchall()
    for row_id, plan_json in rows:
        inferred = _infer_scope_tags(plan_json or "")
        conn.execute(
            "UPDATE plans SET scope_tags = ? WHERE id = ?",
            (inferred, row_id),
        )
    if rows:
        _log.info("Rewashed plan scope_tags 'all' sentinel", row_count=len(rows))
        conn.commit()


# ── RDR-092 Phase 0d.1 (plan-dimensions backfill) ──────────────────────────


#: Verb-from-stem dictionary (29 stems across 5 verb families). Keyed
#: on lowercased tokens that commonly appear in plan ``query`` text.
#: A match is high-confidence (tagged ``backfill``); zero matches fall
#: through to the wh-fallback and get tagged ``backfill-low-conf``.
_BACKFILL_VERB_STEMS: dict[str, str] = {
    # research family (8 stems)
    "find": "research", "search": "research", "list": "research",
    "get": "research", "show": "research", "enumerate": "research",
    "fetch": "research", "retrieve": "research",
    # analyze family (8 stems)
    "analyze": "analyze", "analyse": "analyze",
    "compare": "analyze", "contrast": "analyze",
    "rank": "analyze", "synthesize": "analyze",
    "summarize": "analyze", "summarise": "analyze",
    # review family (5 stems)
    "review": "review", "audit": "review",
    "evaluate": "review", "critique": "review",
    "assess": "review",
    # debug family (5 stems)
    "debug": "debug", "trace": "debug",
    "investigate": "debug", "fix": "debug",
    "troubleshoot": "debug",
    # document family (3 stems)
    "document": "document", "describe": "document",
    "explain": "document",
}

#: Wh-fallback table. Low confidence because a wh-question can map to
#: any verb; the best guess is research for explanatory questions,
#: review for causal "why" questions.
_BACKFILL_WH_FALLBACK: dict[str, str] = {
    "how": "research", "what": "research",
    "why": "review",
    "when": "research", "where": "research", "who": "research",
    "which": "research",
}

#: Stop-words skipped when deriving the plan ``name`` from query text.
_BACKFILL_NAME_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "for", "in", "on", "at", "by", "with", "from", "about",
    "and", "or", "but", "so", "as",
    "how", "what", "why", "when", "where", "who", "which",
    "do", "does", "did", "can", "could", "should", "would", "will",
    "this", "that", "these", "those",
    "i", "we", "you", "they", "it", "he", "she",
})


def _infer_plan_verb_from_query(query: str) -> tuple[str, bool]:
    """Heuristic verb classifier for RDR-092 plan-dimension backfill.

    Returns ``(verb, is_confident)``. High-confidence matches come
    from :data:`_BACKFILL_VERB_STEMS`; wh-fallback matches are
    low-confidence; the ultimate default is ``("research", False)``.
    """
    import re

    tokens = re.findall(r"[a-z][a-z-]+", (query or "").lower())
    for token in tokens:
        if token in _BACKFILL_VERB_STEMS:
            return _BACKFILL_VERB_STEMS[token], True
    for token in tokens:
        if token in _BACKFILL_WH_FALLBACK:
            return _BACKFILL_WH_FALLBACK[token], False
    return "research", False


def _derive_plan_name_from_query(query: str, *, max_words: int = 5) -> str:
    """Kebab-case name from the first 3-5 content tokens of *query*."""
    import re

    tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9_]*", (query or "").lower())
    content = [t for t in tokens if t not in _BACKFILL_NAME_STOP_WORDS]
    take = content[:max_words] if content else tokens[:max_words]
    return "-".join(take) or "backfilled-plan"


def _backfill_plan_dimensions(conn: sqlite3.Connection) -> None:
    """Backfill verb / name / dimensions on NULL-dimension plan rows.

    RDR-092 Phase 0d.1. Touches only rows where ``dimensions IS NULL``;
    authored rows (shipped YAML seeds, already-dimensional grown
    plans, previously-backfilled rows) are left alone. The
    :func:`_infer_plan_verb_from_query` heuristic decides the verb,
    and :func:`_derive_plan_name_from_query` supplies the name. Rows
    whose verb came from a stem match get tagged ``backfill``; rows
    that fell through to the wh-fallback get tagged
    ``backfill-low-conf`` so ``nx plan repair`` can prioritise them.

    The canonical dimensions JSON is ``{"scope":<scope>,"strategy":
    <name>,"verb":<verb>}`` where ``<scope>`` is ``"personal"`` for
    tagged grown rows and ``"global"`` for everything else (legacy
    builtin shapes pre-RDR-078). Idempotent: a second run skips rows
    whose ``dimensions`` is no longer NULL.

    **Collision handling (RDR-092 code-review C-1).** Two NULL-
    dimension rows in the same project whose queries collapse to the
    same kebab name produce identical canonical dimensions JSON and
    would violate the partial UNIQUE ``(project, dimensions) WHERE
    dimensions IS NOT NULL`` index on the second UPDATE. The loop
    catches :class:`sqlite3.IntegrityError` and retries the row with
    the strategy suffixed by the row id (which is monotonic + stable
    within a DB, preserving idempotency across reruns).
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone()
    if not has_table:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if "dimensions" not in cols:
        return

    rows = conn.execute(
        "SELECT id, query, tags FROM plans WHERE dimensions IS NULL"
    ).fetchall()
    if not rows:
        return

    from nexus.plans.schema import canonical_dimensions_json

    backfilled = 0
    low_conf = 0
    collisions = 0
    # Track identities set during this run so within-loop collisions
    # also get the row_id suffix treatment (the DB-level partial
    # UNIQUE index would only catch cross-run collisions because the
    # legacy rows all share dimensions IS NULL until we write).
    claimed: set[tuple[str, str]] = set()
    for row_id, query, tags in rows:
        verb, confident = _infer_plan_verb_from_query(query or "")
        base_name = _derive_plan_name_from_query(query or "")
        scope = "personal" if (tags or "").find("grown") >= 0 else "global"
        project = ""  # Backfill applies to the legacy global project.

        def _dims(name_value: str) -> str:
            return canonical_dimensions_json({
                "scope": scope,
                "strategy": name_value,
                "verb": verb,
            })

        # Pre-check for collision against both already-persisted rows
        # and rows we updated earlier in this same loop.
        name = base_name
        dims_json = _dims(name)
        key = (project, dims_json)
        db_hit = conn.execute(
            "SELECT 1 FROM plans "
            "WHERE project = ? AND dimensions = ? AND id != ? LIMIT 1",
            (project, dims_json, row_id),
        ).fetchone()
        if db_hit or key in claimed:
            # RDR-092 code-review C-1: resolve via a deterministic
            # row-id suffix so reruns produce the same identity.
            name = f"{base_name}-{row_id}"
            dims_json = _dims(name)
            key = (project, dims_json)
            collisions += 1
        claimed.add(key)

        tag_flag = "backfill" if confident else "backfill-low-conf"
        existing_tags = [t for t in (tags or "").split(",") if t]
        if tag_flag not in existing_tags:
            existing_tags.append(tag_flag)
        new_tags = ",".join(existing_tags)

        conn.execute(
            "UPDATE plans SET verb = ?, scope = ?, name = ?, "
            "dimensions = ?, tags = ? WHERE id = ?",
            (verb, scope, name, dims_json, new_tags, row_id),
        )
        if confident:
            backfilled += 1
        else:
            low_conf += 1

    conn.commit()
    _log.info(
        "Backfilled plan dimensions",
        backfilled=backfilled, low_conf=low_conf,
        total_rows=len(rows), collisions=collisions,
    )


# ── RDR-092 Phase 3.1 (plans.match_text column + FTS rebuild) ──────────────


def _add_plan_match_text_column(conn: sqlite3.Connection) -> None:
    """Add ``match_text`` column to plans and rebuild ``plans_fts``.

    RDR-092 Phase 3.1. The hybrid match-text synthesiser's output
    becomes the FTS payload so the T2 FTS lane indexes the same
    dimensional signal the T1 cosine cache embeds (R10 hybrid form).

    Steps:
      1. ``ALTER TABLE plans ADD COLUMN match_text TEXT NOT NULL
         DEFAULT ''`` when the column is absent.
      2. Drop the three ``plans_ai`` / ``plans_ad`` / ``plans_au``
         triggers and the ``plans_fts`` virtual table (FTS5 does not
         support ``ALTER COLUMN``; the only upgrade path is
         drop+recreate).
      3. Recreate ``plans_fts`` indexing
         ``(match_text, tags, project)`` instead of
         ``(query, tags, project)``.
      4. Recreate triggers targeting ``match_text``.
      5. Backfill existing rows: synthesise match_text from
         ``query`` / ``verb`` / ``name`` / ``scope`` via the same
         hybrid shape ``_synthesize_match_text`` ships with.
      6. Rebuild the FTS index from the populated rows.

    Idempotent: re-running on a DB that already has the column and
    the new FTS shape is a no-op (the column guard short-circuits).
    Must run AFTER :func:`_add_plan_dimensional_identity` and
    :func:`_backfill_plan_dimensions` so the match_text backfill has
    verb/name/scope to read.
    """
    has_plans = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone()
    if not has_plans:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}
    has_fts = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='plans_fts'"
    ).fetchone()
    if "match_text" in cols and has_fts:
        # Already on the new schema with the FTS table present;
        # nothing to do.
        return

    # RDR-092 code-review S-1 guard: a process killed between the
    # ALTER TABLE below and the FTS rebuild executescript leaves the
    # column present but plans_fts missing. Without the has_fts check
    # above, the column guard would short-circuit on retry and the
    # legacy rows would silently land with empty match_text. Falling
    # through here is safe: the ALTER is skipped when the column
    # already exists, the DROP statements tolerate missing objects,
    # and the backfill UPDATE is idempotent against already-
    # populated rows.
    _log.info("Adding plans.match_text column + rebuilding plans_fts")
    if "match_text" not in cols:
        conn.execute(
            "ALTER TABLE plans ADD COLUMN match_text TEXT NOT NULL DEFAULT ''"
        )

    # Drop the legacy triggers + FTS table up-front so the backfill
    # UPDATE below does not fan out into an external-content FTS that
    # is about to be rebuilt anyway.
    conn.executescript("""
        DROP TRIGGER IF EXISTS plans_ai;
        DROP TRIGGER IF EXISTS plans_ad;
        DROP TRIGGER IF EXISTS plans_au;
        DROP TABLE  IF EXISTS plans_fts;
    """)

    # Backfill existing rows from query / verb / name / scope using the
    # same hybrid shape save_plan produces on new inserts.
    from nexus.db.t2.plan_library import _synthesize_match_text

    rows = conn.execute(
        "SELECT id, query, verb, name, scope FROM plans"
    ).fetchall()
    for row_id, query, verb, name, scope in rows:
        synthesised = _synthesize_match_text(
            description=query, verb=verb, name=name, scope=scope,
        )
        conn.execute(
            "UPDATE plans SET match_text = ? WHERE id = ?",
            (synthesised, row_id),
        )

    # Recreate plans_fts + triggers on the populated match_text column
    # and rebuild the FTS index from existing rows.
    conn.executescript("""
        CREATE VIRTUAL TABLE plans_fts USING fts5(
            match_text, tags, project,
            content=plans, content_rowid='id'
        );

        CREATE TRIGGER plans_ai AFTER INSERT ON plans BEGIN
            INSERT INTO plans_fts(rowid, match_text, tags, project)
                VALUES (new.id, new.match_text, new.tags, new.project);
        END;
        CREATE TRIGGER plans_ad AFTER DELETE ON plans BEGIN
            INSERT INTO plans_fts(plans_fts, rowid, match_text, tags, project)
                VALUES ('delete', old.id, old.match_text, old.tags, old.project);
        END;
        CREATE TRIGGER plans_au AFTER UPDATE ON plans BEGIN
            INSERT INTO plans_fts(plans_fts, rowid, match_text, tags, project)
                VALUES ('delete', old.id, old.match_text, old.tags, old.project);
            INSERT INTO plans_fts(rowid, match_text, tags, project)
                VALUES (new.id, new.match_text, new.tags, new.project);
        END;
    """)
    conn.execute("INSERT INTO plans_fts(plans_fts) VALUES('rebuild')")
    conn.commit()
    _log.info(
        "plans.match_text migration complete",
        backfilled=len(rows),
    )


def _retire_legacy_operation_shape_plans(conn: sqlite3.Connection) -> None:
    """Delete plans whose plan_json uses the pre-RDR-078 ``operation`` shape.

    RDR-092 Phase 0a retired the ``_PLAN_TEMPLATES`` seed array but did
    not migrate the rows it had previously seeded. Those rows — plus
    user ad-hoc plans saved before the RDR-078 runner landed — carry
    step dicts like ``{"step": 1, "operation": "search", "params":
    {...}}`` rather than the current ``{"tool": "search", "args":
    {...}}``. ``plan_run`` cannot dispatch them; they exist only to
    pollute plan-match results and mask modern replacements (e.g.
    legacy ``find-documents-author`` beating the YAML builtin
    ``find-by-author``).

    Idempotent: after the first run, no legacy-shape rows remain; the
    guarded SELECT returns 0 rows on retry.
    """
    import json as _json

    candidates = conn.execute(
        "SELECT id, plan_json FROM plans WHERE plan_json LIKE '%\"operation\"%'"
    ).fetchall()
    if not candidates:
        return

    legacy_ids: list[int] = []
    for row_id, plan_json_text in candidates:
        try:
            parsed = _json.loads(plan_json_text or "{}")
        except _json.JSONDecodeError:
            continue
        steps = parsed.get("steps") if isinstance(parsed, dict) else None
        if not isinstance(steps, list) or not steps:
            continue
        # Legacy shape: any step has "operation" key AND no step has
        # "tool" key. A plan_json that happens to mention "operation"
        # inside an args payload but uses "tool" correctly is not
        # legacy — the check above (LIKE) is a pre-filter only.
        has_operation = any(
            isinstance(s, dict) and "operation" in s for s in steps
        )
        has_tool = any(
            isinstance(s, dict) and "tool" in s for s in steps
        )
        if has_operation and not has_tool:
            legacy_ids.append(int(row_id))

    if not legacy_ids:
        return

    placeholders = ",".join("?" * len(legacy_ids))
    conn.execute(
        f"DELETE FROM plans WHERE id IN ({placeholders})",
        legacy_ids,
    )
    conn.commit()
    _log.info(
        "retired_legacy_operation_shape_plans",
        deleted=len(legacy_ids),
        ids=legacy_ids,
    )


MIGRATIONS: list[Migration] = [
    Migration("1.10.0", "Memory FTS rebuild with title", migrate_memory_fts),
    Migration("2.8.0", "Add plan project column", migrate_plan_project),
    Migration("3.7.0", "Add memory access tracking", migrate_access_tracking),
    Migration("3.7.0", "Add topics tables", migrate_topics),
    Migration("3.8.0", "Add plan ttl column", migrate_plan_ttl),
    Migration("4.0.0", "Add assigned_by column", migrate_assigned_by),
    Migration("4.0.0", "Add review columns", migrate_review_columns),
    Migration(
        "4.3.0",
        "Add projection-quality columns (similarity, assigned_at, source_collection)",
        _add_projection_quality_columns,
    ),
    Migration(
        "4.4.0",
        "Add RDR-078 dimensional identity + metrics columns to plans",
        _add_plan_dimensional_identity,
    ),
    Migration(
        "4.5.0",
        "Add nx_answer_runs table (RDR-080)",
        migrate_nx_answer_runs,
    ),
    Migration(
        "4.6.0",
        "Add search_telemetry table (RDR-087)",
        migrate_search_telemetry,
    ),
    Migration(
        "4.6.1",
        "Rename search_telemetry.dropped_count to kept_count (RDR-087 errata)",
        migrate_rename_dropped_to_kept,
    ),
    Migration(
        "4.7.0",
        "Add chash_index table (RDR-086 Phase 1.1)",
        migrate_chash_index,
    ),
    Migration(
        "4.8.0",
        "Add plans.scope_tags column (RDR-091 Phase 2a)",
        _add_plan_scope_tags,
    ),
    Migration(
        "4.8.1",
        "Rewash plans.scope_tags 'all' sentinel (RDR-091 critic follow-up)",
        _rewash_plan_scope_tags_all_sentinel,
    ),
    Migration(
        "4.9.10",
        "Add hook_failures table (GH #251)",
        migrate_hook_failures,
    ),
    Migration(
        "4.9.12",
        "Backfill plan dimensions (RDR-092 Phase 0d)",
        _backfill_plan_dimensions,
    ),
    Migration(
        "4.9.13",
        "Add plans.match_text column + rebuild plans_fts (RDR-092 Phase 3)",
        _add_plan_match_text_column,
    ),
    Migration(
        "4.10.1",
        "Retire legacy operation-shape plans (nexus-4m9b)",
        _retire_legacy_operation_shape_plans,
    ),
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
            # RDR-077 RF-3: 3-tuple (doc_id, topic_id, raw_cosine_similarity).
            for doc_id, topic_id, similarity in assignments:
                taxonomy.assign_topic(
                    doc_id,
                    topic_id,
                    assigned_by="projection",
                    similarity=similarity,
                    source_collection=src,
                )
            total_assigned += len(assignments)
            elapsed = time.monotonic() - t0
            print(
                f"  [{i}/{n}] {src}: "
                f"{result.get('total_chunks', 0)} chunks, "
                f"{len(result.get('matched_topics', []))} matches, "
                f"{len(assignments)} attempted ({elapsed:.1f}s)",
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
    # Count actual rows written (INSERT OR IGNORE deduplicates; the per-call
    # 'attempted' counts may exceed actual writes). Lock taken per storage
    # review I-1 — this runs in a long upgrade context where concurrent
    # writes on the same connection are plausible.
    with taxonomy._lock:
        actual_written = taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE assigned_by = 'projection'"
        ).fetchone()[0]
    print(
        f"  Backfill complete: {actual_written} projection assignments stored "
        f"({total_assigned} attempted) in {total_elapsed:.1f}s across {n} collections.",
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

    Concurrency: ``_upgrade_lock`` is held for the entire migration run so
    a second thread opening the same database file never races the
    bootstrap+migrate sequence. An earlier version reserved the
    ``_upgrade_done`` slot *before* running migrations, relying on a
    try/except to discard on failure — but that left a window where
    ``bootstrap_version()`` could raise and a concurrent caller that
    entered under the released lock would see the path as "done" and
    proceed against a half-initialised schema (storage review C-1).
    """
    path_key = _connection_path_key(conn)
    with _upgrade_lock:
        if path_key in _upgrade_done:
            return

        # Run the entire sequence under the lock — bootstrap_version +
        # migrations + version update. Only mark the path done on
        # successful completion so a failure (or crash mid-migration)
        # is retried by the next open.
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

        # Only now — after bootstrap + migrations + version update all
        # succeeded — record the path as done.
        _upgrade_done.add(path_key)


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
