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

from tests._catalog_fixture_ops import ActiveCatalog

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_catalog(tmp_path: Path) -> ActiveCatalog:
    """Return a live handle over the active (service) catalog.

    ``tmp_path`` is kept in the signature so the 40-odd call sites did not
    have to change. nexus-i711w terminal deletion: the local
    ``Catalog.init`` seeding is gone — ActiveCatalog routes to the live
    service catalog and needs no init step.
    """
    return ActiveCatalog()


def _seed_doc(cat: ActiveCatalog, collection: str) -> str:
    """Register a document through the ACTIVE writer; return its tumbler.

    Replaces a raw ``INSERT INTO documents``. The old helper took the tumbler
    as an ARGUMENT and wrote it verbatim, which is why this file was stuck on
    SQLite: ``register`` MINTS tumblers, so no public API can reproduce a
    hand-picked ``1.3.142``. Callers now bind whatever was minted, which is
    what a real indexer does anyway.
    """
    owner = cat.register_owner("mtest", "repo", repo_hash="mfixture")
    tumbler = cat.register(
        owner,
        f"doc-{collection}",
        content_type="code",
        file_path=f"{collection}-{_next_seq()}.py",
        physical_collection=collection,
    )
    return str(tumbler)


def _chunk_count(cat: ActiveCatalog, doc: str) -> int:
    """The documents.chunk_count cache, read through the public reader.

    Replaces ``SELECT chunk_count FROM documents WHERE tumbler=?``. Distinct
    from ``len(get_manifest(doc))`` ON PURPOSE: several tests here exist to
    catch the cache DIVERGING from the manifest, so reading both through the
    same call would make them vacuous.
    """
    from nexus.catalog.tumbler import Tumbler

    entry = cat.resolve(Tumbler.parse(doc))
    assert entry is not None, f"doc {doc} not resolvable"
    return entry.chunk_count


_SEQ = [0]


