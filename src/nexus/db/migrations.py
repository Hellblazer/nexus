# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Centralised T2 schema migration registry.

Extracts all existing ``ALTER TABLE`` / FTS-rebuild migrations from domain
stores into module-level functions, each accepting ``sqlite3.Connection``.
A registry-gated runner (``apply_pending``) executes every migration
introduced AFTER the last-seen CLI version (lower bound only; RDR-170). The
``current_version`` argument is used for the post-run version stamp and the
downgrade guard, NOT as an upper bound on which migrations run.

RDR-076 (nexus-6cn). RDR-170 (nexus-j25po).
"""
from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator

import structlog

_log = structlog.get_logger()


# ── Migration exceptions ─────────────────────────────────────────────────────


class MigrationError(RuntimeError):
    """Raised when a migration cannot proceed due to a data precondition.

    Examples:
      - High-volume unmapped orphan collections in aspect PK migration
        (operator must curate ``collections.superseded_by`` first).
      - Queue not drained before the PK swap
        (pending / in_progress rows would be lost).

    The error message includes structured detail so the operator can
    identify and triage the specific rows.
    """


class MigrationRetry(Exception):
    """Raised by a migration function to signal a transient skip.

    ``apply_pending`` catches this and does NOT add the path to
    ``_upgrade_done``, so the migration is retried on the next DB open.
    Use this when a precondition (e.g. catalog absent, queue non-empty)
    is expected to clear in normal operation without operator action.

    Contrast with ``MigrationError``, which is a permanent fatal condition
    requiring explicit operator remediation.
    """

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


def migrate_dedup_root_topics(conn: sqlite3.Connection) -> None:
    """nexus-slcn7: collapse duplicate root topics, then enforce uniqueness.

    The c-TF-IDF labeler could assign the same label to distinct HDBSCAN clusters,
    and discovery was additive, so a ``(collection, label)`` could end up with
    several root-topic (``parent_id IS NULL``) rows — surfacing as duplicate
    Knowledge Map entries once the RDR-154 read-side dedup band-aid was removed
    (the root-cause prevention is the spec dedup in ``taxonomy_compute``). This
    migration repairs existing data and installs a partial unique index so the
    state cannot recur. Idempotent: re-running is a no-op once deduped.

    For each duplicated ``(collection, label)`` group of root topics it keeps the
    lowest id, moves that group's assignments / child topics / links onto the
    kept id, deletes the redundant rows, and recomputes the kept topic's
    ``doc_count`` from its live assignments.
    """
    has_topics = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='topics'"
    ).fetchone()
    if has_topics is None:
        return

    groups = conn.execute(
        """
        SELECT collection, label, MIN(id) AS keep_id, GROUP_CONCAT(id) AS ids
        FROM topics
        WHERE parent_id IS NULL
        GROUP BY collection, label
        HAVING COUNT(*) > 1
        """
    ).fetchall()

    for collection, label, keep_id, ids in groups:
        dup_ids = [int(i) for i in str(ids).split(",") if int(i) != keep_id]
        for dup_id in dup_ids:
            # Move assignments onto the kept topic (PK (doc_id, topic_id) — a doc
            # already on keep_id is skipped), then drop the redundant rows.
            conn.execute(
                "INSERT OR IGNORE INTO topic_assignments (doc_id, topic_id, assigned_by) "
                "SELECT doc_id, ?, assigned_by FROM topic_assignments WHERE topic_id = ?",
                (keep_id, dup_id),
            )
            conn.execute("DELETE FROM topic_assignments WHERE topic_id = ?", (dup_id,))
            # Re-parent any children of the dup onto the kept topic.
            conn.execute("UPDATE topics SET parent_id = ? WHERE parent_id = ?", (keep_id, dup_id))
            # Drop links touching the dup (recomputable co-occurrence edges) — same
            # strategy as the PG taxonomy-004 changeset, keeping the two backends
            # consistent and avoiding a (from, to) PK-collision repoint.
            conn.execute(
                "DELETE FROM topic_links WHERE from_topic_id = ? OR to_topic_id = ?",
                (dup_id, dup_id),
            )
            conn.execute("DELETE FROM topics WHERE id = ?", (dup_id,))
        conn.execute(
            "UPDATE topics SET doc_count = "
            "(SELECT COUNT(*) FROM topic_assignments WHERE topic_id = ?) WHERE id = ?",
            (keep_id, keep_id),
        )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_root_collection_label "
        "ON topics(collection, label) WHERE parent_id IS NULL"
    )
    conn.commit()


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
    #: RDR-142 P1.1 (nexus-aaz1r): optional READ-ONLY precondition classifier.
    #: Returns whether this step WOULD succeed / defer / gate, WITHOUT any DDL
    #: or row writes — the resolver primitive that lets ``nx upgrade --dry-run``
    #: and ``nx doctor --check-schema`` tell the truth about deferred/gated work.
    #: Steps with no defer/gate path leave this ``None`` (treated as would-succeed).
    precondition: Callable[[sqlite3.Connection], "PreconditionVerdict"] | None = None


# ── RDR-142 P1.1: read-only migration step-resolver primitive ────────────────


class StepOutcome(str, Enum):
    """What a pending migration step WOULD do if ``apply_pending`` ran now."""

    WOULD_SUCCEED = "would-succeed"
    WOULD_DEFER = "would-defer"   # raises MigrationRetry (non-fatal; retried next open)
    WOULD_GATE = "would-gate"     # raises MigrationError (fatal; needs operator action)


@dataclass(frozen=True)
class PreconditionVerdict:
    """Return type of a :class:`Migration` precondition classifier."""

    outcome: StepOutcome
    detail: str = ""        # human reason (for defer/gate)
    remediation: str = ""   # operator next-step (for gate)
    #: A WOULD_GATE that ``apply_pending`` may clear automatically (e.g. the
    #: aspect_extraction_queue undrained gate — the real wrapper attempts
    #: ``drain_worker`` first). Consumers should present these as informational
    #: ("run nx aspects drain"), NOT as a hard blocking failure.
    informational: bool = False


@dataclass(frozen=True)
class StepResolution:
    """One entry from :func:`resolve_pending_steps` — a step + its read-only verdict."""

    name: str
    introduced: str
    outcome: StepOutcome
    detail: str = ""
    remediation: str = ""
    informational: bool = False
    #: True when the step is in ``apply_pending``'s eligible set (``introduced >
    #: last_seen``) — i.e. it WILL run on the next upgrade / daemon start. False
    #: for supplementary :func:`resolve_blocking_steps` entries whose version gate
    #: has already passed: ``apply_pending`` will NOT re-run them; a non-succeed
    #: verdict there signals an incomplete table state with RUNTIME impact, not a
    #: next-start crash.
    eligible: bool = True


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


def migrate_tier_writes(conn: sqlite3.Connection) -> None:
    """Create the ``tier_writes`` table for tier-discipline telemetry (nexus-kren).

    One row per call to a tier-write MCP tool (memory_put, scratch put,
    store_put, plan_save). Records (session_id, ts, tool, tier, agent,
    project, target_title) so that ``nx tier-status`` can audit per-
    session discipline and ``nx doctor --check-tier-discipline`` can
    flag substantive sessions with no write-back.

    Phase 1 of the tier-discipline restoration initiative. Past mining
    of 2622 transcripts (memory: past-conversation-mining-2026-05-06)
    showed only 1.7% of pre-trim sessions wrote back; PR #519's
    surgical hook restoration was only possible because the trim left
    a measurable footprint. This table is the equivalent measurement
    rung for today's restorations.

    Idempotent — no-op if the table already exists. Called lazily from
    ``_record_tier_write`` so the table is created on first use.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tier_writes'"
    ).fetchone()
    if row is not None:
        return
    conn.executescript("""
        CREATE TABLE tier_writes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id   TEXT    NOT NULL,
            ts           TEXT    NOT NULL,
            tool         TEXT    NOT NULL,
            tier         TEXT    NOT NULL,
            agent        TEXT,
            project      TEXT,
            target_title TEXT
        );
        CREATE INDEX idx_tier_writes_session ON tier_writes(session_id);
        CREATE INDEX idx_tier_writes_ts      ON tier_writes(ts);
        CREATE INDEX idx_tier_writes_tool    ON tier_writes(tool);
    """)
    conn.commit()
    _log.info("Migrated: created tier_writes table (nexus-kren)")


def migrate_claude_assisted_remediation_consents(conn: sqlite3.Connection) -> None:
    """Create the ``claude_assisted_remediation_consents`` table (RDR-182 P1.2,
    nexus-ykzbj.6).

    Consent AUDIT for the opt-in ``claude_assisted_remediation.enabled`` flag
    (RDR-182 Gap 3 / Technical Design "Consent audit (net-new)"): one row per
    grant OR revoke event. ``granted`` makes both directions of the toggle
    first-class rows — the flag is durable but revocable
    (``nx config set claude_assisted_remediation.enabled false``), so the
    audit trail must retain the revoke event, not just overwrite the grant.
    Append-only: a revoke never deletes or updates a prior grant row.

    ``scope`` is the consent surface (e.g. ``"remediate:chash-poison"``);
    ``ts`` is caller-supplied (no wall-clock read in the store — the caller
    owns the clock, matching the deterministic-test convention elsewhere in
    this module).

    Idempotent — no-op if the table already exists. Called lazily from
    ``Telemetry.record_consent`` so the table is created on first use,
    mirroring ``migrate_tier_writes`` / ``migrate_nx_answer_runs``.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='claude_assisted_remediation_consents'"
    ).fetchone()
    if row is not None:
        return
    conn.executescript("""
        CREATE TABLE claude_assisted_remediation_consents (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            scope   TEXT    NOT NULL,
            ts      TEXT    NOT NULL,
            granted INTEGER NOT NULL
        );
        CREATE INDEX idx_consents_scope ON claude_assisted_remediation_consents(scope);
    """)
    conn.commit()
    _log.info(
        "migration_created_consents_table",
        table="claude_assisted_remediation_consents",
        bead="nexus-ykzbj.6",
    )


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

    ``HookRegistry.fire_single`` / ``fire_batch`` / ``fire_document``
    in ``nexus.hook_registry`` wrap every post-store hook in a per-hook
    ``try/except`` — a failing hook (e.g.
    ``taxonomy_assign_batch_hook`` raising on missing centroids or a
    ChromaDB timeout) logs a warning and moves on so the enclosing
    write path never rolls back. The dropped failure is currently
    invisible outside structlog output.

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


def migrate_hook_failures_batch_columns(conn: sqlite3.Connection) -> None:
    """Add ``batch_doc_ids`` + ``is_batch`` columns to ``hook_failures``
    for RDR-095 batch-shape failure capture.

    Additive only: existing scalar-doc_id rows are untouched. New batch
    failures populate ``batch_doc_ids`` (JSON-encoded list) and set
    ``is_batch=1``; the legacy ``doc_id`` column carries a representative
    scalar (first id in the batch) so existing scalar readers continue to
    render something meaningful. The reader update for batch shape lands
    in Phase 3 (``nx taxonomy status``).

    Idempotent: no-op when columns already exist or when ``hook_failures``
    has not yet been created (4.9.10 migration runs first in the chain).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hook_failures'"
    ).fetchone()
    if row is None:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hook_failures)").fetchall()}
    changed = False
    if "batch_doc_ids" not in cols:
        conn.execute("ALTER TABLE hook_failures ADD COLUMN batch_doc_ids TEXT")
        changed = True
    if "is_batch" not in cols:
        conn.execute(
            "ALTER TABLE hook_failures ADD COLUMN is_batch INTEGER NOT NULL DEFAULT 0"
        )
        changed = True
    # Commit + log only on an actual schema change: record_hook_failure calls
    # this on every write, so an unconditional commit/log would spam the
    # structured log with spurious "Migrated" events (nexus-9613q review H1).
    if changed:
        conn.commit()
        _log.info("Migrated: hook_failures.batch_doc_ids + is_batch (RDR-095)")


