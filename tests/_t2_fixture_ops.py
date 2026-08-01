# SPDX-License-Identifier: AGPL-3.0-or-later
"""Substrate-agnostic T2 fixture manipulation for the unit suite (nexus-aqbrk).

RDR-158/155 substrate port, bucket 1. ~164 test sites reach into a store's
raw ``.conn`` to set up or read back state that the public store API does
not expose directly. Under the engine substrate those stores are the
``Http*Store`` variants, which have no ``.conn`` at all — they raise a
fail-loud ``AttributeError`` naming the fix (``_raw_handle_guard``).

The helpers route every operation through the stores' public ``import_*``
methods (``POST /v1/*/import``), whose whole purpose is writing
timestamp/tracking fields VERBATIM rather than letting the service stamp
them. The dual-substrate ``has_raw_access`` branches this module carried
during the port collapsed to their service arms when the =sqlite opt-out
died (RDR-158 P3, nexus-7bomn) — exactly the shrink the hoisting was for.

READS ARE NOT BRANCHED. ``MemoryStore.get_all`` / ``HttpMemoryStore.get_all``
both return the full column set (``access_count``, ``last_accessed``,
``timestamp``) and — unlike ``get()`` and ``search()`` — neither tracks
access, so a read-back needs no raw cursor and no branch on either
substrate. Tests that previously read a column via ``SELECT ... FROM memory``
should call :func:`memory_row`, which is a true non-mutating read on both.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nexus.db.t2 import T2Database

__all__ = [
    "backdate_memory",
    "bootstrap_migration_source",
    "memory_row",
    "seed_tier_write",
    "rewrite_memory_row",
    "seed_plan",
    "seed_relevance",
    "set_memory_access_count",
    "utc_stamp",
]


def utc_stamp(*, days: float = 0, seconds: float = 0) -> str:
    """Return an ISO-8601 UTC stamp *days*/*seconds* in the past.

    Second precision with a trailing ``Z``, matching the format the Java
    service emits and the SQLite schema stores.
    """
    moment = datetime.now(UTC) - timedelta(days=days, seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def memory_row(db: T2Database, project: str, title: str) -> dict[str, Any] | None:
    """Return the full memory row for *(project, title)*, or ``None``.

    NON-MUTATING on both substrates — this is the point. ``db.get()`` and
    ``db.search()`` both increment ``access_count`` as a documented
    side-effect, so a test verifying access tracking cannot use them to
    observe the counter without perturbing it. ``get_all()`` performs no
    access tracking on either the SQLite or the service arm.
    """
    for row in db.memory.get_all(project):
        if row["title"] == title:
            return row
    return None


def rewrite_memory_row(
    db: T2Database,
    project: str,
    title: str,
    *,
    timestamp: str | None = None,
    access_count: int | None = None,
    last_accessed: str | None = None,
) -> None:
    """Overwrite tracking/event-time columns on an existing memory row.

    Fields left as ``None`` keep their current values. The row must already
    exist; this is fixture manipulation, not an insert path.

    SQLite arm: a targeted ``UPDATE``. Service arm: ``import_entry``, whose
    whole purpose is writing ``timestamp`` / ``access_count`` /
    ``last_accessed`` verbatim rather than letting the service stamp them
    (``POST /v1/memory/import``, upsert on ``(tenant_id, project, title)``,
    so row identity is preserved).

    Raises:
        LookupError: if no row matches *(project, title)*.
    """
    if timestamp is None and access_count is None and last_accessed is None:
        raise ValueError("rewrite_memory_row: nothing to rewrite")

    store = db.memory

    row = memory_row(db, project, title)
    if row is None:
        raise LookupError(f"no memory row {project!r}/{title!r} to rewrite")

    # import_entry treats last_accessed=None as "never accessed" (SQL NULL);
    # the stores normalise a NULL read back to "". Round-trip "" as None so
    # an untouched never-accessed row stays never-accessed.
    current_last_accessed = row.get("last_accessed") or None

    store.import_entry(
        project=project,
        title=title,
        content=row["content"],
        timestamp=timestamp if timestamp is not None else row["timestamp"],
        tags=row.get("tags") or "",
        ttl=row.get("ttl"),
        agent=row.get("agent"),
        session=row.get("session"),
        access_count=(
            access_count if access_count is not None
            else int(row.get("access_count") or 0)
        ),
        last_accessed=(
            last_accessed if last_accessed is not None else current_last_accessed
        ),
    )


def backdate_memory(
    db: T2Database,
    project: str,
    title: str,
    *,
    days: float = 0,
    seconds: float = 0,
) -> None:
    """Move a memory row's ``timestamp`` *days*/*seconds* into the past.

    The TTL-expiry fixture primitive: ``expire()`` compares ``timestamp``
    against the row's effective TTL on both substrates, so backdating is how
    a test ages an entry without a fake clock.
    """
    rewrite_memory_row(
        db, project, title, timestamp=utc_stamp(days=days, seconds=seconds)
    )


def set_memory_access_count(
    db: T2Database, project: str, title: str, count: int
) -> None:
    """Force a memory row's ``access_count`` to *count*.

    Prefer driving the counter through real reads (``db.get()`` N times)
    where the test's subject IS access tracking. This exists for the
    heat-weighted-expiry tests, where the counter is an input to the
    behaviour under test and N reads would be pure ceremony.
    """
    rewrite_memory_row(db, project, title, access_count=count)


def seed_tier_write(
    db: T2Database,
    *,
    session_id: str,
    tool: str,
    tier: str,
    agent: str | None = None,
    project: str | None = None,
    target_title: str | None = None,
    ts: str | None = None,
) -> None:
    """Insert one ``tier_writes`` row on either substrate.

    The tier-status fixture primitive. ``nx tier-status`` is service-aware
    (``HttpTelemetryStore.query_tier_writes`` is the documented twin of
    ``tier_status._query``), so its tests must be able to seed whichever
    store the CLI will actually read — a raw ``sqlite3`` INSERT seeds a local
    file the service-mode CLI never opens, and every assertion then reads
    "(no writes)".

    SQLite arm: ``migrate_tier_writes`` + a direct INSERT, because there is no
    ``import_tier_write`` on the local store. Service arm:
    ``import_tier_write`` (``POST /v1/telemetry/import``, ``table=tier_writes``),
    whose purpose is writing the row's fields — ``ts`` included — VERBATIM
    rather than letting the service stamp them, which is exactly what fixture
    seeding needs.

    *ts* defaults to now; pass it explicitly when the test's subject is
    ordering or a time window.
    """
    from datetime import UTC, datetime

    stamp = ts or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    store = db.telemetry

    store.import_tier_write(
        session_id=session_id,
        ts=stamp,
        tool=tool,
        tier=tier,
        agent=agent,
        project=project,
        target_title=target_title,
    )


def seed_plan(
    db: T2Database,
    *,
    query: str,
    plan_json: str = "{}",
    created_at: str,
    project: str = "",
    outcome: str = "success",
    tags: str = "",
) -> int:
    """Save one plan carrying an explicit *created_at*. Returns its row id.

    The plan-ordering fixture primitive. ``save_plan`` stamps
    ``created_at=now()`` on both substrates, and second-granularity stamps in
    a fast test tie, so a list-ordering assertion needs the timestamps placed
    deliberately.

    The arms differ in shape, not just in mechanism: the service store's
    ``import_plan`` writes ``created_at`` verbatim at INSERT time, whereas the
    SQLite store has no import path, so it saves first and rewrites after.
    Both end with one row at the requested timestamp.
    """
    store = db.plans

    return store.import_plan(
        project=project,
        query=query,
        plan_json=plan_json,
        outcome=outcome,
        tags=tags,
        created_at=created_at,
    )


def seed_relevance(
    db: T2Database,
    *,
    query: str,
    chunk_id: str,
    action: str = "stored",
    session_id: str = "",
    collection: str = "",
    age_days: float = 0,
) -> None:
    """Insert one ``relevance_log`` row aged *age_days* into the past.

    The retention-purge fixture primitive. ``log_relevance`` stamps
    ``timestamp=now()`` on both substrates, so a test exercising
    ``expire_relevance_log`` has to place the row in the past some other
    way: raw ``UPDATE`` on the SQLite arm, ``import_relevance_row`` (whose
    contract is a verbatim timestamp) on the service arm.

    Both arms write the same second-precision UTC stamp, which compares
    correctly against ``expire_relevance_log``'s ``isoformat()`` cutoff on
    either side.
    """
    stamp = utc_stamp(days=age_days)
    store = db.telemetry

    store.import_relevance_row(
        query=query,
        chunk_id=chunk_id,
        collection=collection,
        action=action,
        session_id=session_id,
        timestamp=stamp,
    )


#: Frozen legacy T2 schema (RDR-158 P4 Stage 4, nexus-i711w). Captured from
#: the FINAL state of the deleted ``nexus.db.migrations`` chain (apply_pending
#: at expected schema version 6.18.1, HEAD e3c00252) via ``sqlite_master`` —
#: the df0c9c25 rehearsal-seeder precedent: tests that need a legacy
#: migration-source ``.db`` to EXIST executescript this frozen DDL instead of
#: running a migration chain that no longer exists. FTS5 shadow tables and
#: ``sqlite_sequence`` are excluded (auto-created by their virtual tables /
#: AUTOINCREMENT). The ``_nexus_version`` row is seeded ``0.0.0`` to match
#: what the chain produced on a catalog-less tmp dir (the catalog-gated steps
#: skipped, so the final stamp never ran); consumers re-stamp it themselves.
_FROZEN_LEGACY_T2_SCHEMA: str = """\
CREATE TABLE memory (
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

CREATE UNIQUE INDEX idx_memory_project_title ON memory(project, title);

CREATE INDEX idx_memory_project       ON memory(project);

CREATE INDEX idx_memory_agent         ON memory(agent);

CREATE INDEX idx_memory_timestamp     ON memory(timestamp);

CREATE INDEX idx_memory_ttl_timestamp ON memory(ttl, timestamp);

CREATE VIRTUAL TABLE memory_fts USING fts5(
    title,
    content,
    tags,
    content='memory',
    content_rowid='id'
);

CREATE TRIGGER memory_ai AFTER INSERT ON memory BEGIN
    INSERT INTO memory_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);

