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
                "SELECT chash, physical_collection, doc_id FROM chash_index"
            ).fetchall())
            alpha = hashlib.sha256(b"alpha code").hexdigest()
            beta = hashlib.sha256(b"beta code").hexdigest()
            assert rows == sorted([
                (alpha, "code__pop_new", "chunk-0"),
                (beta, "code__pop_new", "chunk-1"),
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
                "SELECT chash, physical_collection, doc_id FROM chash_index"
            ).fetchall())
            gamma = hashlib.sha256(b"gamma code").hexdigest()
            delta = hashlib.sha256(b"delta code").hexdigest()
            assert rows == sorted([
                (gamma, "code__pop_existing", "chunk-0"),
                (delta, "code__pop_existing", "chunk-1"),
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
