#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed a LEGACY on-disk Chroma store — the pre-cutover state a real user has.

`make_t3()` returns the service client post-RDR-155-P4a (no local-write escape
hatch), so the only faithful way to produce the migration SOURCE is to write the
Chroma PersistentClient on disk directly, exactly as a pre-cutover install left
it. `nx migrate-to-service --local-path <here>` then detects + ETLs it.

Chunk shape mirrors the repo convention (tests/migration/test_vector_etl.py):
id = sha256(text)[:32] (the chash; round-trips verbatim into pgvector.chash),
documents = the text the service RE-EMBEDS (source vectors are never read by the
ETL), metadata = {position, tag}. Two conformant collections:

  knowledge__rehearsal__minilm-l6-v2-384__v1   (ONNX leg — re-embedded locally)
  knowledge__rehearsal__voyage-context-3__v1   (cloud leg — re-embedded via Voyage)

Usage: seed_legacy.py <chroma_path> [--with-cloud] [--era-hop] [--n N]
       seed_legacy.py <chroma_path> --blocking=collision|pregate [--n N]
       seed_legacy.py <chroma_path> --remove-blocking[=collision|pregate]

Prints one JSON line: {"collections": {name: count, ...}} for the driver to assert
(--blocking prints {"blocking": {...}}; --remove-blocking prints {"removed": [...]}).

--era-hop (RDR-185 P4.3) layers the GH #1408 work-instance shape onto the main
seed: pre-RDR-108 16-char chunk ids as FULL catalog/T2 citizens, including a
store_put-only note that has no source content to re-index. The ladder's
substrate rung must converge these on the wire, so the manifest gains
legacy_ids (what must no longer exist), expected_reid (the exact conformant
chashes the wire transform must produce) and sourceless.

(nexus-lgdel.l2, 2026-08-16): the former --rdr180 land-then-transform gate
shapes (chash_alias / pointer-store cascade fixture, nexus-jxizy.10.10) were
deleted along with their only consumer, rehearse_guided.sh — --guided is
RETIRED (RDR-155 P4b) and refused pre-build by run.sh, so the shapes were
unreachable dead weight.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3

# Build the legacy T2 + catalog stores as RAW SQLITE, never through nexus
# store classes — these ARE the migration source a pre-cutover nx left on
# disk, and the store classes that once wrote them are deleted (RDR-158 P4;
# the =sqlite selector this file used to pin hard-errors since P3). Raw SQL
# against the frozen schemas below is wheel-version-independent, so the same
# file seeds correctly under the working-tree wheel AND the era wheels.

import sys
from datetime import UTC, datetime
from pathlib import Path

import chromadb

_MINILM = "knowledge__rehearsal__minilm-l6-v2-384__v1"
# nexus-pi3s3: the voyage source is a SAME-MODEL passthrough (copied byte-for-byte
# into a voyage-mode service). Its name MUST NOT collide with the minilm→voyage
# cross-model remap target: in voyage mode (--with-cloud, voyage_key_present) the
# migrate re-embeds _MINILM (knowledge/voyage) into knowledge__rehearsal__voyage-
# context-3__v1 (detection.cross_model_target_model). A distinct version segment
# (__v2) keeps a single conformant owner ("rehearsal") while avoiding that clash.
_VOYAGE = "knowledge__rehearsal__voyage-context-3__v2"
# RDR-162 P2: a SOURCELESS store_put-style note — a minilm-384 collection with
# NO backing source file (only a topic_assignment references it). embed_migrate
# (re-reads source files) cannot upgrade it; the cross-model migrate re-embeds
# its STORED text and re-points the assignment to the bge-768 target. This is the
# case that motivated RDR-162.
_NOTE = "knowledge__rehearsal-note__minilm-l6-v2-384__v1"

# nexus-itme7 shape (iii): a pre-RDR-109 MISLABELED collection — voyage-NAMED,
# but its stored vectors are 768-dim local ONNX. Classification measures a
# stored vector (nexus-nb7hr / nexus-x7t5y) and cross-model-remaps it to the
# bge-768 target UNCONDITIONALLY — in voyage mode too (remap_target_model
# returns the local ONNX model for measured-768 content; vectors that were
# never voyage must never bill a voyage re-embed). Part of the MAIN seed:
# this shape MIGRATES (success phase), unlike the --blocking shapes below.
_MISLABEL = "knowledge__rehearsal-mislabel__voyage-context-3__v1"

# nexus-itme7 pre-write BLOCK shapes (GH #667/#1381 field classes), evolved
# for RDR-180 (nexus-jxizy.10.10). Seeded ONLY via --blocking=<group>; NEVER
# entered into the chashes dict, so _seed_t2_and_catalog never sees them (the
# guards fire at classification / collision-audit time, before any catalog
# row would matter).
#   (i)   token-less 2-segment name: dim MUST NOT be 768 (the measured-dim
#         override would rescue it into a remap) and ids MUST be 32-char
#         (16-char would mis-attribute the block to legacy_ids).
#   (ii)  RETIRED AS A BLOCK (nexus-jxizy.10.8): the SUPPORTED-model name
#         with pre-RDR-108 16-char chunk ids now MIGRATES (land-then-
#         transform rehashes chunk_text server-side). Its positive fixture
#         (the former --rdr180 shape, _SHORTID) was itself deleted at
#         nexus-lgdel.l2 along with rehearse_guided.sh, its only consumer.
#   (iii) its Phase-0 slot: _NOTEXT — a conformant, supported-model name
#         whose sampled chunks carry NO TEXT AT ALL. Nothing to rehash from
#         (un-derivable) — the RDR-180 Q4 residual honest block (P2.3).
#   (iv)  collision pair: the stale voyage-named half MUST hold real 768-dim
#         vectors — the measured-dim override remaps it onto the honest
#         sibling's name (target-name collision). A non-768 half would
#         instead trip guided-upgrade's step-2a voyage-capability gate and
#         exit with the wrong diagnostic.
_LEGACYBARE = "knowledge__legacybare"
_NOTEXT = "knowledge__rehearsal-notext__bge-base-en-v15-768__v1"
_PAIR_HONEST = "knowledge__rehearsal-pair__bge-base-en-v15-768__v1"
_PAIR_STALE = "knowledge__rehearsal-pair__voyage-context-3__v1"

