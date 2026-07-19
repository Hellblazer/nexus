# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for chunk_text_hash backfill (RDR-053)."""

from __future__ import annotations

import hashlib

import chromadb
import pytest


def _make_collection(client, name, chunks):
    """Create a collection with chunks, some missing chunk_text_hash."""
    col = client.get_or_create_collection(name)
    ids, docs, metas = [], [], []
    for i, (text, has_hash) in enumerate(chunks):
        meta = {"source_path": "test.py", "content_hash": "filehash"}
        if has_hash:
            meta["chunk_text_hash"] = hashlib.sha256(text.encode()).hexdigest()
        ids.append(f"chunk-{i}")
        docs.append(text)
        metas.append(meta)
    col.add(ids=ids, documents=docs, metadatas=metas)
    return col


class TestBackfillChunkTextHash:
    def test_backfill_adds_missing_hashes(self):
        """Chunks without chunk_text_hash get it added."""
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__test_backfill", [
            ("def foo(): pass", False),
            ("def bar(): return 1", False),
            ("class Baz: x = 1", True),  # already has hash
        ])

        updated, skipped, total = _backfill_chunk_text_hash(col)
        assert total == 3
        assert updated == 2
        assert skipped == 1

        # Verify hashes are correct
        result = col.get(include=["documents", "metadatas"])
        for doc, meta in zip(result["documents"], result["metadatas"]):
            expected = hashlib.sha256(doc.encode()).hexdigest()
            assert meta["chunk_text_hash"] == expected

    def test_backfill_idempotent(self):
        """Running backfill twice produces no changes on second run."""
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__test_idempotent", [
            ("hello world", False),
            ("goodbye world", False),
        ])

        _backfill_chunk_text_hash(col)
        updated, skipped, total = _backfill_chunk_text_hash(col)
        assert updated == 0
        assert skipped == 2

    def test_backfill_preserves_existing_metadata(self):
        """Backfill does not clobber other metadata fields."""
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__test_preserve", [
            ("some code", False),
        ])

        _backfill_chunk_text_hash(col)
        result = col.get(ids=["chunk-0"], include=["metadatas"])
        meta = result["metadatas"][0]
        assert meta["source_path"] == "test.py"
        assert meta["content_hash"] == "filehash"
        assert "chunk_text_hash" in meta

    def test_backfill_empty_collection(self):
        """Empty collection returns zeros."""
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        col = client.get_or_create_collection("code__test_empty")

        updated, skipped, total = _backfill_chunk_text_hash(col)
        assert total == 0
        assert updated == 0
        assert skipped == 0

    def test_backfill_skips_documentless_collections(self):
        """nexus-uebj: ``taxonomy__centroids`` (and any other collection in
        ``_DOCUMENTLESS_COLLECTIONS``) stores embedding + label metadata only,
        no document text. Walking it would emit one
        ``backfill_chunk_text_hash_none_doc`` warning per chunk with no
        actionable signal. The early-return guard skips these collections
        entirely without touching the collection at all.
        """
        from unittest.mock import MagicMock

        from nexus.commands.collection import _backfill_chunk_text_hash

        col = MagicMock()
        col.name = "taxonomy__centroids"

        updated, skipped, total = _backfill_chunk_text_hash(col)

        assert (updated, skipped, total) == (0, 0, 0)
        # The early-return must avoid touching the collection — no fetches,
        # no updates, nothing logged about None documents.
        col.get.assert_not_called()
        col.update.assert_not_called()

    def test_backfill_skips_none_documents_without_crash(self):
        """nexus-p03z Issue 1: when T3 returns ``documents[i] is None``
        (an inconsistency that occurs in practice on rare chunks), the
        backfill code must skip those rows, count them as skipped,
        process the remainder, and return normally.

        Reproduces the live crash that made ``nx catalog backfill``
        unusable on the host catalog. Uses a mocked collection because
        chromadb's public ``add()`` rejects None document strings; the
        None-document state is reachable in cloud T3 but not via local
        EphemeralClient writes.

        nexus-o9an: backfill is now a two-pass walk (pass 1 collects
        ids; pass 2 fetches by exact id). The mock must return shapes
        for both passes.
        """
        from unittest.mock import MagicMock

        from nexus.commands.collection import _backfill_chunk_text_hash

        col = MagicMock()
        col.name = "code__none_doc_repro"
        col.get.side_effect = [
            # Pass 1: 3 ids (< _BACKFILL_BATCH so loop breaks after one call).
            {"ids": ["c1", "c2", "c3"]},
            # Pass 2: batch fetch with docs + embeddings + metadatas.
            {
                "ids": ["c1", "c2", "c3"],
                "documents": ["alpha", None, "gamma"],
                "embeddings": [[0.1], [0.2], [0.3]],
                "metadatas": [
                    {"source_path": "a.py"},
                    {"source_path": "b.py"},
                    {"source_path": "c.py"},
                ],
            },
        ]

        updated, skipped, total = _backfill_chunk_text_hash(col)

        assert total == 3
        assert updated == 2  # alpha + gamma got hashed
        assert skipped == 1  # the None doc skipped, not crashed
        # The col.upsert call must NOT include the None-doc chunk.
        upsert_call = col.upsert.call_args
        assert "c2" not in upsert_call.kwargs["ids"]


