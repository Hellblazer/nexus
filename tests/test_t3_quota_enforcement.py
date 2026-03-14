# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for T3Database quota enforcement: batching, semaphores, query validation (RDR-005)."""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, call, patch

import pytest

from nexus.db.t3 import T3Database


def _make_db_with_mock_col() -> tuple[T3Database, MagicMock]:
    """Return a T3Database wired to a single mock collection.

    The mock client's get_or_create_collection() always returns the same
    mock collection object, letting us inspect upsert/delete call counts.
    """
    mock_col = MagicMock()
    mock_col.count.return_value = 0

    mock_client = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col
    mock_client.get_collection.return_value = mock_col
    mock_client.list_collections.return_value = ["knowledge__test"]

    ef = MagicMock()
    db = T3Database(_client=mock_client, _ef_override=ef)
    return db, mock_col


# ── Phase 2: auto-batching writes ────────────────────────────────────────────

def test_upsert_chunks_300_records_makes_one_upsert_call() -> None:
    """300 records (at the limit) should be a single upsert() call."""
    db, mock_col = _make_db_with_mock_col()
    n = 300
    db.upsert_chunks(
        collection="knowledge__test",
        ids=[f"id-{i}" for i in range(n)],
        documents=["doc"] * n,
        metadatas=[{}] * n,
    )
    assert mock_col.upsert.call_count == 1
    _, kwargs = mock_col.upsert.call_args
    assert len(kwargs["ids"]) == 300


def test_upsert_chunks_301_records_makes_two_upsert_calls() -> None:
    """301 records should be split into two upsert() calls: 300 + 1."""
    db, mock_col = _make_db_with_mock_col()
    n = 301
    db.upsert_chunks(
        collection="knowledge__test",
        ids=[f"id-{i}" for i in range(n)],
        documents=["doc"] * n,
        metadatas=[{}] * n,
    )
    assert mock_col.upsert.call_count == 2
    first_call_ids = mock_col.upsert.call_args_list[0][1]["ids"]
    second_call_ids = mock_col.upsert.call_args_list[1][1]["ids"]
    assert len(first_call_ids) == 300
    assert len(second_call_ids) == 1


def test_upsert_chunks_with_embeddings_5000_records_makes_17_upsert_calls() -> None:
    """5000 records (migration scenario) → ceil(5000/300) = 17 upsert() calls."""
    db, mock_col = _make_db_with_mock_col()
    n = 5_000
    db.upsert_chunks_with_embeddings(
        collection_name="knowledge__test",
        ids=[f"id-{i}" for i in range(n)],
        documents=["doc"] * n,
        embeddings=[[0.1, 0.2]] * n,
        metadatas=[{}] * n,
    )
    assert mock_col.upsert.call_count == 17  # ceil(5000/300) = 17


def test_upsert_chunks_raises_record_too_large_before_any_network_call() -> None:
    """An oversized document raises RecordTooLarge; upsert() is never called."""
    from nexus.db.chroma_quotas import RecordTooLarge, QUOTAS
    db, mock_col = _make_db_with_mock_col()
    oversized_doc = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)

    with pytest.raises(RecordTooLarge):
        db.upsert_chunks(
            collection="knowledge__test",
            ids=["id-0"],
            documents=[oversized_doc],
            metadatas=[{}],
        )

    mock_col.upsert.assert_not_called()


def test_upsert_chunks_raises_name_too_long_for_oversized_id() -> None:
    """An ID over 128 bytes raises NameTooLong; upsert() is never called."""
    from nexus.db.chroma_quotas import NameTooLong, QUOTAS
    db, mock_col = _make_db_with_mock_col()
    long_id = "a" * (QUOTAS.MAX_ID_BYTES + 1)

    with pytest.raises(NameTooLong):
        db.upsert_chunks(
            collection="knowledge__test",
            ids=[long_id],
            documents=["ok"],
            metadatas=[{}],
        )

    mock_col.upsert.assert_not_called()


def test_delete_batch_500_ids_makes_two_delete_calls() -> None:
    """delete_by_source() with 500 IDs → paginated get(), then 2 delete() calls (300 + 200)."""
    db, mock_col = _make_db_with_mock_col()
    # Use side_effect so each col.get() call returns a fresh page (not the same 500 IDs).
    # page1: 300 IDs (full page → continue); page2: 200 IDs (short page → stop).
    page1 = {"ids": [f"id-{i}" for i in range(300)]}
    page2 = {"ids": [f"id-{i}" for i in range(300, 500)]}
    mock_col.get.side_effect = [page1, page2]

    deleted = db.delete_by_source(collection_name="knowledge__test", source_path="/some/file.py")

    assert mock_col.get.call_count == 2
    assert mock_col.delete.call_count == 2
    first_ids = mock_col.delete.call_args_list[0][1]["ids"]
    second_ids = mock_col.delete.call_args_list[1][1]["ids"]
    assert len(first_ids) == 300
    assert len(second_ids) == 200
    assert deleted == 500