# RDR-185 P4.3 (nexus-n7u38.30): the ERA-HOP shapes — the 2026-07-16
# work-instance (GH #1408) footprint, which the LADDER converges rather than
# blocks. Distinct from the _BLOCKING shapes above in one load-bearing way:
# these are FULL CITIZENS (entered into the chashes dict, so
# _seed_t2_and_catalog writes catalog manifests + topic assignments keyed by
# their LEGACY chashes). The blocking shapes are deliberately excluded from
# T2/catalog because they only ever need to trip a pre-write guard — nothing
# downstream of them exists. Under RDR-185 the legacy ids CONVERGE, so the
# old->new map has to cascade through every chash-bearing store; a legacy
# collection with no manifest rows would make that cascade vacuous and let a
# broken cascade pass.
#
#   _ERA_LEGACY  file-backed, 16-char (pre-RDR-108) ids. Its catalog manifest
#                carries the legacy chashes -> the cascade must remap them or
#                the post-migration orphan scan finds every row dangling.
#   _ERA_NOTE    the incident's hard case: store_put-only (NO catalog file
#                document, only a topic_assignment) AND 16-char ids. It has no
#                source content, so re-indexing it is IMPOSSIBLE — the printed
#                remedy that made GH #1408 a dead end. ONLY wire re-id can
#                converge it, and its topic_assignment's doc_id is a legacy
#                chash the cascade must re-point.
_ERA_LEGACY = "knowledge__rehearsal-era__bge-base-en-v15-768__v1"
_ERA_NOTE = "knowledge__rehearsal-era-note__minilm-l6-v2-384__v1"

#: Collections with NO backing source file — only a topic_assignment references
#: them. `_seed_t2_and_catalog` skips the catalog document for these and seeds
#: the assignment instead. Keeping this a SET (not an `== _NOTE` check) is what
#: lets the era-hop add its own sourceless shape without the note-handling
#: silently applying to only one of them.
_SOURCELESS: frozenset[str] = frozenset({_NOTE, _ERA_NOTE})

#: blocking collection -> (seed text prefix, vector dim, chunk-id length,
#: empty_text). ``empty_text=True`` seeds distinct ids (derived from the
#: prefix strings) but EMPTY documents — the probe_has_text=False shape.
_BLOCKING_SPEC: dict[str, tuple[str, int, int, bool]] = {
    _LEGACYBARE: ("bare legacy chunk", 2, 32, False),
    _NOTEXT: ("notext chunk", 2, 32, True),
    _PAIR_HONEST: ("pair honest chunk", 2, 32, False),
    _PAIR_STALE: ("pair stale chunk", 768, 32, False),
}

#: --blocking group -> collections. Per-shape granularity (plan-audit F1): the
#: collision guard fires BEFORE the sequencer pregate, so one guided-upgrade
#: run can emit only ONE of the two block types — Phase 0 seeds them in
#: SEPARATE sub-runs. RDR-180: the pregate group is (i) nonconformant name +
#: (iii) no-text; the retired (ii) legacy-id shape's former positive fixture
#: was deleted at nexus-lgdel.l2 with rehearse_guided.sh, its only consumer.
_BLOCKING_GROUPS: dict[str, tuple[str, ...]] = {
    "collision": (_PAIR_HONEST, _PAIR_STALE),
    "pregate": (_LEGACYBARE, _NOTEXT),
}

# The model the cross-model migrate re-embeds the minilm sources into. This is
# MODE-AWARE (nexus-pi3s3, mirrors detection.cross_model_target_model): a voyage-
# mode service (--with-cloud, voyage_key_present) re-embeds knowledge collections
# into voyage-context-3; a local bge-768 service re-embeds into bge-base-en-v15-768.
# A stale unconditional bge-768 here made the voyage-mode parity assert the wrong
# target collection (service=0 [MISMATCH] false negative).
_BGE_MODEL = "bge-base-en-v15-768"
_VOYAGE_CTX_MODEL = "voyage-context-3"  # knowledge content-type → voyage-context-3


#: Frozen legacy T2 schema — the exact ``sqlite_master`` DDL the deleted
#: client migration chain (``nexus/db/migrations.py``, removed in RDR-158
#: P4 Stage 4) produced at HEAD e3c00252, FTS shadow tables excluded.
#: Byte-identical to ``tests/_t2_fixture_ops._FROZEN_LEGACY_T2_SCHEMA``
#: (kept in lockstep; the reviewer verified the capture against the live
#: chain by running it out-of-band). Seeding raw against this schema
#: replaces the old ``T2Database(run_migrations=True)`` construction,
#: which cannot run on this wheel.
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


