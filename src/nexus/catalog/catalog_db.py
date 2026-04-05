# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import structlog

from nexus.catalog.tumbler import DocumentRecord, LinkRecord, OwnerRecord
from nexus.db.t2 import _sanitize_fts5

_log = structlog.get_logger()

_SCHEMA_SQL = """\
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS owners (
    tumbler_prefix TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    owner_type TEXT NOT NULL,
    repo_hash TEXT,
    description TEXT
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
    metadata JSON
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
"""


class CatalogDB:
    """SQLite query cache for the JSONL-backed catalog."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_SQL)

    def rebuild(
        self,
        owners: dict[str, OwnerRecord],
        documents: dict[str, DocumentRecord],
        links: list[LinkRecord],
    ) -> None:
        """Truncate all tables and reload from JSONL-derived dicts."""
        with self._conn:
            # Delete from base tables — triggers sync FTS automatically
            self._conn.execute("DELETE FROM links")
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("DELETE FROM owners")

            for prefix, o in owners.items():
                self._conn.execute(
                    "INSERT INTO owners (tumbler_prefix, name, owner_type, repo_hash, description) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (prefix, o.name, o.owner_type, o.repo_hash, o.description),
                )

            for tumbler, d in documents.items():
                self._conn.execute(
                    "INSERT INTO documents "
                    "(tumbler, title, author, year, content_type, file_path, "
                    "corpus, physical_collection, chunk_count, head_hash, indexed_at, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    ),
                )

            for lnk in links:
                self._conn.execute(
                    "INSERT INTO links "
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
                        lnk.created,
                        json.dumps(lnk.meta),
                    ),
                )

        _log.debug("catalog_db.rebuild", owners=len(owners), documents=len(documents), links=len(links))

    def next_document_number(self, owner_prefix: str) -> int:
        """Max document number for owner + 1."""
        # Use range query to avoid LIKE prefix collision (1.1.% matches 1.10.*)
        lower = owner_prefix + "."
        parts = owner_prefix.split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        upper = ".".join(parts)
        row = self._conn.execute(
            "SELECT MAX(CAST(substr(tumbler, length(?) + 2) AS INTEGER)) "
            "FROM documents WHERE tumbler >= ? AND tumbler < ?",
            (owner_prefix, lower, upper),
        ).fetchone()
        return (row[0] or 0) + 1

    def search(self, query: str, *, content_type: str | None = None) -> list[dict]:
        """FTS5 MATCH over title, author, corpus, file_path."""
        safe_q = _sanitize_fts5(query)
        if not safe_q.strip():
            return []

        sql = (
            "SELECT d.tumbler, d.title, d.author, d.year, d.content_type, "
            "d.file_path, d.corpus, d.physical_collection, d.chunk_count, "
            "d.head_hash, d.indexed_at "
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
                    "head_hash", "indexed_at"]
        return [dict(zip(columns, row)) for row in rows]

    def close(self) -> None:
        self._conn.close()
