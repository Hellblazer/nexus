# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""T2 catalog store — eighth domain store (RDR-120 P5.A).

Authoritative implementation of the catalog SQLite layer. ``CatalogStore``
owns the ``sqlite3.Connection`` against ``.catalog.db``; the legacy
``nexus.catalog.catalog_db.CatalogDB`` symbol is preserved as an alias
re-export so existing imports continue to resolve unchanged (RDR-120
P5.A.2 thin-shim conversion).

File layout (Hal-approved P5.A grooming): ``.catalog.db`` stays separate
from the seven shared-``nexus.db`` stores. ``CatalogStore`` is the only
T2 store that opens its own SQLite file; the path split is intentional
and explicitly preserved through P5.

RDR-108 invariants enforced here:

- ``Document.tumbler`` is doc identity (PK on ``documents``).
- Chunk natural ID = ``sha256(chunk_text)[:32]`` (chash column on
  ``document_chunks``).
- ``document_chunks`` manifest is authoritative for the doc->chunk
  join.

§A8-exempt content writes documented in
``nexus_rdr/120-research-A9-catalog-extension``:

1. ``collections`` auto-backfill — structurally-bound-to-event-sourcing.
   Pair with the synthetic ``CollectionCreated`` events emitted post-
   construction in :mod:`nexus.catalog.catalog` so live and replayed
   projection states stay bit-equal.
2. ``owners`` PK-swap (nexus-7vuw) — structurally-bound-to-schema.
3. ``document_chunks`` PK-swap (RDR-108 K1) — structurally-bound-to-schema.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

import structlog

from nexus.db.t2._tuning import SERVING_BUSY_TIMEOUT_MS
from nexus.db.t2.memory_store import _sanitize_fts5

if TYPE_CHECKING:  # pragma: no cover — import for type hints only
    from nexus.catalog.tumbler import (
        DocumentRecord,
        LinkRecord,
        OwnerRecord,
    )

_log = structlog.get_logger()


# FTS5 trigger DDL extracted as standalone strings so the bulk-load
# fence (``CatalogStore.bulk_load_documents``) can drop and recreate
# them inside a transaction without re-running the entire schema script.
# Keep in sync with the inline copies in ``_SCHEMA_SQL`` below — the
# inline ones run on initial schema creation, these run on the rebuild
# path.
_DOCUMENTS_FTS_TRIGGERS: tuple[str, ...] = (
    """\
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, title, author, corpus, file_path)
        VALUES (new.rowid, new.title, new.author, new.corpus, new.file_path);
END
""",
    """\
CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, author, corpus, file_path)
        VALUES ('delete', old.rowid, old.title, old.author, old.corpus, old.file_path);
END
""",
    """\
CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, title, author, corpus, file_path)
        VALUES ('delete', old.rowid, old.title, old.author, old.corpus, old.file_path);
    INSERT INTO documents_fts(rowid, title, author, corpus, file_path)
        VALUES (new.rowid, new.title, new.author, new.corpus, new.file_path);
END
""",
)

