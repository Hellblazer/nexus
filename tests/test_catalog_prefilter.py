# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for catalog-scoped pre-filtering (RDR-056 Phase 3, RDR-101 Phase 4).

RDR-101 Phase 4 (nexus-ufyl): the prefilter now emits a ``doc_id`` $in
where-clause keyed on ``json_extract(metadata, '$.doc_id')`` from the
catalog, replacing the legacy ``source_path``-keyed filter so that the
T3 prune of deprecated keys (nexus-o6aa.10.3) can drop ``source_path``
without breaking catalog prefiltering.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.search_engine import _prefilter_from_catalog


def _create_test_catalog_db(db_path: Path, entries: list[dict]) -> sqlite3.Connection:
    """Create a minimal catalog SQLite database with test documents.

    Each entry may carry a top-level ``doc_id`` field; it is stored inside
    the ``metadata`` JSON column under ``$.doc_id`` to match the production
    catalog schema (see ``Catalog.by_doc_id``). Pass an explicit ``metadata``
    dict to bypass auto-injection.
    """
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
        meta = dict(e.get("metadata", {}))
        if "doc_id" in e and "doc_id" not in meta:
            meta["doc_id"] = e["doc_id"]
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
                json.dumps(meta),
            ),
        )
    conn.commit()
    return conn


class _FakeCatalog:
    """Minimal catalog stub with a real SQLite database."""

    def __init__(self, db: sqlite3.Connection, total_docs: int = 100) -> None:
        self._db = db
        self._total_docs = total_docs

    def doc_count(self) -> int:
        return self._total_docs


class TestPrefilterFromCatalog:
    def test_high_selectivity_year_filter(self) -> None:
        """bib_year predicate with <5% match → returns doc_id $in filter."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": f"1.{i}", "title": f"doc{i}", "year": 2024,
                     "file_path": f"/docs/paper_{i}.md",
                     "doc_id": f"new-{i}"}
                    for i in range(3)
                ] + [
                    {"tumbler": f"2.{i}", "title": f"old{i}", "year": 2020,
                     "file_path": f"/docs/old_{i}.md",
                     "doc_id": f"old-{i}"}
                    for i in range(97)
                ],
            )
            cat = _FakeCatalog(db, total_docs=100)
            where = {"bib_year": {"$eq": 2024}}
            result = _prefilter_from_catalog(where, cat)
            assert result is not None
            assert "doc_id" in result
            assert "source_path" not in result
            assert sorted(result["doc_id"]["$in"]) == ["new-0", "new-1", "new-2"]

    def test_emits_doc_id_key_not_source_path(self) -> None:
        """RDR-101 Phase 4 sentinel: the prefilter where-clause keys on
        ``doc_id``, not ``source_path``. Pre-RDR-101 code would emit
        ``{"source_path": {"$in": [<file_paths>]}}``; the migration switches
        the key (and the lookup column) so that nx catalog prune-deprecated-keys
        can drop ``source_path`` from chunk metadata without breaking
        catalog prefiltering.
        """
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": "1.1", "title": "doc1", "year": 2024,
                     "file_path": "/docs/paper.md",
                     "doc_id": "01HXYZ-doc-1"},
                ],
            )
            cat = _FakeCatalog(db, total_docs=100)
            result = _prefilter_from_catalog({"bib_year": {"$eq": 2024}}, cat)
            assert result == {"doc_id": {"$in": ["01HXYZ-doc-1"]}}

    def test_skips_rows_without_doc_id_metadata(self) -> None:
        """Legacy rows lacking metadata.doc_id are silently skipped — the
        prefilter only narrows on rows that have a doc_id, falling back
        to standard search for the rest. If ALL rows lack a doc_id the
        prefilter returns None (nothing to narrow against).
        """
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                # All matching rows have NO doc_id in metadata.
                [
                    {"tumbler": f"1.{i}", "title": f"doc{i}", "year": 2024,
                     "file_path": f"/docs/paper_{i}.md",
                     "metadata": {}}
                    for i in range(3)
                ] + [
                    {"tumbler": f"2.{i}", "title": f"old{i}", "year": 2020,
                     "file_path": f"/docs/old_{i}.md",
                     "metadata": {}}
                    for i in range(97)
                ],
            )
            cat = _FakeCatalog(db, total_docs=100)
            result = _prefilter_from_catalog({"bib_year": {"$eq": 2024}}, cat)
            assert result is None

    def test_low_selectivity_skips_prefilter(self) -> None:
        """Predicate matching >5% of docs → returns None (use standard filter)."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": f"1.{i}", "title": f"doc{i}", "year": 2024,
                     "file_path": f"/docs/paper_{i}.md",
                     "doc_id": f"new-{i}"}
                    for i in range(50)
                ] + [
                    {"tumbler": f"2.{i}", "title": f"old{i}", "year": 2020,
                     "file_path": f"/docs/old_{i}.md",
                     "doc_id": f"old-{i}"}
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
                     "file_path": f"/docs/paper_{i}.md",
                     "doc_id": f"new-{i}"}
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
                     "file_path": "/a.md", "doc_id": "doc-a"},
                    {"tumbler": "1.2", "title": "new2", "year": 2023,
                     "file_path": "/b.md", "doc_id": "doc-b"},
                    {"tumbler": "1.3", "title": "old", "year": 2020,
                     "file_path": "/c.md", "doc_id": "doc-c"},
                ] + [
                    {"tumbler": f"2.{i}", "title": f"filler{i}", "year": 2010,
                     "file_path": f"/filler_{i}.md",
                     "doc_id": f"filler-{i}"}
                    for i in range(97)
                ],
            )
            cat = _FakeCatalog(db, total_docs=100)
            where = {"bib_year": {"$gte": 2023}}
            result = _prefilter_from_catalog(where, cat)
            assert result is not None
            assert "doc_id" in result
            assert sorted(result["doc_id"]["$in"]) == ["doc-a", "doc-b"]

    def test_unsupported_predicate_returns_none(self) -> None:
        """Predicate not mappable to catalog columns → returns None."""
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [{"tumbler": "1.1", "title": "t", "file_path": "/a.md",
                  "doc_id": "doc-a"}],
            )
            cat = _FakeCatalog(db, total_docs=100)
            where = {"custom_field": "value"}
            result = _prefilter_from_catalog(where, cat)
            assert result is None