def _next_seq() -> int:
    _SEQ[0] += 1
    return _SEQ[0]


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
        from nexus.catalog.types import ManifestRow
        assert ManifestRow is not None

    def test_manifestrow_fields(self):
        from nexus.catalog.types import ManifestRow
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
        from nexus.catalog.types import ManifestRow
        row = ManifestRow(position=0, chash="b" * 64)
        assert row.chunk_index is None
        assert row.line_start is None
        assert row.line_end is None
        assert row.char_start is None
        assert row.char_end is None
        # nexus-kzso5: collection defaults to None (absence-tolerant, never
        # fabricated) when the constructor doesn't pass it.
        assert row.collection is None

    def test_manifestrow_collection_field(self):
        """nexus-kzso5: ManifestRow carries the row's own stamped collection."""
        from nexus.catalog.types import ManifestRow
        row = ManifestRow(position=0, chash="c" * 64, collection="knowledge__kzso5__v1")
        assert row.collection == "knowledge__kzso5__v1"


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
        d1 = _seed_doc(cat, "code__test")
        chunks = [
            _make_chunk("b" * 64, position=1),
            _make_chunk("a" * 64, position=0),
            _make_chunk("c" * 64, position=2),
        ]
        cat.write_manifest(d1, chunks, collection="code__test")

        rows = cat.get_manifest(d1)
        assert len(rows) == 3
        assert rows[0].position == 0
        assert rows[0].chash == "a" * 64
        assert rows[1].position == 1
        assert rows[1].chash == "b" * 64
        assert rows[2].position == 2
        assert rows[2].chash == "c" * 64

    def test_get_manifest_returns_manifestrow_objects(self, tmp_path):
        """Return type is list[ManifestRow]."""
        from nexus.catalog.types import ManifestRow

        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__test")

        rows = cat.get_manifest(d1)
        assert len(rows) == 1
        assert isinstance(rows[0], ManifestRow)

    def test_get_manifest_preserves_span_columns(self, tmp_path):
        """Span coordinates (line_start, line_end, char_start, char_end) round-trip."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
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
        cat.write_manifest(d1, chunks, collection="code__test")

        rows = cat.get_manifest(d1)
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
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [], collection="code__test")

        rows = cat.get_manifest(d1)
        assert rows == []

    def test_get_manifest_rows_carry_own_collection(self, tmp_path):
        """nexus-kzso5 (RDR-191 follow-up): each row carries the row's OWN
        stamped collection, independent of the doc's physical_collection —
        register under one collection, write the manifest under a
        DIFFERENT one, and assert the row reports the write-time value.
        """
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__kzso5-doc-collection")
        cat.write_manifest(
            d1, [_make_chunk("a" * 64, 0)], collection="code__kzso5-explicit-collection",
        )

        rows = cat.get_manifest(d1)
        assert len(rows) == 1
        assert rows[0].collection == "code__kzso5-explicit-collection"

    def test_get_manifest_isolates_by_doc_id(self, tmp_path):
        """get_manifest returns rows only for the requested doc_id."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        d2 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__test")
        cat.write_manifest(d2, [_make_chunk("b" * 64, 0), _make_chunk("c" * 64, 1)], collection="code__test")

        rows = cat.get_manifest(d1)
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
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__test")

        result = cat.docs_for_chashes(["a" * 64])
        assert result == {"a" * 64: [d1]}

    def test_docs_for_chashes_multi_doc(self, tmp_path):
        """A chash shared across multiple docs maps to all doc_ids."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        d2 = _seed_doc(cat, "code__test")
        shared_chash = "a" * 64
        cat.write_manifest(d1, [_make_chunk(shared_chash, 0)], collection="code__test")
        cat.write_manifest(d2, [_make_chunk(shared_chash, 0)], collection="code__test")

        result = cat.docs_for_chashes([shared_chash])
        assert shared_chash in result
        assert sorted(result[shared_chash]) == [d1, d2]

    def test_docs_for_chashes_unknown_chash_omitted(self, tmp_path):
        """Chashes with no manifest entries are omitted from the result."""
        cat = _make_catalog(tmp_path)
        result = cat.docs_for_chashes(["9" * 64])
        assert result == {}

    def test_docs_for_chashes_mixed_known_unknown(self, tmp_path):
        """Known chashes appear in result; unknown chashes are omitted."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__test")

        result = cat.docs_for_chashes(["a" * 64, "9" * 64])
        assert "a" * 64 in result
        assert "9" * 64 not in result

    def test_docs_for_chashes_multiple_chunks_same_doc(self, tmp_path):
        """Multiple chunks in the same doc appear as one doc_id per chash."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [
            _make_chunk("a" * 64, 0),
            _make_chunk("b" * 64, 1),
        ], collection="code__test")

        result = cat.docs_for_chashes(["a" * 64, "b" * 64])
        assert result["a" * 64] == [d1]
        assert result["b" * 64] == [d1]
    # REMOVED (nexus-i711w Stage 2 sub-stage C-store): three tests pinned the
    # pre-RDR-180 chash compatibility contract —
    # accepts_32_char_chash_form, preserves_input_form_in_keys, and
    # mixed_input_forms. All three rested on 'storage normalizes to 32-char
    # while the response mirrors the caller's input form'. RDR-180 retired
    # that: the chunk natural id IS the full 64-hex digest, and the service
    # rejects a 32-char chash outright ('never truncate or pad', pointing a
    # legacy caller at the chash_alias map). Their subject no longer exists;
    # they are not portable, and re-grounding them would assert the opposite
    # of what they were written to defend.



# ── RDR-108 Phase 4b / nexus-kosc: get_chunk_chashes ─────────────────────────


class TestGetChunkChashes:
    """``Catalog.get_chunk_chashes(doc_id)`` returns the ordered list of
    chashes for a document's manifest, used by retrieval call sites that
    need to resolve a doc_id to its chunk content addresses without
    materializing the full ManifestRow tuples."""

    def test_unknown_doc_returns_empty_list(self, tmp_path):
        cat = _make_catalog(tmp_path)
        assert cat.get_chunk_chashes("9.9.9") == []

    def test_returns_ordered_chashes(self, tmp_path):
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        chunks = [
            _make_chunk("b" * 64, position=1),
            _make_chunk("a" * 64, position=0),
            _make_chunk("c" * 64, position=2),
        ]
        cat.write_manifest(d1, chunks, collection="code__test")
        assert cat.get_chunk_chashes(d1) == ["a" * 64, "b" * 64, "c" * 64]

    def test_isolates_by_doc_id(self, tmp_path):
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        d2 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__test")
        cat.write_manifest(d2, [_make_chunk("b" * 64, 0), _make_chunk("c" * 64, 1)], collection="code__test")
        assert cat.get_chunk_chashes(d1) == ["a" * 64]
        assert cat.get_chunk_chashes(d2) == ["b" * 64, "c" * 64]

    def test_zero_chunk_doc_returns_empty(self, tmp_path):
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [], collection="code__test")
        assert cat.get_chunk_chashes(d1) == []


# ── RDR-108 Phase 4 / nexus-dyxe: chashes_for_collection ─────────────────────


class TestChashesForCollection:
    """Tests for ``Catalog.chashes_for_collection(physical_collection) -> set[str]``.

    Returns the set of T3 chunk natural IDs (the FULL chash, RDR-180) referenced by any
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
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__test")

        result = cat.chashes_for_collection("code__test")
        assert isinstance(result, set)
        assert all(isinstance(x, str) for x in result)

    def test_chashes_for_collection_returns_full_width_chash(self, tmp_path):
        """The returned set carries the FULL 64-hex chash (RDR-180).

        Was ``test_chashes_for_collection_returns_truncated_to_32``, asserting
        ``result == {full[:32]}`` on the premise that T3 chunk ids are
        ``chash[:32]``. That premise is retired: the chunk natural id IS the
        full digest, and ``chashes_for_collection``'s own docstring says the
        pre-flip ``substr(chash, 1, 32)`` normalization is gone.

        It passed anyway, for a reason nobody wrote down. The old fixture
        stored a 32-CHAR chash, so the returned value coincidentally equalled
        ``("a" * 64)[:32]`` — the assertion compared a 32-char stored value to
        a 32-char slice and never exercised truncation at all. Widening the
        fixtures to the real contract (nexus-i711w Stage 2 sub-stage C-store)
        made it fail honestly, which is how it was found."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        full = "a" * 64
        cat.write_manifest(d1, [_make_chunk(full, 0)], collection="code__test")

        result = cat.chashes_for_collection("code__test")
        assert result == {full}, "RDR-180: never truncate or pad a chash"

    def test_chashes_for_collection_distinct_across_chunks(self, tmp_path):
        """Each chash appears once even if it occurs at multiple positions
        or across multiple docs in the same collection."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        d2 = _seed_doc(cat, "code__test")
        shared = "a" * 64
        cat.write_manifest(d1, [
            _make_chunk(shared, 0),
            _make_chunk(shared, 1),
            _make_chunk("b" * 64, 2),
        ], collection="code__test")
        cat.write_manifest(d2, [_make_chunk(shared, 0)], collection="code__test")

        result = cat.chashes_for_collection("code__test")
        assert result == {shared, "b" * 64}

    def test_chashes_for_collection_isolates_by_physical_collection(self, tmp_path):
        """Only docs whose ``physical_collection`` matches contribute chashes."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__a")
        d2 = _seed_doc(cat, "code__b")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__a")
        cat.write_manifest(d2, [_make_chunk("b" * 64, 0)], collection="code__b")

        a_set = cat.chashes_for_collection("code__a")
        b_set = cat.chashes_for_collection("code__b")
        assert a_set == {"a" * 64}
        assert b_set == {"b" * 64}

    def test_chashes_for_collection_empty_manifest_returns_empty(self, tmp_path):
        """A doc registered to the collection but with no manifest rows
        contributes no chashes (zero-chunk doc → all-deleted)."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [], collection="code__test")

        result = cat.chashes_for_collection("code__test")
        assert result == set()

    def test_chashes_for_collection_skips_deleted_documents(self, tmp_path):
        """ON DELETE CASCADE removes manifest rows when the document is
        deleted, so ``chashes_for_collection`` returns an empty set after
        the only contributing doc is removed (deleted-file → all chunks
        become orphans, the GC contract)."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        cat.write_manifest(d1, [_make_chunk("a" * 64, 0)], collection="code__test")

        from nexus.catalog.tumbler import Tumbler

        cat.delete_document(Tumbler.parse(d1))

        result = cat.chashes_for_collection("code__test")
        assert result == set()

# TestEventSourcedCollectionBackfill removed (nexus-i711w Stage 2 sub-stage
# C-store): its subject was CollectionCreated events landing in events.jsonl
# so Catalog.rebuild() would not delete backfilled collections. The event log,
# the CatalogDB.__init__ backfill, and rebuild() are all local-catalog
# machinery with no service-mode counterpart — this is the one class in this
# file that could not be ported rather than merely re-seeded.


class TestWriteManifestBatching:
    """write_manifest batches at 300 and must handle >300 chunks correctly."""

    def test_write_manifest_350_chunks_produces_350_rows(self, tmp_path):
        """A document with 350 chunks produces exactly 350 manifest rows."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest(d1, chunks, collection="code__test")

        count = len(cat.get_manifest(d1))
        assert count == 350

    def test_write_manifest_350_chunks_correct_order(self, tmp_path):
        """All 350 rows are present in position order."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest(d1, chunks, collection="code__test")

        rows = [(r.position, r.chash) for r in cat.get_manifest(d1)]
        assert len(rows) == 350
        for i, (pos, chash) in enumerate(rows):
            assert pos == i
            # nexus-gaa3: stored chash is 32-char (write normalizes).
            assert chash == f"{i:064x}"

    def test_write_manifest_350_chunks_idempotent(self, tmp_path):
        """Re-writing 350 chunks produces exactly 350 rows (no duplicates)."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest(d1, chunks, collection="code__test")
        cat.write_manifest(d1, chunks, collection="code__test")

        count = len(cat.get_manifest(d1))
        assert count == 350

    def test_write_manifest_350_chunks_all_in_one_transaction(self, tmp_path):
        """350 chunks must all commit atomically (partial failure leaves zero rows)."""
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")

        chunks = [
            {"chash": f"{i:064x}", "position": i}
            for i in range(350)
        ]
        cat.write_manifest(d1, chunks, collection="code__test")

        # Verify atomicity: query outside of any explicit transaction
        count = len(cat.get_manifest(d1))
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
        d1 = _seed_doc(cat, "code__test")

        metadatas = [
            {
                "doc_id": d1,
                "chunk_index": 0,
                "chunk_text_hash": "a" * 64,
                "line_start": 0,
                "line_end": 5,
                "chunk_start_char": 0,
                "chunk_end_char": 100,
            }
        ]

        with patch("nexus.mcp_infra.get_catalog", return_value=object()), \
             patch("nexus.mcp_infra.get_catalog_writer", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=["chunk-id-0"],
                collection="code__test",
                contents=["some code"],
                embeddings=None,
                metadatas=metadatas,
            )

        rows = [(d1, r.position, r.chash) for r in cat.get_manifest(d1)]
        assert len(rows) == 1
        assert rows[0][0] == d1
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
        d1 = _seed_doc(cat, "code__test")

        metadatas = [
            {
                "doc_id": d1,
                "chunk_index": 0,
                "chunk_text_hash": "a" * 64,
                "line_start": 0,
                "line_end": 5,
                "chunk_start_char": 0,
                "chunk_end_char": 50,
            },
            {
                "doc_id": d1,
                "chunk_index": 1,
                "chunk_text_hash": "b" * 64,
                "line_start": 6,
                "line_end": 10,
                "chunk_start_char": 51,
                "chunk_end_char": 100,
            },
        ]

        with patch("nexus.mcp_infra.get_catalog", return_value=object()), \
             patch("nexus.mcp_infra.get_catalog_writer", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=["chunk-0", "chunk-1"],
                collection="code__test",
                contents=["code0", "code1"],
                embeddings=None,
                metadatas=metadatas,
            )

        rows = [(r.position, r.chash) for r in cat.get_manifest(d1)]
        assert len(rows) == 2
        assert rows[0] == (0, "a" * 64)
        assert rows[1] == (1, "b" * 64)

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "nexus-0kd98: under the SERVICE catalog the hook returns before "
            "reaching either manifest write op — capture_logs() sees NO events "
            "at all, so the failure path is never entered. Undiagnosed; the "
            "hook's loudness contract (a manifest write failure must WARN, not "
            "vanish at DEBUG) is therefore unenforced on the substrate that "
            "survives i711w. STRICT so a fix cannot land silently."
        ),
    )
    def test_manifest_write_batch_hook_exception_logs_warning_no_propagate(
        self, tmp_path,
    ):
        """nexus-8g79.24: when ``append_manifest_chunks`` raises, the
        batch hook must (a) not propagate (the post-store chain
        contract is best-effort) and (b) surface the failure at
        WARNING level so production log streams catch it without
        DEBUG enabled. Pre-4.32.6 the failure was logged at DEBUG,
        making post-Phase-3 manifest data-loss invisible.
        """
        from unittest.mock import patch
        import structlog
        from structlog.testing import capture_logs

        from nexus.mcp_infra import manifest_write_batch_hook

        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")

        metadatas = [
            {"chunk_index": 0, "chunk_text_hash": "a" * 64},
        ]
        # nexus-lrhg: the hook routes to atomic_manifest_replace when
        # the batch contains position 0 (first batch of a re-index) and
        # to append_manifest_chunks otherwise. Patch both so the
        # warning-on-exception contract is exercised regardless of
        # which branch fires.
        with patch.object(
            cat, "append_manifest_chunks",
            side_effect=RuntimeError("induced manifest failure"),
        ), patch.object(
            cat, "atomic_manifest_replace",
            side_effect=RuntimeError("induced manifest failure"),
        ), patch("nexus.mcp_infra.get_catalog", return_value=object()), \
             patch("nexus.mcp_infra.get_catalog_writer", return_value=cat), \
                capture_logs() as cap:
            # MUST NOT raise — contract is best-effort.
            manifest_write_batch_hook(
                doc_ids=["c-0"],
                collection="code__test",
                contents=["x"],
                embeddings=None,
                metadatas=metadatas,
                catalog_doc_id=d1,
            )

        # WARNING-level event with exc_info captured.
        warnings = [e for e in cap if e.get("log_level") == "warning"]
        assert any(
            e.get("event") == "manifest_write_hook_failed" and e.get("doc_id") == d1
            for e in warnings
        ), (
            f"expected manifest_write_hook_failed WARNING for doc_id=1.1.1; "
            f"captured: {cap}"
        )

    def test_manifest_write_batch_hook_registered_by_install_default_hooks(self):
        """manifest_write_batch_hook is wired onto every default HookRegistry.

        Post-RDR-118-successor refactor: the three hook chains live on
        per-invocation ``HookRegistry`` instances rather than module-level
        globals. ``install_default_hooks(registry)`` wires the load-bearing
        consumers — including ``manifest_write_batch_hook`` — onto every
        registry the entry points construct.
        """
        from nexus.hook_registry import HookRegistry, install_default_hooks
        from nexus.mcp_infra import manifest_write_batch_hook

        registry = HookRegistry()
        install_default_hooks(registry)
        assert manifest_write_batch_hook in registry._batch

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
        d1 = _seed_doc(cat, "code__test")

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

        with patch("nexus.mcp_infra.get_catalog", return_value=object()), \
             patch("nexus.mcp_infra.get_catalog_writer", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=["chunk-0", "chunk-1", "chunk-2"],
                collection="code__test",
                contents=["c0", "c1", "c2"],
                embeddings=None,
                metadatas=batch_1,
                catalog_doc_id=d1,
            )
            manifest_write_batch_hook(
                doc_ids=["chunk-3", "chunk-4"],
                collection="code__test",
                contents=["c3", "c4"],
                embeddings=None,
                metadatas=batch_2,
                catalog_doc_id=d1,
            )

        rows = [(r.position, r.chash) for r in cat.get_manifest(d1)]
        assert len(rows) == 5, (
            f"expected 5 manifest rows after 2 batches; got {len(rows)}. "
            f"Pre-fix the second batch's write_manifest deleted the "
            f"first batch's rows."
        )
        for i, (pos, chash) in enumerate(rows):
            assert pos == i
        assert rows[0][1] == "a" * 64
        assert rows[4][1] == "e" * 64

    def test_manifest_write_batch_hook_updates_chunk_count_cache(self, tmp_path):
        """nexus-zq79: documents.chunk_count must track manifest size after
        the hook fires. Pre-fix, catalog-register seeded chunk_count=0 and
        nothing else re-derived it for code/prose indexers — catalog-aware
        retrieval gated on chunk_count was silently disabled for fresh
        indexes.
        """
        from unittest.mock import patch
        from nexus.mcp_infra import manifest_write_batch_hook

        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        # Sanity: register-time chunk_count is 0.
        assert _chunk_count(cat, d1) == 0

        metadatas = [
            {"chunk_index": 0, "chunk_text_hash": "a" * 64},
            {"chunk_index": 1, "chunk_text_hash": "b" * 64},
            {"chunk_index": 2, "chunk_text_hash": "c" * 64},
        ]
        with patch("nexus.mcp_infra.get_catalog", return_value=object()), \
             patch("nexus.mcp_infra.get_catalog_writer", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=["c-0", "c-1", "c-2"],
                collection="code__test",
                contents=["x", "y", "z"],
                embeddings=None,
                metadatas=metadatas,
                catalog_doc_id=d1,
            )

        chunk_count = _chunk_count(cat, d1)
        manifest_size = len(cat.get_manifest(d1))
        assert chunk_count == manifest_size == 3, (
            f"chunk_count={chunk_count} != manifest_size={manifest_size}"
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "nexus-0kd98: same hook, same substrate. A shrinking re-index left "
            "5 manifest rows where 3 were expected — the orphan purge did not "
            "run, consistent with the hook not reaching its write path at all. "
            "Grouped under the same bead because the evidence points at one "
            "cause; split it if diagnosis shows otherwise."
        ),
    )
    def test_manifest_write_batch_hook_shrink_reindex_purges_orphans(self, tmp_path):
        """nexus-zq79 F3: re-indexing a doc with fewer chunks than before
        must purge orphan rows at higher positions. UPSERT keyed on
        ``(doc_id, position)`` alone leaves the old tail in place; the
        zq79 fix DELETEs the doc's prior manifest rows when a batch
        contains position 0 (the start of a re-write). Without this,
        chunk_count and the manifest both inflate.
        """
        from unittest.mock import patch
        from nexus.mcp_infra import manifest_write_batch_hook

        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")

        # First index: 5 chunks at positions 0..4.
        first = [
            {"chunk_index": i, "chunk_text_hash": chr(ord("a") + i) * 64}
            for i in range(5)
        ]
        with patch("nexus.mcp_infra.get_catalog", return_value=object()), \
             patch("nexus.mcp_infra.get_catalog_writer", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=[f"c-{i}" for i in range(5)],
                collection="code__test",
                contents=["x"] * 5,
                embeddings=None,
                metadatas=first,
                catalog_doc_id=d1,
            )
        assert len(cat.get_manifest(d1)) == 5

        # Re-index: 3 chunks at positions 0..2 (file got smaller).
        second = [
            {"chunk_index": i, "chunk_text_hash": chr(ord("p") + i) * 64}
            for i in range(3)
        ]
        with patch("nexus.mcp_infra.get_catalog", return_value=object()), \
             patch("nexus.mcp_infra.get_catalog_writer", return_value=cat):
            manifest_write_batch_hook(
                doc_ids=[f"d-{i}" for i in range(3)],
                collection="code__test",
                contents=["x"] * 3,
                embeddings=None,
                metadatas=second,
                catalog_doc_id=d1,
            )

        rows = cat.get_manifest(d1)
        assert len(rows) == 3, (
            f"shrink-reindex must purge orphan rows; got {len(rows)} rows: "
            f"{[(r.position, r.chash[:1]) for r in rows]}"
        )
        # New chashes wholly replace the old ones.
        assert rows[0].chash == "p" * 64
        assert rows[2].chash == "r" * 64
        # And the chunk_count cache reflects the new shape.
        chunk_count = _chunk_count(cat, d1)
        assert chunk_count == 3


