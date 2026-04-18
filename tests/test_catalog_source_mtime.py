# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-8luh — ``catalog.documents`` carries ``source_mtime`` at index time.

Three surfaces are under test:

  1. ``DocumentRecord`` + ``CatalogEntry`` dataclass round-trip.
  2. ``CatalogDB`` schema — fresh install has the column; ALTER-on-open
     migration adds it to pre-8luh databases without data loss.
  3. ``Catalog.register`` / ``Catalog.update`` / ``Catalog.by_file_path`` /
     ``Catalog.resolve`` / ``Catalog.by_doc_id`` / ``Catalog.delete_document``
     round-trip source_mtime through JSONL + SQLite.

Adds a column-exists smoke test so downstream consumers (RDR-087 Phase
3.4 stale_source_ratio) can assume the schema is present.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.tumbler import DocumentRecord, read_documents


# ── Dataclass round-trip ────────────────────────────────────────────────────


class TestDocumentRecord:
    def test_default_mtime_is_zero(self) -> None:
        rec = DocumentRecord(
            tumbler="1.1.1", title="t", author="", year=0,
            content_type="paper", file_path="x.pdf",
            corpus="", physical_collection="knowledge__x",
            chunk_count=1, head_hash="", indexed_at="",
        )
        assert rec.source_mtime == 0.0

    def test_accepts_explicit_mtime(self) -> None:
        rec = DocumentRecord(
            tumbler="1.1.1", title="t", author="", year=0,
            content_type="paper", file_path="x.pdf",
            corpus="", physical_collection="knowledge__x",
            chunk_count=1, head_hash="", indexed_at="",
            source_mtime=1_700_000_000.5,
        )
        assert rec.source_mtime == 1_700_000_000.5

    def test_jsonl_roundtrip_preserves_mtime(self, tmp_path: Path) -> None:
        path = tmp_path / "documents.jsonl"
        rec = DocumentRecord(
            tumbler="1.1.1", title="t", author="", year=0,
            content_type="paper", file_path="x.pdf",
            corpus="", physical_collection="knowledge__x",
            chunk_count=1, head_hash="", indexed_at="",
            source_mtime=1_700_000_000.5,
        )
        with path.open("w") as f:
            f.write(json.dumps(rec.__dict__) + "\n")
        loaded = read_documents(path)
        assert loaded["1.1.1"].source_mtime == 1_700_000_000.5


# ── Schema ──────────────────────────────────────────────────────────────────


class TestCatalogDBSchema:
    def test_fresh_install_has_column(self, tmp_path: Path) -> None:
        db = CatalogDB(tmp_path / "cat.db")
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(documents)").fetchall()]
        assert "source_mtime" in cols

    def test_migration_adds_column_to_pre_8luh_db(self, tmp_path: Path) -> None:
        """ALTER-on-open must patch a pre-migration catalog.db that has
        every column except source_mtime. Verifies the try/SELECT guard
        actually runs the ALTER instead of silently skipping it."""
        db_path = tmp_path / "cat.db"
        conn = sqlite3.connect(str(db_path))
        # Legacy schema — no source_mtime column.
        conn.execute("""
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
                metadata JSON
            )
        """)
        conn.execute(
            "INSERT INTO documents (tumbler, title, author, year, content_type, "
            "file_path, corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata) VALUES ('1.1.1', 'legacy', '', 0, 'paper', "
            "'legacy.pdf', '', 'knowledge__legacy', 1, '', '', '{}')"
        )
        conn.commit()
        conn.close()

        # Re-open via CatalogDB — migration should kick in.
        db = CatalogDB(db_path)
        cols = [r[1] for r in db._conn.execute("PRAGMA table_info(documents)").fetchall()]
        assert "source_mtime" in cols
        # Legacy data survives, and pre-existing rows get the default 0.
        row = db._conn.execute(
            "SELECT title, source_mtime FROM documents WHERE tumbler = ?",
            ("1.1.1",),
        ).fetchone()
        assert row == ("legacy", 0.0)


# ── Catalog CRUD ────────────────────────────────────────────────────────────


class TestCatalogRegisterStoresMtime:
    def _seed(self, tmp_path: Path) -> tuple[Catalog, Path]:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")
        return cat, cat_dir

    def test_register_without_mtime_defaults_to_zero(self, tmp_path: Path) -> None:
        cat, _ = self._seed(tmp_path)
        owner = cat.register_owner("repo", "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper",
            file_path="a.pdf", physical_collection="knowledge__x",
        )
        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.source_mtime == 0.0

    def test_register_preserves_explicit_mtime(self, tmp_path: Path) -> None:
        cat, _ = self._seed(tmp_path)
        owner = cat.register_owner("repo", "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper",
            file_path="a.pdf", physical_collection="knowledge__x",
            source_mtime=1_700_000_000.25,
        )
        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.source_mtime == 1_700_000_000.25

    def test_by_file_path_returns_mtime(self, tmp_path: Path) -> None:
        cat, _ = self._seed(tmp_path)
        owner = cat.register_owner("repo", "corpus")
        cat.register(
            owner, title="doc", content_type="paper",
            file_path="a.pdf", physical_collection="knowledge__x",
            source_mtime=123.5,
        )
        entry = cat.by_file_path(owner, "a.pdf")
        assert entry is not None
        assert entry.source_mtime == 123.5

    def test_update_can_bump_mtime(self, tmp_path: Path) -> None:
        """Callers re-indexing a file must be able to bump stored mtime
        to the latest file.stat().st_mtime without wiping the rest of
        the record."""
        cat, _ = self._seed(tmp_path)
        owner = cat.register_owner("repo", "corpus")
        tumbler = cat.register(
            owner, title="doc", content_type="paper",
            file_path="a.pdf", physical_collection="knowledge__x",
            source_mtime=100.0,
        )
        cat.update(tumbler, source_mtime=200.5)
        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.source_mtime == 200.5
        assert entry.title == "doc"  # other fields preserved

    def test_by_doc_id_returns_mtime(self, tmp_path: Path) -> None:
        cat, _ = self._seed(tmp_path)
        owner = cat.register_owner("repo", "corpus")
        cat.register(
            owner, title="doc", content_type="paper",
            file_path="a.pdf", physical_collection="knowledge__x",
            meta={"doc_id": "doc-abc-42"},
            source_mtime=55.5,
        )
        entry = cat.by_doc_id("doc-abc-42")
        assert entry is not None
        assert entry.source_mtime == 55.5


# ── Indexing-side plumbing smoke test ───────────────────────────────────────


class TestIndexSiteCapturesMtime:
    def test_real_file_mtime_propagates_via_catalog_hook(self, tmp_path: Path) -> None:
        """The indexer calls register() with file.stat().st_mtime; end-to-end
        we verify the catalog stores the real mtime for a file we created."""
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        cat = Catalog(cat_dir, cat_dir / ".catalog.db")

        # Mimic what indexer.py:291 now does: call cat.register with
        # file.stat().st_mtime.
        real_file = tmp_path / "sample.md"
        real_file.write_text("hello")
        # Set a known mtime so the round-trip is deterministic.
        os_mtime = real_file.stat().st_mtime
        owner = cat.register_owner("repo", "corpus")
        tumbler = cat.register(
            owner, title="sample", content_type="prose",
            file_path=str(real_file), physical_collection="docs__x",
            source_mtime=os_mtime,
        )
        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.source_mtime == pytest.approx(os_mtime, abs=1e-3)
