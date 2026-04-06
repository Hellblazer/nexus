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
