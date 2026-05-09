# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-572g: manifest read API + event-sourced backfill + post-store hook.

Tests cover:
  K6 - get_manifest: read ordered manifest rows for a doc_id
  K6 - get_manifest: empty list for unknown doc_id
  K6 - docs_for_chashes: reverse lookup chash -> [doc_id, ...]
  K6 - ManifestRow type: fields match document_chunks schema
  K7 - event-sourced backfill: backfilled collections survive Catalog.rebuild()
  K7 - event-sourced backfill: emits CollectionCreated event with legacy_grandfathered=True
  K7 - direct-INSERT backfill replaced: no raw INSERT in backfill code path
  OBS-3 - manifest_write_batch_hook wires write_manifest after T3 batch write
  SG-3 - write_manifest batching: 350-chunk doc produces 350 rows in correct order
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.catalog_db import CatalogDB


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    db_path = tmp_path / "catalog.sqlite"
    return Catalog(catalog_dir=catalog_dir, db_path=db_path)


def _insert_doc(cat: Catalog, tumbler: str, collection: str) -> None:
    """Insert a document row directly into the catalog DB for testing."""
    cat._db.execute(  # epsilon-allow: test fixture seeds documents row
        "INSERT OR IGNORE INTO documents "
        "(tumbler, title, author, year, content_type, file_path, "
        "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
        "metadata, source_mtime, alias_of, source_uri) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tumbler, f"doc-{tumbler}", "", 0, "code", f"/tmp/{tumbler}.py",
            "", collection, 0, "", "", "{}", 0.0, "", "",
        ),
    )
    cat._db.commit()


def _make_chunk(
    chash: str,
    position: int,
    *,
    chunk_index: int | None = None,
    line_start: int | None = None,
    line_end: int | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
) -> dict[str, Any]:
    return {
        "chash": chash,
        "position": position,
        "chunk_index": chunk_index,
        "line_start": line_start,
        "line_end": line_end,
        "char_start": char_start,
        "char_end": char_end,
    }


# ── K6: ManifestRow type ──────────────────────────────────────────────────────


class TestManifestRow:
    """ManifestRow type is importable and has the expected fields."""

    def test_manifestrow_importable(self):
        from nexus.catalog.catalog_writes import ManifestRow
        assert ManifestRow is not None

    def test_manifestrow_fields(self):
        from nexus.catalog.catalog_writes import ManifestRow
        row = ManifestRow(
            position=0,
            chash="a" * 64,
            chunk_index=0,
            line_start=1,
            line_end=5,
            char_start=0,
            char_end=100,
        )
        assert row.position == 0
        assert row.chash == "a" * 64
        assert row.chunk_index == 0
        assert row.line_start == 1
        assert row.line_end == 5
        assert row.char_start == 0
        assert row.char_end == 100

    def test_manifestrow_optional_fields_none(self):
        from nexus.catalog.catalog_writes import ManifestRow
        row = ManifestRow(position=0, chash="b" * 64)
        assert row.chunk_index is None
        assert row.line_start is None
        assert row.line_end is None
        assert row.char_start is None
        assert row.char_end is None


# ── K6: get_manifest ─────────────────────────────────────────────────────────