_SCHEMA_SQL = """\
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


class CatalogStore:
    """SQLite query cache for the JSONL-backed catalog.

    RDR-120 P5.A authoritative owner of the ``.catalog.db`` handle.
    Surface mirrors the pre-RDR-120 ``CatalogDB``; the legacy symbol
    is re-exported from :mod:`nexus.catalog.catalog_db` as an alias
    so existing imports continue to resolve unchanged.
    """

    def __init__(self, db_path: Path, *, read_only: bool = False) -> None:
        self._path = db_path
        self._read_only = read_only
        # The other seven T2 stores live under the nexus config dir
        # which is auto-materialised. The catalog dir is NOT
        # auto-created in production (Catalog initialisation is the
        # gate), but daemon startup may construct the eighth store
        # before any consumer has initialised the catalog. Create the
        # parent here so ``sqlite3.connect`` does not raise "unable
        # to open database file" on a fresh install.
        db_path.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            # ``mode=ro`` URI form guards against accidental writes
            # when the caller intends to inspect the live catalog
            # while indexers run. Used by the doctor's replay-equality
            # gate against the in-flight catalog file.
            uri = f"file:{db_path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # Reentrant: ``execute()`` callers inside a ``transaction()``
        # context (RDR-101 round-3) re-acquire the lock from the same
        # thread. With a plain ``Lock()`` the nested ``with self._lock:``
        # in ``execute`` would deadlock. ``RLock`` is a strict superset
        # of ``Lock`` for cross-thread mutual exclusion.
        self._lock = threading.RLock()
        # Storage review I-2: match the T2 domain-store concurrency defaults
        # (5 s busy_timeout + WAL) so cross-process writers don't immediately
        # raise ``OperationalError: database is locked`` when the CLI races
        # an indexing writer.
        self._conn.execute(f"PRAGMA busy_timeout={SERVING_BUSY_TIMEOUT_MS}")
        self._conn.execute("PRAGMA journal_mode=WAL")
        # RDR-108 D2 + D5 (nexus-mydi): enable foreign-key enforcement
        # so the document_chunks manifest cannot reference a deleted
        # Document, and future cascade-on-rename machinery (D5
        # chash_index FK) works as advertised. SQLite enforces FK
        # constraints only when this PRAGMA is ON; declared REFERENCES
        # clauses in the schema are otherwise advisory.
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Issue #437: cap WAL growth under long-lived MCP-server reader
        # connections. SQLite's auto-checkpoint can run only as PASSIVE
        # while readers hold pre-checkpoint snapshots; PASSIVE folds
        # frames into the main DB but cannot truncate the WAL file.
        # Without a journal_size_limit the WAL grows unbounded over a
        # multi-hour session (12 MB observed on the reporter's install
        # before manual ``PRAGMA wal_checkpoint(TRUNCATE)``). 64 MiB
        # caps the steady-state size after each successful checkpoint;
        # SQLite reuses the space rather than growing the file.
        if read_only:
            # Read-only opens skip schema / migration setup — the file
            # is opened for inspection and any write would raise.
            self._backfilled_collections = set()
            return
        self._conn.execute("PRAGMA journal_size_limit=67108864")
        self._conn.executescript(_SCHEMA_SQL)
        # Migration: add repo_root column if missing (pre-RDR-060 databases)
        try:
            self._conn.execute("SELECT repo_root FROM owners LIMIT 0")
        except sqlite3.OperationalError:
            with self._conn:
                self._conn.execute("ALTER TABLE owners ADD COLUMN repo_root TEXT DEFAULT ''")

        # RDR-137 Phase 1.5b (nexus-tts0d.2): add owners.head_hash for per-
        # repo git HEAD identity. NULL on pre-migration rows; populated by
        # writers wired in Phase 3 (indexer cutover bead nexus-tts0d.13).
        try:
            self._conn.execute("SELECT head_hash FROM owners LIMIT 0")
        except sqlite3.OperationalError:
            with self._conn:
                self._conn.execute("ALTER TABLE owners ADD COLUMN head_hash TEXT")

        # nexus-7vuw: drop the legacy single-column UNIQUE(name) index
        # on owners (replaced with composite UNIQUE(name, owner_type) in
        # the schema above). Pre-fix DBs carry a sqlite_autoindex_owners_*
        # index backing the old single-column constraint; SQLite cannot
        # ALTER a constraint in place, so the migration rebuilds the
        # owners table from existing rows. Idempotent: skipped when the
        # auto-index is already absent.
        legacy_unique = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='owners' AND name LIKE 'sqlite_autoindex_owners_%' "
            "AND sql IS NULL"
        ).fetchall()
        if legacy_unique:
            # Detect: probe the auto-index. If it indexes only ``name``,
            # we have the pre-fix shape and need to rebuild. The
            # composite UNIQUE(name, owner_type) declared in the schema
            # above creates an auto-index on TWO columns, so single-column
            # auto-indexes can only mean the legacy constraint survived
            # from an older DB.
            needs_rebuild = False
            for (idx_name,) in legacy_unique:
                cols = self._conn.execute(
                    f"PRAGMA index_info({idx_name!r})"
                ).fetchall()
                if len(cols) == 1 and cols[0][2] == "name":
                    needs_rebuild = True
                    break
            if needs_rebuild:
                with self._conn:
                    # RDR-137 followup CRITICAL-2 (nexus-43qgm.2): include
                    # head_hash in the new table + INSERT so the rebuild
                    # path preserves the column. The preceding head_hash
                    # ALTER (line ~374) guarantees the source side has it.
                    self._conn.execute(
                        "CREATE TABLE owners_nexus7vuw_new ("
                        "    tumbler_prefix TEXT PRIMARY KEY, "
                        "    name TEXT NOT NULL, "
                        "    owner_type TEXT NOT NULL, "
                        "    repo_hash TEXT, "
                        "    description TEXT, "
                        "    repo_root TEXT DEFAULT '', "
                        "    head_hash TEXT, "
                        "    UNIQUE(name, owner_type)"
                        ")"
                    )
                    # INSERT OR IGNORE so any pre-existing colliding rows
                    # (which the legacy UNIQUE name constraint would have
                    # already prevented) are dropped silently rather than
                    # tripping the new composite constraint.
                    self._conn.execute(
                        "INSERT OR IGNORE INTO owners_nexus7vuw_new "
                        "(tumbler_prefix, name, owner_type, repo_hash, "
                        " description, repo_root, head_hash) "
                        "SELECT tumbler_prefix, name, owner_type, repo_hash, "
                        "       description, repo_root, head_hash FROM owners"
                    )
                    self._conn.execute("DROP TABLE owners")
                    self._conn.execute(
                        "ALTER TABLE owners_nexus7vuw_new RENAME TO owners"
                    )

        # RDR-137 followup CRITICAL-5 (nexus-43qgm.5): partial unique
        # index on owners.repo_hash. Idempotent via IF NOT EXISTS;
        # closes the TOCTOU race in ensure_owner_for_repo. Adding it
        # here (rather than relying on _SCHEMA_SQL alone) covers
        # legacy DBs created before the schema declaration shipped.
        with self._conn:
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_owners_repo_hash "
                "ON owners(repo_hash) WHERE repo_hash IS NOT NULL AND repo_hash != ''"
            )

        # nexus-8luh: add source_mtime column to existing databases so
        # RDR-087 Phase 3.4's stale_source_ratio has something to read
        # from. Default 0 for pre-migration rows (meaning "unknown").
        try:
            self._conn.execute("SELECT source_mtime FROM documents LIMIT 0")
        except sqlite3.OperationalError:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN source_mtime REAL NOT NULL DEFAULT 0"
                )

        # nexus-s8yz: add alias_of column to existing databases. '' means
        # the document is canonical (not an alias).
        try:
            self._conn.execute("SELECT alias_of FROM documents LIMIT 0")
        except sqlite3.OperationalError:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN alias_of TEXT NOT NULL DEFAULT ''"
                )

        # RDR-096 P2.1 (nexus-ocu9.3): add source_uri column to existing
        # databases. ``''`` on pre-migration rows; new rows get
        # populated at register time. Backfill happens lazily on
        # rebuild from JSONL — DocumentRecord.source_uri carries the
        # value through.
        try:
            self._conn.execute("SELECT source_uri FROM documents LIMIT 0")
        except sqlite3.OperationalError:
            with self._conn:
                self._conn.execute(
                    "ALTER TABLE documents ADD COLUMN source_uri TEXT NOT NULL DEFAULT ''"
                )

        # RDR-101 Phase 1 PR D (nexus-knn3): add bib_* columns to existing
        # databases. The bib disposition deliverable
        # (docs/rdr/post-mortem/rdr-101-bib-disposition.md, Option A)
        # moves these fields off T3 chunk metadata and onto the Document
        # projection. Phase 1 ships the columns empty; Phase 3 wires the
        # projector to populate them from DocumentEnriched v: 1 events.
        # Each ALTER probes for the column first; failure means it was
        # added in a previous run (idempotent migration pattern matches
        # the rest of this method).
        for col_name, col_decl in (
            ("bib_year",                "INTEGER NOT NULL DEFAULT 0"),
            ("bib_authors",             "TEXT NOT NULL DEFAULT ''"),
            ("bib_venue",               "TEXT NOT NULL DEFAULT ''"),
            ("bib_citation_count",      "INTEGER NOT NULL DEFAULT 0"),
            ("bib_semantic_scholar_id", "TEXT NOT NULL DEFAULT ''"),
            ("bib_openalex_id",         "TEXT NOT NULL DEFAULT ''"),
            ("bib_doi",                 "TEXT NOT NULL DEFAULT ''"),
            ("bib_enriched_at",         "TEXT NOT NULL DEFAULT ''"),
        ):
            try:
                self._conn.execute(f"SELECT {col_name} FROM documents LIMIT 0")
            except sqlite3.OperationalError:
                with self._conn:
                    self._conn.execute(
                        f"ALTER TABLE documents ADD COLUMN {col_name} {col_decl}"
                    )

        # Partial indexes on the two bib backend IDs. CREATE INDEX IF NOT
        # EXISTS is safe on a fresh DB (where _SCHEMA_SQL already created
        # them) and on existing DBs (where the columns just landed via
        # ALTER above).
        with self._conn:
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_bib_s2_id "
                "ON documents(bib_semantic_scholar_id) "
                "WHERE bib_semantic_scholar_id != ''"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_documents_bib_oa_id "
                "ON documents(bib_openalex_id) "
                "WHERE bib_openalex_id != ''"
            )

        # RDR-108 Phase 4 (nexus-dyxe): index documents.physical_collection
        # so manifest-based GC's per-collection lookup
        # (``chashes_for_collection`` joins document_chunks to documents
        # filtered by physical_collection) does not full-scan documents.
        # Same column is the filter key for ``list_by_collection`` and
        # ``relocate_collection``. Probe-guarded so stripped-down legacy
        # schemas without ``physical_collection`` (e.g. the alias_of
        # migration test fixture) still open cleanly.
        try:
            self._conn.execute("SELECT physical_collection FROM documents LIMIT 0")
        except sqlite3.OperationalError:
            pass
        else:
            with self._conn:
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_documents_physical_collection "
                    "ON documents(physical_collection)"
                )

        # RDR-108 K1 (nexus-lh8c): add ON DELETE CASCADE to document_chunks
        # for existing databases that were created before this constraint was
        # declared in the schema. SQLite cannot ALTER a FK constraint in place;
        # the 12-step pattern recreates the table with the correct declaration.
        # Idempotent: skipped if the schema already includes ON DELETE CASCADE.
        _chunks_ddl_row = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='document_chunks'"
        ).fetchone()
        if _chunks_ddl_row is not None and "ON DELETE CASCADE" not in _chunks_ddl_row[0]:
            # Detect which columns exist — older schemas may be a strict subset.
            _old_col_names = {
                r[1] for r in self._conn.execute(
                    "PRAGMA table_info(document_chunks)"
                ).fetchall()
            }
            _all_cols = [
                "doc_id", "position", "chash", "chunk_index",
                "line_start", "line_end", "char_start", "char_end",
            ]
            _copy_cols = [c for c in _all_cols if c in _old_col_names]
            _copy_sql = ", ".join(_copy_cols)
            with self._conn:
                self._conn.execute(
                    "CREATE TABLE document_chunks_rdr108k1_new ("
                    "    doc_id      TEXT NOT NULL"
                    "                REFERENCES documents(tumbler) ON DELETE CASCADE,"
                    "    position    INTEGER NOT NULL,"
                    "    chash       TEXT NOT NULL,"
                    "    chunk_index INTEGER,"
                    "    line_start  INTEGER,"
                    "    line_end    INTEGER,"
                    "    char_start  INTEGER,"
                    "    char_end    INTEGER,"
                    "    PRIMARY KEY (doc_id, position)"
                    ")"
                )
                self._conn.execute(
                    f"INSERT INTO document_chunks_rdr108k1_new ({_copy_sql})"
                    f" SELECT {_copy_sql} FROM document_chunks"
                )
                self._conn.execute("DROP TABLE document_chunks")
                self._conn.execute(
                    "ALTER TABLE document_chunks_rdr108k1_new RENAME TO document_chunks"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_document_chunks_chash"
                    " ON document_chunks(chash)"
                )

                # RDR-108 D2 (nexus-mydi): backfill collections rows for any
        # documents.physical_collection value that has no matching
        # collections.name row. Pre-RDR-108 catalogs accumulated docs
        # whose physical_collection was a free-form string, never
        # registered in the collections projection — surfaced as the
        # 11.3x chash_index drift and 76% document_aspects orphan rate
        # in the 2026-05-08 prod-shakeout. This backfill is structural
        # prep for FK enforcement on documents.physical_collection
        # (deferred to a follow-up migration that recreates the
        # documents table per SQLite's 12-step ALTER pattern).
        # Idempotent: the SELECT-then-INSERT pattern with a
        # collections.name PK on the target side is a no-op when the
        # row already exists.
        # Probe-guarded: stripped-down legacy schemas (e.g. the
        # alias_of migration test fixture) may lack the
        # physical_collection column entirely; skip the backfill in
        # that case rather than raise.
        # nexus-572g K7: track inserted names so Catalog.__init__ can
        # emit synthetic CollectionCreated events with
        # legacy_grandfathered=True. Without backing events the rows
        # vanish on Catalog.rebuild() (DELETE FROM collections + JSONL
        # replay). Catalog reads _backfilled_collections immediately
        # after constructing this CatalogStore.
        self._backfilled_collections: set[str] = set()
        try:
            self._conn.execute(
                "SELECT physical_collection FROM documents LIMIT 0"
            )
        except sqlite3.OperationalError:
            pass
        else:
            # RDR-108 D2 (nexus-mydi → nexus-572g): identify candidate
            # collection names before INSERT so we track exactly which
            # names actually land (the INSERT below skips existing rows).
            # The candidate set seeds _emit_backfilled_collection_events
            # (catalog.py) so the event-sourced model stays canonical:
            # synthetic CollectionCreated events are emitted post-rebuild
            # so a JSONL replay materializes the same collections rows.
            #
            # SIG-7 (nexus-872w): created_at is a real ISO timestamp
            # (parameter-bound, not f-string interpolated) so audit
            # tools can distinguish rows that were backfilled from
            # rows that were never written.
            #
            # O-5: INSERT OR IGNORE replaces the NOT IN subquery
            # form (perf — collections.name is a PK so OR IGNORE is
            # the natural shape).
            candidates = {
                row[0]
                for row in self._conn.execute(
                    "SELECT DISTINCT physical_collection FROM documents "
                    "WHERE physical_collection IS NOT NULL "
                    "  AND physical_collection != '' "
                    "  AND physical_collection NOT IN "
                    "      (SELECT name FROM collections)"
                ).fetchall()
            }
            if candidates:
                # nexus-33xm: created_at is INTENTIONALLY left empty
                # here. The companion ``_emit_backfilled_collection_events``
                # (catalog.py) emits CollectionCreated events with
                # ``created_at=""`` for these same names; the projector
                # handler does ``INSERT OR REPLACE ... COALESCE((SELECT
                # created_at FROM collections WHERE name = ?), ?)``. If
                # this auto-bootstrap stamps ``NOW()``, the COALESCE
                # preserves the synthetic stamp on event apply but a
                # fresh ``--replay-equality`` replay (which starts from
                # an empty table) takes the ``""`` from the event
                # payload, producing a permanent drift between live
                # and projected. Empty here keeps the two paths
                # bit-equal.
                #
                # SIG-7 (nexus-872w) audit-distinction goal: the prior
                # NOW() stamp was meant to let audit tools distinguish
                # auto-bootstrapped rows from event-derived rows. That
                # distinction now lives in the synthetic event itself
                # (``payload.created_at == ""`` marks a backfilled row);
                # the SQLite row no longer needs to carry it.
                with self._conn:
                    self._conn.execute(
                        "INSERT OR IGNORE INTO collections "
                        "(name, content_type, owner_id, embedding_model, "
                        " model_version, display_name, legacy_grandfathered, "
                        " superseded_by, superseded_at, created_at) "
                        "SELECT DISTINCT physical_collection, '', '', '', '', '', "
                        "  1, '', '', '' "
                        "FROM documents "
                        "WHERE physical_collection IS NOT NULL "
                        "  AND physical_collection != ''",
                    )
                self._backfilled_collections = candidates

        # RDR-137 Phase 1.5a (nexus-tts0d.1): follow-on pass that
        # populates ``collections.owner_id`` for rows synthesised above
        # (and any pre-existing row with empty owner_id). The
        # conformant-name path is the only one enabled here — it
        # parses RDR-103 four-segment names and extracts the 2nd
        # segment, which is always correct when the name is well-formed.
        # Documents-table fallback for legacy 2-segment names is
        # operator-driven via ``nx catalog backfill-owner-id``.
        from nexus.catalog.collections_owner_backfill import (  # noqa: PLC0415
            backfill_owner_id,
        )
        with self._conn:
            backfill_owner_id(self._conn, include_documents_fallback=False)

    # ── identity ──────────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """Path to the underlying ``.catalog.db`` file."""
        return self._path

    @property
    def backfilled_collections(self) -> set[str]:
        """Names whose ``collections`` row was synthesized at
        construction time. See the §A8 carve-out at top-of-module.
        """
        return self._backfilled_collections

    # ── rebuild (event-replay path) ───────────────────────────────────

    def rebuild(
        self,
        owners: "dict[str, OwnerRecord]",
        documents: "dict[str, DocumentRecord]",
        links: "list[LinkRecord]",
        *,
        consistency_mtime: float | None = None,
    ) -> None:
        """Truncate all tables and reload from JSONL-derived dicts.

        Uses the FTS5 bulk-load fence (drop triggers + INSERT-rebuild)
        so the per-row ``documents_ai`` trigger does NOT queue every
        column for every replayed document — that pattern stalls COMMIT
        for tens of minutes on a catalog with hundreds of thousands of
        rows. With the fence, FTS5 segments are built once at the end
        in source order. See ``bulk_load_documents`` docstring.

        ``consistency_mtime`` (RDR-104 critic Critical #2 fix): when
        supplied, the consistency-marker write happens INSIDE the same
        ``with self._lock, self._conn:`` block as the projection writes
        — atomic. Pre-fix the marker was written by an independent
        ``commit()`` after rebuild returned, which left a refactoring
        hazard (any future code that put the marker write before the
        projection commit would silently corrupt the projection by
        skipping events on the next run). Putting both writes in the
        same transaction makes the atomicity invariant trivially
        true regardless of caller ordering.
        """
        # RDR-108 Phase 3 (nexus-bdag): ``document_chunks`` is FK-bound
        # to ``documents`` with ON DELETE CASCADE. The DELETE FROM
        # documents below would cascade-wipe the manifest, but the
        # projector does not re-emit ``ChunkIndexed`` rows during
        # legacy replay (the manifest is populated by the post-store
        # batch hook, not by the JSONL log yet). Disable FK enforcement
        # around the DELETE+reload so the cascade doesn't fire; INSERTs
        # restore valid references and we re-enable FK afterwards.
        # PRAGMA foreign_keys is a no-op within a transaction, so it
        # must run BEFORE the ``with self._conn:`` block opens its
        # transaction.
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._rebuild_inner(owners, documents, links, consistency_mtime=consistency_mtime)
            # nexus-lrhg #3: with FK enforcement disabled the DELETE FROM
            # documents above did NOT cascade-delete the
            # ``document_chunks`` manifest. Any rows whose ``doc_id``
            # references a tombstoned document (DocumentDeleted in the
            # legacy log that this rebuild replays) survive the wipe and
            # become orphans: they reference a doc_id that the new
            # documents INSERT loop did not restore. PRAGMA foreign_keys
            # =ON below only enforces new writes, not existing rows, so
            # the orphan rows persist silently forever. Wipe them
            # explicitly before re-enabling FK so the post-rebuild state
            # is FK-clean.
            orphan_count = self._conn.execute(
                "SELECT COUNT(*) FROM document_chunks "
                "WHERE doc_id NOT IN (SELECT tumbler FROM documents)"
            ).fetchone()[0]
            if orphan_count:
                self._conn.execute(
                    "DELETE FROM document_chunks "
                    "WHERE doc_id NOT IN (SELECT tumbler FROM documents)"
                )
                self._conn.commit()
                _log.info(
                    "catalog_db_rebuild_orphan_chunks_purged",
                    count=orphan_count,
                )
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")

        _log.debug("catalog_db.rebuild", owners=len(owners), documents=len(documents), links=len(links))

    def _rebuild_inner(
        self,
        owners: "dict[str, OwnerRecord]",
        documents: "dict[str, DocumentRecord]",
        links: "list[LinkRecord]",
        *,
        consistency_mtime: float | None,
    ) -> None:
        """Inner rebuild routine called by :meth:`rebuild` with FK
        enforcement disabled around it (RDR-108 Phase 3)."""
        with self._lock, self._conn, self.bulk_load_documents():
            # Delete from base tables. Triggers are dropped for the
            # duration of the bulk_load_documents fence; FTS5 is
            # rebuilt in one pass at fence-exit.
            self._conn.execute("DELETE FROM links")
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("DELETE FROM owners")
            # RDR-104 Step 0 (Critical #1): clear ``collections`` too.
            # Legacy rebuild has no ``collections`` dict (collections
            # are event-sourced exclusively), so this DELETE leaves
            # the table empty for the event-sourced replay that
            # always follows when ``events.jsonl`` exists. Pre-fix
            # the absence of this DELETE combined with
            # ``_v0_collection_created``'s ``INSERT OR REPLACE`` plus
            # COALESCE-preservation pattern silently inherited stale
            # ``superseded_by``/``superseded_at``/``created_at`` from
            # the prior run.
            self._conn.execute("DELETE FROM collections")

            for prefix, o in owners.items():
                # RDR-137 followup CRITICAL-1 (nexus-43qgm.1): include
                # head_hash so rebuild from JSONL preserves the value.
                # ``getattr`` with default keeps replay-compat for
                # legacy OwnerRecord rows that pre-date the field.
                self._conn.execute(
                    "INSERT OR REPLACE INTO owners "
                    "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root, head_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        prefix, o.name, o.owner_type, o.repo_hash,
                        o.description, o.repo_root,
                        getattr(o, "head_hash", "") or "",
                    ),
                )

            for tumbler, d in documents.items():
                self._conn.execute(
                    "INSERT INTO documents "
                    "(tumbler, title, author, year, content_type, file_path, "
                    "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                    "metadata, source_mtime, alias_of, source_uri) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tumbler,
                        d.title,
                        d.author,
                        d.year,
                        d.content_type,
                        d.file_path,
                        d.corpus,
                        d.physical_collection,
                        d.chunk_count,
                        d.head_hash,
                        d.indexed_at,
                        json.dumps(d.meta),
                        d.source_mtime,
                        d.alias_of,
                        d.source_uri,
                    ),
                )

            for lnk in links:
                self._conn.execute(
                    "INSERT OR IGNORE INTO links "
                    "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
                    "created_by, created_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        lnk.from_t,
                        lnk.to_t,
                        lnk.link_type,
                        lnk.from_span,
                        lnk.to_span,
                        lnk.created_by,
                        lnk.created_at,
                        json.dumps(lnk.meta),
                    ),
                )

            # RDR-104 critic Critical #2 fix: consistency marker write
            # is part of the same transaction as the projection writes.
            # On a mid-rebuild crash, the projection rolls back AND the
            # marker stays at its pre-rebuild value — next run sees the
            # stale marker and re-rebuilds correctly. Pre-fix this lived
            # outside the transaction in `_write_consistency_marker`'s
            # own commit().
            if consistency_mtime is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("last_consistency_mtime", f"{consistency_mtime}"),
                )

    # ── document-number sequence ──────────────────────────────────────

    def next_document_number(self, owner_prefix: str) -> int:
        """Max document number for owner + 1.

        Uses dot-count matching to avoid lexicographic ordering bugs
        (e.g., '1.10' < '1.9' in string comparison).
        """
        depth = len(owner_prefix.split("."))
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(CAST(substr(tumbler, length(?) + 2) AS INTEGER)) "
                "FROM documents WHERE tumbler LIKE ? "
                "AND (length(tumbler) - length(replace(tumbler, '.', ''))) = ?",
                (owner_prefix, owner_prefix + ".%", depth),
            ).fetchone()
            return (row[0] or 0) + 1

    # ── search / traversal ────────────────────────────────────────────

    def search(self, query: str, *, content_type: str | None = None) -> list[dict[str, Any]]:
        """FTS5 MATCH over title, author, corpus, file_path."""
        safe_q = _sanitize_fts5(query)
        if not safe_q.strip():
            return []

        with self._lock:
            sql = (
                "SELECT d.tumbler, d.title, d.author, d.year, d.content_type, "
                "d.file_path, d.corpus, d.physical_collection, d.chunk_count, "
                "d.head_hash, d.indexed_at, d.metadata, d.source_mtime "
                "FROM documents d "
                "JOIN documents_fts f ON d.rowid = f.rowid "
                "WHERE documents_fts MATCH ?"
            )
            params: list[str] = [safe_q]

            if content_type:
                sql += " AND d.content_type = ?"
                params.append(content_type)

            rows = self._conn.execute(sql, params).fetchall()
            columns = ["tumbler", "title", "author", "year", "content_type",
                        "file_path", "corpus", "physical_collection", "chunk_count",
                        "head_hash", "indexed_at", "metadata", "source_mtime"]
            return [dict(zip(columns, row)) for row in rows]

    def descendants(self, prefix: str) -> list[dict[str, Any]]:
        """All documents whose tumbler starts with prefix (any depth).

        Uses LIKE 'prefix.%' so prefix itself is excluded — only strict
        descendants are returned.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT tumbler, title, author, year, content_type, "
                "file_path, corpus, physical_collection, chunk_count, "
                "head_hash, indexed_at, metadata, source_mtime "
                "FROM documents WHERE tumbler LIKE ?",
                (prefix + ".%",),
            ).fetchall()
        columns = [
            "tumbler", "title", "author", "year", "content_type",
            "file_path", "corpus", "physical_collection", "chunk_count",
            "head_hash", "indexed_at", "metadata", "source_mtime",
        ]
        return [dict(zip(columns, row)) for row in rows]

    # ── raw SQL passthrough ───────────────────────────────────────────

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
        """Thread-safe execute wrapper. Acquires _lock before executing."""
        with self._lock:
            return self._conn.execute(sql, params)

    def commit(self) -> None:
        """Thread-safe commit wrapper."""
        with self._lock:
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic transaction context (RDR-101 round-3, atomicity fix).

        Wraps the connection in ``with self._lock, self._conn:`` so a
        sequence of ``execute()`` calls runs as one transaction —
        commits on success, rolls back on any exception. ``_lock`` is
        an ``RLock`` so the inner ``execute()`` calls re-acquire the
        same thread's lock without deadlock.

        Used by ``Catalog._ensure_consistent`` to make the
        DELETE+replay rebuild atomic; pre-fix the three DELETEs
        autocommitted before ``Projector.apply_all`` ran, so any
        exception mid-replay (a malformed event, the v: 1 raise) left
        SQLite empty and unrecoverable until the next mtime tick.
        """
        with self._lock, self._conn:
            yield self._conn

    @contextmanager
    def bulk_load_documents(self) -> Iterator[None]:
        """FTS5 bulk-load fence around mass document writes.

        Drops the ``documents_ai`` / ``documents_au`` / ``documents_ad``
        FTS5 triggers, yields to the caller (which performs the writes),
        then recreates the triggers and runs FTS5's ``rebuild`` command
        to materialize the index in one pass from the final document
        rows.

        Why this exists: the projection-rebuild path in
        ``Catalog._ensure_consistent`` deletes every document and replays
        the entire event log inside one transaction. With per-row FTS5
        triggers active, each replayed INSERT queues every term/column
        in an in-memory hash. SQLite cannot incrementally merge that
        hash into on-disk segments mid-transaction; the merge happens at
        COMMIT, in one shot, and walks every queued entry. On a project
        with hundreds of thousands of events the COMMIT alone takes
        15-20 minutes of CPU on ``fts5IndexCrisismerge`` /
        ``fts5HashEntrySort``, with no user-visible signal that anything
        is happening.

        FTS5's documented bulk-load idiom — ``INSERT INTO fts(fts) VALUES
        ('rebuild')`` — is dramatically faster because it builds segments
        directly from the content table in source order, skipping the
        per-row hash queue entirely.

        MUST be called inside a ``transaction()`` block so the schema
        changes (DROP/CREATE TRIGGER) and the rebuild are part of the
        same atomic unit as the DELETE+replay. If the caller's transaction
        rolls back, the triggers come back too.
        """
        with self._lock:
            for trig in ("documents_ai", "documents_au", "documents_ad"):
                self._conn.execute(f"DROP TRIGGER IF EXISTS {trig}")
        try:
            yield
        finally:
            with self._lock:
                for sql in _DOCUMENTS_FTS_TRIGGERS:
                    self._conn.execute(sql)
                # Bulk-rebuild FTS5 from the final state of `documents`.
                # Cheap when the table is small; orders of magnitude
                # faster than the per-row trigger path on rebuilds with
                # >1k documents.
                self._conn.execute(
                    "INSERT INTO documents_fts(documents_fts) VALUES ('rebuild')"
                )

    # ── lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        with self._lock:
            self._conn.close()
