# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 1.5a: backfill collections.owner_id (nexus-tts0d.1).

The collections projection table has an ``owner_id`` column (RDR-103)
that is empty on every legacy/grandfathered row. Any catalog-backed
reader (Phase 2) doing ``collections JOIN owners`` returns nothing
until owner_id is populated.

Two backfill paths:

- **Conformant-name** (auto, idempotent, safe): parse RDR-103
  four-segment names (``<content_type>__<owner_id>__<model>__v<n>``)
  and extract owner_id from the 2nd segment.
- **Documents-table fallback** (opt-in, CLI-only): for collections that
  have no conformant shape but DO have documents registered against
  them, derive owner_id from the documents' tumblers using
  ``_owner_prefix_of``. Skipped on ambiguity (multiple distinct owners).

Both paths run inside ``backfill_owner_id``; the auto migration in
``CatalogStore.__init__`` invokes it with ``include_documents_fallback=False``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from nexus.catalog.collections_owner_backfill import (
    BackfillResult,
    backfill_owner_id,
)
from nexus.db.t2.catalog import CatalogStore


def _open_raw_with_schema(
    db_path: Path,
    *,
    collections: list[tuple[str, str]],  # (name, owner_id)
    documents: list[tuple[str, str]] = [],  # (tumbler, physical_collection)
) -> sqlite3.Connection:
    """Open a raw sqlite3 connection with the minimal schema seeded.

    This bypasses ``CatalogStore.__init__`` so unit tests of the
    backfill function exercise it on the *pre-migration* state without
    the auto-migration firing first. The caller owns the returned
    connection and is responsible for ``close``.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE collections (
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
        CREATE TABLE documents (
            tumbler TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content_type TEXT,
            file_path TEXT,
            physical_collection TEXT,
            chunk_count INTEGER,
            indexed_at TEXT,
            metadata JSON
        );
        """
    )
    for name, owner_id in collections:
        conn.execute(
            "INSERT INTO collections (name, owner_id, legacy_grandfathered) "
            "VALUES (?, ?, 1)",
            (name, owner_id),
        )
    for tumbler, coll in documents:
        conn.execute(
            "INSERT INTO documents "
            "(tumbler, title, content_type, file_path, physical_collection, "
            " chunk_count, indexed_at, metadata) VALUES "
            "(?, ?, 'paper', '', ?, 0, '', '{}')",
            (tumbler, f"doc-{tumbler}", coll),
        )
    conn.commit()
    return conn