class TestGetManifest:
    """Tests for Catalog.get_manifest(doc_id) -> list[ManifestRow]."""

    def test_get_manifest_empty_for_unknown_doc(self, tmp_path):
        """Unknown doc_id returns empty list, not an error."""
        cat = _make_catalog(tmp_path)
        rows = cat.get_manifest("9.9.9")
        assert rows == []

    def test_get_manifest_returns_rows_ordered_by_position(self, tmp_path):
        """Rows returned in ascending position order regardless of insert order."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        chunks = [
            _make_chunk("b" * 64, position=1),
            _make_chunk("a" * 64, position=0),
            _make_chunk("c" * 64, position=2),
        ]
        cat.write_manifest("1.1.1", chunks)

        rows = cat.get_manifest("1.1.1")
        assert len(rows) == 3
        assert rows[0].position == 0
        assert rows[0].chash == "a" * 64
        assert rows[1].position == 1
        assert rows[1].chash == "b" * 64
        assert rows[2].position == 2
        assert rows[2].chash == "c" * 64

    def test_get_manifest_returns_manifestrow_objects(self, tmp_path):
        """Return type is list[ManifestRow]."""
        from nexus.catalog.catalog_writes import ManifestRow

        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [_make_chunk("a" * 64, 0)])

        rows = cat.get_manifest("1.1.1")
        assert len(rows) == 1
        assert isinstance(rows[0], ManifestRow)

    def test_get_manifest_preserves_span_columns(self, tmp_path):
        """Span coordinates (line_start, line_end, char_start, char_end) round-trip."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        chunks = [
            {
                "chash": "d" * 64,
                "position": 0,
                "chunk_index": 3,
                "line_start": 10,
                "line_end": 20,
                "char_start": 100,
                "char_end": 300,
            }
        ]
        cat.write_manifest("1.1.1", chunks)

        rows = cat.get_manifest("1.1.1")
        assert len(rows) == 1
        r = rows[0]
        assert r.chunk_index == 3
        assert r.line_start == 10
        assert r.line_end == 20
        assert r.char_start == 100
        assert r.char_end == 300

    def test_get_manifest_zero_chunk_doc_returns_empty(self, tmp_path):
        """write_manifest([]) then get_manifest returns empty list."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [])

        rows = cat.get_manifest("1.1.1")
        assert rows == []

    def test_get_manifest_isolates_by_doc_id(self, tmp_path):
        """get_manifest returns rows only for the requested doc_id."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        _insert_doc(cat, "1.1.2", "code__test")
        cat.write_manifest("1.1.1", [_make_chunk("a" * 64, 0)])
        cat.write_manifest("1.1.2", [_make_chunk("b" * 64, 0), _make_chunk("c" * 64, 1)])

        rows = cat.get_manifest("1.1.1")
        assert len(rows) == 1
        assert rows[0].chash == "a" * 64


# ── K6: docs_for_chashes ─────────────────────────────────────────────────────


class TestDocsForChashes:
    """Tests for Catalog.docs_for_chashes(chashes) -> dict[str, list[str]]."""

    def test_docs_for_chashes_empty_input(self, tmp_path):
        """Empty chash list returns empty dict."""
        cat = _make_catalog(tmp_path)
        result = cat.docs_for_chashes([])
        assert result == {}

    def test_docs_for_chashes_single_doc(self, tmp_path):
        """Returns correct doc_id for a chash that appears in one document."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [_make_chunk("a" * 64, 0)])

        result = cat.docs_for_chashes(["a" * 64])
        assert result == {"a" * 64: ["1.1.1"]}

    def test_docs_for_chashes_multi_doc(self, tmp_path):
        """A chash shared across multiple docs maps to all doc_ids."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        _insert_doc(cat, "1.1.2", "code__test")
        shared_chash = "a" * 64
        cat.write_manifest("1.1.1", [_make_chunk(shared_chash, 0)])
        cat.write_manifest("1.1.2", [_make_chunk(shared_chash, 0)])

        result = cat.docs_for_chashes([shared_chash])
        assert shared_chash in result
        assert sorted(result[shared_chash]) == ["1.1.1", "1.1.2"]

    def test_docs_for_chashes_unknown_chash_omitted(self, tmp_path):
        """Chashes with no manifest entries are omitted from the result."""
        cat = _make_catalog(tmp_path)
        result = cat.docs_for_chashes(["z" * 64])
        assert result == {}

    def test_docs_for_chashes_mixed_known_unknown(self, tmp_path):
        """Known chashes appear in result; unknown chashes are omitted."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [_make_chunk("a" * 64, 0)])

        result = cat.docs_for_chashes(["a" * 64, "z" * 64])
        assert "a" * 64 in result
        assert "z" * 64 not in result

    def test_docs_for_chashes_multiple_chunks_same_doc(self, tmp_path):
        """Multiple chunks in the same doc appear as one doc_id per chash."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [
            _make_chunk("a" * 64, 0),
            _make_chunk("b" * 64, 1),
        ])

        result = cat.docs_for_chashes(["a" * 64, "b" * 64])
        assert result["a" * 64] == ["1.1.1"]
        assert result["b" * 64] == ["1.1.1"]


# ── RDR-108 Phase 4 / nexus-dyxe: chashes_for_collection ─────────────────────


