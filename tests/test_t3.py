"""AC1/AC3/AC4/AC5/AC7: T3Database CloudClient init, store, expire, search, collection list."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import chromadb.errors
import pytest

from nexus.db.t3 import T3Database


@pytest.fixture
def mock_chromadb():
    """Patch chromadb at the t3 module level."""
    with patch("nexus.db.t3.chromadb") as m:
        mock_client = MagicMock()
        m.CloudClient.return_value = mock_client
        yield m, mock_client


# ── AC1: CloudClient init ─────────────────────────────────────────────────────

def test_cloudclient_init(mock_chromadb: tuple) -> None:
    """CloudClient receives the correct tenant, database, api_key arguments."""
    chromadb_m, _ = mock_chromadb
    T3Database(tenant="my-tenant", database="my-db", api_key="secret")
    chromadb_m.CloudClient.assert_called_once_with(
        tenant="my-tenant", database="my-db", api_key="secret"
    )


# ── AC2: VoyageAI embedding function selection ────────────────────────────────

def test_voyage_embedding_fn_code_collection(mock_chromadb: tuple) -> None:
    """code__ collections use voyage-code-3."""
    chromadb_m, mock_client = mock_chromadb
    db = T3Database(tenant="t", database="d", api_key="key", voyage_api_key="vkey")
    db.get_or_create_collection("code__myrepo")
    chromadb_m.utils.embedding_functions.VoyageAIEmbeddingFunction.assert_called_with(
        model_name="voyage-code-3", api_key="vkey"
    )


def test_voyage_embedding_fn_knowledge_collection(mock_chromadb: tuple) -> None:
    """knowledge__ collections use voyage-4."""
    chromadb_m, mock_client = mock_chromadb
    db = T3Database(tenant="t", database="d", api_key="key", voyage_api_key="vkey")
    db.get_or_create_collection("knowledge__security")
    chromadb_m.utils.embedding_functions.VoyageAIEmbeddingFunction.assert_called_with(
        model_name="voyage-4", api_key="vkey"
    )


# ── AC3: store file to knowledge__ collection ─────────────────────────────────

def test_store_put_permanent_returns_id(mock_chromadb: tuple) -> None:
    """put() returns a non-empty doc ID."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    doc_id = db.put(
        collection="knowledge__security",
        content="security finding text",
        title="sec.md",
        tags="security,audit",
    )
    assert isinstance(doc_id, str) and len(doc_id) > 0


def test_store_put_permanent_metadata(mock_chromadb: tuple) -> None:
    """Permanent put sets ttl_days=0, expires_at='', store_type='knowledge'."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    db.put(
        collection="knowledge__security",
        content="security finding text",
        title="sec.md",
        tags="security,audit",
        category="security",
        session_id="sess-001",
        source_agent="codebase-deep-analyzer",
    )

    mock_col.upsert.assert_called_once()
    meta = mock_col.upsert.call_args.kwargs["metadatas"][0]
    assert meta["title"] == "sec.md"
    assert meta["tags"] == "security,audit"
    assert meta["category"] == "security"
    assert meta["session_id"] == "sess-001"
    assert meta["source_agent"] == "codebase-deep-analyzer"
    assert meta["ttl_days"] == 0       # permanent
    assert meta["expires_at"] == ""    # permanent sentinel
    assert meta["store_type"] == "knowledge"


def test_store_put_with_ttl_metadata(mock_chromadb: tuple) -> None:
    """put() with ttl_days sets ttl_days > 0 and a valid ISO expires_at."""
    from datetime import UTC, datetime

    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    db.put(
        collection="knowledge__security",
        content="temp finding",
        title="temp.md",
        ttl_days=30,
    )

    meta = mock_col.upsert.call_args.kwargs["metadatas"][0]
    assert meta["ttl_days"] == 30
    assert meta["expires_at"] != ""
    expires = datetime.fromisoformat(meta["expires_at"])
    assert expires > datetime.now(UTC)


# ── AC4: expire guards permanent entries ──────────────────────────────────────

def test_expire_guards_permanent_entries(mock_chromadb: tuple) -> None:
    """expire() filters by ttl_days > 0 in ChromaDB; permanent entries have empty expires_at."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    # No TTL entries returned — permanent entries don't match ttl_days > 0
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    mock_client.list_collections.return_value = ["knowledge__security"]
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.expire()

    assert count == 0
    where = mock_col.get.call_args.kwargs["where"]
    # Must filter by ttl_days > 0 (numeric, so ChromaDB supports it)
    assert where == {"ttl_days": {"$gt": 0}}
    # No delete called — nothing expired
    mock_col.delete.assert_not_called()