END;

CREATE TRIGGER memory_ad AFTER DELETE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
        VALUES ('delete', old.id, old.title, old.content, old.tags);

END;

CREATE TRIGGER memory_au AFTER UPDATE ON memory BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, title, content, tags)
        VALUES ('delete', old.id, old.title, old.content, old.tags);

INSERT INTO memory_fts(rowid, title, content, tags) VALUES (new.id, new.title, new.content, new.tags);

END;

CREATE TABLE plans (
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
    failure_count   INTEGER NOT NULL DEFAULT 0,
    -- RDR-091 Phase 2a: scope_tags captures which corpora/collections a
    -- plan actually touched. Comma-separated, sorted, deduplicated,
    -- hash-suffix-normalized. DEFAULT '' is load-bearing — Phase 2b
    -- treats '' as the scope-agnostic marker. Upgrade-in-place via the
    -- 4.8.0 ``_add_plan_scope_tags`` migration.
    scope_tags      TEXT NOT NULL DEFAULT '',
    -- RDR-092 Phase 3: hybrid match_text. Fresh installs get this
    -- column in the create; existing DBs pick it up via the 4.9.13
    -- ``_add_plan_match_text_column`` migration (which also rebuilds
    -- ``plans_fts`` so the FTS lane indexes match_text instead of
    -- query).
    match_text      TEXT NOT NULL DEFAULT ''
, disabled_at TEXT);