class TestBackfillUpsertHandlesLegacyChunks:
    """nexus-o9an: chunks with 32+ metadata keys (legacy cargo +
    pre-RDR-108 fields) trip ChromaDB Cloud's per-row metadata quota
    when ``col.update`` MERGES the existing metadata with a new
    ``chunk_text_hash``. The previous implementation caught the
    quota error and silently incremented ``skipped``, leaving
    chunks broken. Backfill now uses ``col.upsert`` with metadata
    pre-normalized via the canonical schema funnel; on Cloud this
    REPLACES rather than merges (proven by the prod migration on
    ``docs__1-7__voyage-context-3__v1`` which fixed 600 over-quota
    chunks). EphemeralClient's upsert MERGES, so this test asserts
    the contract that's reliable across both backends: the new
    ``chunk_text_hash`` lands on the chunk and the row is no longer
    a silently-skipped error."""

    def test_legacy_chunk_with_cargo_gets_hash_via_upsert(self):
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        col = client.get_or_create_collection("docs__legacy_cargo")
        col.add(
            ids=["legacy-1"],
            documents=["paper text body"],
            metadatas=[{
                "title": "old-paper",
                "content_type": "pdf",
                "content_hash": "abc",
                "indexed_at": "2024-01-01T00:00:00",
                "corpus": "knowledge",
                "store_type": "knowledge",
                "extraction_method": "docling",
                "doc_id": "1.1.1",
                "chunk_index": 0,
                "chunk_count": 50,
            }],
        )

        updated, skipped, total = _backfill_chunk_text_hash(col)
        # Counted as updated (was missing chunk_text_hash, now has it).
        # The old code would have silently skipped these on Cloud
        # quota failure; the new code routes through upsert which
        # Cloud treats as REPLACE.
        assert (updated, skipped, total) == (1, 0, 1)

        meta = col.get(ids=["legacy-1"], include=["metadatas"])["metadatas"][0]
        assert meta["chunk_text_hash"] == hashlib.sha256(
            b"paper text body"
        ).hexdigest()
        # Title preserved (canonical field, present in both old + new).
        assert meta["title"] == "old-paper"


