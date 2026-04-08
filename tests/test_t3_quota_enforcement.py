# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import chromadb
import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from nexus.db.chroma_quotas import QUOTAS, NameTooLong, RecordTooLarge
from nexus.db.t3 import T3Database


def _make_db_with_mock_col() -> tuple[T3Database, MagicMock]:
    mock_col = MagicMock()
    mock_col.count.return_value = 0
    mock_client = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col
    mock_client.get_collection.return_value = mock_col
    mock_client.list_collections.return_value = ["knowledge__test"]
    db = T3Database(_client=mock_client, _ef_override=MagicMock())
    return db, mock_col


def _make_real_db(col_name: str) -> tuple[T3Database, object]:
    client = chromadb.EphemeralClient()
    ef = DefaultEmbeddingFunction()
    db = T3Database(_client=client, _ef_override=ef)
    col = db.get_or_create_collection(col_name)
    return db, col


# ── Auto-batching writes ────────────────────────────────────────────────────

@pytest.mark.parametrize("n,expected_calls", [
    (300, 1),
    (301, 2),
])
def test_upsert_chunks_batching(n, expected_calls) -> None:
    db, mock_col = _make_db_with_mock_col()
    db.upsert_chunks(
        collection="knowledge__test",
        ids=[f"id-{i}" for i in range(n)],
        documents=["doc"] * n,
        metadatas=[{}] * n,
    )
    assert mock_col.upsert.call_count == expected_calls


def test_upsert_chunks_301_split_sizes() -> None:
    db, mock_col = _make_db_with_mock_col()
    db.upsert_chunks(
        collection="knowledge__test",
        ids=[f"id-{i}" for i in range(301)],
        documents=["doc"] * 301,
        metadatas=[{}] * 301,
    )
    assert len(mock_col.upsert.call_args_list[0][1]["ids"]) == 300
    assert len(mock_col.upsert.call_args_list[1][1]["ids"]) == 1


def test_upsert_chunks_with_embeddings_5000_makes_17_calls() -> None:
    db, mock_col = _make_db_with_mock_col()
    n = 5_000
    db.upsert_chunks_with_embeddings(
        collection_name="knowledge__test",
        ids=[f"id-{i}" for i in range(n)],
        documents=["doc"] * n,
        embeddings=[[0.1, 0.2]] * n,
        metadatas=[{}] * n,
    )
    assert mock_col.upsert.call_count == 17  # ceil(5000/300)