CREATE VIRTUAL TABLE plans_fts USING fts5(
    match_text,
    tags,
    project,
    content=plans,
    content_rowid='id'
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

CREATE TABLE topics (
    id            INTEGER PRIMARY KEY,
    label         TEXT NOT NULL,
    parent_id     INTEGER REFERENCES topics(id),
    collection    TEXT NOT NULL,
    centroid_hash TEXT,
    doc_count     INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'pending',
    terms         TEXT
);

CREATE TABLE taxonomy_meta (
    collection              TEXT PRIMARY KEY,
    last_discover_doc_count INTEGER NOT NULL DEFAULT 0,
    last_discover_at        TEXT
);

CREATE TABLE topic_assignments (
    doc_id      TEXT NOT NULL,
    topic_id    INTEGER NOT NULL REFERENCES topics(id),
    assigned_by TEXT NOT NULL DEFAULT 'hdbscan', similarity REAL, assigned_at TEXT, source_collection TEXT,
    PRIMARY KEY (doc_id, topic_id)
);

CREATE TABLE topic_links (
    from_topic_id INTEGER NOT NULL REFERENCES topics(id),
    to_topic_id   INTEGER NOT NULL REFERENCES topics(id),
    link_count    INTEGER NOT NULL DEFAULT 0,
    link_types    TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (from_topic_id, to_topic_id)
);

CREATE TABLE relevance_log (
    id         INTEGER PRIMARY KEY,
    query      TEXT NOT NULL,
    chunk_id   TEXT NOT NULL,
    collection TEXT,
    action     TEXT NOT NULL,
    session_id TEXT,
    timestamp  TEXT NOT NULL
);

CREATE INDEX idx_relevance_log_query
    ON relevance_log(query);

CREATE INDEX idx_relevance_log_chunk
    ON relevance_log(chunk_id);

CREATE INDEX idx_relevance_log_session
    ON relevance_log(session_id);

CREATE TABLE retention_markers (
    relation      TEXT    PRIMARY KEY,
    total_deleted INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT    NOT NULL
);

CREATE TABLE search_telemetry (
    ts             TEXT    NOT NULL,
    query_hash     TEXT    NOT NULL,
    collection     TEXT    NOT NULL,
    raw_count      INTEGER NOT NULL,
    kept_count     INTEGER NOT NULL,
    top_distance   REAL,
    threshold      REAL,
    PRIMARY KEY (ts, query_hash, collection)
);

CREATE INDEX idx_search_tel_collection
    ON search_telemetry(collection);

CREATE INDEX idx_search_tel_ts
    ON search_telemetry(ts);

CREATE TABLE _nexus_version (    key   TEXT PRIMARY KEY,    value TEXT NOT NULL);

CREATE INDEX idx_topic_assignments_source ON topic_assignments(source_collection, assigned_by);

CREATE INDEX idx_plans_verb ON plans(verb);

CREATE INDEX idx_plans_scope ON plans(scope);

CREATE INDEX idx_plans_verb_scope ON plans(verb, scope);

CREATE UNIQUE INDEX idx_plans_project_dimensions ON plans(project, dimensions) WHERE dimensions IS NOT NULL;

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
        );