class TestBackfillTwoPassPaginationContract:
    """nexus-o9an: pass 1 collects every chunk id with a lightweight
    ``include=[]`` payload before pass 2 fetches by exact id. This
    sidesteps ChromaDB Cloud's offset-pagination instability (where a
    naive offset+update loop can revisit some chunks and miss others)."""

    def test_pass1_uses_lightweight_payload_pass2_uses_exact_ids(self):
        from unittest.mock import MagicMock

        from nexus.commands.collection import _backfill_chunk_text_hash

        col = MagicMock()
        col.name = "code__two_pass_contract"
        # Pass 1 short-circuits on a partial page (< _BACKFILL_BATCH);
        # only one pass-1 call fires before the loop breaks.
        col.get.side_effect = [
            # Pass 1: 2 ids (less than batch size, so loop breaks).
            {"ids": ["a", "b"]},
            # Pass 2: batch fetch with embeddings + docs + metas.
            {
                "ids": ["a", "b"],
                "documents": ["doc-a", "doc-b"],
                "embeddings": [[0.1], [0.2]],
                "metadatas": [{}, {}],
            },
        ]

        _backfill_chunk_text_hash(col)

        calls = col.get.call_args_list
        assert len(calls) == 2, (
            f"expected 1 pass-1 call + 1 pass-2 call, got {len(calls)}"
        )
        # Pass 1: lightweight (include=[]), offset-based.
        assert calls[0].kwargs.get("include") == []
        assert calls[0].kwargs.get("offset") == 0
        # Pass 2: ids= (exact-id lookup), NOT offset-based.
        assert calls[1].kwargs.get("ids") == ["a", "b"]
        assert "offset" not in calls[1].kwargs
        assert set(calls[1].kwargs.get("include", [])) == {
            "documents", "embeddings", "metadatas",
        }

    def test_pass1_paginates_when_first_page_full(self):
        """When pass-1 page is exactly _BACKFILL_BATCH, the loop
        continues with offset += BATCH and probes for more.
        Locks the multi-page pass-1 path that the order-stability
        contract depends on."""
        from unittest.mock import MagicMock

        from nexus.commands.collection import (
            _BACKFILL_BATCH,
            _backfill_chunk_text_hash,
        )

        col = MagicMock()
        col.name = "code__paginated"
        # Pass 1 page 1 returns exactly _BACKFILL_BATCH ids → loop continues.
        page1_ids = [f"id-{i}" for i in range(_BACKFILL_BATCH)]
        col.get.side_effect = [
            {"ids": page1_ids},                     # pass 1, page 1 (full)
            {"ids": []},                            # pass 1, page 2 (empty)
            {                                        # pass 2, batch fetch
                "ids": page1_ids,
                "documents": [f"doc-{i}" for i in range(_BACKFILL_BATCH)],
                "embeddings": [[0.0]] * _BACKFILL_BATCH,
                "metadatas": [{"chunk_text_hash": "x"}] * _BACKFILL_BATCH,
            },
        ]

        _backfill_chunk_text_hash(col)

        calls = col.get.call_args_list
        assert len(calls) == 3
        assert calls[0].kwargs.get("offset") == 0
        assert calls[1].kwargs.get("offset") == _BACKFILL_BATCH
        # Pass 2 still uses ids= regardless of pass-1 page count.
        assert calls[2].kwargs.get("ids") == page1_ids


# ── Phase 1.3 (nexus-ppl) — T2 chash_index reconciliation ────────────────────