def test_expire_deletes_expired_entries(mock_chromadb: tuple) -> None:
    """expire() deletes entries whose expires_at is in the past."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    past = "2020-01-01T00:00:00+00:00"
    mock_col.get.return_value = {
        "ids": ["id-1", "id-2"],
        "metadatas": [{"expires_at": past}, {"expires_at": past}],
    }
    mock_client.list_collections.return_value = ["knowledge__security"]
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.expire()

    assert count == 2
    mock_col.delete.assert_called_once_with(ids=["id-1", "id-2"])


def test_expire_skips_non_knowledge_collections(mock_chromadb: tuple) -> None:
    """expire() only processes knowledge__ collections."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    past = "2020-01-01T00:00:00+00:00"
    mock_col.get.return_value = {
        "ids": ["stale-id"],
        "metadatas": [{"expires_at": past}],
    }
    mock_client.list_collections.return_value = [
        "code__myrepo", "docs__papers", "knowledge__sec"
    ]
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.expire()

    mock_client.get_collection.assert_called_once_with("knowledge__sec")
    assert count == 1


# ── AC5: search single corpus ─────────────────────────────────────────────────

def test_search_single_corpus_results_ordered(mock_chromadb: tuple) -> None:
    """search() returns results from a single corpus sorted by distance."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.count.return_value = 3
    mock_col.query.return_value = {
        "ids": [["id-1", "id-2"]],
        "documents": [["content one", "content two"]],
        "metadatas": [[{"title": "t1", "tags": "x"}, {"title": "t2", "tags": "y"}]],
        "distances": [[0.1, 0.5]],
    }
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    results = db.search("my query", ["knowledge__security"], n_results=5)

    assert len(results) == 2
    assert results[0]["id"] == "id-1"
    assert results[0]["distance"] == 0.1
    assert results[1]["id"] == "id-2"


def test_search_caps_n_results_to_collection_count(mock_chromadb: tuple) -> None:
    """search() caps n_results to collection.count() to avoid chromadb error."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.count.return_value = 2  # only 2 docs in collection
    mock_col.query.return_value = {
        "ids": [["id-a", "id-b"]],
        "documents": [["doc a", "doc b"]],
        "metadatas": [[{}, {}]],
        "distances": [[0.2, 0.8]],
    }
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    db.search("query", ["knowledge__sec"], n_results=10)

    mock_col.query.assert_called_once_with(
        query_texts=["query"],
        n_results=2,  # capped to count
        include=["documents", "metadatas", "distances"],
    )