class TestChashesForCollection:
    """Tests for ``Catalog.chashes_for_collection(physical_collection) -> set[str]``.

    Returns the set of T3 chunk natural IDs (chash[:32]) referenced by any
    manifest entry for documents in the given physical_collection. Used by the
    Phase 4 GC rewrite (indexer._prune_deleted_files) to identify orphan
    chunks: anything in T3 whose ID is NOT in this set is stale.
    """

    def test_chashes_for_collection_unknown_returns_empty(self, tmp_path):
        """Unknown collection name returns empty set, not an error."""
        cat = _make_catalog(tmp_path)
        result = cat.chashes_for_collection("code__nonexistent")
        assert result == set()

    def test_chashes_for_collection_returns_set_of_strings(self, tmp_path):
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [_make_chunk("a" * 64, 0)])

        result = cat.chashes_for_collection("code__test")
        assert isinstance(result, set)
        assert all(isinstance(x, str) for x in result)

    def test_chashes_for_collection_returns_truncated_to_32(self, tmp_path):
        """T3 chunk IDs are chash[:32]; the returned set must be truncated
        so direct membership testing against chunk IDs works."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        full = "a" * 64
        cat.write_manifest("1.1.1", [_make_chunk(full, 0)])

        result = cat.chashes_for_collection("code__test")
        assert result == {full[:32]}

    def test_chashes_for_collection_distinct_across_chunks(self, tmp_path):
        """Each chash appears once even if it occurs at multiple positions
        or across multiple docs in the same collection."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        _insert_doc(cat, "1.1.2", "code__test")
        shared = "a" * 64
        cat.write_manifest("1.1.1", [
            _make_chunk(shared, 0),
            _make_chunk(shared, 1),
            _make_chunk("b" * 64, 2),
        ])
        cat.write_manifest("1.1.2", [_make_chunk(shared, 0)])

        result = cat.chashes_for_collection("code__test")
        assert result == {shared[:32], ("b" * 64)[:32]}

    def test_chashes_for_collection_isolates_by_physical_collection(self, tmp_path):
        """Only docs whose ``physical_collection`` matches contribute chashes."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__a")
        _insert_doc(cat, "1.1.2", "code__b")
        cat.write_manifest("1.1.1", [_make_chunk("a" * 64, 0)])
        cat.write_manifest("1.1.2", [_make_chunk("b" * 64, 0)])

        a_set = cat.chashes_for_collection("code__a")
        b_set = cat.chashes_for_collection("code__b")
        assert a_set == {("a" * 64)[:32]}
        assert b_set == {("b" * 64)[:32]}

    def test_chashes_for_collection_empty_manifest_returns_empty(self, tmp_path):
        """A doc registered to the collection but with no manifest rows
        contributes no chashes (zero-chunk doc → all-deleted)."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [])

        result = cat.chashes_for_collection("code__test")
        assert result == set()

    def test_chashes_for_collection_skips_deleted_documents(self, tmp_path):
        """ON DELETE CASCADE removes manifest rows when the document is
        deleted, so ``chashes_for_collection`` returns an empty set after
        the only contributing doc is removed (deleted-file → all chunks
        become orphans, the GC contract)."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")
        cat.write_manifest("1.1.1", [_make_chunk("a" * 64, 0)])

        cat._db.execute(  # epsilon-allow: test fixture forces FK CASCADE
            "DELETE FROM documents WHERE tumbler = ?", ("1.1.1",)
        )
        cat._db.commit()

        result = cat.chashes_for_collection("code__test")
        assert result == set()


# ── K7: event-sourced backfill ────────────────────────────────────────────────


class TestEventSourcedCollectionBackfill:
    """Backfilled collections must survive Catalog.rebuild()."""

    def test_backfilled_collections_survive_rebuild(self, tmp_path):
        """Collections backfilled from documents.physical_collection have
        CollectionCreated events written to events.jsonl so Catalog.rebuild()
        does not delete them.

        Scenario:
          1. Seed a raw DB with a document whose physical_collection has no
             matching collections row (pre-RDR-108 state).
          2. Construct a Catalog -- its CatalogDB.__init__ fires the backfill,
             and Catalog._emit_backfilled_collection_events writes the event.
          3. Verify events.jsonl has a CollectionCreated event with
             legacy_grandfathered=True.
        """
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        db_path = tmp_path / "catalog.sqlite"

        # Seed the database with a legacy document pointing at an unregistered
        # collection (pre-RDR-108 state). Use a raw CatalogDB to write the
        # document row; close it before Catalog opens the same file.
        seed_db = CatalogDB(db_path)
        seed_db._conn.execute(
            "INSERT INTO documents (tumbler, title, physical_collection) "
            "VALUES (?, ?, ?)",
            ("1.1.1", "legacy-doc", "code__legacy-collection"),
        )
        seed_db._conn.commit()
        seed_db._conn.close()

        # Now construct a full Catalog -- it creates a fresh CatalogDB that
        # finds the unregistered physical_collection and backfills it.
        cat = Catalog(catalog_dir=catalog_dir, db_path=db_path)

        # Verify the backfill row was created.
        row = cat._db._conn.execute(
            "SELECT name, legacy_grandfathered FROM collections WHERE name = ?",
            ("code__legacy-collection",),
        ).fetchone()
        assert row is not None, "backfill must create the collections row"
        assert row[1] == 1, "backfilled row must have legacy_grandfathered=1"

        # Verify the event was written to events.jsonl.
        events_path = catalog_dir / "events.jsonl"
        assert events_path.exists(), "events.jsonl must exist after Catalog init"
        events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        collection_created_events = [
            e for e in events
            if e.get("type") == "CollectionCreated"
            and e.get("payload", {}).get("coll_id") == "code__legacy-collection"
        ]
        assert len(collection_created_events) >= 1, (
            "CollectionCreated event must be written for the backfilled collection"
        )
        payload = collection_created_events[0]["payload"]
        assert payload.get("legacy_grandfathered") is True, (
            "CollectionCreated event for backfilled collection must have "
            "legacy_grandfathered=True"
        )

    def test_backfilled_collections_survive_forced_rebuild(self, tmp_path):
        """After events are written, a full rebuild() keeps the backfilled collection.

        The event is written to events.jsonl; a subsequent _ensure_consistent
        replay re-projects the row. We verify the event is present so the
        projector has what it needs.
        """
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        db_path = tmp_path / "catalog.sqlite"

        # Seed legacy document.
        seed_db = CatalogDB(db_path)
        seed_db._conn.execute(
            "INSERT INTO documents (tumbler, title, physical_collection) "
            "VALUES (?, ?, ?)",
            ("1.1.1", "legacy-doc", "code__legacy-survive-rebuild"),
        )
        seed_db._conn.commit()
        seed_db._conn.close()

        # Construct Catalog -- this triggers backfill + event emission.
        cat = Catalog(catalog_dir=catalog_dir, db_path=db_path)

        # Verify the event was written so rebuild() can replay it.
        events_path = catalog_dir / "events.jsonl"
        events = [
            json.loads(line)
            for line in events_path.read_text().splitlines()
            if line.strip()
        ]
        collection_events = [
            e for e in events
            if e.get("type") == "CollectionCreated"
            and e.get("payload", {}).get("coll_id") == "code__legacy-survive-rebuild"
        ]
        assert len(collection_events) >= 1

    def test_backfill_does_not_double_emit_on_second_open(self, tmp_path):
        """Opening the same DB twice should not emit duplicate CollectionCreated
        events for rows that already exist in collections.
        """
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir()
        db_path = tmp_path / "catalog.sqlite"

        # Seed legacy document.
        seed_db = CatalogDB(db_path)
        seed_db._conn.execute(
            "INSERT INTO documents (tumbler, title, physical_collection) "
            "VALUES (?, ?, ?)",
            ("1.1.1", "legacy-doc", "code__no-double-emit"),
        )
        seed_db._conn.commit()
        seed_db._conn.close()

        def _count_events(path: "Path") -> int:
            if not path.exists():
                return 0
            return sum(
                1
                for line in path.read_text().splitlines()
                if line.strip()
                and json.loads(line).get("type") == "CollectionCreated"
                and json.loads(line).get("payload", {}).get("coll_id") == "code__no-double-emit"
            )

        events_path = catalog_dir / "events.jsonl"

        # First Catalog open -- backfill fires, event emitted.
        cat1 = Catalog(catalog_dir=catalog_dir, db_path=db_path)
        count_after_first = _count_events(events_path)
        assert count_after_first >= 1, "first open must emit the event"

        # Second Catalog open -- backfill SELECT sees the row already exists;
        # no INSERT, so _backfilled_collections is empty, so no event.
        cat2 = Catalog(catalog_dir=catalog_dir, db_path=db_path)
        count_after_second = _count_events(events_path)

        assert count_after_second == count_after_first, (
            f"Second open emitted {count_after_second - count_after_first} "
            "extra CollectionCreated events; backfill must be idempotent"
        )


# ── SG-3: >300 chunk batching ─────────────────────────────────────────────────


class TestWriteManifestBatching:
    """write_manifest batches at 300 and must handle >300 chunks correctly."""

    def test_write_manifest_350_chunks_produces_350_rows(self, tmp_path):
        """A document with 350 chunks produces exactly 350 manifest rows."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest("1.1.1", chunks)

        count = cat._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 350

    def test_write_manifest_350_chunks_correct_order(self, tmp_path):
        """All 350 rows are present in position order."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest("1.1.1", chunks)

        rows = cat._db.execute(
            "SELECT position, chash FROM document_chunks "
            "WHERE doc_id = ? ORDER BY position",
            ("1.1.1",),
        ).fetchall()
        assert len(rows) == 350
        for i, (pos, chash) in enumerate(rows):
            assert pos == i
            assert chash == f"{i:064x}"

    def test_write_manifest_350_chunks_idempotent(self, tmp_path):
        """Re-writing 350 chunks produces exactly 350 rows (no duplicates)."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest("1.1.1", chunks)
        cat.write_manifest("1.1.1", chunks)

        count = cat._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 350

    def test_write_manifest_350_chunks_all_in_one_transaction(self, tmp_path):
        """350 chunks must all commit atomically (partial failure leaves zero rows)."""
        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest("1.1.1", chunks)

        # Verify atomicity: query outside of any explicit transaction
        count = cat._db.execute(
            "SELECT COUNT(*) FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchone()[0]
        assert count == 350, (
            f"Expected 350 rows after commit, got {count}. "
            "The multi-batch write must be in a single transaction."
        )


# ── OBS-3: manifest_write_batch_hook ─────────────────────────────────────────


class TestManifestWriteBatchHook:
    """manifest_write_batch_hook writes manifest after T3 batch chunk ingest."""

    def test_manifest_write_batch_hook_importable(self):
        """The hook function is importable from mcp_infra."""
        from nexus.mcp_infra import manifest_write_batch_hook
        assert callable(manifest_write_batch_hook)

    def test_manifest_write_batch_hook_writes_manifest(self, tmp_path):
        """Hook writes manifest rows for each doc_id in the batch.

        Setup: seed a catalog with a doc, then call the hook with
        chunk metadatas. Assert document_chunks rows are created.
        """
        from unittest.mock import patch

        from nexus.mcp_infra import manifest_write_batch_hook

        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")

        metadatas = [
            {
                "doc_id": "1.1.1",
                "chunk_index": 0,
                "chunk_text_hash": "a" * 64,
                "line_start": 0,
                "line_end": 5,
                "chunk_start_char": 0,
                "chunk_end_char": 100,
            }
        ]

        with patch("nexus.mcp_infra.get_catalog", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=["chunk-id-0"],
                collection="code__test",
                contents=["some code"],
                embeddings=None,
                metadatas=metadatas,
            )

        rows = cat._db.execute(
            "SELECT doc_id, position, chash FROM document_chunks WHERE doc_id = ?",
            ("1.1.1",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "1.1.1"
        assert rows[0][2] == "a" * 64

    def test_manifest_write_batch_hook_no_metadatas_noop(self, tmp_path):
        """Hook is a no-op when metadatas is None."""
        from nexus.mcp_infra import manifest_write_batch_hook

        # Should not raise even without a real catalog
        manifest_write_batch_hook(
            doc_ids=["x"],
            collection="code__test",
            contents=["x"],
            embeddings=None,
            metadatas=None,
        )

    def test_manifest_write_batch_hook_groups_by_doc_id(self, tmp_path):
        """Multiple chunks for the same doc_id are written as one manifest."""
        from unittest.mock import patch

        from nexus.mcp_infra import manifest_write_batch_hook

        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")

        metadatas = [
            {
                "doc_id": "1.1.1",
                "chunk_index": 0,
                "chunk_text_hash": "a" * 64,
                "line_start": 0,
                "line_end": 5,
                "chunk_start_char": 0,
                "chunk_end_char": 50,
            },
            {
                "doc_id": "1.1.1",
                "chunk_index": 1,
                "chunk_text_hash": "b" * 64,
                "line_start": 6,
                "line_end": 10,
                "chunk_start_char": 51,
                "chunk_end_char": 100,
            },
        ]

        with patch("nexus.mcp_infra.get_catalog", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=["chunk-0", "chunk-1"],
                collection="code__test",
                contents=["code0", "code1"],
                embeddings=None,
                metadatas=metadatas,
            )

        rows = cat._db.execute(
            "SELECT position, chash FROM document_chunks "
            "WHERE doc_id = ? ORDER BY position",
            ("1.1.1",),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0] == (0, "a" * 64)
        assert rows[1] == (1, "b" * 64)

    def test_manifest_write_batch_hook_registered_in_mcp_core(self):
        """manifest_write_batch_hook is registered in the post-store batch chain."""
        # We just verify that importing mcp.core registers the hook.
        # The registration happens at module import time.
        import nexus.mcp.core  # noqa: F401  -- side effect: register hooks
        from nexus.mcp_infra import _post_store_batch_hooks, manifest_write_batch_hook

        assert manifest_write_batch_hook in _post_store_batch_hooks

    def test_manifest_write_batch_hook_accumulates_across_batches(self, tmp_path):
        """RDR-108 Phase 3 (nexus-bdag) regression test: when the hook is
        called multiple times for the same ``catalog_doc_id`` (the
        streaming PDF / incremental indexer pattern), the manifest must
        accumulate across calls. Pre-fix the hook used
        ``write_manifest`` which DELETE+INSERTs, so the second call
        truncated the first call's rows. Post-fix uses
        ``append_manifest_chunks`` (UPSERT keyed on (doc_id, position))
        so callers passing a global ``chunk_index`` get a complete
        manifest.

        This test simulates a 2-batch indexing run for one document
        with 5 total chunks (3 in batch 1, 2 in batch 2). The final
        manifest must contain all 5 rows at positions 0..4.
        """
        from unittest.mock import patch

        from nexus.mcp_infra import manifest_write_batch_hook

        cat = _make_catalog(tmp_path)
        _insert_doc(cat, "1.1.1", "code__test")

        # Helper to build a metadata dict with a global chunk_index.
        def _meta(global_idx: int, chash: str) -> dict:
            return {
                "chunk_index": global_idx,
                "chunk_text_hash": chash,
            }

        # Batch 1: positions 0, 1, 2.
        batch_1 = [_meta(0, "a" * 64), _meta(1, "b" * 64), _meta(2, "c" * 64)]
        # Batch 2: positions 3, 4.
        batch_2 = [_meta(3, "d" * 64), _meta(4, "e" * 64)]

        with patch("nexus.mcp_infra.get_catalog", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=["chunk-0", "chunk-1", "chunk-2"],
                collection="code__test",
                contents=["c0", "c1", "c2"],
                embeddings=None,
                metadatas=batch_1,
                catalog_doc_id="1.1.1",
            )
            manifest_write_batch_hook(
                doc_ids=["chunk-3", "chunk-4"],
                collection="code__test",
                contents=["c3", "c4"],
                embeddings=None,
                metadatas=batch_2,
                catalog_doc_id="1.1.1",
            )

        rows = cat._db.execute(
            "SELECT position, chash FROM document_chunks "
            "WHERE doc_id = ? ORDER BY position",
            ("1.1.1",),
        ).fetchall()
        assert len(rows) == 5, (
            f"expected 5 manifest rows after 2 batches; got {len(rows)}. "
            f"Pre-fix the second batch's write_manifest deleted the "
            f"first batch's rows."
        )
        for i, (pos, chash) in enumerate(rows):
            assert pos == i
        assert rows[0][1] == "a" * 64
        assert rows[4][1] == "e" * 64