# ── Frozen legacy catalog schema (raw-SQLite seeding) ────────────────────────
# The local Catalog implementation (nexus.catalog.catalog / nexus.db.t2.catalog)
# was DELETED by the RDR-158/RDR-155 substrate retirement (i711w): the on-disk
# ``.catalog.db`` is now a frozen MIGRATION SOURCE that the ETL and the
# rehearsal assertions read via raw sqlite3. This seeder produces that LEGACY
# artifact, so raw SQLite is the correct tool here (same class as
# migrations.py's rehomed schemas; tests/e2e is outside the NO-SQLITE DDL
# census, which scans src/ only).
#
# Frozen migration-SOURCE schema, copied VERBATIM from the deleted
# ``src/nexus/db/t2/catalog.py`` ``_SCHEMA_SQL`` at df0c9c25 (the last
# pre-deletion commit). Do not evolve it — a legacy artifact's schema is
# immutable by definition; the upgrade ladder owns forward conversion.
_LEGACY_CATALOG_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS owners (
    tumbler_prefix TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    repo_hash TEXT,
    description TEXT,
    repo_root TEXT DEFAULT '',
    -- RDR-137 Phase 1.5b (nexus-tts0d.2): per-repo git HEAD identity,
    -- previously held by ~/.config/nexus/repos.json. The indexer's
    -- staleness skip compares the running repo's git HEAD against this
    -- column; A1 verdict rejected documents.source_mtime as equivalent
    -- because a repo HEAD can advance without any tracked file's mtime
    -- changing (remote-only merge, ff-only pull of tag-only commits).
    -- NULL on pre-migration rows AND on owners without a tracked HEAD
    -- (e.g. ``curator`` owners minted by ``nx index pdf --corpus name``).
    head_hash TEXT,
    -- nexus-7vuw: name UNIQUE was a too-strict invariant. A repo and a
    -- curator are different namespaces, so a repo named "nexus" should
    -- coexist with a curator named "nexus" (e.g. ``nx index pdf
    -- --corpus nexus`` after ``nx index repo .``). Pre-fix, the second
    -- INSERT OR REPLACE silently obliterated the first row via the
    -- name UNIQUE conflict, leaving owner_for_repo(repo_hash) returning
    -- None and the indexer falling through to path-derived collection
    -- naming. Composite UNIQUE keeps name-collision detection where it
    -- belongs (within an owner_type).
    UNIQUE(name, owner_type)
);

-- RDR-137 followup CRITICAL-5 (nexus-43qgm.5): partial unique index
-- on repo_hash so the TOCTOU race in ensure_owner_for_repo (lookup-
-- then-register) cannot create duplicate owner rows for the same
-- repository. Excludes empty / NULL repo_hash so curator owners
-- (which never carry a repo_hash) coexist without conflict.
CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_repo_hash
    ON owners(repo_hash) WHERE repo_hash IS NOT NULL AND repo_hash != '';

CREATE TABLE IF NOT EXISTS documents (
    tumbler TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    year INTEGER,
    content_type TEXT,
    file_path TEXT,
    corpus TEXT,
    physical_collection TEXT,
    chunk_count INTEGER,
    head_hash TEXT,
    indexed_at TEXT,
    metadata JSON,
    source_mtime REAL NOT NULL DEFAULT 0,
    -- nexus-s8yz: permanent tumbler aliasing. When a document is
    -- consolidated into a canonical owner (dedupe-owners, nexus-tmbh),
    -- its row is kept and alias_of is set to the canonical tumbler.
    -- External references (plan templates, prose citations, links
    -- written by other systems) continue to resolve via alias_of —
    -- that is the stability promise tumblers were chosen for.
    -- '' (empty) means "this is the canonical document".
    alias_of TEXT NOT NULL DEFAULT '',
    -- RDR-096 P2.1: persistent URI identity. ``''`` (empty) on
    -- legacy rows; populated for new registers after P2.1 ships.
    -- Backfill derives URIs from ``file_path + physical_collection``.
    source_uri TEXT NOT NULL DEFAULT '',
    -- RDR-101 Phase 1 PR D (nexus-knn3): bibliographic enrichment
    -- columns from the bib disposition deliverable
    -- (docs/rdr/post-mortem/rdr-101-bib-disposition.md, Option A).
    -- The bib_* fields move OFF T3 chunk metadata and live exactly once
    -- on the Document projection. Phase 1 ships the empty columns;
    -- Phase 3 wires DocumentEnriched v: 1 events to populate them
    -- through the projector. The two indexed ID columns are the
    -- "this title was enriched on backend X" cardinality marker that
    -- nx enrich bib's skip query will read against (Phase 4); the
    -- partial indexes (created below) make that query a sub-millisecond
    -- presence test instead of a 300-row Chroma pagination.
    bib_year INTEGER NOT NULL DEFAULT 0,
    bib_authors TEXT NOT NULL DEFAULT '',
    bib_venue TEXT NOT NULL DEFAULT '',
    bib_citation_count INTEGER NOT NULL DEFAULT 0,
    bib_semantic_scholar_id TEXT NOT NULL DEFAULT '',
    bib_openalex_id TEXT NOT NULL DEFAULT '',
    bib_doi TEXT NOT NULL DEFAULT '',
    bib_enriched_at TEXT NOT NULL DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, author, corpus, file_path,
    content=documents, content_rowid=rowid
);

CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, author, corpus, file_path)
        VALUES (new.rowid, new.title, new.author, new.corpus, new.file_path);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, author, corpus, file_path)
        VALUES ('delete', old.rowid, old.title, old.author, old.corpus, old.file_path);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, author, corpus, file_path)
        VALUES ('delete', old.rowid, old.title, old.author, old.corpus, old.file_path);
    INSERT INTO documents_fts(rowid, title, author, corpus, file_path)
        VALUES (new.rowid, new.title, new.author, new.corpus, new.file_path);