@pytest.mark.parametrize("make_id,make_doc,exc_type", [
    (lambda: "id-0", lambda: "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1), RecordTooLarge),
    (lambda: "a" * (QUOTAS.MAX_ID_BYTES + 1), lambda: "ok", NameTooLong),
])
def test_upsert_chunks_validation_rejects_before_network(make_id, make_doc, exc_type) -> None:
    db, mock_col = _make_db_with_mock_col()
    with pytest.raises(exc_type):
        db.upsert_chunks(
            collection="knowledge__test",
            ids=[make_id()], documents=[make_doc()], metadatas=[{}],
        )
    mock_col.upsert.assert_not_called()


def test_delete_batch_500_ids_makes_two_delete_calls() -> None:
    db, mock_col = _make_db_with_mock_col()
    mock_col.get.side_effect = [
        {"ids": [f"id-{i}" for i in range(300)]},
        {"ids": [f"id-{i}" for i in range(300, 500)]},
    ]
    deleted = db.delete_by_source(collection_name="knowledge__test", source_path="/some/file.py")
    assert mock_col.delete.call_count == 2
    assert len(mock_col.delete.call_args_list[0][1]["ids"]) == 300
    assert len(mock_col.delete.call_args_list[1][1]["ids"]) == 200
    assert deleted == 500


def test_update_chunks_400_records_makes_two_calls() -> None:
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


# ── expire() paginated get ──────────────────────────────────────────────────

def _make_expire_pages(n_expired: int, past: str) -> list[dict]:
    pages = []
    for start in range(0, n_expired, 300):
        end = min(start + 300, n_expired)
        pages.append({
            "ids": [f"id-{i}" for i in range(start, end)],
            "metadatas": [{"ttl_days": 1, "expires_at": past}] * (end - start),
        })
    pages.append({"ids": [], "metadatas": []})
    return pages


def test_expire_processes_more_than_300_expired_records() -> None:
    from datetime import UTC, datetime, timedelta
    db, mock_col = _make_db_with_mock_col()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    mock_col.get.side_effect = _make_expire_pages(450, past)
    mock_col.count.return_value = 450
    db._client.list_collections.return_value = [MagicMock(name="knowledge__test")]
    total = db.expire()
    assert total == 450
    all_deleted = [id for c in mock_col.delete.call_args_list for id in c[1]["ids"]]
    assert len(all_deleted) == 450


def test_expire_accumulates_then_deletes_not_interleaved() -> None:
    from datetime import UTC, datetime, timedelta
    db, mock_col = _make_db_with_mock_col()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    mock_col.get.side_effect = [
        {"ids": [f"id-{i}" for i in range(300)], "metadatas": [{"ttl_days": 1, "expires_at": past}] * 300},
        {"ids": [f"id-{i}" for i in range(300, 350)], "metadatas": [{"ttl_days": 1, "expires_at": past}] * 50},
    ]
    db._client.list_collections.return_value = [MagicMock(name="knowledge__test")]
    db.expire()
    call_names = [c[0] for c in mock_col.method_calls]
    get_indices = [i for i, name in enumerate(call_names) if name == "get"]
    delete_indices = [i for i, name in enumerate(call_names) if name == "delete"]
    assert len(get_indices) >= 2
    assert delete_indices
    assert max(get_indices) < min(delete_indices)


# ── Query validation and clamping ───────────────────────────────────────────

def test_search_clamps_n_results_to_300(caplog) -> None:
    db, mock_col = _make_db_with_mock_col()
    mock_col.count.return_value = 500
    mock_col.query.return_value = {"ids": [[]], "documents": [[]], "metadatas": [[]], "distances": [[]]}
    db.search(query="test", collection_names=["knowledge__test"], n_results=500)
    assert mock_col.query.call_args[1]["n_results"] <= 300


def test_list_store_clamps_limit_to_300() -> None:
    db, mock_col = _make_db_with_mock_col()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    db.list_store(collection="knowledge__test", limit=999)
    assert mock_col.get.call_args[1]["limit"] <= QUOTAS.MAX_QUERY_RESULTS


# ── Concurrency semaphores ──────────────────────────────────────────────────

def _run_concurrent(target, n_threads=15, thread_kwargs_fn=None):
    active_at_once: list[int] = []
    active_count = 0
    lock = threading.Lock()

    def counting_wrapper(original_fn):
        def wrapper(**kwargs):
            nonlocal active_count
            with lock:
                active_count += 1
                active_at_once.append(active_count)
            time.sleep(0.05)
            with lock:
                active_count -= 1
            return original_fn(**kwargs) if original_fn else None
        return wrapper

    return active_at_once, counting_wrapper


@pytest.mark.parametrize("method,make_kwargs,mock_attr", [
    ("upsert_chunks", lambda i: {"collection": "knowledge__test", "ids": [f"t{i}-id-0"], "documents": ["doc"], "metadatas": [{}]}, "upsert"),
    ("put", lambda i: {"collection": "knowledge__test", "content": f"content {i}", "title": f"title {i}"}, "upsert"),
])
def test_write_semaphore_limits_concurrent_writes(method, make_kwargs, mock_attr) -> None:
    active_at_once: list[int] = []
    active_count = 0
    lock = threading.Lock()

    def counting_fn(**kwargs):
        nonlocal active_count
        with lock:
            active_count += 1
            active_at_once.append(active_count)
        time.sleep(0.05)
        with lock:
            active_count -= 1

    db, mock_col = _make_db_with_mock_col()
    getattr(mock_col, mock_attr).side_effect = counting_fn
    threads = [
        threading.Thread(target=getattr(db, method), kwargs=make_kwargs(i))
        for i in range(15)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(active_at_once) <= QUOTAS.MAX_CONCURRENT_WRITES


def test_read_semaphore_limits_concurrent_reads() -> None:
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
    threads = [threading.Thread(target=db.list_store, kwargs={"collection": "knowledge__test"}) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert max(active_at_once) <= QUOTAS.MAX_CONCURRENT_READS


# ── _write_batch drop-and-warn guard ────────────────────────────────────────

@pytest.mark.parametrize("docs,ids,expected_stored", [
    (["x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1), "hello world"], ["oversized-1", "normal-1"], ["normal-1"]),
    ([f"doc content {i}" for i in range(5)], [f"id-{i}" for i in range(5)], [f"id-{i}" for i in range(5)]),
    (["x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)] * 2, ["big-1", "big-2"], []),
])
def test_write_batch_filtering(docs, ids, expected_stored) -> None:
    col_name = f"code__test_{len(expected_stored)}"
    db, col = _make_real_db(col_name)
    metas = [{"source_path": f"f{i}.py"} for i in range(len(ids))]
    db._write_batch(col, col_name, ids=ids, documents=docs, metadatas=metas)
    result = col.get(ids=ids) if expected_stored else col.get()
    assert sorted(result["ids"]) == sorted(expected_stored)