def _row_owner_id(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute(
        "SELECT owner_id FROM collections WHERE name = ?", (name,)
    ).fetchone()
    return None if row is None else row[0]


class TestConformantNameBackfill:
    def test_extracts_owner_id_from_rdr103_name(self, tmp_path: Path) -> None:
        conn = _open_raw_with_schema(
            tmp_path / "cat.db",
            collections=[("code__nexus-1-1__voyage-code-3__v1", "")],
        )
        try:
            result = backfill_owner_id(conn)
            conn.commit()
            assert isinstance(result, BackfillResult)
            assert result.updated_from_name == 1
            assert result.updated_from_documents == 0
            assert result.skipped_ambiguous == 0
            assert result.skipped_unresolvable == 0
            assert (
                _row_owner_id(conn, "code__nexus-1-1__voyage-code-3__v1")
                == "nexus-1-1"
            )
        finally:
            conn.close()

    def test_skips_already_populated_rows(self, tmp_path: Path) -> None:
        """Idempotency contract: rows with non-empty owner_id are untouched."""
        conn = _open_raw_with_schema(
            tmp_path / "cat.db",
            collections=[("code__nexus-1-1__voyage-code-3__v1", "already-set")],
        )
        try:
            result = backfill_owner_id(conn)
            conn.commit()
            assert result.updated_from_name == 0
            assert (
                _row_owner_id(conn, "code__nexus-1-1__voyage-code-3__v1")
                == "already-set"
            )
        finally:
            conn.close()

    def test_skips_legacy_two_segment_name_without_documents_fallback(
        self, tmp_path: Path,
    ) -> None:
        """Legacy 2-segment names cannot be parsed; without the documents
        fallback (auto-migration mode) they stay empty."""
        conn = _open_raw_with_schema(
            tmp_path / "cat.db",
            collections=[("knowledge__delos", "")],
        )
        try:
            result = backfill_owner_id(conn)
            assert result.updated_from_name == 0
            assert result.skipped_unresolvable == 1
            assert _row_owner_id(conn, "knowledge__delos") == ""
        finally:
            conn.close()

    def test_double_run_is_noop(self, tmp_path: Path) -> None:
        conn = _open_raw_with_schema(
            tmp_path / "cat.db",
            collections=[("docs__art-1-2__voyage-context-3__v1", "")],
        )
        try:
            backfill_owner_id(conn)
            conn.commit()
            second = backfill_owner_id(conn)
            conn.commit()
            assert second.updated_from_name == 0
            assert second.updated_from_documents == 0
        finally:
            conn.close()


class TestDocumentsFallback:
    def test_recovers_owner_id_from_unique_document(self, tmp_path: Path) -> None:
        """Legacy collection name (cannot parse) with one document
        belonging to a known owner → owner_id derived from tumbler."""
        conn = _open_raw_with_schema(
            tmp_path / "cat.db",
            collections=[("knowledge__delos", "")],
            documents=[("3.4.1", "knowledge__delos")],
        )
        try:
            result = backfill_owner_id(conn, include_documents_fallback=True)
            conn.commit()
            assert result.updated_from_documents == 1
            assert _row_owner_id(conn, "knowledge__delos") == "3-4"
        finally:
            conn.close()

    def test_skips_ambiguous_multi_owner_collection(self, tmp_path: Path) -> None:
        """Documents from two distinct owners → ambiguous, skipped with
        the row left empty for operator review."""
        conn = _open_raw_with_schema(
            tmp_path / "cat.db",
            collections=[("knowledge__shared", "")],
            documents=[
                ("3.4.1", "knowledge__shared"),
                ("4.5.1", "knowledge__shared"),
            ],
        )
        try:
            result = backfill_owner_id(conn, include_documents_fallback=True)
            conn.commit()
            assert result.skipped_ambiguous == 1
            assert result.updated_from_documents == 0
            assert _row_owner_id(conn, "knowledge__shared") == ""
        finally:
            conn.close()

    def test_dry_run_reports_but_does_not_write(self, tmp_path: Path) -> None:
        conn = _open_raw_with_schema(
            tmp_path / "cat.db",
            collections=[("code__nexus-1-1__voyage-code-3__v1", "")],
        )
        try:
            result = backfill_owner_id(conn, dry_run=True)
            conn.commit()
            assert result.updated_from_name == 1
            assert (
                _row_owner_id(conn, "code__nexus-1-1__voyage-code-3__v1") == ""
            )
        finally:
            conn.close()


class TestAutoMigrationInStoreInit:
    def test_construction_backfills_conformant_collections(
        self, tmp_path: Path,
    ) -> None:
        """A fresh CatalogStore opens, applies the auto-migration, and
        the conformant-named row gets its owner_id populated without an
        explicit call.

        This wires the migration end-to-end: the collections row exists
        from the RDR-108 D2 auto-backfill (line 642 in catalog.py) with
        owner_id='', and the RDR-137 Phase 1.5a follow-on pass populates
        it.
        """
        db_path = tmp_path / "cat.db"
        store = CatalogStore(db_path)
        # Seed a document that triggers the existing RDR-108 auto-backfill
        # of a collections row (with owner_id=''). The follow-on backfill
        # should then populate it from the conformant name.
        store._conn.execute(
            "INSERT INTO documents "
            "(tumbler, title, content_type, file_path, physical_collection, "
            " chunk_count, indexed_at, metadata) VALUES "
            "('5.7.3', 'doc', 'paper', '', "
            "'code__example-5-7__voyage-code-3__v1', 0, '', '{}')"
        )
        store._conn.commit()
        store._conn.close()

        # Reopen — auto-backfill of collections row fires first, then
        # the owner_id backfill follow-on populates it.
        store = CatalogStore(db_path)
        owner = _row_owner_id(
            store._conn, "code__example-5-7__voyage-code-3__v1",
        )
        store._conn.close()
        assert owner == "example-5-7"
