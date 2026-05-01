# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 1 PR D bib columns on the catalog Document projection.

Coverage:
- Fresh CatalogDB construction yields all 8 bib_* columns with the
  documented types and defaults (the schema_sql path).
- A pre-bib-columns CatalogDB (simulated by dropping the columns from a
  fresh schema, then reconstructing) gets the columns added via the
  inline ALTER TABLE migration (the upgrade path).
- Both partial indexes exist after construction, in either path.
- The partial indexes are actually partial (the WHERE clause survives
  the migration; without it, an indexed seek of "is enriched on
  backend X" reads every row).
- Existing rows survive the upgrade with empty bib_* defaults.
- Insert/update round-trips of bib_* values land verbatim.

Phase 1 only ships the schema; no projector handler populates these
columns yet (Phase 3 wires DocumentEnriched v: 1 → projection). These
tests therefore use direct SQLite writes to exercise the schema
contract, not the projector.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.catalog.catalog_db import CatalogDB


_BIB_COLUMNS: dict[str, tuple[str, str]] = {
    "bib_year":                ("INTEGER", "0"),
    "bib_authors":             ("TEXT",    "''"),
    "bib_venue":               ("TEXT",    "''"),
    "bib_citation_count":      ("INTEGER", "0"),
    "bib_semantic_scholar_id": ("TEXT",    "''"),
    "bib_openalex_id":         ("TEXT",    "''"),
    "bib_doi":                 ("TEXT",    "''"),
    "bib_enriched_at":         ("TEXT",    "''"),
}


def _columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {
        row[1]: {"type": row[2], "notnull": row[3], "dflt": row[4], "pk": row[5]}
        for row in cur.fetchall()
    }


def _index_sql(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (name,),
    ).fetchone()
    return row[0] if row else None


# ── Fresh-install path ───────────────────────────────────────────────────


class TestFreshSchema:
    def test_all_bib_columns_present(self, tmp_path: Path):
        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            cols = _columns(db._conn, "documents")
            for name in _BIB_COLUMNS:
                assert name in cols, f"missing column {name}"
        finally:
            db.close()

    def test_bib_column_types_and_defaults(self, tmp_path: Path):
        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            cols = _columns(db._conn, "documents")
            for name, (expected_type, expected_dflt) in _BIB_COLUMNS.items():
                meta = cols[name]
                assert meta["type"] == expected_type, (
                    f"{name}: expected type {expected_type}, got {meta['type']}"
                )
                # NOT NULL DEFAULT shows up as notnull=1 + dflt set
                assert meta["notnull"] == 1, f"{name} should be NOT NULL"
                assert meta["dflt"] == expected_dflt, (
                    f"{name}: expected default {expected_dflt!r}, "
                    f"got {meta['dflt']!r}"
                )
        finally:
            db.close()

    def test_partial_index_on_bib_s2_id(self, tmp_path: Path):
        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            sql = _index_sql(db._conn, "idx_documents_bib_s2_id")
            assert sql is not None, (
                "idx_documents_bib_s2_id index missing — Phase 4 skip-query "
                "for nx enrich bib will scan instead of seeking"
            )
            # Partial: WHERE clause survives.
            assert "bib_semantic_scholar_id" in sql
            assert "!= ''" in sql or "<>" in sql
        finally:
            db.close()

    def test_partial_index_on_bib_oa_id(self, tmp_path: Path):
        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            sql = _index_sql(db._conn, "idx_documents_bib_oa_id")
            assert sql is not None
            assert "bib_openalex_id" in sql
            assert "!= ''" in sql or "<>" in sql
        finally:
            db.close()


# ── Upgrade path: existing pre-bib DB ────────────────────────────────────


class TestUpgradeFromLegacySchema:
    """Simulate a pre-Phase-1-PR-D CatalogDB and verify the inline ALTER
    TABLE migrations bring it up to spec."""

    def test_inline_migration_adds_missing_bib_columns(self, tmp_path: Path):
        # Create a "legacy" DB by hand: documents table without bib_*
        # columns and with NO partial indexes. Then construct CatalogDB
        # against the same path — the constructor's inline migrations
        # must add the columns + indexes.
        legacy_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(legacy_path))
        try:
            conn.executescript("""
                CREATE TABLE documents (
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
                    source_uri TEXT NOT NULL DEFAULT ''
                );
            """)
            # Pre-existing row: must survive the migration with empty
            # bib_* defaults.
            conn.execute(
                "INSERT INTO documents (tumbler, title, author, year, "
                "content_type, file_path, corpus, physical_collection, "
                "chunk_count, head_hash, indexed_at, metadata, source_mtime, "
                "alias_of, source_uri) VALUES "
                "(?, ?, '', 0, '', '', '', '', 0, '', '', '{}', 0, '', '')",
                ("1.1.1", "legacy-row"),
            )
            conn.commit()
        finally:
            conn.close()

        # Construct CatalogDB — inline migrations run.
        db = CatalogDB(legacy_path)
        try:
            cols = _columns(db._conn, "documents")
            for name, (expected_type, expected_dflt) in _BIB_COLUMNS.items():
                assert name in cols, f"upgrade missed {name}"
                assert cols[name]["type"] == expected_type
                assert cols[name]["dflt"] == expected_dflt

            # Partial indexes present
            assert _index_sql(db._conn, "idx_documents_bib_s2_id") is not None
            assert _index_sql(db._conn, "idx_documents_bib_oa_id") is not None

            # Pre-existing row survived with empty bib_* defaults.
            row = db._conn.execute(
                "SELECT bib_year, bib_authors, bib_venue, bib_citation_count, "
                "bib_semantic_scholar_id, bib_openalex_id, bib_doi, "
                "bib_enriched_at FROM documents WHERE tumbler = ?",
                ("1.1.1",),
            ).fetchone()
            assert row == (0, "", "", 0, "", "", "", "")
        finally:
            db.close()

    def test_double_construction_is_idempotent(self, tmp_path: Path):
        # Constructing CatalogDB twice on the same path must not error
        # (each ALTER probes for the column first, indexes are IF NOT
        # EXISTS).
        path = tmp_path / ".catalog.db"
        CatalogDB(path).close()
        # Second construction — must not raise.
        db = CatalogDB(path)
        try:
            cols = _columns(db._conn, "documents")
            for name in _BIB_COLUMNS:
                assert name in cols
        finally:
            db.close()


