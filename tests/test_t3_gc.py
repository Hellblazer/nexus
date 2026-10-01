# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-r5eo: the ``T3Database`` primitives ``nx t3 gc`` once used (RDR-101 Phase 6).

``nx t3 gc`` itself no longer uses them: since RDR-192 Step 8 (nexus-wbfpw.18) it takes its
candidates from the engine's reapable listing and moves them with the engine's own statement, never
by chunk id. The verb is covered by ``tests/test_wbfpw18_t3_gc_wire.py`` (routes and refusals) and
``tests/test_wbfpw18_t3_gc_substrate.py`` (the engine's behaviour). What remains here are the two
``T3Database`` methods, still public API with other callers, and the ``get_embeddings`` ordering
contract this module has always carried.

Tests use a real T3Database backed by an in-process vector client + the bundled MiniLM embedder, so
no Cloud credentials are needed.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

from nexus.db.t3 import T3Database
from tests.conftest import make_vector_test_client


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def t3_db():
    """Real T3Database backed by an ephemeral local Chroma."""
    return T3Database(
        _client=make_vector_test_client(),
        _ef_override=DefaultEmbeddingFunction(),
    )


def _seed_chunk(
    t3_db: T3Database,
    *,
    collection: str,
    chunk_id: str,
    content: str,
    indexed_at: str,
    chunk_text_hash: str | None = None,
    doc_id: str | None = None,
) -> None:
    """Insert one chunk with the metadata the T3Database listing tests read."""
    meta: dict = {"indexed_at": indexed_at}
    if chunk_text_hash is not None:
        meta["chunk_text_hash"] = chunk_text_hash
    if doc_id is not None:
        meta["doc_id"] = doc_id
    col = t3_db._client.get_or_create_collection(collection)
    col.add(ids=[chunk_id], documents=[content], metadatas=[meta])


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── T3Database new methods ────────────────────────────────────────────────


def test_list_chunks_with_metadata_returns_doc_id_and_indexed_at(t3_db):
    """``list_chunks_with_metadata`` yields ``(chunk_id, metadata_subset)``."""
    coll = "knowledge__test_list"
    now = _iso(datetime.now(UTC))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        doc_id="1.1.1", indexed_at=now,
    )
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c2", content="y",
        doc_id="1.1.2", indexed_at=now,
    )
    rows = list(t3_db.list_chunks_with_metadata(coll))
    by_id = {cid: meta for cid, meta in rows}
    assert by_id["c1"] == {"doc_id": "1.1.1", "indexed_at": now}
    assert by_id["c2"] == {"doc_id": "1.1.2", "indexed_at": now}


def test_list_chunks_with_metadata_missing_collection(t3_db):
    assert list(t3_db.list_chunks_with_metadata("knowledge__nonexistent")) == []


def test_delete_by_chunk_ids_deletes_only_listed(t3_db):
    """``delete_by_chunk_ids`` deletes the listed ids and returns the count."""
    coll = "knowledge__test_gc_delete_by_ids"
    now = _iso(datetime.now(UTC))
    for cid in ("c1", "c2", "c3"):
        _seed_chunk(
            t3_db, collection=coll, chunk_id=cid, content=cid,
            doc_id="1.1.1", indexed_at=now,
        )
    deleted = t3_db.delete_by_chunk_ids(coll, ["c1", "c3"])
    assert deleted == 2
    surviving = t3_db._client.get_collection(coll).get()["ids"]
    assert surviving == ["c2"]


def test_delete_by_chunk_ids_missing_collection_returns_zero(t3_db):
    assert t3_db.delete_by_chunk_ids("knowledge__nonexistent", ["c1"]) == 0


def test_delete_by_chunk_ids_empty_list(t3_db):
    coll = "knowledge__test_gc_empty_list"
    now = _iso(datetime.now(UTC))
    _seed_chunk(
        t3_db, collection=coll, chunk_id="c1", content="x",
        doc_id="1.1.1", indexed_at=now,
    )
    assert t3_db.delete_by_chunk_ids(coll, []) == 0
    assert t3_db._client.get_collection(coll).count() == 1


class TestGetEmbeddingsRequestOrder:
    """nexus-pebfx.7 critic: Chroma's col.get(ids=...) returns rows in
    INTERNAL insertion order, not request order — positional consumption
    misattributed embeddings whenever the orders differed (false
    contradiction flags, wrong cluster geometry). T3Database.get_embeddings
    must reorder to request order, exactly like the service path."""

    def test_rows_follow_request_order_not_insertion_order(self):
        import numpy as np
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

        from nexus.db.t3 import T3Database

        client = make_vector_test_client()
        t3 = T3Database(_client=client, _ef_override=DefaultEmbeddingFunction())
        col = t3.get_or_create_collection("knowledge__ordertest", strict=False)
        # Insert in REVERSE alphabetical order so insertion order != request
        # order for the ["id-a", "id-b"] request below.
        col.add(
            ids=["id-b", "id-a"],
            documents=["text for b", "text for a"],
            metadatas=[{"k": "b"}, {"k": "a"}],
        )
        direct = col.get(ids=["id-a", "id-b"], include=["embeddings"])
        by_id = dict(zip(direct["ids"], direct["embeddings"]))

        result = t3.get_embeddings("knowledge__ordertest", ["id-a", "id-b"])
        assert result.shape[0] == 2
        assert np.allclose(result[0], np.array(by_id["id-a"], dtype=np.float32))
        assert np.allclose(result[1], np.array(by_id["id-b"], dtype=np.float32))

    def test_missing_ids_dropped(self):
        from nexus.db.minilm_direct import MiniLMDirectEmbeddingFunction as DefaultEmbeddingFunction

        from nexus.db.t3 import T3Database

        client = make_vector_test_client()
        t3 = T3Database(_client=client, _ef_override=DefaultEmbeddingFunction())
        col = t3.get_or_create_collection("knowledge__ordertest2", strict=False)
        col.add(ids=["only"], documents=["text"], metadatas=[{"k": "v"}])
        result = t3.get_embeddings("knowledge__ordertest2", ["only", "absent"])
        assert result.shape[0] == 1
