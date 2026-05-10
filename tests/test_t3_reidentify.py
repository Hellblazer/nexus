# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jc63: T3 chunk re-identification (RDR-108 Phase 2).

Re-upserts every T3 chunk under a content-derived natural ID
(``chunk_text_hash[:32]``), reusing the chunk's existing embedding
so the migration is free of Voyage calls. After re-upsert, the
old chunk IDs are batch-deleted.

Tests cover:
  - basic happy path: chunks migrate from old IDs to ``chunk_text_hash[:32]``
  - idempotent: re-running on a fully-migrated collection performs zero writes
  - resumable: a partial run can be re-invoked safely (un-deleted old IDs swept)
  - embedding reuse: byte-identical embeddings before and after migration
  - metadata strip: doc_id, chunk_index, chunk_count removed at re-upsert
  - taxonomy carve-out: ``taxonomy__*`` collections skipped by default
  - missing chunk_text_hash raises a structured error (not KeyError)
  - cross-collection chash dedup: same chunk text in two collections stays
    independent (chromadb natural IDs are per-collection)
  - within-collection identical chunks collapse to one T3 record
  - dry-run: no writes, no deletes
  - quota compliance: page size <= 300, batch deletes <= 300
  - CLI: --collection, --dry-run, --no-dry-run, --all-collections flags
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t3 import T3Database


# ── Helpers ──────────────────────────────────────────────────────────────────


def _unique_coll(prefix: str = "code") -> str:
    """Return a unique collection name per call.

    chromadb.EphemeralClient instances share an in-memory backend singleton;
    data seeded in one test is visible to subsequent tests unless collection
    names are isolated per call.
    """
    return f"{prefix}__{uuid.uuid4().hex[:12]}"


