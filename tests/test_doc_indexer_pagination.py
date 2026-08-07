# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2.5 (historical) — stale-chunk prune pagination beyond the
300-record ChromaDB limit.

nexus-tbkk1: the pagination logic this file exercised (``_index_document``'s
``col.get(where={"source_path": ...})`` prune loop) was DELETED as dead code.
RDR-102 D2 (2026-05-02) removed ``source_path`` from ``make_chunk_metadata``,
so that where-clause never matched a real chunk row — which means the
pagination it exercised was, like the prune itself, never reachable in
production either: a query that always returns zero rows never has a second
page to fetch. See ``nexus.doc_indexer._identity_where``'s docstring and
``tests/test_doc_indexer.py::test_stale_chunk_pruning_deleted_as_dead_code``
for the full rationale and the surviving deletion-proof coverage. The real
cross-document prune protection is ``mcp_infra._sweep_superseded_vectors``.

This file is kept (rather than deleted) as a kill-control regression test:
if the deleted prune block were reintroduced, a >300-stale-id T3 double
would make it issue a source_path-keyed query and delete calls again.
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

    nexus-tbkk1 UPDATE (2026-08-05): the prune (and its union guard) this
    paragraph originally described are DELETED dead code — see the
    module docstring above. This fixture's doc_id stub is retained for
    the STILL-LIVE reason in the paragraph above (avoiding a real
    ``_fence_complete`` refusal against a MagicMock T3 that never
    actually lands chunks in the real engine), which is unrelated to the
    now-removed prune. Explicitly declare "no catalog" here too —
    preserving this file's exact-count assertions deterministically
    rather than depending on how the real engine's
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
    """nexus-tbkk1 kill-control regression tests. The pagination logic
    this class originally tested (``_index_document``'s stale-chunk prune
    loop) is DELETED dead code — see the module docstring above."""

    def test_prune_stale_beyond_300_no_longer_queried_or_deleted(
        self, sample_file: Path, monkeypatch,
    ) -> None:
        """Seeds a legacy-shaped T3 double with 350 old stale ids (>300,
        the pagination-triggering case) that the pre-nexus-tbkk1 code
        would have paginated through in 2 batches and deleted entirely.

        Kill control: reintroducing the deleted prune block makes
        ``col.get`` get called with a ``source_path`` where-clause
        (seeded to answer with the 350 legacy ids) and ``col.delete``
        fire; this test fails the moment that regresses.
        """
        set_credentials(monkeypatch)
        pdf_path_str = str(sample_file.resolve())
        old_ids = [f"old_hash_________{i}" for i in range(350)]
        new_chunk_count = 10

        def mock_get(**kwargs):
            if kwargs.get("limit") == 1:
                return {"ids": [], "metadatas": []}
            if kwargs.get("where") == {"source_path": pdf_path_str}:
                offset = kwargs.get("offset", 0)
                limit = kwargs.get("limit", 300)
                return {"ids": old_ids[offset:offset + limit]}
            return {"ids": [], "metadatas": []}

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
            force=True,  # bypass staleness check
        )

        assert result == new_chunk_count
        mock_col.delete.assert_not_called()

    def test_prune_stale_under_300_no_longer_queried_or_deleted(
        self, sample_file: Path, monkeypatch,
    ) -> None:
        """Same kill control as above, under the 300-record pagination
        boundary (the pre-existing pagination fix's baseline case)."""
        set_credentials(monkeypatch)
        pdf_path_str = str(sample_file.resolve())
        old_ids = [f"old_hash_________{i}" for i in range(50)]
        new_chunk_count = 5

        def mock_get(**kwargs):
            if kwargs.get("limit") == 1:
                return {"ids": [], "metadatas": []}
            if kwargs.get("where") == {"source_path": pdf_path_str}:
                offset = kwargs.get("offset", 0)
                limit = kwargs.get("limit", 300)
                return {"ids": old_ids[offset:offset + limit]}
            return {"ids": [], "metadatas": []}

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