def test_update_chunks_400_records_makes_two_update_calls() -> None:
    """update_chunks() with 400 records → 2 update() calls (300 + 100)."""
    db, mock_col = _make_db_with_mock_col()
    n = 400
    db.update_chunks(
        collection="knowledge__test",
        ids=[f"id-{i}" for i in range(n)],
        metadatas=[{"frecency_score": 1.0}] * n,
    )
    assert mock_col.update.call_count == 2
    assert len(mock_col.update.call_args_list[0][1]["ids"]) == 300
    assert len(mock_col.update.call_args_list[1][1]["ids"]) == 100


# ── Phase 2: expire() paginated get ──────────────────────────────────────────

def test_expire_processes_more_than_300_expired_records() -> None:
    """expire() must paginate col.get() to handle >300 TTL entries."""
    from datetime import UTC, datetime, timedelta
    db, mock_col = _make_db_with_mock_col()

    now = datetime.now(UTC)
    past = (now - timedelta(days=1)).isoformat()
    n_expired = 450

    # First page: 300 results; second page: 150 results; third page: empty
    page1 = {
        "ids": [f"id-{i}" for i in range(300)],
        "metadatas": [{"ttl_days": 1, "expires_at": past}] * 300,
    }
    page2 = {
        "ids": [f"id-{i}" for i in range(300, 450)],
        "metadatas": [{"ttl_days": 1, "expires_at": past}] * 150,
    }
    page3 = {"ids": [], "metadatas": []}

    mock_col.get.side_effect = [page1, page2, page3]
    mock_col.count.return_value = n_expired

    # Need to wire list_collections to return the mock collection
    db._client.list_collections.return_value = [MagicMock(name="knowledge__test")]

    total = db.expire()

    assert total == n_expired
    # All IDs should be deleted; accumulated first, then delete_batch called
    all_deleted_ids: list[str] = []
    for c in mock_col.delete.call_args_list:
        all_deleted_ids.extend(c[1]["ids"])
    assert len(all_deleted_ids) == n_expired


def test_expire_accumulates_then_deletes_not_interleaved() -> None:
    """expire() must accumulate all IDs first, then call delete — not interleave."""
    from datetime import UTC, datetime, timedelta
    db, mock_col = _make_db_with_mock_col()

    now = datetime.now(UTC)
    past = (now - timedelta(days=1)).isoformat()

    # page1: 300 IDs (full page → continue); page2: 50 IDs (short page → stop).
    page1 = {
        "ids": [f"id-{i}" for i in range(300)],
        "metadatas": [{"ttl_days": 1, "expires_at": past}] * 300,
    }
    page2 = {
        "ids": [f"id-{i}" for i in range(300, 350)],
        "metadatas": [{"ttl_days": 1, "expires_at": past}] * 50,
    }
    mock_col.get.side_effect = [page1, page2]

    db._client.list_collections.return_value = [MagicMock(name="knowledge__test")]

    db.expire()

    # Verify ordering: all get() calls must precede all delete() calls.
    call_names = [c[0] for c in mock_col.method_calls]
    get_indices = [i for i, name in enumerate(call_names) if name == "get"]
    delete_indices = [i for i, name in enumerate(call_names) if name == "delete"]
    assert len(get_indices) >= 2, "Expected multiple get() calls for pagination"
    assert delete_indices, "Expected at least one delete() call"
    assert max(get_indices) < min(delete_indices), (
        "All get() calls must precede all delete() calls"
    )


# ── Phase 3: query validation and n_results clamping ─────────────────────────

def test_search_clamps_n_results_to_300_and_warns(caplog) -> None:
    """search() with n_results > 300 should clamp to 300 and emit a warning."""
    import logging
    db, mock_col = _make_db_with_mock_col()
    mock_col.count.return_value = 500  # non-empty collection

    # Mock query to return empty results
    mock_col.query.return_value = {
        "ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]
    }

    db.search(query="test", collection_names=["knowledge__test"], n_results=500)

    # Verify query was called with n_results ≤ 300
    assert mock_col.query.called
    call_kwargs = mock_col.query.call_args[1]
    assert call_kwargs["n_results"] <= 300


def test_list_store_clamps_limit_to_300() -> None:
    """list_store() with limit > 300 should clamp to 300."""
    from nexus.db.chroma_quotas import QUOTAS
    db, mock_col = _make_db_with_mock_col()
    mock_col.get.return_value = {"ids": [], "metadatas": []}

    db.list_store(collection="knowledge__test", limit=999)

    assert mock_col.get.called
    call_kwargs = mock_col.get.call_args[1]
    assert call_kwargs["limit"] <= QUOTAS.MAX_QUERY_RESULTS


# ── Phase 3: concurrency semaphores ──────────────────────────────────────────