END;

CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    from_tumbler TEXT NOT NULL,
    to_tumbler TEXT NOT NULL,
    link_type TEXT NOT NULL,
    from_span TEXT,
    to_span TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT,
    metadata JSON
);

CREATE INDEX IF NOT EXISTS idx_links_from ON links(from_tumbler);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_tumbler);
CREATE INDEX IF NOT EXISTS idx_links_type ON links(link_type);
CREATE INDEX IF NOT EXISTS idx_links_created_by ON links(created_by);
CREATE INDEX IF NOT EXISTS idx_links_from_type ON links(from_tumbler, link_type);
CREATE INDEX IF NOT EXISTS idx_links_to_type ON links(to_tumbler, link_type);

CREATE UNIQUE INDEX IF NOT EXISTS idx_links_unique
    ON links(from_tumbler, to_tumbler, link_type);

CREATE INDEX IF NOT EXISTS idx_links_created_by_type
    ON links(created_by, link_type);

CREATE INDEX IF NOT EXISTS idx_documents_tumbler
    ON documents(tumbler);

-- RDR-101 Phase 6 (nexus-o6aa.14): first-class Collections projection.
-- One row per ChromaDB collection name. Materialized from
-- CollectionCreated events; legacy_grandfathered is projection-derived
-- from corpus.is_conformant_collection_name (no event-payload extension
-- required, v: 0 stays stable). Read paths consult this table to
-- distinguish post-Phase-6 canonical names from grandfathered legacy
-- names; write paths consult it to short-circuit re-registration.
CREATE TABLE IF NOT EXISTS collections (
    name TEXT PRIMARY KEY,
    content_type TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    embedding_model TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    -- 1 = name does NOT match is_conformant_collection_name; the row
    -- exists only because the collection predates RDR-101 Phase 6 or
    -- was manually registered by the operator. Read paths accept it.
    legacy_grandfathered INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT NOT NULL DEFAULT '',
    superseded_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_collections_legacy
    ON collections(legacy_grandfathered);
CREATE INDEX IF NOT EXISTS idx_collections_owner
    ON collections(owner_id);

-- nexus-wehp: cross-process consistency-marker table. Stores the
-- highest canonical-source mtime that was successfully projected into
-- this SQLite cache. Catalog._ensure_consistent reads it on
-- construction to skip the DELETE+replay rebuild when the projection
-- is already up to date, eliminating the 'database is locked'
-- contention that surfaced when CLI write-side verbs raced an
-- nx-mcp-held connection in v4.23.0. A fresh SQLite cache has no
-- row, returns 0.0, and the rebuild fires (the e2e test invariant
-- 'fresh cache against existing catalog dir sees the data').
CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- RDR-103 Phase 2: ``Catalog.collection_for`` resolves a
-- ``(content_type, owner_id, embedding_model)`` triple to the
-- highest-versioned conformant collection. Without this index the
-- lookup is a full scan over the projection.
CREATE INDEX IF NOT EXISTS idx_collections_tuple
    ON collections(content_type, owner_id, embedding_model);

-- RDR-101 Phase 1 PR D (nexus-knn3) partial indexes on bib backend IDs
-- live in the post-migration block in __init__: the legacy-DB upgrade
-- path has to ALTER TABLE the bib columns into existence before the
-- partial-index CREATE can reference them.

-- RDR-108 D2 (nexus-mydi): document_chunks manifest. The catalog is
-- the authoritative source of truth for doc->chunk ordering (the
-- "tree" layer of the git/IPFS-style blob+tree split). T3 chunks are
-- content-addressed blobs keyed on chunk_text_hash[:32]; this table
-- records the ordered (doc_id, position) -> chash references that
-- compose each Document. The same chash can appear at multiple
-- (doc_id, position) rows: the manifest preserves position; T3
-- stores content once. Optional positional columns (line_start /
-- line_end / char_start / char_end) carry display-friendly span
-- coordinates so retrieval doesn't have to re-derive them from the
-- source file. chunk_index is the chunker-assigned ordinal at index
-- time, retained for reference; position is the canonical ordering
-- key from this RDR onward.
CREATE TABLE IF NOT EXISTS document_chunks (
    doc_id      TEXT NOT NULL REFERENCES documents(tumbler) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    chash       TEXT NOT NULL,
    chunk_index INTEGER,
    line_start  INTEGER,
    line_end    INTEGER,
    char_start  INTEGER,
    char_end    INTEGER,
    PRIMARY KEY (doc_id, position)
);

CREATE INDEX IF NOT EXISTS idx_document_chunks_chash
    ON document_chunks(chash);
"""


def _remap_model(source: str, model: str) -> str:
    """Swap the model segment of a conformant 4-segment collection name."""
    seg = source.split("__")
    seg[2] = model
    return "__".join(seg)


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _sha_full(text: str) -> str:
    """The FULL sha256 hexdigest — the canonical RDR-180 chash identity."""
    return hashlib.sha256(text.encode()).hexdigest()


def _seed(
    client, name: str, n: int, *, prefix: str, dim: int = 2, id_len: int = 32,
    empty_text: bool = False,
) -> list[str]:
    """Seed a legacy Chroma collection; return the chunk chashes (ids).

    ``dim`` (nexus-pi3s3): the CROSS-MODEL re-embed legs (minilm→bge/voyage) never
    read the source vector — the ETL re-embeds the documents server-side — so a
    nonsensical 2-dim stub suffices (matches the repo's ETL fixtures). The
    SAME-MODEL voyage passthrough is different: it COPIES the stored vector
    byte-for-byte into chunks_1024, so its source vectors must be the real
    dimension (1024) or the service's RDR-156 schema guard rejects the upsert
    ("embedder produced a 2-dim vector ... dispatches to embedding_1024"). Values are
    irrelevant (parity asserts COUNT, not similarity) — only the dim matters.

    ``id_len`` (nexus-itme7 / RDR-180): 16 seeds pre-RDR-108 16-char chunk
    ids (``sha256[:16]``, the GH #1390 canon-chat era). Under land-then-
    transform these MIGRATE by server-side rehash (nexus-jxizy.10.8).
    Everything else keeps the 32-char chash identity.

    ``empty_text`` (nexus-jxizy.10.10): ids stay derived from the prefix
    strings (distinct), but the stored documents are EMPTY — the
    probe_has_text=False shape behind the RDR-180 residual honest block
    (nothing to rehash from, un-derivable).
    """
    texts = [f"{prefix} {i:04d}" for i in range(n)]
    ids = [_chash(t)[:id_len] for t in texts]
    col = client.get_or_create_collection(name)
    col.add(
        ids=ids,
        documents=["" for _ in texts] if empty_text else texts,
        metadatas=[{"position": i, "tag": "rehearsal"} for i in range(n)],
        embeddings=[[float(i)] + [1.0] * (dim - 1) for i in range(n)],
    )
    return ids


def _seed_t2_and_catalog(
    collections: dict[str, list[str]],
) -> dict[str, int]:
    """Build the legacy T2 memory.db (one note) + a catalog-CONSISTENT footprint.

    migrate-to-service sequences T2 → catalog → T3. The validation gate refuses
    to unlock when the migrated catalog is empty (orphan check would be vacuous —
    a false pass). So for each seeded Chroma collection we register a catalog
    document and write its document_chunks manifest referencing the SAME chashes,
    making the post-migration orphan scan (catalog manifest ⨝ pgvector chash)
    meaningful. Returns {"t2_notes": N, "catalog_docs": M}.
    """
    from nexus.config import nexus_config_dir

    cfg = nexus_config_dir()
    cfg.mkdir(parents=True, exist_ok=True)
    # Raw-SQLite seeding of the frozen legacy T2 (see the
    # _FROZEN_LEGACY_T2_SCHEMA provenance comment): the deleted
    # ``T2Database(run_migrations=True)`` + ``MemoryStore.put`` calls are
    # replicated as the exact rows they produced; the schema's FTS triggers
    # keep memory_fts in sync. The version stamp mirrors what
    # ``run_migrations=True`` used to write (the installed wheel version).
    t2_conn = sqlite3.connect(str(cfg / "memory.db"))
    t2_conn.executescript(_FROZEN_LEGACY_T2_SCHEMA)
    try:
        from importlib.metadata import version as _dist_version
        _stamp = _dist_version("conexus")
    except Exception:
        _stamp = "0.0.0"
    t2_conn.execute(
        "INSERT OR REPLACE INTO _nexus_version (key, value) "
        "VALUES ('cli_version', ?)",
        (_stamp,),
    )
    _now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    t2_conn.execute(
        "INSERT INTO memory (project, title, content, tags, timestamp, ttl) "
        "VALUES ('rehearsal', 'legacy-note', 'pre-cutover note', "
        "'rehearsal', ?, 0)",
        (_now,),
    )
    t2_conn.commit()

    # RDR-162 P2: a SOURCELESS note assignment — a topic + a topic_assignment
    # whose ``source_collection`` is the note collection, with NO catalog file
    # document. The cross-model migrate must re-point this assignment to the
    # bge-768 target so the post-migration taxonomy-consistency check resolves.
    #
    # RDR-185 P4.3: for the era-hop's _ERA_NOTE the assignment's doc_id is a
    # LEGACY (16-char) chash, so the rung's remap cascade must re-point the
    # doc_id as well as the collection. topic_assignments is exactly the store
    # RDR-180's original inventory missed (RDR-180 Failure Modes) and the .13
    # audit re-found — seeding it with a legacy key is what makes that leg of
    # the cascade falsifiable here.
    for note_coll in sorted(_SOURCELESS & set(collections)):
        label = f"{note_coll.split('__')[1]}-topic"
        t2_conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at) "
            "VALUES (?, ?, ?, ?)",
            (label, note_coll, 1, "2026-06-18T00:00:00Z"),
        )
        topic_id = t2_conn.execute(
            "SELECT id FROM topics WHERE collection = ?", (note_coll,)
        ).fetchone()[0]
        t2_conn.execute(
            "INSERT INTO topic_assignments "
            "(doc_id, topic_id, assigned_by, source_collection) "
            "VALUES (?, ?, 'manual', ?)",
            (collections[note_coll][0], topic_id, note_coll),
        )
        t2_conn.commit()

    t2_conn.close()

    # Raw-SQLite seeding of the frozen legacy catalog (see the
    # _LEGACY_CATALOG_SCHEMA_SQL provenance comment). The deleted
    # ``Catalog.init`` / ``register_owner`` / ``register_collection`` /
    # ``register`` / ``write_manifest`` calls are replicated below as the
    # exact SQLite rows they produced (event-sourced projector SQL at
    # df0c9c25 — schema triggers keep documents_fts in sync). Only the
    # ``.catalog.db`` artifact is seeded: the JSONL/git sidecars the old
    # Catalog also wrote are not consumed by any rehearsal leg or by the
    # ETL (both read the SQLite file raw), and OMITTING documents.jsonl
    # is load-bearing for the era-hop leg — a legacy Catalog construction
    # only fires its DELETE+replay rebuild when documents.jsonl exists.
    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat_conn = sqlite3.connect(str(cat_dir / ".catalog.db"))
    cat_conn.executescript(_LEGACY_CATALOG_SCHEMA_SQL)

    repo_root = "/tmp/rehearsal-src"
    Path(repo_root).mkdir(parents=True, exist_ok=True)
    # register_owner("rehearsal", "project", repo_hash="rehearsal01",
    # repo_root=repo_root): mint the next ``1.<n>`` prefix (fresh DB -> 1.1)
    # unless the (name, owner_type) row already exists — the UNIQUE key the
    # deleted projector's INSERT OR REPLACE conflicted on.
    row = cat_conn.execute(
        "SELECT tumbler_prefix FROM owners WHERE name = ? AND owner_type = ?",
        ("rehearsal", "project"),
    ).fetchone()
    if row:
        owner_prefix = row[0]
    else:
        row = cat_conn.execute(
            "SELECT COALESCE(MAX(CAST(SUBSTR(tumbler_prefix, "
            "INSTR(tumbler_prefix, '.') + 1) AS INTEGER)), 0) "
            "FROM owners WHERE tumbler_prefix LIKE '1.%'"
        ).fetchone()
        owner_prefix = f"1.{(row[0] or 0) + 1}"
    cat_conn.execute(
        "INSERT OR REPLACE INTO owners "
        "(tumbler_prefix, name, owner_type, repo_hash, description, "
        "repo_root, head_hash) VALUES (?, ?, ?, ?, ?, ?, "
        "COALESCE((SELECT head_hash FROM owners "
        "WHERE name = ? AND owner_type = ?), ''))",
        (owner_prefix, "rehearsal", "project", "rehearsal01", "",
         repo_root, "rehearsal", "project"),
    )

    # nexus-qeoxf: register EVERY seeded collection in catalog_collections
    # (RDR-103, the collection-name authority), mirroring a real pre-cutover
    # install. The cross-model migrate's reference cascade renames the collection
    # via POST /v1/catalog/collections/rename, which the service 404s when the
    # source is absent from catalog_collections (handleCollectionRename ->
    # repo.collectionExists == false). A real RDR-103 user HAS these rows (the
    # catalog ETL migrates the `collections` table), so the rehearsal must seed
    # them too — else it injects a spurious non-fatal cascade 404 that does not
    # occur in production. Includes _NOTE: sourceless as a DOCUMENT, but still a
    # registered COLLECTION. Names are conformant 4-segment
    # (<content_type>__<owner>__<model>__v<n>); supply the segments so they
    # round-trip exactly. legacy_grandfathered is hardwired 0: every seeded
    # name is conformant 4-segment, which is exactly what the deleted
    # register_collection's is_conformant_collection_name check computed.
    coll_ts = datetime.now(UTC).isoformat()
    for coll in collections:
        seg = coll.split("__")
        cat_conn.execute(
            "INSERT OR REPLACE INTO collections "
            "(name, content_type, owner_id, embedding_model, model_version, "
            "display_name, legacy_grandfathered, superseded_by, "
            "superseded_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, "
            "COALESCE((SELECT superseded_by FROM collections WHERE name = ?), ''), "
            "COALESCE((SELECT superseded_at FROM collections WHERE name = ?), ''), "
            "COALESCE((SELECT created_at FROM collections WHERE name = ?), ?))",
            (coll, seg[0], seg[1], seg[2], seg[3], coll,
             coll, coll, coll, coll_ts),
        )

    # Sequential doc tumblers under the owner prefix, exactly as the deleted
    # ``register`` minted them (fresh owner next_seq=1 -> 1.1.1, 1.1.2, ...).
    # Resume from the high-water mark so a re-run against an existing DB
    # never re-mints an occupied tumbler.
    depth = len(owner_prefix.split("."))
    row = cat_conn.execute(
        "SELECT COALESCE(MAX(CAST(SUBSTR(tumbler, LENGTH(?) + 2) AS INTEGER)), 0) "
        "FROM documents WHERE tumbler LIKE ? "
        "AND (LENGTH(tumbler) - LENGTH(REPLACE(tumbler, '.', ''))) = ?",
        (owner_prefix, owner_prefix + ".%", depth),
    ).fetchone()
    next_doc_num = (row[0] or 0) + 1

    docs = 0
    for coll, chashes in collections.items():
        # The SOURCELESS cases: no catalog file document, only the
        # topic_assignment seeded above references them.
        if coll in _SOURCELESS:
            continue
        fp = f"{repo_root}/{coll}.md"
        Path(fp).write_text("rehearsal legacy doc\n")
        now = datetime.now(UTC).isoformat()
        # register(): idempotent by file_path within the owner prefix;
        # otherwise INSERT the row the deleted projector wrote for a
        # DocumentRegistered event (source_uri = file://<abspath>, empty
        # author/corpus/head_hash/alias_of, metadata '{}', bib_* defaults).
        row = cat_conn.execute(
            "SELECT tumbler FROM documents WHERE file_path = ? "
            "AND tumbler LIKE ? "
            "AND (LENGTH(tumbler) - LENGTH(REPLACE(tumbler, '.', ''))) = ?",
            (fp, owner_prefix + ".%", depth),
        ).fetchone()
        if row:
            doc = row[0]
        else:
            doc = f"{owner_prefix}.{next_doc_num}"
            next_doc_num += 1
            cat_conn.execute(
                "INSERT INTO documents "
                "(tumbler, title, author, year, content_type, file_path, "
                "corpus, physical_collection, chunk_count, head_hash, "
                "indexed_at, metadata, source_mtime, alias_of, source_uri) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc, coll, "", 0, "knowledge", fp, "", coll, len(chashes),
                 "", now, json.dumps({}), 0.0, "", "file://" + fp),
            )
        # write_manifest(): DELETE-then-INSERT (idempotent), positions
        # 0..n-1, chunk_index/span columns NULL, then the nexus-p5qk8
        # indexed_at refresh a manifest write performed.
        cat_conn.execute("DELETE FROM document_chunks WHERE doc_id = ?", (doc,))
        cat_conn.executemany(
            "INSERT INTO document_chunks "
            "(doc_id, position, chash, chunk_index, "
            " line_start, line_end, char_start, char_end) "
            "VALUES (?, ?, ?, NULL, NULL, NULL, NULL, NULL)",
            [(doc, i, c) for i, c in enumerate(chashes)],
        )
        cat_conn.execute(
            "UPDATE documents SET indexed_at = ? WHERE tumbler = ?",
            (datetime.now(UTC).isoformat(), doc),
        )
        docs += 1
    cat_conn.commit()
    cat_conn.close()
    return {"t2_notes": 1, "catalog_docs": docs}


def _blocking_group(args: list[str], flag: str) -> tuple[str, ...] | None:
    """Resolve ``--blocking=<group>`` / ``--remove-blocking[=<group>]`` args.

    Returns the group's collections, or ``None`` when *flag* is absent. A bare
    ``--remove-blocking`` resolves to ALL blocking collections (cleanup form);
    a bare ``--blocking`` is refused — seeding both groups in one store would
    let the collision guard mask the pregate (one run emits exactly ONE block
    type, plan-audit F1), silently making the pregate assertions vacuous.
    Unknown groups exit loud (2) for the same reason.
    """
    for a in args:
        if a == flag:
            if flag == "--blocking":
                print(
                    "--blocking requires a group: --blocking=collision|pregate "
                    "(one guided-upgrade run can emit only ONE block type)",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            return tuple(_BLOCKING_SPEC)
        if a.startswith(flag + "="):
            group = a.split("=", 1)[1]
            if group not in _BLOCKING_GROUPS:
                print(
                    f"unknown {flag} group {group!r} "
                    f"(choose from {sorted(_BLOCKING_GROUPS)})",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            return _BLOCKING_GROUPS[group]
    return None


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(
            "usage: seed_legacy.py <chroma_path> [--with-cloud] [--era-hop] [--n N]\n"
            "       seed_legacy.py <chroma_path> --blocking=collision|pregate [--n N]\n"
            "       seed_legacy.py <chroma_path> --remove-blocking[=collision|pregate]",
            file=sys.stderr,
        )
        return 2
    path = args[0]
    with_cloud = "--with-cloud" in args
    era_hop = "--era-hop" in args
    if era_hop and with_cloud:
        # The era shapes are seeded 768/384-dim local content whose remap target
        # is always the bge-768 name; a voyage-only service has no bge embedder
        # and 422s the leg (the same incoherence _MISLABEL documents above).
        print("--era-hop and --with-cloud are incoherent (era shapes remap onto "
              "the local bge-768 target; cloud is voyage-only)", file=sys.stderr)
        return 2
    n = 12
    if "--n" in args:
        n = int(args[args.index("--n") + 1])

    client = chromadb.PersistentClient(path=path)

    # nexus-itme7 blocking modes — early return BEFORE the T2/catalog seeding:
    # the block shapes must never enter the chashes dict (no catalog document,
    # no manifest, no T2 note) and these modes make zero config-dir writes, so
    # they are trivially sanity-runnable outside the container. NOTE: the
    # blocking shapes alone are NOT a runnable guided-upgrade fixture —
    # migrate_cmd's T2/catalog existence pre-check fires before any guard, so
    # Phase 0 layers them ON TOP of the main seed's footprint.
    blocking = _blocking_group(args, "--blocking")
    removing = _blocking_group(args, "--remove-blocking")
    if blocking is not None and removing is not None:
        print("--blocking and --remove-blocking are mutually exclusive", file=sys.stderr)
        return 2
    if blocking is not None:
        seeded_blocking: dict[str, int] = {}
        for bname in blocking:
            prefix, dim, id_len, empty_text = _BLOCKING_SPEC[bname]
            seeded_blocking[bname] = len(
                _seed(client, bname, n, prefix=prefix, dim=dim, id_len=id_len,
                      empty_text=empty_text)
            )
        print(json.dumps({"blocking": seeded_blocking}))
        return 0
    if removing is not None:
        removed: list[str] = []
        for bname in removing:
            try:
                client.delete_collection(bname)
            except Exception:  # noqa: BLE001 — absent collection: removal is idempotent
                continue
            removed.append(bname)
        print(json.dumps({"removed": removed}))
        return 0

    chashes: dict[str, list[str]] = {}
    chashes[_MINILM] = _seed(client, _MINILM, n, prefix="onnx chunk")
    chashes[_NOTE] = _seed(client, _NOTE, n, prefix="note chunk")
    # RDR-185 P4.3 (nexus-n7u38.30): the ERA-HOP footprint — the GH #1408
    # work-instance shape the ladder must converge UNATTENDED. Layered ON TOP
    # of the main seed (not instead of it): a real era install holds a mix, and
    # a conformant collection migrating beside a legacy one is what proves the
    # rung composes per-collection legs rather than treating the store as one
    # uniform era.
    if era_hop:
        chashes[_ERA_LEGACY] = _seed(
            client, _ERA_LEGACY, n, prefix="era legacy chunk", dim=768, id_len=16,
        )
        chashes[_ERA_NOTE] = _seed(
            client, _ERA_NOTE, n, prefix="era note chunk", id_len=16,
        )
    # Shape (iii): voyage-NAMED, but the stored vectors are real 768-dim — the
    # measured-dim override (nexus-nb7hr/x7t5y) reclassifies it and the migrate
    # re-embeds it into the bge-768 target. Registered in T2/catalog like every
    # other MAIN-seed collection: this one DOES migrate, so the rename cascade
    # and the post-migration orphan scan are meaningful for it.
    #
    # Safe for the LOCAL-mode main-seed callers (rehearse.sh default leg,
    # rehearse_cold.sh, rehearse_hole_punch.sh — yaeex critique): they drive
    # the same _run_migration the guided hand-off used to; their parity and
    # rollback-safety checks iterate this manifest generically
    # (cross.get(name, name), no hardcoded counts).
    #
    # NOT seeded on --with-cloud (first with-cloud run post-itme7, 2026-07-13):
    # remap_target_model returns the local ONNX model UNCONDITIONALLY for
    # measured-768 content (measured-ONNX vectors must never bill a voyage
    # re-embed), so the target is always the bge-768 name — which a voyage-mode
    # service refuses with HTTP 422 (no bge embedder), failing the whole leg
    # structurally. The itme7 design scoped the legacy shapes to --guided
    # (amendment 7); the with-cloud leg keeps its original pre-itme7 manifest.
    # The mislabel-on-voyage-service PRODUCT behaviour (pregate should block it
    # up front rather than a mid-flight 422) is tracked separately.
    if not with_cloud:
        chashes[_MISLABEL] = _seed(client, _MISLABEL, n, prefix="mislabel chunk", dim=768)
    if with_cloud:
        # 1024-dim source vectors: the voyage same-model passthrough COPIES them
        # (no re-embed) into chunks_1024 (nexus-pi3s3).
        chashes[_VOYAGE] = _seed(client, _VOYAGE, n, prefix="voyage chunk", dim=1024)
    t2 = _seed_t2_and_catalog(chashes)
    seeded = {name: len(ids) for name, ids in chashes.items()}
    # cross_model: source -> the target the migrate re-embeds into, MODE-AWARE
    # (nexus-pi3s3). voyage_key_present (== with_cloud here) decides the target
    # model exactly as detection.cross_model_target_model does: voyage-context-3
    # in voyage mode, bge-768 in local mode. The voyage source itself is a
    # SAME-MODEL passthrough (NOT remapped) so it is absent from this map; the
    # parity check then verifies it under its own name (cross.get(name, name)).
    _tgt_model = _VOYAGE_CTX_MODEL if with_cloud else _BGE_MODEL
    cross_model = {
        _MINILM: _remap_model(_MINILM, _tgt_model),
        _NOTE: _remap_model(_NOTE, _tgt_model),
    }
    if era_hop:
        # _ERA_NOTE is minilm-384 -> re-embedded into the mode's target model,
        # exactly like _NOTE. _ERA_LEGACY is ALREADY bge-768-named, so it is a
        # same-name leg: only its chunk IDENTITY changes (wire re-id), not its
        # collection. Absent from cross_model => the parity check resolves it
        # under its own name via cross.get(name, name).
        cross_model[_ERA_NOTE] = _remap_model(_ERA_NOTE, _tgt_model)
    if not with_cloud:
        # Shape (iii) is NOT mode-aware: remap_target_model returns the local
        # ONNX model UNCONDITIONALLY for measured-768 content (voyage mode
        # included — measured-ONNX vectors must never bill a voyage re-embed),
        # so the target is always the bge-768 name. Distinct owner segment
        # ("rehearsal-mislabel") keeps it collision-free with every other
        # main-seed target. Skipped on --with-cloud (see the seeding note
        # above): a voyage-mode service cannot embed the bge target.
        cross_model[_MISLABEL] = _remap_model(_MISLABEL, _BGE_MODEL)
    out: dict[str, object] = {"collections": seeded, "cross_model": cross_model, **t2}
    if era_hop:
        # The driver asserts the CONVERGENCE, so it needs the before-state: the
        # exact legacy ids that must no longer exist anywhere post-walk, and the
        # text they were derived from (the rung recomputes the FULL
        # sha256(text) hexdigest on the wire — the RDR-180 canonical identity;
        # nexus-i5rbk widened the transform to the 32-hex era class too — so
        # the expected new id is derivable here, and it makes the assertion
        # exact rather than a "looks conformant" shape check).
        out["legacy_ids"] = {
            _ERA_LEGACY: chashes[_ERA_LEGACY],
            _ERA_NOTE: chashes[_ERA_NOTE],
        }
        out["expected_reid"] = {
            _ERA_LEGACY: [_sha_full(f"era legacy chunk {i:04d}") for i in range(n)],
            _ERA_NOTE: [_sha_full(f"era note chunk {i:04d}") for i in range(n)],
        }
        out["sourceless"] = sorted(_SOURCELESS)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