def _seed_chunk(
    t3_db: T3Database,
    *,
    collection: str,
    chunk_id: str,
    content: str,
    chunk_text_hash: str,
    doc_id: str | None = None,
    chunk_index: int | None = None,
    chunk_count: int | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> None:
    """Insert one chunk with the metadata that re-identify reads."""
    col = t3_db._client.get_or_create_collection(collection)
    meta: dict[str, Any] = {"chunk_text_hash": chunk_text_hash}
    if doc_id is not None:
        meta["doc_id"] = doc_id
    if chunk_index is not None:
        meta["chunk_index"] = chunk_index
    if chunk_count is not None:
        meta["chunk_count"] = chunk_count
    if extra_meta:
        meta.update(extra_meta)
    col.add(ids=[chunk_id], documents=[content], metadatas=[meta])


def _ids_in(t3_db: T3Database, collection: str) -> set[str]:
    """Return the full set of chunk IDs currently in a collection."""
    col = t3_db._client.get_or_create_collection(collection)
    return set(col.get()["ids"])


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def t3_db():
    """Real T3Database backed by an ephemeral local Chroma."""
    return T3Database(
        _client=chromadb.EphemeralClient(),
        _ef_override=DefaultEmbeddingFunction(),
    )


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── Unit tests: reidentify_collection ────────────────────────────────────────


class TestReidentifyCollection:
    """Tests for the core re-identification function."""

    def test_migrates_to_chunk_text_hash_prefix(self, t3_db):
        """Chunks are re-upserted under chunk_text_hash[:32] natural IDs."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-id-1",
            content="hello world", chunk_text_hash="a" * 64,
            doc_id="1.1.1", chunk_index=0, chunk_count=1,
        )

        result = reidentify_collection(t3_db, coll, dry_run=False)

        assert result.chunks_migrated == 1
        assert result.chunks_deleted == 1
        assert "a" * 32 in _ids_in(t3_db, coll)
        assert "legacy-id-1" not in _ids_in(t3_db, coll)

    def test_normalizes_metadata_to_canonical_schema(self, t3_db):
        """RDR-108 nexus-6l9p: reidentify routes metadata through the
        canonical schema funnel (``_normalize_for_write``), dropping
        all cargo not in ``ALLOWED_TOP_LEVEL`` rather than just the
        3-field RDR-108 Phase 3 set. This sidesteps the per-row
        ``NumMetadataKeys`` quota error that fires when legacy chunks
        carry 32+ keys."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-id-1",
            content="hello", chunk_text_hash="b" * 64,
            doc_id="1.1.1", chunk_index=2, chunk_count=5,
            extra_meta={
                # Pre-RDR-102 D2: source_path was canonical, now cargo.
                "source_path": "/tmp/foo.py",
                # Canonical fields that survive normalize.
                "line_start": 0,
                "title": "foo.py:0-5",
                "content_type": "code",
            },
        )

        reidentify_collection(t3_db, coll, dry_run=False)

        col = t3_db._client.get_or_create_collection(coll)
        result = col.get(ids=["b" * 32], include=["metadatas"])
        meta = result["metadatas"][0]
        # RDR-108 Phase 3 fields dropped (the original strip target).
        assert "doc_id" not in meta
        assert "chunk_index" not in meta
        assert "chunk_count" not in meta
        # RDR-102 D2 cargo dropped by canonical normalize.
        assert "source_path" not in meta
        # Canonical fields preserved.
        assert meta.get("line_start") == 0
        assert meta.get("title") == "foo.py:0-5"
        assert meta.get("chunk_text_hash") == "b" * 64

    def test_normalize_drops_legacy_cargo_for_over_quota_chunks(self, t3_db):
        """RDR-108 nexus-6l9p regression: a chunk seeded with 33 metadata
        keys (legacy cargo + pre-RDR-108 fields) must reidentify cleanly,
        producing a normalized payload under the per-row quota.

        Pre-fix path used a 3-field strip; the resulting upsert merged
        with the chunk's existing cargo and tripped Cloud's
        ``NumMetadataKeys`` quota at 33 > 32. The canonical funnel drops
        cargo so the upsert lands under quota."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll(prefix="docs")
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-cargo-1",
            content="paper text body", chunk_text_hash="c" * 64,
            doc_id="1.1.1", chunk_index=0, chunk_count=50,
            extra_meta={
                "title": "old-paper",
                "content_type": "pdf",
                "content_hash": "abc",
                "indexed_at": "2024-01-01T00:00:00",
                "source_path": "/papers/old.pdf",
                # Cargo that the canonical schema drops:
                "corpus": "knowledge",
                "store_type": "knowledge",
                "expires_at": "",
                "extraction_method": "docling",
                "format": "markdown",
                "is_image_pdf": False,
                "has_formulas": False,
                "page_count": 12,
                "chunk_type": "text",
            },
        )

        reidentify_collection(t3_db, coll, dry_run=False)

        col = t3_db._client.get_or_create_collection(coll)
        result = col.get(ids=["c" * 32], include=["metadatas"])
        meta = result["metadatas"][0]
        # The hash is preserved (the chunk migrated).
        assert meta["chunk_text_hash"] == "c" * 64
        # Cargo dropped by canonical normalize.
        for cargo_key in (
            "corpus", "store_type", "expires_at", "extraction_method",
            "format", "is_image_pdf", "has_formulas", "page_count",
            "chunk_type", "source_path",
            "doc_id", "chunk_index", "chunk_count",
        ):
            assert cargo_key not in meta, (
                f"{cargo_key!r} should be stripped by canonical "
                f"normalize, got meta={sorted(meta)}"
            )
        # Canonical fields preserved.
        assert meta.get("title") == "old-paper"
        assert meta.get("content_type") == "pdf"
        # nexus-2exh review caveat #3: lock the per-row metadata
        # quota bound. ChromaDB Cloud rejects records whose metadata
        # has more than 32 keys (NumMetadataKeys quota); the canonical
        # normalize must produce a payload that respects it. The
        # MAX_SAFE_TOP_LEVEL_KEYS constant in metadata_schema is the
        # canonical bound (32).
        from nexus.metadata_schema import MAX_SAFE_TOP_LEVEL_KEYS
        assert len(meta) <= MAX_SAFE_TOP_LEVEL_KEYS, (
            f"normalized metadata must fit under the per-row key "
            f"quota ({MAX_SAFE_TOP_LEVEL_KEYS}); got {len(meta)} "
            f"keys: {sorted(meta)}"
        )

    def test_idempotent_on_fully_migrated_collection(self, t3_db):
        """Re-running on a fully-migrated collection performs zero writes."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-id-1",
            content="hello", chunk_text_hash="c" * 64,
            doc_id="1.1.1", chunk_index=0,
        )

        # First run migrates.
        reidentify_collection(t3_db, coll, dry_run=False)
        ids_after_first = _ids_in(t3_db, coll)
        assert ids_after_first == {"c" * 32}

        # Second run is a no-op.
        result2 = reidentify_collection(t3_db, coll, dry_run=False)
        assert result2.chunks_migrated == 0
        assert result2.chunks_deleted == 0
        assert result2.chunks_already_migrated == 1
        assert _ids_in(t3_db, coll) == ids_after_first

    def test_resume_after_partial_migration(self, t3_db):
        """A crashed mid-collection run can be re-invoked safely.

        Simulates a partial migration by manually upserting two chunks
        under their new IDs without deleting their old IDs. Re-running
        the migration must sweep the un-deleted old IDs without
        damaging the already-migrated new IDs.
        """
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-1",
            content="hello", chunk_text_hash="d" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-2",
            content="world", chunk_text_hash="e" * 64,
        )
        # Simulate the partial state: new IDs already added, old IDs
        # still present (a crash before Phase 2b delete fires).
        _seed_chunk(
            t3_db, collection=coll, chunk_id="d" * 32,
            content="hello", chunk_text_hash="d" * 64,
        )

        result = reidentify_collection(t3_db, coll, dry_run=False)

        # legacy-1 collected as old (its new_id "ddd...32" already exists,
        # idempotent overwrite). legacy-2 fully migrated.
        assert "d" * 32 in _ids_in(t3_db, coll)
        assert "e" * 32 in _ids_in(t3_db, coll)
        assert "legacy-1" not in _ids_in(t3_db, coll)
        assert "legacy-2" not in _ids_in(t3_db, coll)
        # Both old IDs collected and deleted.
        assert result.chunks_deleted == 2

    def test_embedding_round_trip_byte_identical(self, t3_db):
        """Byte-identical embeddings before and after migration (no Voyage call)."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-1",
            content="rendezvous embedding", chunk_text_hash="f" * 64,
        )

        col = t3_db._client.get_or_create_collection(coll)
        before = col.get(
            ids=["legacy-1"], include=["embeddings"]
        )["embeddings"][0]

        reidentify_collection(t3_db, coll, dry_run=False)

        after = col.get(
            ids=["f" * 32], include=["embeddings"]
        )["embeddings"][0]

        # numpy-array equality at the byte level. Voyage was never called;
        # the original embedding was reused on re-upsert.
        assert list(before) == list(after)

    def test_taxonomy_collection_skipped(self, t3_db):
        """taxonomy__* collections are skipped by default."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll(prefix="taxonomy")
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=["centroid-1"],
            documents=["centroid content"],
            metadatas=[{"centroid_hash": "abc123"}],
        )

        result = reidentify_collection(t3_db, coll, dry_run=False)

        assert result.skipped_taxonomy is True
        assert result.chunks_migrated == 0
        assert "centroid-1" in _ids_in(t3_db, coll)

    def test_missing_chunk_text_hash_raises_structured_error(self, t3_db):
        """Chunks without chunk_text_hash raise MissingChunkHashError, not KeyError."""
        from nexus.db.t3_reidentify import (
            MissingChunkHashError,
            reidentify_collection,
        )

        coll = _unique_coll()
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=["legacy-noh"],
            documents=["pre-RDR-053 chunk"],
            metadatas=[{"doc_id": "1.1.1", "chunk_index": 0}],  # no chunk_text_hash
        )

        with pytest.raises(MissingChunkHashError) as exc_info:
            reidentify_collection(t3_db, coll, dry_run=False)
        assert "legacy-noh" in str(exc_info.value)
        assert coll in str(exc_info.value)

    def test_within_collection_identical_chunks_collapse(self, t3_db):
        """Identical chunk text in the same collection collapses to one T3 record.

        Both old cids must still be deleted: the second duplicate is dropped
        from the upsert batch (chromadb rejects duplicate ids in one call)
        but its cid is added to seen_old_ids and Phase 2b deletes it.
        """
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-1",
            content="identical text", chunk_text_hash="1" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-2",
            content="identical text", chunk_text_hash="1" * 64,
        )

        result = reidentify_collection(t3_db, coll, dry_run=False)

        ids = _ids_in(t3_db, coll)
        assert ids == {"1" * 32}
        # Both old cids deleted (collapse-path doesn't skip cleanup).
        assert result.chunks_deleted == 2

    def test_cross_collection_chash_dedup_independent(self, t3_db):
        """Same chunk text in two collections produces independent records.

        ChromaDB natural IDs are per-collection, so the two collections
        each end up with one chunk under the same string ID, but
        they're separate records.
        """
        from nexus.db.t3_reidentify import reidentify_collection

        coll_a = _unique_coll()
        coll_b = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll_a, chunk_id="legacy-A",
            content="shared text", chunk_text_hash="2" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll_b, chunk_id="legacy-B",
            content="shared text", chunk_text_hash="2" * 64,
        )

        reidentify_collection(t3_db, coll_a, dry_run=False)
        reidentify_collection(t3_db, coll_b, dry_run=False)

        assert "2" * 32 in _ids_in(t3_db, coll_a)
        assert "2" * 32 in _ids_in(t3_db, coll_b)

        # Documents stay independent.
        col_a = t3_db._client.get_or_create_collection(coll_a)
        col_b = t3_db._client.get_or_create_collection(coll_b)
        assert col_a.get(ids=["2" * 32])["documents"] == ["shared text"]
        assert col_b.get(ids=["2" * 32])["documents"] == ["shared text"]

    def test_dry_run_does_not_write_or_delete(self, t3_db):
        """--dry-run does not write new IDs and does not delete old IDs."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-1",
            content="hello", chunk_text_hash="3" * 64,
        )

        result = reidentify_collection(t3_db, coll, dry_run=True)

        # Counts what WOULD migrate
        assert result.chunks_migrated == 1
        # But actually nothing was written or deleted
        ids = _ids_in(t3_db, coll)
        assert "legacy-1" in ids
        assert "3" * 32 not in ids

    def test_absent_collection_returns_empty_result(self, t3_db):
        """An unknown T3 collection returns an empty result, not an error."""
        from nexus.db.t3_reidentify import reidentify_collection

        result = reidentify_collection(t3_db, "code__no_such_coll_xyz", dry_run=False)
        assert result.chunks_migrated == 0
        assert result.chunks_deleted == 0

    def test_cross_batch_collapse_dedupes_correctly(self, t3_db):
        """Identical chunk_text_hash across pass-2 batches still collapses.

        The seen_new_ids set is initialized outside the batch loop so the
        collapse path works across batch boundaries. With page_size=2 and
        three duplicates, the first batch upserts the new id; the second
        and third batches see new_id in seen_new_ids and skip the upsert
        but still add their cids to seen_old_ids for deletion.
        """
        from nexus.db import t3_reidentify
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        for i in range(3):
            _seed_chunk(
                t3_db, collection=coll, chunk_id=f"legacy-{i}",
                content="duplicate", chunk_text_hash="d" * 64,
            )

        with patch.object(t3_reidentify, "_PAGE_SIZE", 2):
            result = reidentify_collection(t3_db, coll, dry_run=False)

        ids = _ids_in(t3_db, coll)
        assert ids == {"d" * 32}
        # All three old cids deleted, even though only one upsert fired.
        assert result.chunks_deleted == 3
        assert result.chunks_migrated == 1

    def test_missing_hash_in_non_first_batch_propagates(self, t3_db):
        """MissingChunkHashError raises from any batch, not only batch 1.

        Patches _PAGE_SIZE=1 and seeds two chunks with the bad chunk
        second so the error fires from pass-2 iteration after a
        successful first batch.
        """
        from nexus.db import t3_reidentify
        from nexus.db.t3_reidentify import (
            MissingChunkHashError,
            reidentify_collection,
        )

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-good",
            content="ok", chunk_text_hash="a" * 64,
        )
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=["legacy-bad"],
            documents=["pre-RDR-053"],
            metadatas=[{"doc_id": "1.1.1"}],  # no chunk_text_hash
        )

        with patch.object(t3_reidentify, "_PAGE_SIZE", 1):
            with pytest.raises(MissingChunkHashError) as exc_info:
                reidentify_collection(t3_db, coll, dry_run=False)
        assert "legacy-bad" in str(exc_info.value)

    def test_missing_hash_raises_in_dry_run(self, t3_db):
        """Dry-run does not suppress the missing-hash error.

        The fail-loud contract (re-gate S3) must hold even when no
        writes are scheduled. The error fires before the dry-run guard.
        """
        from nexus.db.t3_reidentify import (
            MissingChunkHashError,
            reidentify_collection,
        )

        coll = _unique_coll()
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=["legacy-noh"],
            documents=["pre-RDR-053"],
            metadatas=[{"doc_id": "1.1.1"}],
        )

        with pytest.raises(MissingChunkHashError):
            reidentify_collection(t3_db, coll, dry_run=True)


class TestReidentifyPagination:
    """Verify quota compliance: page size <= 300, batch deletes <= 300."""

    def test_multi_page_iteration_migrates_all_chunks(self, t3_db):
        """Multi-page pagination correctly walks past page 1.

        Patches _PAGE_SIZE to 3 so seven seeded chunks span three pages
        (3 + 3 + 1). The chunk_text_hash values are constructed so each
        chunk's first 32 chars are unique (variation in the high nibbles,
        not the low ones).
        """
        from nexus.db import t3_reidentify
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        # Hashes vary in the FIRST 32 chars so chunk_text_hash[:32] is
        # distinct per chunk. Pattern: digit i repeated 32 times, then
        # zeros to pad to 64 chars.
        new_ids_expected = []
        for i in range(7):
            chash = (str(i) * 32) + ("0" * 32)
            new_ids_expected.append(chash[:32])
            _seed_chunk(
                t3_db, collection=coll, chunk_id=f"legacy-{i}",
                content=f"content-{i}", chunk_text_hash=chash,
            )

        with patch.object(t3_reidentify, "_PAGE_SIZE", 3):
            result = reidentify_collection(t3_db, coll, dry_run=False)

        assert result.chunks_examined == 7
        assert result.chunks_migrated == 7
        assert result.chunks_deleted == 7
        ids = _ids_in(t3_db, coll)
        for i, new_id in enumerate(new_ids_expected):
            assert f"legacy-{i}" not in ids
            assert new_id in ids

    def test_get_page_size_never_exceeds_300(self, t3_db):
        """col.get is called with limit <= 300."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-1",
            content="x", chunk_text_hash="4" * 64,
        )

        col = t3_db._client.get_or_create_collection(coll)
        original_get = col.get
        get_calls: list[dict] = []

        def _tracking_get(**kwargs):
            get_calls.append(kwargs)
            return original_get(**kwargs)

        with (
            patch.object(col, "get", side_effect=_tracking_get),
            patch.object(t3_db._client, "get_collection", return_value=col),
        ):
            reidentify_collection(t3_db, coll, dry_run=False)

        assert get_calls
        for call in get_calls:
            limit = call.get("limit")
            if limit is not None:
                assert limit <= 300, (
                    f"col.get called with limit={limit} > 300 (quota violation)"
                )

    def test_delete_batch_size_never_exceeds_300(self, t3_db):
        """col.delete is called with batches of <= 300 IDs."""
        from nexus.db.t3_reidentify import reidentify_collection

        coll = _unique_coll()
        # Seed 5 chunks (well under 300) and assert delete batches are bounded.
        for i in range(5):
            _seed_chunk(
                t3_db, collection=coll, chunk_id=f"legacy-{i}",
                content=f"content-{i}",
                chunk_text_hash=f"{i:064x}",
            )

        col = t3_db._client.get_or_create_collection(coll)
        original_delete = col.delete
        delete_calls: list[dict] = []

        def _tracking_delete(**kwargs):
            delete_calls.append(kwargs)
            return original_delete(**kwargs)

        with (
            patch.object(col, "delete", side_effect=_tracking_delete),
            patch.object(t3_db._client, "get_collection", return_value=col),
        ):
            reidentify_collection(t3_db, coll, dry_run=False)

        for call in delete_calls:
            ids = call.get("ids", [])
            assert len(ids) <= 300, (
                f"col.delete called with {len(ids)} ids > 300 (quota violation)"
            )