def migrate_hook_failures_chain_column(conn: sqlite3.Connection) -> None:
    """Add ``chain`` TEXT column to ``hook_failures`` for RDR-089 P0.1.

    Replaces the previous ``is_batch`` boolean encoding with an enum-like
    text column that is forward-compatible as new chain shapes land.
    Values:

    * ``'single'`` — the original single-document chain (RDR-070).
    * ``'batch'`` — the batch chain (RDR-095).
    * ``'document'`` — the document-grain chain introduced by RDR-089.

    Additive only: ``is_batch`` and ``batch_doc_ids`` are retained for
    back-compat with pre-4.14.2 readers. Existing write paths dual-write
    ``chain`` alongside ``is_batch`` (see ``_record_batch_hook_failure``);
    new readers may prefer ``chain``. Known consumers that still read
    ``is_batch``: ``src/nexus/commands/taxonomy_cmd.py`` (status
    display) and ~11 test files exercising the 4.14.1 schema directly.
    A future migration may drop ``is_batch`` once those consumers
    migrate to ``chain``.

    Data backfill: ``UPDATE hook_failures SET chain='batch' WHERE
    is_batch=1`` so historical RDR-095 rows are correctly classified
    after this migration runs. Rows with ``is_batch=0`` keep the column
    default ``'single'`` — no UPDATE needed.

    Idempotent: no-op when the column already exists or when
    ``hook_failures`` has not yet been created (4.9.10 migration runs
    first in the chain).
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='hook_failures'"
    ).fetchone()
    if row is None:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(hook_failures)").fetchall()}
    if "chain" not in cols:
        conn.execute(
            "ALTER TABLE hook_failures "
            "ADD COLUMN chain TEXT NOT NULL DEFAULT 'single'"
        )
        # Backfill historical batch rows captured at the 4.14.1 schema.
        if "is_batch" in cols:
            conn.execute(
                "UPDATE hook_failures SET chain='batch' WHERE is_batch=1"
            )
        # Commit + log only on an actual schema change: record_hook_failure
        # calls this on every write, so an unconditional commit/log would spam
        # the structured log with spurious "Migrated" events (nexus-9613q H1).
        conn.commit()
        _log.info("Migrated: hook_failures.chain enum column (RDR-089)")


def migrate_document_aspects_table(conn: sqlite3.Connection) -> None:
    """Create the ``document_aspects`` table for RDR-089 P1.1.

    Schema (locked by RDR — see ``docs/rdr/rdr-089-structured-aspect-
    extraction-at-ingest.md``):

      - collection             TEXT NOT NULL
      - source_path            TEXT NOT NULL
      - problem_formulation    TEXT
      - proposed_method        TEXT
      - experimental_datasets  TEXT     -- JSON array (may be NULL)
      - experimental_baselines TEXT     -- JSON array (may be NULL)
      - experimental_results   TEXT
      - extras                 TEXT     -- JSON object (may be NULL)
      - confidence             REAL
      - extracted_at           TEXT NOT NULL
      - model_version          TEXT NOT NULL
      - extractor_name         TEXT NOT NULL
      - PRIMARY KEY (collection, source_path)

    Compound PK rationale: per-chunk doc_id is intentionally not in
    schema. Multiple chunks of the same source document map to a
    single aspect row. The store's upsert semantics are COMPLETE
    OVERWRITE — each new extraction replaces the previous row
    verbatim (no diff/merge, no per-field stability check).

    Secondary index ``idx_document_aspects_extractor`` supports the
    ``list_by_extractor_version(name, max_version)`` query used by
    re-extraction logic to find rows whose ``extractor_name`` matches
    AND whose ``model_version`` is strictly below a threshold.

    Idempotent: ``CREATE IF NOT EXISTS`` makes re-application a no-op.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS document_aspects (
            collection             TEXT NOT NULL,
            source_path            TEXT NOT NULL,
            problem_formulation    TEXT,
            proposed_method        TEXT,
            experimental_datasets  TEXT,
            experimental_baselines TEXT,
            experimental_results   TEXT,
            extras                 TEXT,
            confidence             REAL,
            extracted_at           TEXT NOT NULL,
            model_version          TEXT NOT NULL,
            extractor_name         TEXT NOT NULL,
            PRIMARY KEY (collection, source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_document_aspects_extractor
            ON document_aspects(extractor_name, model_version);
    """)
    conn.commit()
    _log.info("Migrated: created document_aspects table (RDR-089)")


def migrate_document_highlights_table(conn: sqlite3.Connection) -> None:
    """Create the ``document_highlights`` table for RDR-139 Layer E.

    Per-document DEVONthink highlight / mention markdown notes, keyed by the
    catalog tumbler (``doc_id``). Deliberately separate from ``document_aspects``
    so free-text highlights do not contend with the aspect worker's whole-row
    overwrite or its confidence gate.

    Idempotent: ``CREATE IF NOT EXISTS``. The ``DocumentHighlights`` store also
    self-creates this table on construction, so fresh installs and tests get it
    without waiting on this migration (mirrors ``document_aspects``).
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS document_highlights (
            doc_id        TEXT PRIMARY KEY,
            source_uri    TEXT,
            collection    TEXT,
            highlights_md TEXT,
            mentions_md   TEXT,
            ingested_at   TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_document_highlights_source_uri
            ON document_highlights(source_uri);
    """)
    conn.commit()
    _log.info("Migrated: created document_highlights table (RDR-139 Layer E)")


def migrate_aspect_extraction_queue_table(conn: sqlite3.Connection) -> None:
    """Create the ``aspect_extraction_queue`` table for RDR-089
    follow-up (nexus-qeo8).

    Durable WAL buffer feeding the async aspect-extraction worker. The
    P1.3 spike invalidated Critical Assumption #2 (per-doc <3 s);
    inline synchronous extraction is replaced by the
    enqueue→worker→upsert pattern.

    Schema:

      - collection      TEXT NOT NULL
      - source_path     TEXT NOT NULL
      - content_hash    TEXT NOT NULL DEFAULT ''  (hint for downstream)
      - status          TEXT NOT NULL DEFAULT 'pending'
                                       (pending | in_progress | failed)
      - retry_count     INTEGER NOT NULL DEFAULT 0
      - enqueued_at     TEXT NOT NULL
      - last_attempt_at TEXT
      - last_error      TEXT
      - PRIMARY KEY (collection, source_path)

    PRIMARY KEY mirrors ``document_aspects`` so re-enqueue at the same
    key replaces the row in place. Secondary index on ``status``
    keeps the worker's per-poll SELECT an index seek (the worker
    polls every 2 s by default).

    Idempotent: ``CREATE IF NOT EXISTS`` makes re-application a no-op.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS aspect_extraction_queue (
            collection      TEXT NOT NULL,
            source_path     TEXT NOT NULL,
            content_hash    TEXT NOT NULL DEFAULT '',
            content         TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'pending',
            retry_count     INTEGER NOT NULL DEFAULT 0,
            enqueued_at     TEXT NOT NULL,
            last_attempt_at TEXT,
            last_error      TEXT,
            PRIMARY KEY (collection, source_path)
        );
        CREATE INDEX IF NOT EXISTS idx_aspect_queue_status
            ON aspect_extraction_queue(status);
    """)
    conn.commit()
    _log.info("Migrated: created aspect_extraction_queue table (RDR-089 follow-up)")


def migrate_aspect_promotion_log_table(conn: sqlite3.Connection) -> None:
    """Create the ``aspect_promotion_log`` audit table for RDR-089
    Phase E (extras → fixed-column promotion).

    Replaces lazy ``CREATE IF NOT EXISTS`` in
    ``nexus.aspect_promotion._ensure_audit_table`` with a registered
    migration so ``nx doctor --check-schema`` can audit the table's
    presence and operators restoring T2 from backup get the table
    even before a first promotion call.

    Substantive critic finding: lazy table creation was breaking
    the auditability claim — a backup-restored DB had no record of
    the table's existence in the migration registry, and
    ``check-schema`` did not surface the gap.

    Idempotent: ``CREATE IF NOT EXISTS`` makes re-application a
    no-op. The lazy ``_ensure_audit_table`` call in the promotion
    module continues to exist as a defensive guard for any
    extreme-legacy DB that bypassed migrations entirely.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS aspect_promotion_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            field_name      TEXT NOT NULL,
            sql_type        TEXT NOT NULL,
            column_added    INTEGER NOT NULL,
            rows_backfilled INTEGER NOT NULL DEFAULT 0,
            rows_pruned     INTEGER NOT NULL DEFAULT 0,
            pruned          INTEGER NOT NULL DEFAULT 0,
            promoted_at     TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_aspect_promotion_log_field
            ON aspect_promotion_log(field_name);
    """)
    conn.commit()
    _log.info(
        "Migrated: created aspect_promotion_log table (RDR-089 Phase E)"
    )


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


def migrate_frecency_projection_table(conn: sqlite3.Connection) -> None:
    """Create the ``frecency`` projection table (RDR-101 Phase 1 PR D).

    Phase 0 design gap 1 surfaced that RDR-101 §Entities omitted a
    Frecency projection while Phase 5 plans to relocate
    ``frecency_score`` / ``ttl_days`` / ``miss_count`` /
    ``last_hit_at`` / ``embedded_at`` off T3 chunk metadata. This
    migration ships the schema; Phase 5 will fill in the read/write
    paths once the relocation work begins.

    Schema choices (Phase 1 simpler-path direction, 2026-05-01):

    - ``chunk_id`` is the FK back to ``Chunk.chunk_id`` (the Chroma
      natural ID, also the PK of the future Phase 5 ``chunks`` table).
      Phase 1 has no ``chunks`` table to FK into, so the relationship
      is enforced by convention; Phase 5 makes it a real FK.
    - ``expires_at`` is NOT a column. Decay queries derive it from
      ``(embedded_at + ttl_days)`` at read time. If the read pattern
      ever forces a ``WHERE expires_at < ?`` index seek, a generated
      column or a follow-up migration can materialize it; Phase 1
      does not predict that need.
    - The Frecency projection is a mutable side table — Phase 1 does
      NOT participate in the event log. ``ChunkAccessed`` /
      ``ChunkMissed`` events are out of scope for now; if the doctor
      verb later needs to verify frecency state matches the log, a
      sub-RDR can introduce them.

    Idempotent: ``CREATE IF NOT EXISTS`` makes re-application a no-op.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS frecency (
            chunk_id        TEXT PRIMARY KEY,
            embedded_at     TEXT NOT NULL DEFAULT '',
            ttl_days        INTEGER NOT NULL DEFAULT 0,
            frecency_score  REAL NOT NULL DEFAULT 0,
            miss_count      INTEGER NOT NULL DEFAULT 0,
            last_hit_at     TEXT NOT NULL DEFAULT ''
        );
    """)
    conn.commit()
    _log.info("Migrated: created frecency projection table (RDR-101 Phase 1 PR D)")


def migrate_chash_index_rename_doc_id(conn: sqlite3.Connection) -> None:
    """Rename ``chash_index.doc_id`` to ``chunk_chroma_id`` (RDR-101 Phase 0).

    The original column name collides with RDR-101's ``Document.doc_id``
    (UUID7 document identity); this column has always carried the
    ChromaDB-scoped chunk natural ID. Phase 0 deliverable nexus-o6aa.3
    (``docs/rdr/post-mortem/rdr-101-rdr086-collision.md``) chose Option A
    (rename) ahead of Phase 3 to remove the disambiguation tax across
    every reader joining catalog and chash_index.

    SQLite 3.25+ supports ``ALTER TABLE ... RENAME COLUMN`` natively. The
    primary key ``(chash, physical_collection)`` is unaffected; the
    secondary index ``idx_chash_index_collection`` on
    ``physical_collection`` is unaffected.

    Idempotent in three states:
      1. Table absent (fresh install): no-op (the create-if-not-exists
         path in ``ChashIndex._init_schema`` and ``migrate_chash_index``
         already use the new name).
      2. Table present with ``chunk_chroma_id`` column (already migrated):
         no-op.
      3. Table present with legacy ``doc_id`` column: rename column.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chash_index'"
    ).fetchone()
    if row is None:
        return
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(chash_index)").fetchall()
    }
    if "chunk_chroma_id" in cols:
        return
    if "doc_id" not in cols:
        # Table exists but has neither column — schema is unrecognized.
        # Log a warning so an operator can correlate it with the doctor
        # verb's later divergence report rather than only finding out
        # at query time when production code reads chunk_chroma_id.
        _log.warning(
            "chash_index_unrecognized_schema",
            cols=sorted(cols),
            note=(
                "chash_index table exists but has neither doc_id nor "
                "chunk_chroma_id; skipping rename. Run nx catalog "
                "doctor to investigate."
            ),
        )
        return
    conn.execute(
        "ALTER TABLE chash_index RENAME COLUMN doc_id TO chunk_chroma_id"
    )
    conn.commit()