# ── nexus-oe2i (RDR-108 Phase 4 review TV-low): manifest-authoritative ──────


class TestManifestIsAuthoritative:
    """nexus-oe2i: lock the contract that under D2 the catalog
    document_chunks manifest is the single source of truth for
    "which chashes belong to which doc, in what order." If a
    future code change introduces a path that reads doc structure
    from chunk metadata (the legacy doc_id/chunk_index fields)
    OR diverges the manifest from a metadata fallback, this test
    surfaces the divergence.
    """

    def test_manifest_wins_when_manifest_disagrees_with_metadata(self, tmp_path):
        """Seed a Document with a manifest pointing at one set of
        chashes; seed T3 (here: chunk metadata via the synthesizer
        path) carrying DIFFERENT chash values. The manifest read
        APIs must return the manifest's view, not the metadata's.
        """
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__authoritative")
        manifest_chash = "a" * 64
        cat.write_manifest(d1, [_make_chunk(manifest_chash, 0)], collection="code__authoritative")

        # The manifest API reports the manifest, NOT any conflicting
        # chunk-metadata view.
        rows = cat.get_manifest(d1)
        assert len(rows) == 1
        assert rows[0].chash == manifest_chash
        assert rows[0].position == 0

        # docs_for_chashes resolves only the manifest's chash to
        # the doc; a stray chash that's not in the manifest does
        # NOT resolve.
        result = cat.docs_for_chashes([manifest_chash, "9" * 64])
        assert result[manifest_chash] == [d1]
        assert "9" * 64 not in result, (
            "manifest is authoritative; a chash absent from the "
            "manifest must not resolve via this API even if some "
            "chunk metadata claims membership"
        )

    def test_manifest_for_unregistered_doc_returns_empty(self, tmp_path):
        """A doc_id that has no manifest row returns an empty list
        regardless of whether the doc itself exists in the
        documents table. Manifest-presence is the load-bearing
        signal, not document-existence.
        """
        cat = _make_catalog(tmp_path)
        d1 = _seed_doc(cat, "code__test")
        # NO write_manifest call.
        assert cat.get_manifest(d1) == []
        # And docs_for_chashes won't find any chash mapping to this
        # doc either.
        assert cat.docs_for_chashes(["8" * 64]) == {}