class TestBackfillPopulatesT2ChashIndex:
    """RDR-086 Phase 1.3: the same per-chunk pass that writes chunk_text_hash
    to T3 must also populate T2 chash_index for downstream resolution.
    Reconciles gaps from Phase 1.2 dual-write failures and pre-Phase-1
    collections that were indexed before the dual-write existed.
    """

    def test_backfill_populates_t2_chash_index_for_newly_hashed_chunks(self, tmp_path):
        """Chunks that gain chunk_text_hash in T3 also gain chash_index rows in T2."""
        from nexus.commands.collection import _backfill_chunk_text_hash
        from nexus.db.t2.chash_index import ChashIndex

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__pop_new", [
            ("alpha code", False),
            ("beta code", False),
        ])

        store = ChashIndex(tmp_path / "t2.db")
        try:
            updated, skipped, total = _backfill_chunk_text_hash(
                col, chash_index=store,
            )
            assert updated == 2
            assert total == 2

            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            alpha = hashlib.sha256(b"alpha code").hexdigest()
            beta = hashlib.sha256(b"beta code").hexdigest()
            assert rows == sorted([
                (alpha, "code__pop_new"),
                (beta, "code__pop_new"),
            ])
        finally:
            store.close()

    def test_backfill_populates_t2_for_already_hashed_chunks(self, tmp_path):
        """Reconciliation path: chunks that already have chunk_text_hash in T3
        but whose chash_index row is missing must still be registered.
        """
        from nexus.commands.collection import _backfill_chunk_text_hash
        from nexus.db.t2.chash_index import ChashIndex

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__pop_existing", [
            ("gamma code", True),   # already hashed in T3
            ("delta code", True),   # already hashed in T3
        ])

        store = ChashIndex(tmp_path / "t2.db")
        try:
            updated, skipped, total = _backfill_chunk_text_hash(
                col, chash_index=store,
            )
            # T3-side: no new hashes — but T2 must still be populated
            assert updated == 0
            assert skipped == 2
            assert total == 2

            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            gamma = hashlib.sha256(b"gamma code").hexdigest()
            delta = hashlib.sha256(b"delta code").hexdigest()
            assert rows == sorted([
                (gamma, "code__pop_existing"),
                (delta, "code__pop_existing"),
            ])
        finally:
            store.close()

    def test_backfill_t2_idempotent(self, tmp_path):
        """Running backfill twice produces the same chash_index rows (INSERT OR REPLACE)."""
        from nexus.commands.collection import _backfill_chunk_text_hash
        from nexus.db.t2.chash_index import ChashIndex

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__t2_idem", [
            ("epsilon", False),
            ("zeta", False),
        ])

        store = ChashIndex(tmp_path / "t2.db")
        try:
            _backfill_chunk_text_hash(col, chash_index=store)
            _backfill_chunk_text_hash(col, chash_index=store)

            count = store.conn.execute(
                "SELECT COUNT(*) FROM chash_index"
            ).fetchone()[0]
            assert count == 2  # exactly 2 rows, not 4
        finally:
            store.close()

    def test_backfill_without_chash_index_backward_compat(self, tmp_path):
        """chash_index=None preserves existing T3-only behavior for legacy callers
        (nx catalog setup, nx catalog audit). No T2 writes, no errors.
        """
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__no_t2", [
            ("eta", False),
            ("theta", True),
        ])

        # chash_index omitted — must behave as before
        updated, skipped, total = _backfill_chunk_text_hash(col)
        assert updated == 1
        assert skipped == 1
        assert total == 2


# ── Phase 1.5 (nexus-jfi) — progress reporting ───────────────────────────────


class TestBackfillProgressReporting:
    """_backfill_chunk_text_hash must invoke on_progress at batch boundaries
    so the CLI's tqdm wrapper can drive a visual bar for the ~25–70 min
    full-corpus runs (278k chunks across 136 collections, RF-11).
    """

    def test_on_progress_called_per_batch(self):
        """With _BACKFILL_BATCH=300, a 301-chunk collection must yield 2 callback invocations."""
        from nexus.commands import collection as collection_mod
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        # Use a small batch size to keep the test fast while still exercising
        # the "multiple batches" branch.
        monkey_orig = collection_mod._BACKFILL_BATCH
        try:
            collection_mod._BACKFILL_BATCH = 5
            col = _make_collection(client, "code__progress", [
                (f"chunk {i} code", False) for i in range(12)
            ])
            calls: list[tuple[int, int, int]] = []

            def _observe(updated: int, skipped: int, total: int) -> None:
                calls.append((updated, skipped, total))

            _backfill_chunk_text_hash(col, on_progress=_observe)

            # 12 chunks / batch 5 = 3 batches → 3 callback invocations
            assert len(calls) == 3, f"expected 3 callbacks, got {len(calls)}: {calls}"
            # Monotonic non-decreasing on total
            assert calls[0][2] <= calls[1][2] <= calls[2][2]
            # Final call must reflect full corpus
            assert calls[-1][2] == 12
        finally:
            collection_mod._BACKFILL_BATCH = monkey_orig

    def test_on_progress_none_is_silent(self):
        """Omitting on_progress is fine — no coupling to reporting UX."""
        from nexus.commands.collection import _backfill_chunk_text_hash

        client = chromadb.EphemeralClient()
        col = _make_collection(client, "code__silent", [
            ("whatever", False),
        ])

        # Must not raise.
        _backfill_chunk_text_hash(col, on_progress=None)