def test_write_semaphore_limits_concurrent_upserts_to_10() -> None:
    """Concurrent upsert_chunks() calls on same collection are bounded to 10."""
    from nexus.db.chroma_quotas import QUOTAS

    active_at_once: list[int] = []
    active_count = 0
    lock = threading.Lock()

    original_upsert = None

    def counting_upsert(**kwargs):
        nonlocal active_count
        with lock:
            active_count += 1
            active_at_once.append(active_count)
        time.sleep(0.05)  # hold the semaphore briefly
        with lock:
            active_count -= 1

    db, mock_col = _make_db_with_mock_col()
    mock_col.upsert.side_effect = counting_upsert

    threads = []
    for i in range(15):
        t = threading.Thread(
            target=db.upsert_chunks,
            kwargs={
                "collection": "knowledge__test",
                "ids": [f"t{i}-id-0"],
                "documents": ["doc"],
                "metadatas": [{}],
            },
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max(active_at_once) <= QUOTAS.MAX_CONCURRENT_WRITES, (
        f"Max concurrent writes was {max(active_at_once)}, expected ≤ {QUOTAS.MAX_CONCURRENT_WRITES}"
    )


def test_read_semaphore_limits_concurrent_reads_to_10() -> None:
    """Concurrent list_store() calls on same collection are bounded to 10 reads."""
    from nexus.db.chroma_quotas import QUOTAS

    active_at_once: list[int] = []
    active_count = 0
    lock = threading.Lock()

    def counting_get(**kwargs):
        nonlocal active_count
        with lock:
            active_count += 1
            active_at_once.append(active_count)
        time.sleep(0.05)
        with lock:
            active_count -= 1
        return {"ids": [], "metadatas": []}

    db, mock_col = _make_db_with_mock_col()
    mock_col.get.side_effect = counting_get

    threads = []
    for _ in range(15):
        t = threading.Thread(
            target=db.list_store,
            kwargs={"collection": "knowledge__test"},
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max(active_at_once) <= QUOTAS.MAX_CONCURRENT_READS, (
        f"Max concurrent reads was {max(active_at_once)}, expected ≤ {QUOTAS.MAX_CONCURRENT_READS}"
    )


def test_put_write_semaphore_limits_concurrent_puts_to_10() -> None:
    """Concurrent put() calls on same collection are bounded to 10 concurrent writes."""
    from nexus.db.chroma_quotas import QUOTAS

    active_at_once: list[int] = []
    active_count = 0
    lock = threading.Lock()

    def counting_upsert(**kwargs):
        nonlocal active_count
        with lock:
            active_count += 1
            active_at_once.append(active_count)
        time.sleep(0.05)
        with lock:
            active_count -= 1

    db, mock_col = _make_db_with_mock_col()
    mock_col.upsert.side_effect = counting_upsert

    threads = []
    for i in range(15):
        t = threading.Thread(
            target=db.put,
            kwargs={
                "collection": "knowledge__test",
                "content": f"content {i}",
                "title": f"title {i}",
            },
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert max(active_at_once) <= QUOTAS.MAX_CONCURRENT_WRITES, (
        f"Max concurrent writes was {max(active_at_once)}, expected ≤ {QUOTAS.MAX_CONCURRENT_WRITES}"
    )


# ── _write_batch last-resort drop-and-warn guard ──────────────────────────────

def test_write_batch_drops_oversized_document() -> None:
    """_write_batch silently drops documents exceeding MAX_DOCUMENT_BYTES."""
    import chromadb
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    from nexus.db.chroma_quotas import QUOTAS

    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    col = db.get_or_create_collection("code__test_oversized")

    oversized_doc = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
    normal_doc = "hello world"

    db._write_batch(
        col, "code__test_oversized",
        ids=["oversized-1", "normal-1"],
        documents=[oversized_doc, normal_doc],
        metadatas=[{"source_path": "big.py"}, {"source_path": "small.py"}],
    )
    result = col.get(ids=["normal-1"])
    assert len(result["ids"]) == 1
    assert result["ids"][0] == "normal-1"

    result = col.get(ids=["oversized-1"])
    assert len(result["ids"]) == 0


def test_write_batch_passes_valid_documents() -> None:
    """_write_batch upserts all documents within the byte limit."""
    import chromadb
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    col = db.get_or_create_collection("code__test_valid")

    docs = [f"doc content {i}" for i in range(5)]
    ids = [f"id-{i}" for i in range(5)]
    metas = [{"source_path": f"file{i}.py"} for i in range(5)]

    db._write_batch(col, "code__test_valid", ids=ids, documents=docs, metadatas=metas)

    result = col.get(ids=ids)
    assert len(result["ids"]) == 5


def test_write_batch_drops_all_oversized_returns_early() -> None:
    """_write_batch with only oversized docs does nothing (no upsert call)."""
    import chromadb
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
    from nexus.db.chroma_quotas import QUOTAS

    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    col = db.get_or_create_collection("code__test_all_oversized")

    oversized = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
    db._write_batch(
        col, "code__test_all_oversized",
        ids=["big-1", "big-2"],
        documents=[oversized, oversized],
        metadatas=[{"source_path": "a.py"}, {"source_path": "b.py"}],
    )
    result = col.get()
    assert len(result["ids"]) == 0