CREATE TABLE chash_index (
            chash                TEXT NOT NULL,
            physical_collection  TEXT NOT NULL,
            created_at           TEXT NOT NULL,
            PRIMARY KEY (chash, physical_collection)
        );

CREATE INDEX idx_chash_index_collection
            ON chash_index(physical_collection);

CREATE TABLE hook_failures (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id      TEXT NOT NULL DEFAULT '',
            collection  TEXT NOT NULL DEFAULT '',
            hook_name   TEXT NOT NULL,
            error       TEXT NOT NULL DEFAULT '',
            occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        , batch_doc_ids TEXT, is_batch INTEGER NOT NULL DEFAULT 0, chain TEXT NOT NULL DEFAULT 'single');

CREATE INDEX idx_hook_failures_occurred_at
            ON hook_failures(occurred_at);

CREATE INDEX idx_hook_failures_collection
            ON hook_failures(collection);

CREATE TABLE document_aspects (
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
            extractor_name         TEXT NOT NULL, source_uri TEXT, salient_sentences TEXT,
            PRIMARY KEY (collection, source_path)
        );

CREATE INDEX idx_document_aspects_extractor
            ON document_aspects(extractor_name, model_version);

CREATE TABLE aspect_extraction_queue (
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

CREATE INDEX idx_aspect_queue_status
            ON aspect_extraction_queue(status);

CREATE TABLE aspect_promotion_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            field_name      TEXT NOT NULL,
            sql_type        TEXT NOT NULL,
            column_added    INTEGER NOT NULL,
            rows_backfilled INTEGER NOT NULL DEFAULT 0,
            rows_pruned     INTEGER NOT NULL DEFAULT 0,
            pruned          INTEGER NOT NULL DEFAULT 0,
            promoted_at     TEXT NOT NULL
        );

