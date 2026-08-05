# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2.5 — verify stale-chunk pruning paginates beyond 300-record ChromaDB limit.

Bug: doc_indexer.py _index_document() calls col.get(where={"source_path": ...})
without limit=/offset pagination.  ChromaDB Cloud returns at most 300 records
per get() call, so documents producing >300 chunks leave orphan stale chunks
after re-indexing.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.doc_indexer import _index_document
from tests.conftest import set_credentials


@pytest.fixture(autouse=True)
def _no_catalog_identity(monkeypatch):
    """nexus-tp8yk D2a/D3: this file's ``mock_t3`` MagicMock never writes
    real chunks anywhere the real T2-everywhere catalog engine's T3 can
    see, but ``_index_document`` still resolves a REAL catalog doc_id by
    default (autouse engine substrate) — which would make the new
    PROPAGATING ``_fence_complete`` refuse (referenced>0, present=0) and
    would route the prune's D3 union guard through a real
    ``docs_for_chashes`` call the synthetic ``old_hash_________N`` test
    ids were never written to exercise. This file's actual subject is
    PAGINATION of the stale-chunk delete loop, not catalog identity or
    the RUNFENCE/union-guard contracts (owned elsewhere) — stub doc_id
    resolution to "" so ``_index_document`` takes its no-catalog-ingest
    branch (no fence calls).

    nexus-tp8yk D3 substantive-critic SIGNIFICANT (2026-08-04): the
    prune's union guard used to gate on ``if doc_id:`` and fall back to
    unconditional delete whenever it was empty — that gate is GONE
    (``indexer_utils.prune_orphan_candidates`` now makes the "no
    catalog" vs. "catalog healthy, doc_id unresolved" distinction on
    reader availability, not on *doc_id*), so an empty doc_id ALONE no
    longer bypasses the real catalog reader. Explicitly declare "no
    catalog" here too — preserving this file's exact-count assertions
    deterministically rather than depending on how the real engine's
    ``docs_for_chashes`` happens to answer for non-hex synthetic ids.
    """
    monkeypatch.setattr(
        "nexus.doc_indexer._register_or_lookup_doc_id", lambda *a, **kw: "",
    )
    monkeypatch.setattr(
        "nexus.catalog.factory.make_catalog_reader", lambda *a, **kw: None,
    )


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    p = tmp_path / "big_doc.md"
    p.write_text("content for testing pagination")
    return p


def _make_embed_fn(dim: int = 3):
    """Return an embed_fn that produces deterministic embeddings (no Voyage API)."""
    def embed_fn(texts: list[str], model: str) -> tuple[list[list[float]], str]:
        return [[0.1] * dim for _ in texts], model
    return embed_fn


def _make_chunk_fn(num_chunks: int):
    """Return a chunk_fn that produces *num_chunks* fake chunks."""
    def chunk_fn(file_path, content_hash, target_model, now_iso, corpus):
        return [
            (
                f"{content_hash[:16]}_{i}",
                f"chunk text {i}",
                {
                    "source_path": str(file_path),
                    "corpus": corpus,
                    "store_type": "markdown",
                    "embedding_model": target_model,
                    "content_hash": content_hash,
                    "indexed_at": now_iso,
                    "chunk_index": i,
                    "chunk_count": num_chunks,
                },
            )
            for i in range(num_chunks)
        ]
    return chunk_fn


