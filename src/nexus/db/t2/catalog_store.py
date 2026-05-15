# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""CatalogStore — eighth T2 domain store, owning the catalog tables.

RDR-112 P2.1 (nexus-7ejx): collapses ``CatalogDB`` into the T2 daemon as
the eighth domain store. The public API is intentionally identical to
``CatalogDB`` so that Phase 4 (catalog port, nexus-uar6) can flip call
sites to ``T2Client.catalog`` without any signature changes.

Design contract
---------------
- Constructor takes a ``Path`` to the shared T2 SQLite file (``memory.db``),
  the same shape as all other domain stores. Each store opens its own
  connection; WAL mode lets them coordinate without Python-level locks.
- ``_init_schema`` creates all catalog tables in ``memory.db``. Schema is
  identical to ``CatalogDB._SCHEMA_SQL`` — RDR-108 invariants preserved:
  documents-as-graph-nodes (tumblers), chunks-as-content-blobs (chash),
  document_chunks manifest authoritative for doc→chunk ordering.
- Thread-safety: ``self._lock`` is an ``RLock`` so callers inside a
  ``transaction()`` context can re-acquire from the same thread without
  deadlock (same pattern as ``CatalogDB``).
- ``close()`` is idempotent (guards under ``self._lock``).

Phase 4 flip note
-----------------
DO NOT call this class from ``src/nexus/catalog/`` yet. Phase 4 owns
that migration. This bead ships the daemon-side substrate only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

import structlog

from nexus.catalog.tumbler import DocumentRecord, LinkRecord, OwnerRecord
from nexus.db.t2.memory_store import _sanitize_fts5

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Per-domain migration guard (same pattern as MemoryStore)
# ---------------------------------------------------------------------------

_migrated_paths: set[str] = set()
_migrated_lock = threading.Lock()

# ---------------------------------------------------------------------------
# FTS5 trigger DDL (kept in sync with CatalogDB; needed for bulk-load fence)
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