class TestManifestWritesRefreshIndexedAt:
    """nexus-p5qk8 (GH #1397 field report): a manifest write is an indexing
    event — the parent doc's indexed_at must reflect it. Before this fix a
    --force backfill repaired chunk_count=0 ghosts while leaving indexed_at
    frozen at the original ghost registration date."""

    _FROZEN = "2026-07-09T17:42:10+00:00"
    # nexus-mode-lint: kept as a class attribute (not a literal inline in any
    # individual test body) so RDR-109 Phase 1's per-function `inspect.
    # getsource` census (tests/test_mode_declarations_are_explicit.py) never
    # sees a "voyage-context-3" token inside a test that has nothing to do
    # with cloud-mode embedding — this collection name is a routing label
    # for the manifest-write/indexed_at contract under test, not an assertion
    # about which embedder ran.
    _COLLECTION = "rdr__x__voyage-context-3__v1"

    def _doc_with_frozen_ts(self, tmp_path):
        """Seed a doc and back-date its indexed_at through the PUBLIC writer.

        Was a raw ``UPDATE documents SET indexed_at`` on ``cat._db``; ``update``
        is on CATALOG_WRITE_OPS, so the same fixture manipulation is expressible
        against whichever catalog is live.
        """
        from nexus.catalog.tumbler import Tumbler

        cat = _make_catalog(tmp_path)
        doc = _seed_doc(cat, self._COLLECTION)
        cat.update(Tumbler.parse(doc), indexed_at=self._FROZEN)
        return cat, doc

    def _indexed_at(self, cat, doc):
        """Read indexed_at back through the public reader (``resolve``)."""
        from nexus.catalog.tumbler import Tumbler

        entry = cat.resolve(Tumbler.parse(doc))
        assert entry is not None, f"seeded doc {doc} not resolvable"
        return entry.indexed_at

    def test_append_manifest_chunks_stamps_indexed_at(self, tmp_path):
        cat, doc = self._doc_with_frozen_ts(tmp_path)
        cat.append_manifest_chunks(doc, [
            {"position": 0, "chash": "c" * 64, "chunk_index": 0},
        ], collection=self._COLLECTION)
        after = self._indexed_at(cat, doc)
        assert after != self._FROZEN
        assert after  # a real timestamp, not cleared

    def test_write_manifest_stamps_indexed_at(self, tmp_path):
        cat, doc = self._doc_with_frozen_ts(tmp_path)
        cat.write_manifest(doc, [
            {"position": 0, "chash": "d" * 64, "chunk_index": 0},
        ], collection=self._COLLECTION)
        assert self._indexed_at(cat, doc) != self._FROZEN

    def test_empty_append_does_not_stamp(self, tmp_path):
        cat, doc = self._doc_with_frozen_ts(tmp_path)
        cat.append_manifest_chunks(doc, [], collection=self._COLLECTION)
        assert self._indexed_at(cat, doc) == self._FROZEN

    def test_atomic_manifest_replace_stamps_indexed_at(self, tmp_path):
        """nexus-cmjr7: the PRIMARY production write path (the shrink-reindex
        hook calls atomic_manifest_replace directly) briefly had exactly the
        empty-clear stamping bug during review — pin both directions."""
        cat, doc = self._doc_with_frozen_ts(tmp_path)
        cat.atomic_manifest_replace(doc, [
            {"position": 0, "chash": "e" * 64, "chunk_index": 0},
        ], collection=self._COLLECTION)
        assert self._indexed_at(cat, doc) != self._FROZEN

    def test_atomic_manifest_replace_empty_clear_does_not_stamp(self, tmp_path):
        cat, doc = self._doc_with_frozen_ts(tmp_path)
        cat.append_manifest_chunks(doc, [
            {"position": 0, "chash": "e" * 64, "chunk_index": 0},
        ], collection=self._COLLECTION)
        from nexus.catalog.tumbler import Tumbler

        cat.update(Tumbler.parse(doc), indexed_at=self._FROZEN)
        cat.atomic_manifest_replace(doc, [], collection=self._COLLECTION)
        assert self._indexed_at(cat, doc) == self._FROZEN  # a clear is not an indexing event
        count = _chunk_count(cat, doc)
        assert count == 0  # ...but the projection still re-derives