class TestStaleChunkPaginatedPruning:
    """Verify that stale-chunk pruning handles >300 existing chunks."""

    def test_prune_stale_beyond_300(self, sample_file: Path, monkeypatch):
        """Documents with >300 existing chunks must have ALL stale chunks pruned.

        Simulates a re-index where 350 old chunks exist but only 10 new chunks
        are produced.  All 340 stale chunks must be deleted.
        """
        set_credentials(monkeypatch)
        content_hash = hashlib.sha256(sample_file.read_bytes()).hexdigest()

        # -- Mock T3 collection --
        mock_col = MagicMock()

        # First col.get() is the staleness check (limit=1) — return empty to force indexing
        # Subsequent col.get() calls are the pagination loop for stale-chunk pruning
        old_ids = [f"old_hash_________{i}" for i in range(350)]
        new_chunk_count = 10

        def mock_get(**kwargs):
            # First call: staleness check (has limit=1)
            if kwargs.get("limit") == 1:
                return {"ids": [], "metadatas": []}

            # Pagination calls for stale-chunk pruning
            offset = kwargs.get("offset", 0)
            limit = kwargs.get("limit", 300)
            page = old_ids[offset:offset + limit]
            return {"ids": page}

        mock_col.get = mock_get
        mock_col.delete = MagicMock()

        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = mock_col

        chunk_fn = _make_chunk_fn(new_chunk_count)
        embed_fn = _make_embed_fn()

        result = _index_document(
            sample_file,
            "test_corpus",
            chunk_fn,
            t3=mock_t3,
            embed_fn=embed_fn,
            force=True,  # bypass staleness check
        )

        assert result == new_chunk_count

        # Verify delete was called in MAX_RECORDS_PER_WRITE=300-sized batches
        # (indexing review I4: the unbatched single-call shape violated the
        # ChromaDB Cloud quota). For 350 stale IDs we expect 2 calls: 300 + 50.
        assert mock_col.delete.call_count == 2, (
            f"expected 2 batched deletes for 350 stale IDs, "
            f"got {mock_col.delete.call_count}"
        )
        # Aggregate the IDs across both calls.
        deleted_ids: list[str] = []
        for call in mock_col.delete.call_args_list:
            ids = call.kwargs.get("ids") or call.args[0]
            assert len(ids) <= 300, f"batch size {len(ids)} exceeds quota cap"
            deleted_ids.extend(ids)

        # All 350 old IDs that are NOT in the new set should be deleted
        new_ids = {f"{content_hash[:16]}_{i}" for i in range(new_chunk_count)}
        expected_stale = [eid for eid in old_ids if eid not in new_ids]
        assert set(deleted_ids) == set(expected_stale)
        assert len(deleted_ids) == 350  # none of the old IDs match the new hash prefix

    def test_prune_stale_under_300_still_works(self, sample_file: Path, monkeypatch):
        """Documents with <300 existing chunks still have stale chunks pruned (no regression)."""
        set_credentials(monkeypatch)

        old_ids = [f"old_hash_________{i}" for i in range(50)]
        new_chunk_count = 5

        def mock_get(**kwargs):
            if kwargs.get("limit") == 1:
                return {"ids": [], "metadatas": []}
            offset = kwargs.get("offset", 0)
            limit = kwargs.get("limit", 300)
            page = old_ids[offset:offset + limit]
            return {"ids": page}

        mock_col = MagicMock()
        mock_col.get = mock_get
        mock_col.delete = MagicMock()

        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = mock_col

        result = _index_document(
            sample_file,
            "test_corpus",
            _make_chunk_fn(new_chunk_count),
            t3=mock_t3,
            embed_fn=_make_embed_fn(),
            force=True,
        )

        assert result == new_chunk_count
        mock_col.delete.assert_called_once()
        deleted_ids = mock_col.delete.call_args[1].get("ids") or mock_col.delete.call_args[0][0]
        assert len(deleted_ids) == 50  # all old chunks are stale

    def test_no_stale_chunks_no_delete(self, sample_file: Path, monkeypatch):
        """When all existing chunks match new chunks, delete is not called."""
        set_credentials(monkeypatch)
        content_hash = hashlib.sha256(sample_file.read_bytes()).hexdigest()
        new_chunk_count = 5
        new_ids = [f"{content_hash[:16]}_{i}" for i in range(new_chunk_count)]

        def mock_get(**kwargs):
            if kwargs.get("limit") == 1:
                return {"ids": [], "metadatas": []}
            offset = kwargs.get("offset", 0)
            limit = kwargs.get("limit", 300)
            page = new_ids[offset:offset + limit]
            return {"ids": page}

        mock_col = MagicMock()
        mock_col.get = mock_get
        mock_col.delete = MagicMock()

        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = mock_col

        result = _index_document(
            sample_file,
            "test_corpus",
            _make_chunk_fn(new_chunk_count),
            t3=mock_t3,
            embed_fn=_make_embed_fn(),
            force=True,
        )

        assert result == new_chunk_count
        mock_col.delete.assert_not_called()