def _add_plan_disabled_at(conn: sqlite3.Connection) -> None:
    """Add the ``disabled_at`` column to ``plans`` (nexus-mrzp).

    Soft-disable lets operators retire a plan from the matcher without
    deleting the row (preserves run history; supports A/B and rollback).
    The column is nullable: ``NULL`` means active, an ISO-8601 timestamp
    means disabled at that time.

    Idempotent via ``PRAGMA table_info`` guard. No-op on a DB without
    the ``plans`` table (fresh install before plan_library instantiates).
    """
    has_table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='plans'"
    ).fetchone()
    if not has_table:
        return

    cols = {row[1] for row in conn.execute("PRAGMA table_info(plans)").fetchall()}
    if "disabled_at" in cols:
        return
    _log.info("Adding plans column", column="disabled_at")
    conn.execute("ALTER TABLE plans ADD COLUMN disabled_at TEXT")
    conn.commit()


def _add_plan_scope_tags(conn: sqlite3.Connection) -> None:
    """Add the ``scope_tags`` column to ``plans``.

    RDR-091 Phase 2a (bead ``nexus-x6pr``). DDL-only now (RDR-120 §A8
    / nexus-rv7x6): the per-row inference backfill body moved to
    ``nx plan repair scope-tags``. Operators upgrading from pre-4.8.0
    must run the verb to populate the column.

    Idempotent via the column guard. No-op on a DB without a
    ``plans`` table.
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
        conn.commit()


def _rewash_plan_scope_tags_all_sentinel(conn: sqlite3.Connection) -> None:
    """RDR-091 critic follow-up (``nexus-dfok``). Pure data rewash;
    moved to ``nx plan repair scope-tags`` (RDR-120 §A8 / nexus-rv7x6).
    No-op now; step retained so the version chain remains monotone.
    """
    return


# ── RDR-092 Phase 0d.1 (plan-dimensions backfill) ──────────────────────────
#
# RDR-120 §A8 / nexus-rv7x6: the inference helpers
# (_BACKFILL_VERB_STEMS, _infer_plan_verb_from_query,
# _derive_plan_name_from_query, etc.) moved to nexus.plans.repair
# alongside the dispatch body. The migration step below is a no-op
# now; the verb owns the work.


def _backfill_plan_dimensions(conn: sqlite3.Connection) -> None:
    """RDR-092 Phase 0d.1. Pure data backfill; moved to
    ``nx plan repair dimensions`` (RDR-120 §A8 / nexus-rv7x6).
    No-op now; step retained so the version chain remains monotone.
    """
    return


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

    # Drop legacy triggers + FTS table; recreate against match_text.
    # RDR-120 §A8 / nexus-rv7x6: the per-row backfill UPDATE that
    # populated match_text from query/verb/name/scope moved to
    # ``nx plan repair match-text``. After this migration runs the
    # column exists at DEFAULT '' and the FTS rebuild produces empty
    # entries; the verb populates rows on the consumer's terms and
    # the AFTER UPDATE trigger refreshes FTS per row.
    conn.executescript("""
        DROP TRIGGER IF EXISTS plans_ai;
        DROP TRIGGER IF EXISTS plans_ad;
        DROP TRIGGER IF EXISTS plans_au;
        DROP TABLE  IF EXISTS plans_fts;

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
    _log.info("plans.match_text DDL migration complete")


def _retire_legacy_operation_shape_plans(conn: sqlite3.Connection) -> None:
    """RDR-092 Phase 0a legacy-shape DELETE. Pure data sweep; moved
    to ``nx plan repair retire-legacy`` (RDR-120 §A8 / nexus-rv7x6).
    No-op now; step retained so the version chain remains monotone.
    """
    return


def _backfill_builtin_bindings(conn: sqlite3.Connection) -> None:
    """RDR-091 nexus-80tk follow-up. Patches required_bindings /
    optional_bindings into legacy builtin rows. Pure data backfill;
    moved to ``nx plan repair builtin-bindings`` (RDR-120 §A8 /
    nexus-rv7x6). No-op now; step retained so the version chain
    remains monotone.
    """
    return


