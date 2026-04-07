# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for catalog-scoped pre-filtering (RDR-056 Phase 3)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.search_engine import _prefilter_from_catalog


def _create_test_catalog_db(db_path: Path, entries: list[dict]) -> sqlite3.Connection:
    """Create a minimal catalog SQLite database with test documents."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS documents ("
        "  tumbler TEXT PRIMARY KEY, title TEXT NOT NULL, author TEXT,"
        "  year INTEGER, content_type TEXT, file_path TEXT, corpus TEXT,"
        "  physical_collection TEXT, chunk_count INTEGER,"
        "  head_hash TEXT, indexed_at TEXT, metadata JSON"
        ")"
    )
    for e in entries:
        conn.execute(
            "INSERT INTO documents (tumbler, title, author, year, content_type, "
            "file_path, corpus, physical_collection, chunk_count, head_hash, "
            "indexed_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                e.get("tumbler", "1.1"),
                e.get("title", "test"),
                e.get("author"),
                e.get("year"),
                e.get("content_type"),
                e.get("file_path"),
                e.get("corpus"),
                e.get("physical_collection"),
                e.get("chunk_count", 1),
                e.get("head_hash"),
                e.get("indexed_at"),
                json.dumps(e.get("metadata", {})),
            ),
        )
    conn.commit()
    return conn


class _FakeCatalog:
    """Minimal catalog stub with a real SQLite database."""

    def __init__(self, db: sqlite3.Connection, total_docs: int = 100) -> None:
        self._db = db
        self._total_docs = total_docs

    def ids_for_predicates(self, predicates: dict) -> list[str]:
        """Query file_paths matching predicates."""
        from nexus.search_engine import _catalog_ids_for_predicates
        return _catalog_ids_for_predicates(self._db, predicates)

    def doc_count(self) -> int:
        return self._total_docs


class TestPrefilterFromCatalog:
    def test_high_selectivity_year_filter(self) -> None:
        """bib_year predicate with <5% match → returns source_path $in filter."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": f"1.{i}", "title": f"doc{i}", "year": 2024,
                     "file_path": f"/docs/paper_{i}.md"}
                    for i in range(3)
                ] + [
                    {"tumbler": f"2.{i}", "title": f"old{i}", "year": 2020,
                     "file_path": f"/docs/old_{i}.md"}
                    for i in range(97)
                ],
            )
            cat = _FakeCatalog(db, total_docs=100)
            where = {"bib_year": {"$eq": 2024}}
            result = _prefilter_from_catalog(where, cat)
            assert result is not None
            # Should contain source_path $in filter
            assert "source_path" in str(result)

    def test_low_selectivity_skips_prefilter(self) -> None:
        """Predicate matching >5% of docs → returns None (use standard filter)."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": f"1.{i}", "title": f"doc{i}", "year": 2024,
                     "file_path": f"/docs/paper_{i}.md"}
                    for i in range(50)
                ] + [
                    {"tumbler": f"2.{i}", "title": f"old{i}", "year": 2020,
                     "file_path": f"/docs/old_{i}.md"}
                    for i in range(50)
                ],
            )
            cat = _FakeCatalog(db, total_docs=100)
            where = {"bib_year": {"$eq": 2024}}
            result = _prefilter_from_catalog(where, cat)
            assert result is None  # 50% match, too broad

    def test_no_catalog_returns_none(self) -> None:
        """catalog=None → returns None (no pre-filtering)."""
        result = _prefilter_from_catalog({"bib_year": 2024}, None)
        assert result is None

    def test_no_where_returns_none(self) -> None:
        """No where predicates → no pre-filtering needed."""
        result = _prefilter_from_catalog(None, MagicMock())
        assert result is None

    def test_too_many_ids_falls_back(self) -> None:
        """More than 500 matching IDs → returns None (too expensive for $in)."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": f"1.{i}", "title": f"doc{i}", "year": 2024,
                     "file_path": f"/docs/paper_{i}.md"}
                    for i in range(501)
                ],
            )
            cat = _FakeCatalog(db, total_docs=20000)
            where = {"bib_year": {"$eq": 2024}}
            result = _prefilter_from_catalog(where, cat)
            assert result is None  # >500 IDs

    def test_empty_catalog_returns_none(self) -> None:
        """Empty catalog → no matches → returns None."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(Path(td) / "cat.db", [])
            cat = _FakeCatalog(db, total_docs=0)
            where = {"bib_year": {"$eq": 2024}}
            result = _prefilter_from_catalog(where, cat)
            assert result is None

    def test_year_range_filter(self) -> None:
        """bib_year range (>=2023) → filters correctly."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": "1.1", "title": "new1", "year": 2024,
                     "file_path": "/a.md"},
                    {"tumbler": "1.2", "title": "new2", "year": 2023,
                     "file_path": "/b.md"},
                    {"tumbler": "1.3", "title": "old", "year": 2020,
                     "file_path": "/c.md"},
                ] + [
                    {"tumbler": f"2.{i}", "title": f"filler{i}", "year": 2010,
                     "file_path": f"/filler_{i}.md"}
                    for i in range(97)
                ],
            )
            cat = _FakeCatalog(db, total_docs=100)
            where = {"bib_year": {"$gte": 2023}}
            result = _prefilter_from_catalog(where, cat)
            assert result is not None

    def test_unsupported_predicate_returns_none(self) -> None:
        """Predicate not mappable to catalog columns → returns None."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [{"tumbler": "1.1", "title": "t", "file_path": "/a.md"}],
            )
            cat = _FakeCatalog(db, total_docs=100)
            where = {"custom_field": "value"}
            result = _prefilter_from_catalog(where, cat)
            assert result is None


class TestCatalogIdsForPredicates:
    def test_year_eq(self) -> None:
        from nexus.search_engine import _catalog_ids_for_predicates
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": "1.1", "title": "a", "year": 2024, "file_path": "/a.md"},
                    {"tumbler": "1.2", "title": "b", "year": 2020, "file_path": "/b.md"},
                ],
            )
            paths = _catalog_ids_for_predicates(db, {"bib_year": {"$eq": 2024}})
            assert paths == ["/a.md"]

    def test_year_gte(self) -> None:
        from nexus.search_engine import _catalog_ids_for_predicates
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": "1.1", "title": "a", "year": 2024, "file_path": "/a.md"},
                    {"tumbler": "1.2", "title": "b", "year": 2023, "file_path": "/b.md"},
                    {"tumbler": "1.3", "title": "c", "year": 2020, "file_path": "/c.md"},
                ],
            )
            paths = _catalog_ids_for_predicates(db, {"bib_year": {"$gte": 2023}})
            assert sorted(paths) == ["/a.md", "/b.md"]
