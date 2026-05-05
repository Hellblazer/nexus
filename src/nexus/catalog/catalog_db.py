# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import structlog

from nexus.catalog.tumbler import DocumentRecord, LinkRecord, OwnerRecord
from nexus.db.t2 import _sanitize_fts5

_log = structlog.get_logger()

# FTS5 trigger DDL extracted as standalone strings so the bulk-load
# fence (``CatalogDB.bulk_load_documents``) can drop and recreate them
# inside a transaction without re-running the entire schema script. Keep
# in sync with the inline copies in ``_SCHEMA_SQL`` below — the inline
# ones run on initial schema creation, these run on the rebuild path.
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
"""


class CatalogDB:
    """SQLite query cache for the JSONL-backed catalog."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
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
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)
        # Migration: add repo_root column if missing (pre-RDR-060 databases)
        try:
            self._conn.execute("SELECT repo_root FROM owners LIMIT 0")
        except sqlite3.OperationalError:
            with self._conn:
                self._conn.execute("ALTER TABLE owners ADD COLUMN repo_root TEXT DEFAULT ''")

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
                    self._conn.execute(
                        "CREATE TABLE owners_nexus7vuw_new ("
                        "    tumbler_prefix TEXT PRIMARY KEY, "
                        "    name TEXT NOT NULL, "
                        "    owner_type TEXT NOT NULL, "
                        "    repo_hash TEXT, "
                        "    description TEXT, "
                        "    repo_root TEXT DEFAULT '', "
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
                        " description, repo_root) "
                        "SELECT tumbler_prefix, name, owner_type, repo_hash, "
                        "       description, repo_root FROM owners"
                    )
                    self._conn.execute("DROP TABLE owners")
                    self._conn.execute(
                        "ALTER TABLE owners_nexus7vuw_new RENAME TO owners"
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

    def rebuild(
        self,
        owners: dict[str, OwnerRecord],
        documents: dict[str, DocumentRecord],
        links: list[LinkRecord],
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
        with self._lock, self._conn, self.bulk_load_documents():
            # Delete from base tables. Triggers are dropped for the
            # duration of the bulk_load_documents fence; FTS5 is
            # rebuilt in one pass at fence-exit.
            self._conn.execute("DELETE FROM links")
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("DELETE FROM owners")

            for prefix, o in owners.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO owners (tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (prefix, o.name, o.owner_type, o.repo_hash, o.description, o.repo_root),
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

        _log.debug("catalog_db.rebuild", owners=len(owners), documents=len(documents), links=len(links))

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

    def search(self, query: str, *, content_type: str | None = None) -> list[dict]:
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

    def descendants(self, prefix: str) -> list[dict]:
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

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        """Thread-safe execute wrapper. Acquires _lock before executing."""
        with self._lock:
            return self._conn.execute(sql, params)

    def commit(self) -> None:
        """Thread-safe commit wrapper."""
        with self._lock:
            self._conn.commit()

    @contextmanager
    def transaction(self):
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
    def bulk_load_documents(self):
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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