def migrate_document_aspects_source_uri(conn: sqlite3.Connection) -> None:
    """RDR-096 P2.1: add ``source_uri`` TEXT column to ``document_aspects``.

    DDL-only (substrate boundary, RDR-120 §A8 / nexus-6y2a9). The
    per-row backfill body that previously ran here moved to the
    consumer verb ``nx aspects backfill-source-uri``. Operators
    upgrading from a pre-4.16.0 install must run the verb before the
    next upgrade pass; ``migrate_drop_source_path_column`` (4.31.0)
    refuses to drop ``source_path`` until every row has a non-empty
    ``source_uri``, and the error message names the verb.

    Idempotent:

    * ``ALTER TABLE`` is gated by ``PRAGMA table_info`` so re-runs
      do not re-add the column.
    * No-op when the table doesn't exist (defensive, matches the
      pattern of older RDR-089 migrations on this same registry).
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()}
    if not cols:
        return
    if "source_uri" not in cols:
        conn.execute("ALTER TABLE document_aspects ADD COLUMN source_uri TEXT")
        conn.commit()


def migrate_document_aspects_source_uri_backfill_empty(conn: sqlite3.Connection) -> None:
    """nexus-pnje (4.26.2): pure-data backfill of empty ``source_uri``
    rows. Now a no-op (RDR-120 §A8 / nexus-6y2a9): the body moved to
    ``nx aspects backfill-source-uri``.

    The migration step stays registered so the version chain remains
    monotone; running it on an upgrade-from-pre-4.26.2 install is
    legal and produces no writes. Operators with rows that still
    have NULL/empty ``source_uri`` must run the consumer verb
    explicitly; ``migrate_drop_source_path_column`` (4.31.0) raises
    MigrationError until they do.
    """
    return


def migrate_drop_source_path_column(conn: sqlite3.Connection) -> None:
    """nexus-ocu9.11 (RDR-096 P5.2 final deprecation): drop
    ``source_path`` from ``document_aspects``.

    Pre-conditions enforced as a hard audit:

    * Column exists in the live schema. If not, the migration is a
      no-op (already dropped).
    * Every row has ``source_uri`` populated (NOT NULL and not the
      empty string). The two-release deprecation window had two
      backfill migrations land first
      (``migrate_document_aspects_source_uri`` at 4.16.0 and
      ``migrate_document_aspects_source_uri_backfill_empty`` /
      ``nexus-pnje`` at 4.26.2); after both, every row should be
      addressable by URI alone. If any row still has NULL or empty
      source_uri, the audit raises ``MigrationError`` and the
      migration aborts so the operator can triage rather than
      silently destroying the only addressing path.

    Implementation: when ``source_path`` is still part of the PRIMARY
    KEY (``je0b`` was skipped because the catalog was absent), SQLite
    refuses ``ALTER TABLE ... DROP COLUMN``. Falls back to the 4-step
    table-rebuild pattern in that case. Otherwise uses the simple
    DROP COLUMN path. Idempotent on the column-presence check.
    """
    cols = {
        r[1] for r in conn.execute(
            "PRAGMA table_info(document_aspects)"
        ).fetchall()
    }
    if not cols:
        return
    if "source_path" not in cols:
        return

    bad_rows = conn.execute(
        "SELECT COUNT(*) FROM document_aspects "
        "WHERE source_uri IS NULL OR source_uri = ''"
    ).fetchone()[0]
    if bad_rows > 0:
        raise MigrationError(
            f"nexus-ocu9.11: refusing to drop document_aspects.source_path "
            f"because {bad_rows} row(s) still have NULL or empty "
            f"source_uri. After dropping the column those rows would be "
            f"unaddressable. Run `nx aspects backfill-source-uri --apply` "
            f"to populate the URIs, then re-run `nx upgrade`. (Per "
            f"RDR-120 §A8 / nexus-6y2a9 the backfill is consumer-driven; "
            f"the substrate no longer runs it at startup.)"
        )

    pk_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()
        if r[5] > 0
    }
    if "source_path" in pk_cols:
        # ``je0b`` (4.30.0) hasn't run yet (typically because the catalog is
        # absent and je0b raised MigrationRetry). Dropping a PK column is
        # refused by SQLite; rebuilding the table here would diverge from
        # the je0b post-state and break the runtime upsert path that still
        # writes ``source_path`` as a denorm cache. Defer: ``apply_pending``
        # leaves the version unbumped and re-runs all skipped migrations on
        # the next DB open. Once the catalog exists and je0b succeeds,
        # ``source_path`` is no longer in the PK and the simple DROP path
        # below applies.
        raise MigrationRetry(
            "source_path still in PRIMARY KEY — defer until "
            "migrate_document_aspects_pk_to_doc_id (je0b) has run"
        )

    conn.execute("ALTER TABLE document_aspects DROP COLUMN source_path")
    conn.commit()
    _log.info(
        "migrate_drop_source_path_column",
        table="document_aspects",
    )


def migrate_drop_null_aspect_rows(conn: sqlite3.Connection) -> None:
    """RDR-096 P2.2: drop pre-RDR-096 read-failure rows. Now a no-op
    (RDR-120 §A8 / nexus-6y2a9): the seven-clause-discriminator DELETE
    body moved to ``nx aspects gc-pre-rdr096``.

    The migration step stays registered so the version chain remains
    monotone; running it on an upgrade-from-pre-4.16.0 install is
    legal and produces no writes. Operators who want the historical
    cleanup must run the consumer verb explicitly.
    """
    return


# ── RDR-108 Phase 1c: Aspect PK migration helpers ───────────────────────────

#: Default row count above which an unmapped orphan collection triggers a
#: fail-loud error rather than a silent hard-delete. Operators must curate
#: ``collections.superseded_by`` for these (or run ``nx aspects gc-fixtures``
#: for known test-fixture prefixes) before running the migration.
#: OBS-4: Override at runtime with NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD
#: env var (read fresh on each call to _check_high_volume_orphans).
#:
#: RDR-120 §A8 / nexus-yulol: the fixture-DELETE block that previously
#: ran at Step 2 of each PK-swap migration was carved out to the
#: consumer verb ``nx aspects gc-fixtures``. The substrate retains
#: only the structurally-required PK swap; operators run the fixture
#: cleanup explicitly against named patterns. ``_FIXTURE_COLLECTION_PATTERNS``
#: and ``_is_fixture_collection`` moved to ``nexus.commands.aspects``.
_HIGH_VOLUME_ORPHAN_THRESHOLD: int = 10


def _attach_catalog(conn: sqlite3.Connection, catalog_db_path: Path) -> None:
    """Attach the catalog DB as ``cat_db`` on *conn*.

    SQLite ATTACH DATABASE allows cross-DB JOINs without reading the
    entire catalog into Python. The caller must call
    ``conn.execute("DETACH DATABASE cat_db")`` when finished to avoid
    leaving dangling attached schemas on the connection.
    """
    conn.execute("ATTACH DATABASE ? AS cat_db", (str(catalog_db_path),))


def _detach_catalog(conn: sqlite3.Connection) -> None:
    """Detach ``cat_db`` from *conn* (idempotent — ignored if not attached)."""
    try:
        conn.execute("DETACH DATABASE cat_db")
    except sqlite3.OperationalError:
        pass  # Not attached — no-op.


def _backfill_doc_ids_via_catalog(
    conn: sqlite3.Connection,
    *,
    table: str,
) -> None:
    """Two-pass backfill of the ``doc_id`` column in *table* using the
    catalog ``documents`` table (attached as ``cat_db``).

    Pass 1: direct JOIN on (collection = physical_collection) AND
            (source_path = file_path).
    Pass 2: follow ``collections.superseded_by`` one level to handle
            legacy collection names that were superseded after indexing.

    Fixture rows and remaining unmapped rows are NOT touched here;
    callers handle those separately.
    """
    # Pass 1: direct match — only update rows where the subquery finds a match.
    # The INNER JOIN form avoids the "subquery returns NULL" NOT NULL violation
    # that a correlated subquery with no match would produce.
    conn.execute(f"""
        UPDATE {table}
        SET doc_id = cat_db.documents.tumbler
        FROM cat_db.documents
        WHERE {table}.collection = cat_db.documents.physical_collection
          AND {table}.source_path = cat_db.documents.file_path
          AND {table}.doc_id = ''
    """)
    conn.commit()

    # Pass 2: one-hop supersede chain — only for rows still unmapped.
    # Logic: find the collection row where collections.name matches the
    # aspect's legacy collection, then look up the document in the
    # successor collection (collections.superseded_by) with the same file_path.
    conn.execute(f"""
        UPDATE {table}
        SET doc_id = cat_db.documents.tumbler
        FROM cat_db.documents
        JOIN cat_db.collections ON cat_db.documents.physical_collection = cat_db.collections.superseded_by
        WHERE cat_db.collections.superseded_by != ''
          AND cat_db.collections.name = {table}.collection
          AND cat_db.documents.file_path = {table}.source_path
          AND {table}.doc_id = ''
    """)
    conn.commit()


def _high_volume_orphan_threshold() -> int:
    """OBS-4: the high-volume-orphan gate threshold, read from
    ``NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD`` (default
    ``_HIGH_VOLUME_ORPHAN_THRESHOLD`` = 10) on every call.

    RDR-142 P1.1: shared by the real gate (:func:`_check_high_volume_orphans`)
    and the read-only resolver classifier so the two cannot drift on the cutoff.
    """
    import os as _os  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load

    return int(
        _os.environ.get(
            "NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD",
            str(_HIGH_VOLUME_ORPHAN_THRESHOLD),
        )
    )


def _orphan_gate_message(rows: list, *, table: str) -> str:
    """SIG-4: build the operator-facing high-volume-orphan gate message.

    RDR-142 P1.1: shared by the real gate and the resolver classifier so the
    remediation text the operator sees is identical whether it comes from a
    crashed ``apply_pending`` or a ``--dry-run`` / ``--check-schema`` report.
    *rows* is a list of ``(collection, n)`` pairs.
    """
    detail = "; ".join(f"{coll} ({n} rows)" for coll, n in rows)
    remediation_lines = "\n".join(
        f"  nx catalog rename-collection {coll} <new-collection-name> --yes"
        for coll, _ in rows
    )
    return (
        f"RDR-108 Phase 1c: {table} has high-volume unmapped orphan collection(s): "
        f"{detail}.\n"
        f"\n"
        f"These are derived aspect rows (RDR-089) whose source documents are no "
        f"longer in the catalog. You have two options:\n"
        f"\n"
        f"1. If the collection was RENAMED, point it at its successor so the rows "
        f"re-map instead of dropping (sets collections.superseded_by):\n"
        f"{remediation_lines}\n"
        f"   Then re-run `nx upgrade`.\n"
        f"\n"
        f"2. If the collection is STALE, let the migration drop the orphans "
        f"(aspects are regenerable via `nx enrich aspects <collection>`). Raise "
        f"the gate threshold for this run:\n"
        f"   NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD=100000 nx upgrade"
    )


def _check_high_volume_orphans(conn: sqlite3.Connection, *, table: str) -> None:
    """Raise ``MigrationError`` if any collection has >threshold unmapped rows
    (``doc_id`` still ``''``).

    RDR-142 P1.1: the cutoff (:func:`_high_volume_orphan_threshold`) and the
    operator message (:func:`_orphan_gate_message`) are shared with the
    read-only resolver classifier so the gate and its prediction cannot drift.
    """
    threshold = _high_volume_orphan_threshold()
    rows = conn.execute(f"""
        SELECT collection, COUNT(*) AS n
        FROM {table}
        WHERE doc_id = ''
        GROUP BY collection
        HAVING n > {threshold}
        ORDER BY n DESC
    """).fetchall()
    if not rows:
        return
    raise MigrationError(_orphan_gate_message(rows, table=table))


def _hard_delete_unmapped(conn: sqlite3.Connection, *, table: str) -> int:
    """DELETE rows still at doc_id='' (low-volume orphans acceptable to drop).

    Returns the number of deleted rows.
    """
    cur = conn.execute(f"DELETE FROM {table} WHERE doc_id = ''")
    conn.commit()
    return cur.rowcount


def _is_already_migrated(conn: sqlite3.Connection, *, table: str) -> bool:
    """Return True iff *table* already has ``doc_id`` as its sole PRIMARY KEY.

    Used for idempotency: re-running the migration on an already-migrated DB
    is a no-op.
    """
    # Check the PK column(s) from PRAGMA table_info (pk flag > 0 means PK member)
    pk_cols = {
        r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
        if r[5] == 1
    }
    return pk_cols == {"doc_id"}


def migrate_document_aspects_pk_to_doc_id(
    conn: sqlite3.Connection,
    *,
    catalog_db_path: Path,
) -> None:
    """RDR-108 Phase 1c: migrate ``document_aspects`` PRIMARY KEY from
    ``(collection, source_path)`` to ``(doc_id)``.

    SQLite does not support ``DROP CONSTRAINT`` or ``ADD PRIMARY KEY``, so
    this uses an ALTER pattern:

    1. Add ``doc_id TEXT NOT NULL DEFAULT ''`` to the existing table.
    2. Backfill ``doc_id`` via JOIN against catalog ``documents``.
       Pass 1: direct (collection, file_path) match.
       Pass 2: one-hop ``collections.superseded_by`` chain.
    3. Raise ``MigrationError`` if any collection has >10 unmapped rows
       (operator must curate supersede mappings first; for known test-
       fixture prefixes run ``nx aspects gc-fixtures --yes`` first).
    4. Hard-delete remaining low-volume unmapped rows.
    5. CREATE TABLE ``document_aspects_new`` with ``PRIMARY KEY (doc_id)``
       and current columns; INSERT from old (deduplicating by latest
       ``extracted_at`` when two old rows collapse to same doc_id).
    6. DROP TABLE old; RENAME new; recreate indexes.

    RDR-120 §A8 (nexus-yulol): the fixture-row hard-delete that used
    to run as Step 2 moved to the consumer verb ``nx aspects gc-fixtures``.
    Operators with known test-fixture collections must run the verb
    before the migration; otherwise high-volume fixture rows trip
    Step 3.

    Idempotent: if ``doc_id`` is already the sole PK, returns immediately.

    Args:
        conn: Open connection to ``memory.db``.
        catalog_db_path: Path to ``catalog/.catalog.db``. Used for
            cross-DB JOIN via ``ATTACH DATABASE``.
    """
    if _is_already_migrated(conn, table="document_aspects"):
        _log.info("migrate_document_aspects_pk_to_doc_id_skip", reason="already migrated")
        return

    # Check table exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()}
    if not cols:
        _log.info("migrate_document_aspects_pk_to_doc_id_skip", reason="table does not exist")
        return

    # Step 1: add doc_id column if not present
    if "doc_id" not in cols:
        conn.execute(
            "ALTER TABLE document_aspects ADD COLUMN doc_id TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()

    # Step 2: backfill doc_id via catalog JOIN
    # (RDR-120 §A8 / nexus-yulol: the fixture-DELETE pre-step that
    # previously ran here moved to ``nx aspects gc-fixtures``.)
    _attach_catalog(conn, catalog_db_path)
    try:
        _backfill_doc_ids_via_catalog(conn, table="document_aspects")

        # Step 3: fail-loud for high-volume unmapped collections
        _check_high_volume_orphans(conn, table="document_aspects")
    finally:
        _detach_catalog(conn)

    # Step 4: hard-delete remaining low-volume unmapped rows
    dropped = _hard_delete_unmapped(conn, table="document_aspects")
    if dropped:
        _log.info(
            "migrate_document_aspects_pk_to_doc_id_dropped_orphans",
            count=dropped,
        )

    # Steps 5-6: CREATE new table with (doc_id) PK, INSERT deduped rows,
    # DROP old, RENAME, recreate indexes.
    #
    # K3 fix (RDR-108 Phase 1, nexus-lh8c): atomic table swap via explicit
    # conn.execute() calls inside "with conn:" (BEGIN/COMMIT scope), replacing
    # the previous approach which used auto-committing DDL that was NOT atomic.
    # Each step in the DROP TABLE -> RENAME sequence is inside the same
    # transaction; a crash mid-way rolls back, preserving the old table.
    #
    # K8 fix (RDR-108 Phase 1, nexus-lh8c): deterministic dedup via
    # ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY extracted_at DESC) CTE,
    # replacing a fragile HAVING clause that relied on undocumented SQLite behavior.
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS document_aspects_new (
                doc_id                 TEXT NOT NULL,
                collection             TEXT NOT NULL DEFAULT \'\',
                source_path            TEXT NOT NULL DEFAULT \'\',
                problem_formulation    TEXT,
                proposed_method        TEXT,
                experimental_datasets  TEXT,
                experimental_baselines TEXT,
                experimental_results   TEXT,
                extras                 TEXT,
                confidence             REAL,
                extracted_at           TEXT NOT NULL,
                model_version          TEXT NOT NULL,
                extractor_name         TEXT NOT NULL,
                source_uri             TEXT,
                PRIMARY KEY (doc_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO document_aspects_new
                (doc_id, collection, source_path,
                 problem_formulation, proposed_method,
                 experimental_datasets, experimental_baselines,
                 experimental_results, extras, confidence,
                 extracted_at, model_version, extractor_name, source_uri)
            WITH latest AS (
                SELECT
                    doc_id, collection, source_path,
                    problem_formulation, proposed_method,
                    experimental_datasets, experimental_baselines,
                    experimental_results, extras, confidence,
                    extracted_at, model_version, extractor_name, source_uri,
                    ROW_NUMBER() OVER (
                        PARTITION BY doc_id
                        ORDER BY extracted_at DESC
                    ) AS rn
                FROM document_aspects
            )
            SELECT
                doc_id, collection, source_path,
                problem_formulation, proposed_method,
                experimental_datasets, experimental_baselines,
                experimental_results, extras, confidence,
                extracted_at, model_version, extractor_name, source_uri
            FROM latest
            WHERE rn = 1
            """
        )
        conn.execute("DROP TABLE document_aspects")
        conn.execute(
            "ALTER TABLE document_aspects_new RENAME TO document_aspects"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_aspects_extractor "
            "ON document_aspects(extractor_name, model_version)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_document_aspects_collection "
            "ON document_aspects(collection)"
        )

    _log.info("migrate_document_aspects_pk_to_doc_id_done")