class TestCatalogDocIdsForPredicates:
    def test_year_eq(self) -> None:
        from nexus.search_engine import _catalog_doc_ids_for_predicates
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": "1.1", "title": "a", "year": 2024,
                     "file_path": "/a.md", "doc_id": "doc-a"},
                    {"tumbler": "1.2", "title": "b", "year": 2020,
                     "file_path": "/b.md", "doc_id": "doc-b"},
                ],
            )
            doc_ids = _catalog_doc_ids_for_predicates(db, {"bib_year": {"$eq": 2024}})
            assert doc_ids == ["doc-a"]

    def test_year_gte(self) -> None:
        from nexus.search_engine import _catalog_doc_ids_for_predicates
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": "1.1", "title": "a", "year": 2024,
                     "file_path": "/a.md", "doc_id": "doc-a"},
                    {"tumbler": "1.2", "title": "b", "year": 2023,
                     "file_path": "/b.md", "doc_id": "doc-b"},
                    {"tumbler": "1.3", "title": "c", "year": 2020,
                     "file_path": "/c.md", "doc_id": "doc-c"},
                ],
            )
            doc_ids = _catalog_doc_ids_for_predicates(db, {"bib_year": {"$gte": 2023}})
            assert sorted(doc_ids) == ["doc-a", "doc-b"]

    def test_skips_rows_without_doc_id(self) -> None:
        """Rows whose metadata lacks $.doc_id are excluded (IS NOT NULL)."""
        from nexus.search_engine import _catalog_doc_ids_for_predicates
        with tempfile.TemporaryDirectory() as td:
            db = _create_test_catalog_db(
                Path(td) / "cat.db",
                [
                    {"tumbler": "1.1", "title": "a", "year": 2024,
                     "file_path": "/a.md", "doc_id": "doc-a"},
                    # No doc_id metadata — must be skipped.
                    {"tumbler": "1.2", "title": "b", "year": 2024,
                     "file_path": "/b.md", "metadata": {}},
                ],
            )
            doc_ids = _catalog_doc_ids_for_predicates(db, {"bib_year": {"$eq": 2024}})
            assert doc_ids == ["doc-a"]