def test_search_empty_collection_returns_empty(mock_chromadb: tuple) -> None:
    """search() returns [] without querying when collection is empty."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.count.return_value = 0
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    results = db.search("query", ["knowledge__sec"], n_results=10)

    assert results == []
    mock_col.query.assert_not_called()


def test_search_skips_missing_collection_without_creating(mock_chromadb: tuple) -> None:
    """search() skips non-existent collections without creating them (read-only)."""
    chromadb_m, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError(
        "Collection not found"
    )

    db = T3Database(tenant="t", database="d", api_key="k")
    results = db.search("query", ["knowledge__missing"], n_results=10)

    assert results == []
    mock_client.get_or_create_collection.assert_not_called()


# ── AC7: collection list ──────────────────────────────────────────────────────

def test_list_collections_returns_names_and_counts(mock_chromadb: tuple) -> None:
    """list_collections() returns dicts with name and count."""
    _, mock_client = mock_chromadb
    mock_col1, mock_col2 = MagicMock(), MagicMock()
    mock_col1.count.return_value = 42
    mock_col2.count.return_value = 7
    mock_client.list_collections.return_value = ["code__myrepo", "knowledge__sec"]
    mock_client.get_collection.side_effect = [mock_col1, mock_col2]

    db = T3Database(tenant="t", database="d", api_key="k")
    result = db.list_collections()

    assert len(result) == 2
    by_name = {r["name"]: r for r in result}
    assert by_name["code__myrepo"]["count"] == 42
    assert by_name["knowledge__sec"]["count"] == 7


def test_list_collections_empty(mock_chromadb: tuple) -> None:
    _, mock_client = mock_chromadb
    mock_client.list_collections.return_value = []

    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.list_collections() == []


# ── Deterministic ID ──────────────────────────────────────────────────────────

def test_put_same_collection_and_title_produces_same_id(mock_chromadb: tuple) -> None:
    """Two put() calls with the same collection+title must produce the same document ID.

    This enables upsert semantics: repeated writes to the same logical document
    update in-place rather than accumulating duplicates.
    """
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    id1 = db.put(collection="knowledge__sec", content="first version", title="finding.md")
    id2 = db.put(collection="knowledge__sec", content="updated version", title="finding.md")

    assert id1 == id2
    # upsert called twice with the same ID → second call updates in place
    assert mock_col.upsert.call_count == 2
    assert mock_col.upsert.call_args_list[0].kwargs["ids"][0] == id1
    assert mock_col.upsert.call_args_list[1].kwargs["ids"][0] == id1


def test_put_different_titles_produce_different_ids(mock_chromadb: tuple) -> None:
    """Different titles within the same collection produce different document IDs."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    id_a = db.put(collection="knowledge__sec", content="chunk A", title="chunk-a.md")
    id_b = db.put(collection="knowledge__sec", content="chunk B", title="chunk-b.md")

    assert id_a != id_b


def test_put_id_is_deterministic_across_processes(mock_chromadb: tuple) -> None:
    """ID generation must not use Python hash() or uuid4() — must be process-stable."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    # Call twice: same collection+title, same content → same ID
    id1 = db.put(collection="code__repo", content="same text", title="file.py:1-50")
    id2 = db.put(collection="code__repo", content="same text", title="file.py:1-50")
    assert id1 == id2


# ── nexus-nmg: embedding function cache ──────────────────────────────────────

def test_embedding_fn_cached_per_collection_name(mock_chromadb: tuple) -> None:
    """_embedding_fn returns the same object on repeated calls for the same name."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    with patch("nexus.db.t3.chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction") as mock_ef_cls:
        mock_ef_cls.return_value = MagicMock(name="ef_instance")
        db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vk")

        ef1 = db._embedding_fn("knowledge__topic")
        ef2 = db._embedding_fn("knowledge__topic")

    assert ef1 is ef2
    assert mock_ef_cls.call_count == 1, "EmbeddingFunction should be constructed only once per name"


def test_embedding_fn_different_names_not_confused(mock_chromadb: tuple) -> None:
    """Different collection names get different (separately cached) embedding functions."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    ef_a = MagicMock(name="ef_code")
    ef_b = MagicMock(name="ef_knowledge")

    with patch("nexus.db.t3.chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction", side_effect=[ef_a, ef_b]) as mock_ef_cls:
        db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vk")

        r1 = db._embedding_fn("code__repo")
        r2 = db._embedding_fn("knowledge__topic")
        r3 = db._embedding_fn("code__repo")  # cache hit

    assert r1 is ef_a
    assert r2 is ef_b
    assert r3 is ef_a
    assert mock_ef_cls.call_count == 2  # only 2 constructions, not 3