def migrate_aspect_extraction_queue_pk_to_doc_id(
    conn: sqlite3.Connection,
    *,
    catalog_db_path: Path,
) -> None:
    """RDR-108 Phase 1c: migrate ``aspect_extraction_queue`` PRIMARY KEY from
    ``(collection, source_path)`` to ``(doc_id)``.

    Pre-migration drain precondition: the queue must have zero rows with
    ``status != 'failed'`` (i.e., no pending or in_progress rows). Pending /
    in_progress rows would be dropped by the CREATE-new + INSERT + DROP-old
    PK swap and the worker would silently lose those extractions. Raises
    ``MigrationError`` if the precondition fails.

    Uses the same ALTER pattern as ``migrate_document_aspects_pk_to_doc_id``.
    The queue is typically empty in production at migration time, but the
    migration handles non-empty queues (of failed rows only) correctly.

    RDR-120 §A8 (nexus-yulol): the fixture-row hard-delete that used
    to run as Step 2 moved to ``nx aspects gc-fixtures``. Operators
    with known test-fixture collections must run the verb before the
    migration.

    Idempotent: if ``doc_id`` is already the sole PK, returns immediately.

    Args:
        conn: Open connection to ``memory.db``.
        catalog_db_path: Path to ``catalog/.catalog.db``. Used for
            cross-DB JOIN via ``ATTACH DATABASE``.
    """
    if _is_already_migrated(conn, table="aspect_extraction_queue"):
        _log.info("migrate_aspect_queue_pk_to_doc_id_skip", reason="already migrated")
        return

    # Check table exists
    cols = {r[1] for r in conn.execute("PRAGMA table_info(aspect_extraction_queue)").fetchall()}
    if not cols:
        _log.info("migrate_aspect_queue_pk_to_doc_id_skip", reason="table does not exist")
        return

    # Drain precondition: fail if any pending or in_progress rows remain
    actionable = conn.execute(
        "SELECT COUNT(*) FROM aspect_extraction_queue WHERE status != 'failed'"
    ).fetchone()
    actionable_count = actionable[0] if actionable else 0
    if actionable_count > 0:
        raise MigrationError(
            f"RDR-108 Phase 1c: aspect_extraction_queue is not drained. "
            f"{actionable_count} row(s) with status pending or in_progress remain. "
            "To drain the queue and retry: run 'nx aspects drain' or call "
            "drain_worker(timeout=30)."
        )

    # Step 1: add doc_id column if not present
    if "doc_id" not in cols:
        conn.execute(
            "ALTER TABLE aspect_extraction_queue ADD COLUMN doc_id TEXT NOT NULL DEFAULT ''"
        )
        conn.commit()

    # Step 2: backfill doc_id via catalog JOIN (only for failed rows at this point).
    # (RDR-120 §A8 / nexus-yulol: fixture-DELETE pre-step moved to
    # ``nx aspects gc-fixtures``.)
    _attach_catalog(conn, catalog_db_path)
    try:
        _backfill_doc_ids_via_catalog(conn, table="aspect_extraction_queue")
        _check_high_volume_orphans(conn, table="aspect_extraction_queue")
    finally:
        _detach_catalog(conn)

    # Step 3: hard-delete remaining unmapped low-volume rows
    dropped = _hard_delete_unmapped(conn, table="aspect_extraction_queue")
    if dropped:
        _log.info(
            "migrate_aspect_queue_pk_to_doc_id_dropped_orphans",
            count=dropped,
        )

    # Steps 4-6: CREATE new table with (doc_id) PK; dedup by latest enqueued_at.
    #
    # K3 fix (RDR-108 Phase 1, nexus-lh8c): atomic table swap via explicit
    # conn.execute() inside "with conn:". See migrate_document_aspects_pk_to_doc_id
    # for the full rationale.
    #
    # K8 fix (RDR-108 Phase 1, nexus-lh8c): ROW_NUMBER() CTE replaces the fragile
    # HAVING enqueued_at = MAX(enqueued_at) no-op. Latest enqueued_at wins.
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aspect_extraction_queue_new (
                doc_id          TEXT NOT NULL,
                collection      TEXT NOT NULL DEFAULT \'\',
                source_path     TEXT NOT NULL DEFAULT \'\',
                content_hash    TEXT NOT NULL DEFAULT \'\',
                content         TEXT NOT NULL DEFAULT \'\',
                status          TEXT NOT NULL DEFAULT \'pending\',
                retry_count     INTEGER NOT NULL DEFAULT 0,
                enqueued_at     TEXT NOT NULL,
                last_attempt_at TEXT,
                last_error      TEXT,
                PRIMARY KEY (doc_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO aspect_extraction_queue_new
                (doc_id, collection, source_path,
                 content_hash, content, status,
                 retry_count, enqueued_at, last_attempt_at, last_error)
            WITH latest AS (
                SELECT
                    doc_id, collection, source_path,
                    content_hash, content, status,
                    retry_count, enqueued_at, last_attempt_at, last_error,
                    ROW_NUMBER() OVER (
                        PARTITION BY doc_id
                        ORDER BY enqueued_at DESC
                    ) AS rn
                FROM aspect_extraction_queue
            )
            SELECT
                doc_id, collection, source_path,
                content_hash, content, status,
                retry_count, enqueued_at, last_attempt_at, last_error
            FROM latest
            WHERE rn = 1
            """
        )
        conn.execute("DROP TABLE aspect_extraction_queue")
        conn.execute(
            "ALTER TABLE aspect_extraction_queue_new RENAME TO aspect_extraction_queue"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aspect_queue_status "
            "ON aspect_extraction_queue(status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_aspect_queue_collection "
            "ON aspect_extraction_queue(collection)"
        )

    _log.info("migrate_aspect_queue_pk_to_doc_id_done")


def _catalog_db_path_from_conn(conn: sqlite3.Connection) -> Path:
    """Derive the catalog DB path from the memory.db connection.

    The catalog DB lives at ``<nexus_config_dir>/catalog/.catalog.db``.
    We infer ``<nexus_config_dir>`` as the parent of the directory that
    contains ``memory.db`` (i.e., ``memory.db`` is at
    ``<nexus_config_dir>/memory.db``).

    If the path cannot be inferred (e.g., in-memory DB or non-standard
    layout), falls back to a path derived from ``NEXUS_CONFIG_DIR`` env
    or the default ``~/.config/nexus``.
    """
    # ``PRAGMA database_list`` returns (seq, name, file) triples.
    # Row with name='main' gives the file path.
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            mem_path = Path(row[2]).resolve()
            # memory.db is at <config_dir>/memory.db
            config_dir = mem_path.parent
            return config_dir / "catalog" / ".catalog.db"

    # Fallback: use NEXUS_CONFIG_DIR env or default
    import os  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load
    override = os.environ.get("NEXUS_CONFIG_DIR", "").strip()
    if override:
        return Path(override) / "catalog" / ".catalog.db"
    return Path.home() / ".config" / "nexus" / "catalog" / ".catalog.db"


def _drop_chash_index_chunk_chroma_id(conn: sqlite3.Connection) -> None:
    """Drop the ``chash_index.chunk_chroma_id`` column (RDR-108 Phase 4a /
    nexus-mmf5).

    Under RDR-108 D1 (nexus-kmb6) the chunk natural ID is
    ``chunk_text_hash[:32]`` -- a pure function of ``chash`` -- so the
    denormalized column has no remaining readers (nexus-z1mu removed
    the audit's only reader; nexus-kosc retargeted the catalog_spans
    resolver to derive ``hex_chash[:32]`` directly).

    Idempotent in three states:
      1. Table absent (fresh install): no-op (the create-if-not-exists
         path in ``ChashIndex._init_schema`` already uses the new
         schema).
      2. Table present without ``chunk_chroma_id`` (already migrated):
         no-op.
      3. Table present with ``chunk_chroma_id``: drop the column.

    SQLite 3.35+ supports ``ALTER TABLE ... DROP COLUMN`` natively. The
    primary key ``(chash, physical_collection)`` and the secondary
    index ``idx_chash_index_collection`` are unaffected.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='chash_index'"
    ).fetchone()
    if row is None:
        return
    cols = {
        r[1]
        for r in conn.execute("PRAGMA table_info(chash_index)").fetchall()
    }
    if "chunk_chroma_id" not in cols:
        return
    conn.execute("ALTER TABLE chash_index DROP COLUMN chunk_chroma_id")
    conn.commit()


def _migrate_document_aspects_pk_via_apply_pending(conn: sqlite3.Connection) -> None:
    """Wrapper for ``apply_pending`` compatibility.

    ``apply_pending`` calls migrations as ``fn(conn)`` with no extra args.
    This wrapper resolves the catalog DB path from the connection and
    delegates to ``migrate_document_aspects_pk_to_doc_id``.

    Raises ``MigrationRetry`` when the catalog is absent so the path is
    NOT marked done in ``_upgrade_done`` and will be retried on the next
    DB open once the catalog has been populated.
    """
    catalog_path = _catalog_db_path_from_conn(conn)
    if not catalog_path.exists():
        _log.warning(
            "migrate_document_aspects_pk_skip_no_catalog",
            catalog_path=str(catalog_path),
        )
        raise MigrationRetry(
            "catalog absent — retry deferred until catalog exists: "
            f"{catalog_path}"
        )
    migrate_document_aspects_pk_to_doc_id(conn, catalog_db_path=catalog_path)


def _migrate_aspect_queue_pk_via_apply_pending(conn: sqlite3.Connection) -> None:
    """Wrapper for ``apply_pending`` compatibility.

    ``apply_pending`` calls migrations as ``fn(conn)`` with no extra args.
    This wrapper resolves the catalog DB path from the connection and
    delegates to ``migrate_aspect_extraction_queue_pk_to_doc_id``.

    K2 (RDR-108 Phase 1, nexus-lh8c): attempt to drain the aspect worker
    before checking the queue. If the queue is still non-empty after the
    drain attempt, the underlying migration raises ``MigrationError``.

    Raises ``MigrationRetry`` when the catalog is absent so the path is
    NOT marked done in ``_upgrade_done`` and will be retried on the next
    DB open once the catalog has been populated.
    """
    catalog_path = _catalog_db_path_from_conn(conn)
    if not catalog_path.exists():
        _log.warning(
            "migrate_aspect_queue_pk_skip_no_catalog",
            catalog_path=str(catalog_path),
        )
        raise MigrationRetry(
            "catalog absent — retry deferred until catalog exists: "
            f"{catalog_path}"
        )
    # Attempt to drain in-flight rows before the drain precondition check.
    # Imports lazily to avoid a circular import at module load time.
    try:
        import nexus.aspect_worker as _aw  # noqa: PLC0415 — circular-dep avoidance (nexus.aspect_worker)
        memory_path = catalog_path.parent.parent / "memory.db"
        _aw.drain_worker(memory_path, timeout=30)
    except Exception as _drain_err:  # noqa: BLE001 — best-effort pre-drain; failure logged via _log.warning and the drain precondition check proceeds
        _log.warning(
            "migrate_aspect_queue_drain_attempt_failed",
            error=str(_drain_err),
        )
    migrate_aspect_extraction_queue_pk_to_doc_id(conn, catalog_db_path=catalog_path)


# ── RDR-142 P1.1: read-only precondition classifiers + resolver ──────────────
#
# Each classifier runs the SAME probes the real migration fn runs (catalog
# presence, PRAGMA table_info, drain COUNT, orphan COUNT) but does NO DDL and NO
# row writes — it reports a verdict instead of mutating or raising. The real fn
# and the classifier share the gate cutoff + message helpers; an agreement test
# (tests/test_rdr142_step_resolver.py) runs both on identical fixtures and
# asserts the verdicts match, the load-bearing anti-drift guard.


def _predict_orphan_collections_readonly(
    conn: sqlite3.Connection, *, table: str, catalog_db_path: Path
) -> "list[tuple[str, int]]":
    """READ-ONLY prediction of the high-volume-orphan gate.

    Mirrors :func:`_backfill_doc_ids_via_catalog` (Pass 1 direct match + Pass 2
    one-hop ``collections.superseded_by``) as a ``NOT EXISTS`` SELECT counting,
    per collection, the rows that WOULD remain unmapped — without running the
    backfill UPDATEs. Returns ``[(collection, n), ...]`` for collections over
    the shared threshold, or ``[]``. Considers only rows the backfill would
    consider (``doc_id = ''`` when the column exists; all rows otherwise).
    """
    threshold = _high_volume_orphan_threshold()
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    # Restrict to backfill-considered rows, exactly as the real passes do.
    unmapped_clause = "t.doc_id = ''" if "doc_id" in cols else "1=1"
    _attach_catalog(conn, catalog_db_path)
    try:
        try:
            rows = conn.execute(f"""
                SELECT t.collection, COUNT(*) AS n
                FROM {table} t
                WHERE {unmapped_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM cat_db.documents d
                      WHERE t.collection = d.physical_collection
                        AND t.source_path = d.file_path
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM cat_db.documents d
                      JOIN cat_db.collections c
                        ON d.physical_collection = c.superseded_by
                      WHERE c.superseded_by != ''
                        AND c.name = t.collection
                        AND d.file_path = t.source_path
                  )
                GROUP BY t.collection
                HAVING n > {threshold}
                ORDER BY n DESC
            """).fetchall()
        except sqlite3.OperationalError as exc:
            _log.warning(
                "orphan_predict_catalog_join_fallback",
                table=table,
                error=str(exc),
                note="catalog schema unusable for the accurate JOIN; using conservative unmapped-row count (may over-count mappable rows)",
            )
            # Catalog missing the expected documents/collections schema (corrupt
            # / partially-initialised). Rather than crash the READ-ONLY resolver —
            # which would make ``nx upgrade --dry-run`` / ``--check-schema`` fail
            # where they should report — fall back to the legacy simple
            # unmapped-row count (the pre-resolver ``_check_deferred_migrations``
            # probe). Over-counts mappable rows but never crashes; a well-formed
            # catalog takes the accurate JOIN above.
            rows = conn.execute(f"""
                SELECT t.collection, COUNT(*) AS n
                FROM {table} t
                WHERE {unmapped_clause}
                GROUP BY t.collection
                HAVING n > {threshold}
                ORDER BY n DESC
            """).fetchall()
    finally:
        _detach_catalog(conn)
    return rows


def _precondition_document_aspects_pk(conn: sqlite3.Connection) -> "PreconditionVerdict":
    """Read-only verdict for ``_migrate_document_aspects_pk_via_apply_pending``.

    Order: already-migrated / table-absent → would-succeed FIRST (a completed or
    nonexistent table is done regardless of catalog presence — checking catalog
    first would falsely report a migrated-but-catalog-absent table as deferred,
    and such a table is never in apply_pending's eligible set anyway). Then
    catalog-absent → defer, then the orphan gate — mirroring the real wrapper for
    every UNMIGRATED-table state (where catalog-absent does defer).
    """
    if _is_already_migrated(conn, table="document_aspects"):
        return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()}
    if not cols:
        return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)  # table absent → no-op
    catalog_path = _catalog_db_path_from_conn(conn)
    if not catalog_path.exists():
        return PreconditionVerdict(
            StepOutcome.WOULD_DEFER,
            detail=f"catalog absent — retry deferred until catalog exists: {catalog_path}",
        )
    orphans = _predict_orphan_collections_readonly(
        conn, table="document_aspects", catalog_db_path=catalog_path
    )
    if orphans:
        return PreconditionVerdict(
            StepOutcome.WOULD_GATE,
            detail="; ".join(f"{c} ({n} rows)" for c, n in orphans),
            remediation=_orphan_gate_message(orphans, table="document_aspects"),
        )
    return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)