# ── nexus-zpnq: pass-2 fetch / upsert pipelining ─────────────────────────────


class TestReidentifyPipelining:
    """nexus-zpnq: pass-2 must fetch batch N+1 in parallel with the
    processing+upsert of batch N. Reverting the pipelining (back to
    sequential ``col.get -> col.upsert -> col.get -> col.upsert``)
    fails the timing-overlap test below.
    """

    def test_pass2_fetch_overlaps_with_upsert(self, t3_db, monkeypatch):
        """The look-ahead fetch (executor thread) must run
        concurrently with the prior batch's upsert (main thread).
        Test asserts that a fetch is in flight at the same wall-
        clock moment as an upsert; reverting to a fully-sequential
        ``fetch -> upsert -> fetch -> upsert`` loop fails this.
        """
        import threading
        import time

        coll_name = _unique_coll()
        col = t3_db._client.get_or_create_collection(coll_name)

        # Seed 3 pages worth of chunks. _PAGE_SIZE is monkeypatched
        # down so the test runs fast (production _PAGE_SIZE=300).
        from nexus.db import t3_reidentify as rmod
        monkeypatch.setattr(rmod, "_PAGE_SIZE", 3)

        ids = [f"old-{i}" for i in range(7)]  # 3 pages of 3 + 1
        col.add(
            ids=ids,
            documents=[f"text-{i}" for i in range(7)],
            metadatas=[
                {"chunk_text_hash": f"h{i:062d}aa"}
                for i in range(7)
            ],
            embeddings=[[0.1] * 3 for _ in range(7)],
        )

        # Track whether a get and an upsert are EVER concurrently
        # in flight (the pipelining contract).
        get_in_flight = False
        upsert_in_flight = False
        observed_overlap = False
        lock = threading.Lock()

        target_col = t3_db._client.get_collection(coll_name)
        real_get = target_col.get
        real_upsert = target_col.upsert

        def _slow_get(*args, **kwargs):
            nonlocal get_in_flight, observed_overlap
            with lock:
                get_in_flight = True
                if upsert_in_flight:
                    observed_overlap = True
            try:
                # ids= calls are pass-2 batches; offset= calls are
                # pass-1 id discovery (no upsert in flight then,
                # so they don't trigger the overlap signal anyway).
                time.sleep(0.05)
                return real_get(*args, **kwargs)
            finally:
                with lock:
                    get_in_flight = False

        def _slow_upsert(*args, **kwargs):
            nonlocal upsert_in_flight, observed_overlap
            with lock:
                upsert_in_flight = True
                if get_in_flight:
                    observed_overlap = True
            try:
                time.sleep(0.05)
                return real_upsert(*args, **kwargs)
            finally:
                with lock:
                    upsert_in_flight = False

        monkeypatch.setattr(target_col, "get", _slow_get)
        monkeypatch.setattr(target_col, "upsert", _slow_upsert)
        monkeypatch.setattr(
            t3_db, "_client_for",
            lambda _name: type(
                "X", (), {"get_collection": lambda self, n: target_col},
            )(),
        )

        rmod.reidentify_collection(t3_db, coll_name, dry_run=False)

        assert observed_overlap, (
            "pass-2 must overlap a look-ahead fetch with the prior "
            "batch's upsert (zpnq pipelining contract). Reverting to "
            "sequential fetch->upsert keeps the two strictly "
            "alternating and observed_overlap stays False."
        )

    def test_pipelining_preserves_dedupe_semantics(self, t3_db, monkeypatch):
        """The seen_new_ids dedupe set is touched only from the main
        thread; pipelining the fetch must not introduce concurrent
        writes to it. Two batches whose chunks all share the same
        chash must collapse to a single upsert in total (one new id,
        rest skipped as identical-text-collapse).
        """
        from nexus.db import t3_reidentify as rmod
        monkeypatch.setattr(rmod, "_PAGE_SIZE", 2)

        coll_name = _unique_coll()
        col = t3_db._client.get_or_create_collection(coll_name)

        # 4 chunks, 2 unique chashes -> 2 batches of 2; under
        # pipelining, batch 2 is being fetched while batch 1 upsert
        # is processing. The dedupe set must still correctly skip
        # cross-batch duplicates.
        chash_a = "a" * 64
        chash_b = "b" * 64
        col.add(
            ids=["c1", "c2", "c3", "c4"],
            documents=["a-text", "a-dup", "b-text", "b-dup"],
            metadatas=[
                {"chunk_text_hash": chash_a},
                {"chunk_text_hash": chash_a},
                {"chunk_text_hash": chash_b},
                {"chunk_text_hash": chash_b},
            ],
            embeddings=[[0.1] * 3 for _ in range(4)],
        )

        result = rmod.reidentify_collection(
            t3_db, coll_name, dry_run=False,
        )
        assert result.chunks_examined == 4
        # Two unique chashes -> two upserts under the new ids.
        assert result.chunks_migrated == 2, (
            f"identical-text collapse broken under pipelining: "
            f"expected 2 unique migrations, got {result.chunks_migrated}"
        )
        # All 4 old ids deleted (the 2 collapsed dupes still get the
        # old-cid delete; only the upsert is skipped).
        assert result.chunks_deleted == 4