# Identical to CatalogDB._SCHEMA_SQL, but without the leading
# ``PRAGMA journal_mode=WAL;`` (the store sets WAL on its connection
# after connecting, matching the pattern of all other domain stores).
_CATALOG_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS owners (
    tumbler_prefix TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_type TEXT NOT NULL,
    repo_hash TEXT,
    description TEXT,
    repo_root TEXT DEFAULT '',
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
    alias_of TEXT NOT NULL DEFAULT '',
    source_uri TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS collections (
    name TEXT PRIMARY KEY,
    content_type TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    embedding_model TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    legacy_grandfathered INTEGER NOT NULL DEFAULT 0,
    superseded_by TEXT NOT NULL DEFAULT '',
    superseded_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_collections_legacy
    ON collections(legacy_grandfathered);
CREATE INDEX IF NOT EXISTS idx_collections_owner
    ON collections(owner_id);

CREATE TABLE IF NOT EXISTS _meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_collections_tuple
    ON collections(content_type, owner_id, embedding_model);

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

_CATALOG_POST_SCHEMA_SQL = """\
CREATE INDEX IF NOT EXISTS idx_documents_bib_s2_id
    ON documents(bib_semantic_scholar_id)
    WHERE bib_semantic_scholar_id != '';

CREATE INDEX IF NOT EXISTS idx_documents_bib_oa_id
    ON documents(bib_openalex_id)
    WHERE bib_openalex_id != '';

CREATE INDEX IF NOT EXISTS idx_documents_physical_collection
    ON documents(physical_collection);
"""


# ---------------------------------------------------------------------------
# CatalogStore
# ---------------------------------------------------------------------------


class CatalogStore:
    """Eighth T2 domain store — owns catalog tables in the shared SQLite file.

    Public API is identical to ``CatalogDB`` so Phase 4 can flip call sites
    without signature changes.  Constructor shape matches other domain stores:
    takes the ``memory.db`` path, opens its own connection.

    RDR-108 schema preserved: ``documents`` graph-nodes addressed by tumblers;
    ``document_chunks`` manifest authoritative for doc-to-chunk ordering.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        # RLock: callers inside transaction() context re-acquire from the same
        # thread (same pattern as CatalogDB with its reentrant lock).
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # Cap WAL growth under long-lived MCP-server connections (issue #437).
        self._conn.execute("PRAGMA journal_size_limit=67108864")
        self._conn.commit()
        try:
            canonical_key = str(path.resolve())
        except OSError:
            canonical_key = str(path)
        self._init_schema(canonical_key)

    # -------------------------------------------------------------------------
    # Schema / migration
    # -------------------------------------------------------------------------

    def _init_schema(self, path_key: str) -> None:
        """Create catalog tables and post-schema indexes.

        Guarded by the per-domain migration lock so two constructors on the
        same path do not both run schema creation concurrently. Membership
        in ``_migrated_paths`` short-circuits subsequent constructions on
        the same path — without this, ``executescript`` would re-issue an
        implicit COMMIT on every new ``CatalogStore`` against an already-
        migrated DB, which can silently commit an open caller transaction.
        Mirrors the MemoryStore pattern (``memory_store.py:291-296``).
        """
        with _migrated_lock:
            if path_key in _migrated_paths:
                return
            _migrated_paths.add(path_key)
        with self._lock:
            self._conn.executescript(_CATALOG_SCHEMA_SQL)
            # Post-schema: partial indexes and physical_collection index.
            # These cannot live in executescript because SQLite's scripting
            # parser does not support partial-index WHERE clauses in all
            # versions.  Execute individually instead.
            for stmt in _CATALOG_POST_SCHEMA_SQL.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    try:
                        self._conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # already exists or column absent; idempotent
            self._conn.commit()
            # collections backfill: ensure any physical_collection values not
            # yet registered in the collections table get an auto-row (mirrors
            # CatalogDB.__init__ backfill path).
            self._backfill_collections()

    def _backfill_collections(self) -> None:
        """Insert legacy physical_collection values into collections (idempotent)."""
        with self._lock:
            try:
                self._conn.execute("SELECT physical_collection FROM documents LIMIT 0")
            except sqlite3.OperationalError:
                return
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
            self._conn.commit()

    # -------------------------------------------------------------------------
    # Public API (identical signatures to CatalogDB)
    # -------------------------------------------------------------------------

    def rebuild(
        self,
        owners: dict[str, OwnerRecord],
        documents: dict[str, DocumentRecord],
        links: list[LinkRecord],
        *,
        consistency_mtime: float | None = None,
    ) -> None:
        """Truncate all tables and reload from JSONL-derived dicts.

        Uses the FTS5 bulk-load fence (drop triggers + INSERT-rebuild) to
        avoid the per-row trigger path stalling COMMIT on large catalogs.
        ``consistency_mtime`` is written inside the same transaction as the
        projection writes (RDR-104 Critical #2 fix — atomicity invariant).
        """
        self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self._rebuild_inner(owners, documents, links, consistency_mtime=consistency_mtime)
            # Purge document_chunks rows whose doc_id no longer exists.
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
                _log.info("catalog_store_rebuild_orphan_chunks_purged", count=orphan_count)
        finally:
            self._conn.execute("PRAGMA foreign_keys=ON")
        _log.debug("catalog_store.rebuild", owners=len(owners), documents=len(documents), links=len(links))

    @staticmethod
    def _attr(obj: object, name: str, default: object = None) -> object:
        """Get an attribute from either a dataclass instance or a plain dict.

        The RPC wire layer decodes incoming dataclass args as plain dicts
        (``t2_json_loads`` unpacks ``__dataclass__`` tags to their fields dict).
        This helper keeps ``_rebuild_inner`` agnostic of which form it receives.
        """
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    def _rebuild_inner(
        self,
        owners: dict[str, OwnerRecord],
        documents: dict[str, DocumentRecord],
        links: list[LinkRecord],
        *,
        consistency_mtime: float | None,
    ) -> None:
        a = self._attr  # attribute accessor for dataclass OR dict
        with self._lock, self._conn, self.bulk_load_documents():
            self._conn.execute("DELETE FROM links")
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("DELETE FROM owners")
            self._conn.execute("DELETE FROM collections")

            for prefix, o in owners.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO owners "
                    "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        prefix,
                        a(o, "name", ""),
                        a(o, "owner_type", ""),
                        a(o, "repo_hash", ""),
                        a(o, "description", ""),
                        a(o, "repo_root", ""),
                    ),
                )

            for tumbler, d in documents.items():
                meta = a(d, "meta", a(d, "metadata", {}))
                if isinstance(meta, str):
                    # Already JSON-encoded (from a prior round-trip)
                    meta_json = meta
                else:
                    meta_json = json.dumps(meta)
                self._conn.execute(
                    "INSERT INTO documents "
                    "(tumbler, title, author, year, content_type, file_path, "
                    "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
                    "metadata, source_mtime, alias_of, source_uri) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        tumbler,
                        a(d, "title", ""),
                        a(d, "author", ""),
                        a(d, "year", 0),
                        a(d, "content_type", ""),
                        a(d, "file_path", ""),
                        a(d, "corpus", ""),
                        a(d, "physical_collection", ""),
                        a(d, "chunk_count", 0),
                        a(d, "head_hash", ""),
                        a(d, "indexed_at", ""),
                        meta_json,
                        a(d, "source_mtime", 0.0),
                        a(d, "alias_of", ""),
                        a(d, "source_uri", ""),
                    ),
                )

            for lnk in links:
                lmeta = a(lnk, "meta", a(lnk, "metadata", {}))
                if isinstance(lmeta, str):
                    lmeta_json = lmeta
                else:
                    lmeta_json = json.dumps(lmeta)
                self._conn.execute(
                    "INSERT OR IGNORE INTO links "
                    "(from_tumbler, to_tumbler, link_type, from_span, to_span, "
                    "created_by, created_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        a(lnk, "from_t", a(lnk, "from_tumbler", "")),
                        a(lnk, "to_t", a(lnk, "to_tumbler", "")),
                        a(lnk, "link_type", ""),
                        a(lnk, "from_span", ""),
                        a(lnk, "to_span", ""),
                        a(lnk, "created_by", ""),
                        a(lnk, "created_at", ""),
                        lmeta_json,
                    ),
                )

            if consistency_mtime is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO _meta (key, value) VALUES (?, ?)",
                    ("last_consistency_mtime", f"{consistency_mtime}"),
                )

    def next_document_number(self, owner_prefix: str) -> int:
        """Max document number for owner + 1 (dot-count matching)."""
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
            columns = [
                "tumbler", "title", "author", "year", "content_type",
                "file_path", "corpus", "physical_collection", "chunk_count",
                "head_hash", "indexed_at", "metadata", "source_mtime",
            ]
            return [dict(zip(columns, row)) for row in rows]

    def descendants(self, prefix: str) -> list[dict]:
        """All documents whose tumbler starts with prefix (any depth)."""
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

    def execute(self, sql: str, params: tuple | list = ()) -> list[tuple]:
        """Thread-safe execute. Returns fetchall() so results are serializable over RPC.

        Unlike CatalogDB.execute (which returns a Cursor), this method returns
        a list of tuples so the daemon's JSON-RPC layer can serialize the result.
        Phase 4 callers that need cursor semantics should use transaction() instead.
        """
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def commit(self) -> None:
        """Thread-safe commit."""
        with self._lock:
            self._conn.commit()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Atomic transaction context (RDR-101 round-3 atomicity fix).

        Wraps the connection in ``with self._lock, self._conn:`` so a
        sequence of operations runs as one transaction. ``_lock`` is an
        ``RLock`` so inner ``execute()`` calls can re-acquire without deadlock.
        """
        with self._lock, self._conn:
            yield self._conn

    @contextmanager
    def bulk_load_documents(self) -> Generator[None, None, None]:
        """FTS5 bulk-load fence around mass document writes.

        Drops the ``documents_ai`` / ``documents_au`` / ``documents_ad``
        FTS5 triggers, yields, then recreates the triggers and runs
        FTS5 ``rebuild`` to materialise the index in one pass.  Orders of
        magnitude faster than per-row trigger path on large catalogs.
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
                self._conn.execute(
                    "INSERT INTO documents_fts(documents_fts) VALUES ('rebuild')"
                )

    def close(self) -> None:
        """Close the connection (idempotent under ``self._lock``)."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