CREATE INDEX idx_aspect_promotion_log_field
            ON aspect_promotion_log(field_name);

CREATE TABLE frecency (
            chunk_id        TEXT PRIMARY KEY,
            embedded_at     TEXT NOT NULL DEFAULT '',
            ttl_days        INTEGER NOT NULL DEFAULT 0,
            frecency_score  REAL NOT NULL DEFAULT 0,
            miss_count      INTEGER NOT NULL DEFAULT 0,
            last_hit_at     TEXT NOT NULL DEFAULT ''
        );

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

CREATE TABLE document_highlights (
            doc_id        TEXT PRIMARY KEY,
            source_uri    TEXT,
            collection    TEXT,
            highlights_md TEXT,
            mentions_md   TEXT,
            ingested_at   TEXT NOT NULL
        );

CREATE INDEX idx_document_highlights_source_uri
            ON document_highlights(source_uri);

CREATE UNIQUE INDEX idx_topics_root_collection_label ON topics(collection, label) WHERE parent_id IS NULL;

CREATE TABLE claude_assisted_remediation_consents (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            scope   TEXT    NOT NULL,
            ts      TEXT    NOT NULL,
            granted INTEGER NOT NULL
        );

CREATE INDEX idx_consents_scope ON claude_assisted_remediation_consents(scope);;
"""


def bootstrap_migration_source(db: "Path") -> None:
    """Create a pre-migration SQLite source DB for a test to read or guard.

    Use in any test that needs a legacy ``.db`` to EXIST — the frozen
    migration source RDR-176 Gap 2 protects. Two kinds of test need this, and
    BOTH must keep running in service mode:

      - tests whose subject reads SQLite by definition (migration-source
        readers)
      - tests whose subject is a SERVICE-MODE GUARD over a legacy DB
        (tests/db/test_rdr176_non_mutation.py,
        tests/test_rdr176_guard_coverage.py)

    RDR-158 P4 Stage 4 (nexus-i711w): this used to run the live migration
    chain (``apply_pending``); that chain is deleted with
    ``nexus/db/migrations.py``, so the fixture executescripts the FROZEN DDL
    snapshot above instead. Same result — a well-formed legacy source file —
    without any live migration machinery.

    Deliberately a test-fixture builder only: production code must never
    materialise this schema (the Gap 2 immutability guarantee).
    """
    import sqlite3  # noqa: PLC0415 — test-fixture helper; keep module import cheap

    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_FROZEN_LEGACY_T2_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO _nexus_version (key, value) "
            "VALUES ('cli_version', '0.0.0')"
        )
        conn.commit()
    finally:
        conn.close()