def _precondition_aspect_queue_pk(conn: sqlite3.Connection) -> "PreconditionVerdict":
    """Read-only verdict for ``_migrate_aspect_queue_pk_via_apply_pending``.

    Order: already-migrated / table-absent → would-succeed first (see the
    document_aspects classifier), then catalog-absent → defer, then the undrained
    gate (read-only COUNT, no drain), then the orphan gate.
    """
    if _is_already_migrated(conn, table="aspect_extraction_queue"):
        return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(aspect_extraction_queue)").fetchall()}
    if not cols:
        return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)
    catalog_path = _catalog_db_path_from_conn(conn)
    if not catalog_path.exists():
        return PreconditionVerdict(
            StepOutcome.WOULD_DEFER,
            detail=f"catalog absent — retry deferred until catalog exists: {catalog_path}",
        )
    # Undrained gate: the real wrapper attempts a drain FIRST, so this is a SOFT
    # gate — apply_pending may clear it automatically. Marked informational so the
    # dry-run presents "run nx aspects drain", not a hard BLOCKED failure.
    actionable = conn.execute(
        "SELECT COUNT(*) FROM aspect_extraction_queue WHERE status != 'failed'"
    ).fetchone()
    if (actionable[0] if actionable else 0) > 0:
        return PreconditionVerdict(
            StepOutcome.WOULD_GATE,
            detail=f"{actionable[0]} pending/in_progress queue row(s) — not drained",
            remediation="apply_pending attempts drain_worker(timeout=30) first; run 'nx aspects drain' if it persists",
            informational=True,
        )
    orphans = _predict_orphan_collections_readonly(
        conn, table="aspect_extraction_queue", catalog_db_path=catalog_path
    )
    if orphans:
        return PreconditionVerdict(
            StepOutcome.WOULD_GATE,
            detail="; ".join(f"{c} ({n} rows)" for c, n in orphans),
            remediation=_orphan_gate_message(orphans, table="aspect_extraction_queue"),
        )
    return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)