# ── Round-trip: schema accepts and returns bib values ────────────────────


class TestBibValueRoundtrip:
    def test_insert_and_select_full_bib_payload(self, tmp_path: Path):
        db = CatalogDB(tmp_path / ".catalog.db")
        try:
            db._conn.execute(
                "INSERT INTO documents (tumbler, title, author, year, "
                "content_type, file_path, corpus, physical_collection, "
                "chunk_count, head_hash, indexed_at, metadata, source_mtime, "
                "alias_of, source_uri, bib_year, bib_authors, bib_venue, "
                "bib_citation_count, bib_semantic_scholar_id, "
                "bib_openalex_id, bib_doi, bib_enriched_at) "
                "VALUES (?, ?, '', 0, '', '', '', '', 0, '', '', '{}', 0, "
                "'', '', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "1.7.42", "test-paper",
                    2024, "Smith, J. and Jones, A.", "ACL", 17,
                    "ss-id-abc", "oa-id-xyz", "10.0/example",
                    "2026-04-30T12:00:00+00:00",
                ),
            )
            db._conn.commit()
            row = db._conn.execute(
                "SELECT bib_year, bib_authors, bib_venue, bib_citation_count, "
                "bib_semantic_scholar_id, bib_openalex_id, bib_doi, "
                "bib_enriched_at FROM documents WHERE tumbler = ?",
                ("1.7.42",),
            ).fetchone()
            assert row == (
                2024, "Smith, J. and Jones, A.", "ACL", 17,
                "ss-id-abc", "oa-id-xyz", "10.0/example",
                "2026-04-30T12:00:00+00:00",
            )
        finally:
            db.close()