# ── CLI tests: nx t3 reidentify ──────────────────────────────────────────────


class TestReidentifyCLI:
    """Tests for ``nx t3 reidentify`` CLI command."""

    def test_cli_dry_run_default_writes_nothing(self, t3_db, runner):
        """--dry-run is the default (defensive); nothing written or deleted."""
        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-1",
            content="hello", chunk_text_hash="5" * 64,
        )

        with patch(
            "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
        ):
            result = runner.invoke(
                main,
                ["t3", "reidentify", "--collection", coll],
            )
        assert result.exit_code == 0, result.output
        # default is dry-run — old ID still present, new ID absent
        ids = _ids_in(t3_db, coll)
        assert "legacy-1" in ids
        assert "5" * 32 not in ids

    def test_cli_no_dry_run_migrates(self, t3_db, runner):
        """--no-dry-run actually migrates chunks."""
        coll = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll, chunk_id="legacy-1",
            content="hello", chunk_text_hash="6" * 64,
        )

        with patch(
            "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
        ):
            result = runner.invoke(
                main,
                ["t3", "reidentify", "--collection", coll, "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output
        ids = _ids_in(t3_db, coll)
        assert "legacy-1" not in ids
        assert "6" * 32 in ids

    def test_cli_all_collections_iterates(self, t3_db, runner):
        """--all-collections iterates every T3 collection.

        We narrow the CLI's view via a mocked list_collections() so the
        test isn't polluted by ephemeral-client state shared with sibling
        tests (project memory: chromadb.EphemeralClient instances share
        an in-memory backend singleton).
        """
        coll_a = _unique_coll()
        coll_b = _unique_coll()
        _seed_chunk(
            t3_db, collection=coll_a, chunk_id="legacy-A",
            content="a", chunk_text_hash="7" * 64,
        )
        _seed_chunk(
            t3_db, collection=coll_b, chunk_id="legacy-B",
            content="b", chunk_text_hash="8" * 64,
        )

        with (
            patch(
                "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
            ),
            patch.object(
                t3_db,
                "list_collections",
                return_value=[
                    {"name": coll_a, "count": 1},
                    {"name": coll_b, "count": 1},
                ],
            ),
        ):
            result = runner.invoke(
                main,
                ["t3", "reidentify", "--all-collections", "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "7" * 32 in _ids_in(t3_db, coll_a)
        assert "8" * 32 in _ids_in(t3_db, coll_b)

    def test_cli_taxonomy_skipped_in_output(self, t3_db, runner):
        """taxonomy__* collections are reported as skipped in output."""
        coll = _unique_coll(prefix="taxonomy")
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=["centroid-1"],
            documents=["centroid"],
            metadatas=[{"centroid_hash": "abc"}],
        )

        with patch(
            "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
        ):
            result = runner.invoke(
                main,
                ["t3", "reidentify", "--collection", coll, "--no-dry-run"],
            )
        assert result.exit_code == 0, result.output
        assert "skip" in result.output.lower()

    def test_cli_missing_hash_exits_nonzero(self, t3_db, runner):
        """Missing chunk_text_hash causes the CLI to exit with non-zero status."""
        coll = _unique_coll()
        col = t3_db._client.get_or_create_collection(coll)
        col.add(
            ids=["legacy-noh"],
            documents=["pre-RDR-053"],
            metadatas=[{"doc_id": "1.1.1", "chunk_index": 0}],
        )

        with patch(
            "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
        ):
            result = runner.invoke(
                main,
                ["t3", "reidentify", "--collection", coll, "--no-dry-run"],
            )
        assert result.exit_code != 0
        assert "chunk_text_hash" in result.output.lower()

    def test_cli_requires_collection_or_all(self, t3_db, runner):
        """Either --collection or --all-collections must be specified."""
        with patch(
            "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
        ):
            result = runner.invoke(main, ["t3", "reidentify"])
        assert result.exit_code != 0
        assert "collection" in result.output.lower()

    def test_cli_rejects_both_collection_and_all(self, t3_db, runner):
        """--collection and --all-collections together raise UsageError."""
        with patch(
            "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
        ):
            result = runner.invoke(
                main,
                ["t3", "reidentify", "--collection", "x", "--all-collections"],
            )
        assert result.exit_code != 0
        assert "exactly one" in result.output.lower()

    def test_cli_all_collections_parallel_processes_concurrently(self, t3_db, runner):
        """RDR-108 Phase 5 follow-up (nexus-qlm2): --all-collections
        with --max-workers > 1 runs collection processing concurrently.

        Each collection is independent (separate ID namespace), so
        parallel execution is safe and gives N-x speedup bounded by the
        operator's ChromaDB Cloud rate limits.

        We verify three properties:
          1. Every collection is processed (no skips beyond the
             documented carve-outs).
          2. Total counts in the summary match the per-collection
             results regardless of completion order.
          3. The reidentify_collection callable is dispatched within a
             worker pool (the function call timestamps interleave),
             not strictly sequentially.
        """
        import threading
        import time
        from collections import defaultdict
        from unittest.mock import patch as _patch

        # Seed three collections; each will need migration.
        colls = [_unique_coll() for _ in range(3)]
        for i, coll in enumerate(colls):
            _seed_chunk(
                t3_db, collection=coll, chunk_id=f"legacy-{i}",
                content=f"content-{i}", chunk_text_hash=str(i) * 64,
            )

        # Wrap reidentify_collection so we can observe call timing.
        from nexus.db import t3_reidentify as _ri
        call_times: dict[str, tuple[float, float]] = {}
        lock = threading.Lock()
        original = _ri.reidentify_collection

        def _timed(t3, coll_name, *, dry_run):
            t0 = time.monotonic()
            time.sleep(0.05)  # force overlap window
            res = original(t3, coll_name, dry_run=dry_run)
            t1 = time.monotonic()
            with lock:
                call_times[coll_name] = (t0, t1)
            return res

        with (
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            patch.object(
                t3_db,
                "list_collections",
                return_value=[{"name": c, "count": 1} for c in colls],
            ),
            _patch(
                "nexus.db.t3_reidentify.reidentify_collection",
                side_effect=_timed,
            ),
        ):
            result = runner.invoke(
                main,
                [
                    "t3", "reidentify",
                    "--all-collections",
                    "--no-dry-run",
                    "--max-workers", "3",
                ],
            )

        assert result.exit_code == 0, result.output
        # All three collections were processed.
        for c in colls:
            assert c in call_times, (
                f"collection {c} was never dispatched; got {list(call_times)}"
            )
        # Concurrency check: at least one pair of collections must have
        # overlapping (start, end) windows under max_workers=3.
        windows = sorted(call_times.values())
        any_overlap = any(
            windows[i][1] > windows[i + 1][0]  # later starts before earlier ends
            for i in range(len(windows) - 1)
        )
        assert any_overlap, (
            f"expected overlapping execution windows under max_workers=3; "
            f"observed serial windows {windows}"
        )
        # Summary aggregates correctly.
        assert "across 3 collection(s)" in result.output

    def test_cli_max_workers_one_falls_back_to_serial(self, t3_db, runner):
        """``--max-workers 1`` (or default in single-collection mode)
        executes serially and produces deterministic per-collection
        ordering, useful for debugging and operator-readable logs."""
        import time
        import threading

        colls = [_unique_coll() for _ in range(2)]
        for i, coll in enumerate(colls):
            _seed_chunk(
                t3_db, collection=coll, chunk_id=f"legacy-{i}",
                content=f"content-{i}", chunk_text_hash=str(i + 5) * 64,
            )

        from nexus.db import t3_reidentify as _ri
        call_order: list[str] = []
        order_lock = threading.Lock()
        original = _ri.reidentify_collection

        def _ordered(t3, coll_name, *, dry_run):
            with order_lock:
                call_order.append(coll_name)
            time.sleep(0.02)
            return original(t3, coll_name, dry_run=dry_run)

        with (
            patch("nexus.commands.t3._make_t3_for_backfill", return_value=t3_db),
            patch.object(
                t3_db,
                "list_collections",
                return_value=[{"name": c, "count": 1} for c in colls],
            ),
            patch(
                "nexus.db.t3_reidentify.reidentify_collection",
                side_effect=_ordered,
            ),
        ):
            result = runner.invoke(
                main,
                [
                    "t3", "reidentify",
                    "--all-collections",
                    "--no-dry-run",
                    "--max-workers", "1",
                ],
            )

        assert result.exit_code == 0, result.output
        # Strict serial dispatch order matches input order.
        assert call_order == colls

    def test_cli_all_collections_continues_past_errors(self, t3_db, runner):
        """--all-collections accumulates errors and exits nonzero,
        but processes every collection in the iteration first.

        Mixes a taxonomy carve-out, a valid collection, and a hashless
        collection. The valid one must migrate, the taxonomy one must
        be reported skipped, and the hashless one must surface the
        structured error and force exit_code != 0.
        """
        coll_valid = _unique_coll()
        coll_tax = _unique_coll(prefix="taxonomy")
        coll_bad = _unique_coll()

        _seed_chunk(
            t3_db, collection=coll_valid, chunk_id="legacy-valid",
            content="hello", chunk_text_hash="9" * 64,
        )
        col_tax = t3_db._client.get_or_create_collection(coll_tax)
        col_tax.add(
            ids=["centroid-1"],
            documents=["centroid"],
            metadatas=[{"centroid_hash": "abc"}],
        )
        col_bad = t3_db._client.get_or_create_collection(coll_bad)
        col_bad.add(
            ids=["legacy-noh"],
            documents=["pre-RDR-053"],
            metadatas=[{"doc_id": "1.1.1"}],
        )

        with (
            patch(
                "nexus.commands.t3._make_t3_for_backfill", return_value=t3_db
            ),
            patch.object(
                t3_db,
                "list_collections",
                return_value=[
                    {"name": coll_valid, "count": 1},
                    {"name": coll_tax, "count": 1},
                    {"name": coll_bad, "count": 1},
                ],
            ),
        ):
            result = runner.invoke(
                main,
                ["t3", "reidentify", "--all-collections", "--no-dry-run"],
            )

        assert result.exit_code != 0  # error present
        # Valid collection migrated despite the bad one's error
        assert "9" * 32 in _ids_in(t3_db, coll_valid)
        # Taxonomy reported skipped
        assert "skip" in result.output.lower()
        # Bad collection surfaced the structured error
        assert "chunk_text_hash" in result.output.lower()