def _precondition_drop_source_path(conn: sqlite3.Connection) -> "PreconditionVerdict":
    """Read-only verdict for ``migrate_drop_source_path_column``.

    Ordering mirrors the real fn: column-presence no-op → bad-source_uri gate →
    source_path-in-PK defer.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()}
    if not cols or "source_path" not in cols:
        return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)
    bad = conn.execute(
        "SELECT COUNT(*) FROM document_aspects WHERE source_uri IS NULL OR source_uri = ''"
    ).fetchone()[0]
    if bad > 0:
        return PreconditionVerdict(
            StepOutcome.WOULD_GATE,
            detail=f"{bad} row(s) with NULL/empty source_uri would become unaddressable",
            remediation="run `nx aspects backfill-source-uri --apply`, then re-run nx upgrade",
        )
    pk_cols = {
        r[1] for r in conn.execute("PRAGMA table_info(document_aspects)").fetchall()
        if r[5] > 0
    }
    if "source_path" in pk_cols:
        return PreconditionVerdict(
            StepOutcome.WOULD_DEFER,
            detail="source_path still in PRIMARY KEY — defer until je0b has run",
        )
    return PreconditionVerdict(StepOutcome.WOULD_SUCCEED)


def _read_only_stored_version(conn: sqlite3.Connection) -> str:
    """Read the recorded ``_nexus_version.cli_version`` WITHOUT writing.

    Unlike :func:`bootstrap_version` (which CREATEs tables + seeds the row), the
    resolver must be side-effect-free, so it reads directly and defaults to
    ``0.0.0`` when the version table/row is absent (a not-yet-bootstrapped DB —
    on which every registered step is trivially pending).
    """
    try:
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return "0.0.0"  # _nexus_version table absent
    return row[0] if row else "0.0.0"


def resolve_pending_steps(
    conn: sqlite3.Connection, current_version: str, *, last_seen: str | None = None
) -> "list[StepResolution]":
    """READ-ONLY report of which pending migration steps would succeed/defer/gate.

    RDR-142 P1.1 (nexus-aaz1r). Iterates the SAME eligible set ``apply_pending``
    uses — ``introduced > last_seen`` (lower bound only; RDR-170 dropped the
    upper bound). For each eligible step it runs the step's read-only
    precondition (or reports would-succeed when a step has none). Performs NO
    DDL and NO row writes.

    KNOWN DIVERGENCE — aspect_extraction_queue undrained gate: the real wrapper
    attempts ``drain_worker(timeout=30)`` BEFORE the drain-count check, so a
    queue with in-flight rows that the worker clears would actually SUCCEED. The
    classifier never drains (read-only), so it reports WOULD_GATE on the current
    non-empty state. A consumer (e.g. the future ``--dry-run`` rewire) should
    present this as informational — "run ``nx aspects drain``" — not a hard
    failure, to avoid crying wolf on every upgrade window with an active worker.

    ``current_version`` does NOT cap the eligible set (RDR-170 made the upper
    bound vacuous); it is accepted for signature stability with ``apply_pending``
    and possible future use. ``last_seen`` overrides the stored version when the
    caller already knows it (e.g. ``nx upgrade --force`` resets it to ``0.0.0``
    to preview a full re-migration); when ``None`` it is read READ-ONLY from
    ``_nexus_version`` (defaulting to ``0.0.0`` on an uninitialised DB, so every
    registered step reports as pending — the set ``apply_pending`` would attempt).
    """
    _ = current_version  # intentionally not used for gating (see docstring)
    effective = last_seen if last_seen is not None else _read_only_stored_version(conn)
    last_seen_t = _parse_version(effective)
    out: list[StepResolution] = []
    for m in MIGRATIONS:
        if _parse_version(m.introduced) > last_seen_t:
            verdict = (
                m.precondition(conn)
                if m.precondition is not None
                else PreconditionVerdict(StepOutcome.WOULD_SUCCEED)
            )
            out.append(
                StepResolution(
                    name=m.name,
                    introduced=m.introduced,
                    outcome=verdict.outcome,
                    detail=verdict.detail,
                    remediation=verdict.remediation,
                    informational=verdict.informational,
                    eligible=True,
                )
            )
    return out


def resolve_blocking_steps(
    conn: sqlite3.Connection, current_version: str, *, last_seen: str | None = None
) -> "list[StepResolution]":
    """READ-ONLY report for ``nx upgrade --dry-run`` (RDR-142 P2.1): the
    version-eligible steps (:func:`resolve_pending_steps`) PLUS any
    precondition-bearing step that is NOT version-eligible but whose read-only
    precondition still reports would-defer / would-gate.

    The second set is the table-state safety the pre-resolver
    ``_check_deferred_migrations`` stopgap provided: a defer/gate step whose
    table is in an incomplete state (legacy PK, high-volume orphans, undrained
    queue, ``source_path`` still present) even though the version row has
    advanced past it. Such a step is not re-run by ``apply_pending``'s
    version-gated loop, but it signals genuine incomplete migration work the
    operator must see — so dry-run coverage never regresses below the stopgap.

    Best-effort on the supplementary probe: a precondition that cannot evaluate
    against an unusual / partial schema is skipped rather than crashing the
    read-only dry-run.
    """
    eligible = resolve_pending_steps(conn, current_version, last_seen=last_seen)
    seen = {s.name for s in eligible}
    out = list(eligible)
    for m in MIGRATIONS:
        if m.precondition is None or m.name in seen:
            continue
        try:
            verdict = m.precondition(conn)
        except Exception as exc:  # noqa: BLE001 — supplementary state probe is best-effort; a precondition that cannot evaluate on a partial schema is skipped, not fatal to the read-only dry-run
            _log.debug("resolve_blocking_state_probe_skipped", name=m.name, error=str(exc))
            continue
        if verdict.outcome != StepOutcome.WOULD_SUCCEED:
            out.append(
                StepResolution(
                    name=m.name,
                    introduced=m.introduced,
                    outcome=verdict.outcome,
                    detail=verdict.detail,
                    remediation=verdict.remediation,
                    informational=verdict.informational,
                    eligible=False,  # version gate passed → runtime impact, not a next-start crash
                )
            )
    return out


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
    Migration(
        "4.10.2",
        "Backfill required/optional bindings on builtin plans (nexus-uyc6)",
        _backfill_builtin_bindings,
    ),
    Migration(
        "4.14.1",
        "Add hook_failures.batch_doc_ids + is_batch (RDR-095)",
        migrate_hook_failures_batch_columns,
    ),
    Migration(
        "4.14.2",
        "Add hook_failures.chain enum column + backfill batch rows (RDR-089)",
        migrate_hook_failures_chain_column,
    ),
    Migration(
        "4.14.2",
        "Create document_aspects table (RDR-089 P1.1)",
        migrate_document_aspects_table,
    ),
    Migration(
        "4.14.2",
        "Create aspect_extraction_queue table (RDR-089 follow-up nexus-qeo8)",
        migrate_aspect_extraction_queue_table,
    ),
    Migration(
        "4.14.2",
        "Create aspect_promotion_log table (RDR-089 Phase E)",
        migrate_aspect_promotion_log_table,
    ),
    Migration(
        "4.16.0",
        "Add document_aspects.source_uri column + backfill (RDR-096 P2.1)",
        migrate_document_aspects_source_uri,
    ),
    Migration(
        "4.16.0",
        "Drop pre-RDR-096 null-field aspect rows (RDR-096 P2.2)",
        migrate_drop_null_aspect_rows,
    ),
    Migration(
        "4.17.1",
        "Add plans.disabled_at column for soft-disable (nexus-mrzp)",
        _add_plan_disabled_at,
    ),
    Migration(
        "4.21.3",
        "Rename chash_index.doc_id to chunk_chroma_id (RDR-101 Phase 0 nexus-o6aa.3)",
        migrate_chash_index_rename_doc_id,
    ),
    Migration(
        "4.21.4",
        "Create frecency projection table (RDR-101 Phase 1 PR D nexus-knn3)",
        migrate_frecency_projection_table,
    ),
    Migration(
        "4.25.5",
        "Create tier_writes table (Phase 1A tier-discipline telemetry, nexus-kren)",
        migrate_tier_writes,
    ),
    Migration(
        "4.26.2",
        "Backfill document_aspects.source_uri rows where empty string (nexus-pnje)",
        migrate_document_aspects_source_uri_backfill_empty,
    ),
    Migration(
        "4.30.0",
        "Drop chash_index.chunk_chroma_id column (RDR-108 Phase 4a, nexus-mmf5)",
        _drop_chash_index_chunk_chroma_id,
    ),
    # nexus-4s2o reland of nexus-je0b: RDR-108 Phase 1c PK switch to
    # doc_id for document_aspects + aspect_extraction_queue. The
    # ``_resolve_doc_id`` substrate in DocumentAspects.upsert (4.31.5)
    # plus the test surgery in this commit unblock the reland.
    # nexus-ocu9.11 (drop document_aspects.source_path) shipped at
    # 4.31.0 — the deferment is closed.
    Migration(
        "4.30.0",
        "RDR-108 Phase 1c: PK switch document_aspects to doc_id (nexus-je0b)",
        _migrate_document_aspects_pk_via_apply_pending,
        precondition=_precondition_document_aspects_pk,
    ),
    Migration(
        "4.30.0",
        "RDR-108 Phase 1c: PK switch aspect_extraction_queue to doc_id (nexus-je0b)",
        _migrate_aspect_queue_pk_via_apply_pending,
        precondition=_precondition_aspect_queue_pk,
    ),
    # nexus-6xp2 reland of nexus-ocu9.11: drop document_aspects.source_path.
    # DocumentAspects.upsert/get/delete/list/rename_collection now branch
    # on _has_source_path_column(); operators/aspect_sql.py was already
    # ocu9.11-aware. Migration body raises MigrationRetry when je0b
    # hasn't run yet (source_path still in PK), so registration order
    # vs je0b is forgiving.
    Migration(
        "4.31.0",
        "RDR-096 P5.2: drop document_aspects.source_path column (nexus-ocu9.11)",
        migrate_drop_source_path_column,
        precondition=_precondition_drop_source_path,
    ),
    Migration(
        "4.32.1",
        "RDR-109 Phase 5: add document_aspects.salient_sentences column",
        lambda conn: _migrate_add_aspects_salient_sentences(conn),
    ),
    Migration(
        "5.5.0",
        "RDR-139 Layer E: add document_highlights table",
        migrate_document_highlights_table,
    ),
    Migration(
        "5.10.7",
        "nexus-slcn7: merge duplicate root topics + unique (collection,label) index",
        migrate_dedup_root_topics,
    ),
    Migration(
        "6.6.2",
        "RDR-182 P1.2: add claude_assisted_remediation_consents table (nexus-ykzbj.6)",
        migrate_claude_assisted_remediation_consents,
    ),
]


def _migrate_add_aspects_salient_sentences(conn: sqlite3.Connection) -> None:
    """RDR-109 Phase 5: add ``salient_sentences`` TEXT column to
    ``document_aspects``. JSON-encoded array of strings; NULL on rows
    written before Phase 5 ships.

    Idempotent: gated by ``PRAGMA table_info``. No-op if the table
    doesn't exist (fresh installs hit the column via the base schema in
    ``_DOCUMENT_ASPECTS_SCHEMA_SQL`` directly).
    """
    cols = {
        r[1] for r in conn.execute(
            "PRAGMA table_info(document_aspects)"
        ).fetchall()
    }
    if not cols:
        return
    if "salient_sentences" in cols:
        return
    conn.execute(
        "ALTER TABLE document_aspects ADD COLUMN salient_sentences TEXT"
    )
    conn.commit()


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
    import sys  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load
    import time  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load

    import structlog  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load

    log = structlog.get_logger()

    collections = taxonomy.get_distinct_collections()
    if not collections:
        log.info("backfill_projection_skip", reason="no topics discovered yet")
        return

    n = len(collections)
    print(  # noqa: T201 — long-running upgrade progress to stderr; structured event emitted via log.info below
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
            print(  # noqa: T201 — long-running upgrade progress to stderr (per-collection tick)
                f"  [{i}/{n}] {src}: "
                f"{result.get('total_chunks', 0)} chunks, "
                f"{len(result.get('matched_topics', []))} matches, "
                f"{len(assignments)} attempted ({elapsed:.1f}s)",
                file=sys.stderr,
            )
        except Exception as e:  # noqa: BLE001 — per-collection backfill must not abort migration; logged via log.warning
            log.warning("backfill_projection_collection_failed",
                        collection=src, exc_info=True)
            print(f"  [{i}/{n}] {src}: SKIPPED ({type(e).__name__})",  # noqa: T201 — upgrade progress to stderr; structured event via log.warning above
                  file=sys.stderr)

    # Generate co-occurrence links from the new projection assignments
    print("  Generating co-occurrence topic links...", file=sys.stderr)  # noqa: T201 — long-running upgrade progress to stderr
    try:
        link_count = taxonomy.generate_cooccurrence_links()
        print(f"  Generated {link_count} co-occurrence links.", file=sys.stderr)  # noqa: T201 — long-running upgrade progress to stderr
    except Exception:  # noqa: BLE001 — co-occurrence link gen is best-effort; logged via log.warning
        log.warning("backfill_cooccurrence_failed", exc_info=True)
        print("  Co-occurrence link generation: SKIPPED", file=sys.stderr)  # noqa: T201 — upgrade progress to stderr; structured event via log.warning above

    total_elapsed = time.monotonic() - total_start
    # Count actual rows written (INSERT OR IGNORE deduplicates; the per-call
    # 'attempted' counts may exceed actual writes). Lock taken per storage
    # review I-1 — this runs in a long upgrade context where concurrent
    # writes on the same connection are plausible.
    with taxonomy._lock:
        actual_written = taxonomy.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE assigned_by = 'projection'"
        ).fetchone()[0]
    print(  # noqa: T201 — long-running upgrade progress to stderr; structured event emitted via log.info below
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

def expected_t2_schema_version() -> str:
    """Return the highest T2 schema version this client's code can produce.

    RDR-120 P3b (nexus-e9x4l): T2Client surfaces this to the daemon
    during the connection handshake; the daemon compares against the
    schema version recorded in its own ``_nexus_version`` row and
    fails loud on mismatch.

    RDR-170: this is ``max(package_version, max(MIGRATIONS introduced))`` —
    the **registry**, not the package string, is the authority on the schema
    this code can produce. On a frozen / ahead-of-release branch (e.g. develop
    pinned at ``5.10.6`` while the registry already carries an ``introduced=
    5.10.7`` step) the package version understates the schema ``apply_pending``
    actually applies and stamps. Reporting the registry max keeps the daemon
    stamp, the client↔daemon handshake (``t2_client._do_handshake``), and the
    cold-start fast path (``_cold_start_is_current_and_wal``) in agreement.

    Same-wheel client and daemon read the identical ``MIGRATIONS`` list and so
    compute the identical value — the RDR-120 P3b "agree when running the same
    wheel" invariant is preserved, just sourced from the registry. A genuinely
    older wheel computes a lower registry max and still mismatches a newer
    daemon, so cross-wheel skew detection is intact. On a released build, where
    ``package_version >= registry_max``, this is a no-op.
    """
    try:
        from importlib.metadata import version as _pkg_version  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load

        pkg = _pkg_version("conexus")
    except Exception:  # noqa: BLE001 — best-effort version read; falls back to 0.0.0
        pkg = "0.0.0"
    # Version-aware max — NOT string max ("5.5.0" > "5.10.7" lexically).
    registry_max = max(
        (m.introduced for m in MIGRATIONS), key=_parse_version, default="0.0.0"
    )
    return registry_max if _parse_version(registry_max) > _parse_version(pkg) else pkg


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

#: RDR-128 P2: filename of the cross-process migration lock, in the same
#: directory as ``memory.db`` (the config dir).
T2_MIGRATION_LOCK_FILE: str = "t2_migration.lock"


@contextmanager
def t2_migration_flock(lock_dir: Path) -> Iterator[None]:
    """Exclusive, cross-process advisory lock for T2 schema migration (RDR-128 P2).

    Both ``nx upgrade`` and the daemon's own startup migration
    (``T2Database.bootstrap_schema``) take this lock before running
    ``apply_pending``, so the two migration paths SERIALIZE instead of
    racing on SQLite's single WAL writer lock — the structural contention
    that produced the 5.0.2-5.0.4 daemon incidents and the post-5.0.4
    crash-loop.

    Blocking ``LOCK_EX``: a second migrator WAITS for the first rather than
    failing. The lock is bound to the open file descriptor, so the OS
    releases it automatically when the fd is closed — including on process
    death. A migrator that crashes mid-run therefore never strands the
    lock; the next migrator acquires it and re-runs (``apply_pending`` is
    idempotent and only records completion on success).

    The lock file lives at ``<lock_dir>/t2_migration.lock``; both callers
    pass the directory containing ``memory.db`` so they share one lock.
    """
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / T2_MIGRATION_LOCK_FILE
    fd = -1
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)  # blocking — serialize against the other migrator
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        # Guard: if os.open itself raised, fd is still -1 (nothing to close,
        # and we must not raise NameError from the finally and mask it).
        if fd >= 0:
            os.close(fd)


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
    """Run every migration introduced AFTER last-seen (lower bound only).

    RDR-170: ``current_version`` is NOT an upper bound on which migrations
    execute — it is used only for the post-run ``_nexus_version`` stamp and the
    downgrade guard. The ``MIGRATIONS`` registry ships in the same wheel as this
    runner (a client can never hold a registered migration newer than its own
    code), so an upper bound only ever fired on a frozen / ahead-of-release
    branch, where it WRONGLY suppressed a migration whose implementation is
    present (nexus-j25po).

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
    import time as _time  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load

    path_key = _connection_path_key(conn)
    with _upgrade_lock:
        if path_key in _upgrade_done:
            return

        # OBS-1: record migration session start for telemetry.
        _t_start = _time.monotonic()
        _log.info(
            "migration_start",
            path_key=path_key,
            current_version=current_version,
        )

        # Run the entire sequence under the lock — bootstrap_version +
        # migrations + version update. Only mark the path done on
        # successful completion so a failure (or crash mid-migration)
        # is retried by the next open.
        last_seen = bootstrap_version(conn)
        last_seen_t = _parse_version(last_seen)
        current_t = _parse_version(current_version)

        # Filter and execute eligible migrations
        steps_run = 0
        any_skipped = False
        for m in MIGRATIONS:
            m_ver = _parse_version(m.introduced)
            # RDR-170: gate on the lower bound ONLY. The MIGRATIONS registry
            # ships in the same wheel as this runner, so a client can never hold
            # a registered migration newer than its own code; the old upper
            # bound (``m_ver <= current_t``) therefore only ever fired on a
            # frozen / ahead-of-release branch (e.g. develop pinned below the
            # next release), where it WRONGLY suppressed a migration whose
            # implementation is present and intended to run. ``current_version``
            # is retained only for the post-run version stamp + guards below.
            if m_ver > last_seen_t:
                _t_step = _time.monotonic()
                _log.info(
                    "migration_step_start",
                    name=m.name,
                    introduced=m.introduced,
                )
                try:
                    m.fn(conn)
                except MigrationRetry as _retry:
                    _log.warning(
                        "migration_step_skipped",
                        name=m.name,
                        introduced=m.introduced,
                        reason=str(_retry),
                    )
                    any_skipped = True
                    continue
                steps_run += 1
                _log.info(
                    "migration_step_done",
                    name=m.name,
                    introduced=m.introduced,
                    duration_ms=round((_time.monotonic() - _t_step) * 1000),
                )

        # When any step was skipped (transient precondition), do NOT update
        # the stored version or mark the path done — so the skipped migrations
        # are retried on the next DB open (CG-2 / K11 nexus-lh8c).
        if any_skipped:
            _log.warning(
                "migration_skipped_not_marking_done",
                path_key=path_key,
                steps_run=steps_run,
            )
            return

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

        # OBS-1: emit migration session completion telemetry.
        _log.info(
            "migration_done",
            path_key=path_key,
            steps_run=steps_run,
            duration_ms=round((_time.monotonic() - _t_start) * 1000),
        )


def _connection_path_key(conn: sqlite3.Connection) -> str:
    """Extract a stable key for the connection's database file.

    For ``:memory:`` connections, returns a unique key per connection
    (avoids collisions when multiple in-memory DBs exist in the same
    process).  For file-based connections, resolves symlinks via
    ``Path.resolve()`` to match the canonicalisation used by
    ``T2Database.__init__()`` and ``_run_upgrade()``.
    """
    from pathlib import Path  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load

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
    from nexus.db.t2.catalog_taxonomy import _TAXONOMY_SCHEMA_SQL  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load
    from nexus.db.t2.memory_store import _MEMORY_SCHEMA_SQL  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load
    from nexus.db.t2.plan_library import _PLANS_SCHEMA_SQL  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load
    from nexus.db.t2.telemetry import _TELEMETRY_SCHEMA_SQL  # noqa: PLC0415 — deferred import — migration-step-local, avoids import cost on every load

    conn.executescript(_MEMORY_SCHEMA_SQL)
    conn.executescript(_PLANS_SCHEMA_SQL)
    conn.executescript(_TAXONOMY_SCHEMA_SQL)
    conn.executescript(_TELEMETRY_SCHEMA_SQL)
    conn.commit()
