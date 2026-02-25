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
    """code__ collections use voyage-4 at query time (universal query model)."""
    chromadb_m, mock_client = mock_chromadb
    db = T3Database(tenant="t", database="d", api_key="key", voyage_api_key="vkey")
    db.get_or_create_collection("code__myrepo")
    chromadb_m.utils.embedding_functions.VoyageAIEmbeddingFunction.assert_called_with(
        model_name="voyage-4", api_key="vkey"
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
    assert meta["embedding_model"] == "voyage-4"  # knowledge__ always uses voyage-4


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


# ── nexus-5n4: make_t3() factory ──────────────────────────────────────────────

def test_make_t3_returns_t3database(mock_chromadb: tuple) -> None:
    """make_t3() returns a T3Database instance."""
    from nexus.db import make_t3

    with patch("nexus.db.get_credential", side_effect=lambda k: f"val-{k}"):
        db = make_t3()

    assert isinstance(db, T3Database)


def test_make_t3_uses_credentials(mock_chromadb: tuple) -> None:
    """make_t3() passes all four credentials to T3Database."""
    from nexus.db import make_t3

    creds = {
        "chroma_tenant": "my-tenant",
        "chroma_database": "my-db",
        "chroma_api_key": "ck-abc",
        "voyage_api_key": "vk-xyz",
    }
    with patch("nexus.db.get_credential", side_effect=lambda k: creds.get(k, "")):
        db = make_t3()

    mock_chromadb[0].CloudClient.assert_called_once_with(
        tenant="my-tenant", database="my-db", api_key="ck-abc"
    )
    assert db._voyage_api_key == "vk-xyz"


def test_make_t3_client_injection(mock_chromadb: tuple) -> None:
    """make_t3(_client=...) injects a test client, bypassing CloudClient."""
    import chromadb as _chromadb
    from nexus.db import make_t3

    fake_client = MagicMock()
    with patch("nexus.db.get_credential", return_value="x"):
        db = make_t3(_client=fake_client)

    # CloudClient should NOT have been called because _client was injected
    mock_chromadb[0].CloudClient.assert_not_called()
    assert db._client is fake_client


# ── nexus-tyo: upsert_chunks() ────────────────────────────────────────────────

def test_upsert_chunks_calls_col_upsert(mock_chromadb: tuple) -> None:
    """upsert_chunks() calls col.upsert with the provided ids/docs/metadatas."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    db.upsert_chunks(
        collection="code__myrepo",
        ids=["id-1", "id-2"],
        documents=["chunk one", "chunk two"],
        metadatas=[{"title": "f.py:1-10"}, {"title": "f.py:11-20"}],
    )

    mock_col.upsert.assert_called_once_with(
        ids=["id-1", "id-2"],
        documents=["chunk one", "chunk two"],
        metadatas=[{"title": "f.py:1-10"}, {"title": "f.py:11-20"}],
    )


def test_upsert_chunks_passes_all_metadata_fields(mock_chromadb: tuple) -> None:
    """upsert_chunks() does not truncate or filter metadata — all fields pass through."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    rich_meta = {
        "title": "f.py:1-5",
        "tags": "py",
        "category": "code",
        "session_id": "",
        "source_agent": "nexus-indexer",
        "store_type": "code",
        "indexed_at": "2026-01-01T00:00:00+00:00",
        "expires_at": "",
        "ttl_days": 0,
        "source_path": "src/foo.py",
        "start_line": 1,
        "end_line": 5,
        "frecency_score": 0.42,
    }
    db.upsert_chunks(
        collection="code__myrepo",
        ids=["abc123"],
        documents=["def foo(): pass"],
        metadatas=[rich_meta],
    )

    call_kwargs = mock_col.upsert.call_args.kwargs
    assert call_kwargs["metadatas"][0] == rich_meta


def test_upsert_chunks_uses_correct_embedding_fn(mock_chromadb: tuple) -> None:
    """upsert_chunks() routes through get_or_create_collection for embedding selection."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vk")
    db.upsert_chunks(
        collection="docs__corpus",
        ids=["d1"],
        documents=["some text"],
        metadatas=[{"source_path": "doc.pdf"}],
    )

    # Verify it went through get_or_create_collection (not a raw client call)
    mock_client.get_or_create_collection.assert_called_once()
    call_args = mock_client.get_or_create_collection.call_args
    assert call_args.args[0] == "docs__corpus"


# ── nexus-370: delete_by_source ───────────────────────────────────────────────

def test_delete_by_source_removes_correct_chunks(mock_chromadb: tuple) -> None:
    """delete_by_source() removes all chunks for the given source_path, leaving others intact."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    # Source A has 3 chunks; source B check is separate
    mock_col.get.return_value = {"ids": ["a1", "a2", "a3"]}
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.delete_by_source("code__myrepo", "src/foo.py")

    mock_col.get.assert_called_once_with(
        where={"source_path": "src/foo.py"}, include=[]
    )
    mock_col.delete.assert_called_once_with(ids=["a1", "a2", "a3"])
    assert count == 3


def test_delete_by_source_returns_count(mock_chromadb: tuple) -> None:
    """delete_by_source() returns the number of deleted chunks."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": ["x1", "x2"]}
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    result = db.delete_by_source("knowledge__wiki", "notes/page.md")

    assert result == 2


def test_delete_by_source_missing_source_returns_zero(mock_chromadb: tuple) -> None:
    """delete_by_source() returns 0 and does not call delete when no chunks match."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": []}
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    result = db.delete_by_source("code__myrepo", "nonexistent/file.py")

    assert result == 0
    mock_col.delete.assert_not_called()


# ── nexus-370: collection_metadata ───────────────────────────────────────────

def test_collection_metadata_returns_correct_fields(mock_chromadb: tuple) -> None:
    """collection_metadata() returns name, count, embedding_model, index_model."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.count.return_value = 17
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    meta = db.collection_metadata("docs__corpus")

    assert meta["name"] == "docs__corpus"
    assert meta["count"] == 17
    assert meta["embedding_model"] == "voyage-4"
    assert meta["index_model"] == "voyage-context-3"


def test_collection_metadata_code_collection(mock_chromadb: tuple) -> None:
    """collection_metadata() for code__: voyage-4 query model, voyage-code-3 index model."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.count.return_value = 5
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    meta = db.collection_metadata("code__myrepo")

    assert meta["embedding_model"] == "voyage-4"
    assert meta["index_model"] == "voyage-code-3"


# ── nexus-370: upsert_chunks_with_embeddings ─────────────────────────────────

def test_upsert_chunks_with_embeddings_stores_and_retrieves(mock_chromadb: tuple) -> None:
    """upsert_chunks_with_embeddings() passes pre-computed embeddings to col.upsert."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    ids = ["chunk-1", "chunk-2"]
    documents = ["first chunk text", "second chunk text"]
    metadatas = [{"source_path": "doc.pdf", "page": 1}, {"source_path": "doc.pdf", "page": 2}]

    db = T3Database(tenant="t", database="d", api_key="k")
    db.upsert_chunks_with_embeddings(
        collection_name="docs__corpus",
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )

    mock_col.upsert.assert_called_once_with(
        ids=ids,
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
    )


def test_upsert_chunks_with_embeddings_uses_get_or_create(mock_chromadb: tuple) -> None:
    """upsert_chunks_with_embeddings() routes through get_or_create_collection."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    db.upsert_chunks_with_embeddings(
        collection_name="knowledge__wiki",
        ids=["k1"],
        documents=["some doc"],
        embeddings=[[0.9, 0.1, 0.5]],
        metadatas=[{"source_path": "wiki/page.md"}],
    )

    mock_client.get_or_create_collection.assert_called_once()
    assert mock_client.get_or_create_collection.call_args.args[0] == "knowledge__wiki"


# ── update_chunks ─────────────────────────────────────────────────────────────

def test_update_chunks_calls_col_update_without_documents(mock_chromadb: tuple) -> None:
    """update_chunks() calls col.update with ids+metadatas only — no documents — preserving embeddings."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    db.update_chunks(
        collection="code__myrepo",
        ids=["id-1", "id-2"],
        metadatas=[
            {"frecency_score": 0.9, "source_path": "src/foo.py"},
            {"frecency_score": 0.9, "source_path": "src/foo.py"},
        ],
    )

    mock_col.update.assert_called_once_with(
        ids=["id-1", "id-2"],
        metadatas=[
            {"frecency_score": 0.9, "source_path": "src/foo.py"},
            {"frecency_score": 0.9, "source_path": "src/foo.py"},
        ],
    )
    # documents must NOT be passed — that would trigger re-embedding
    call_kwargs = mock_col.update.call_args.kwargs
    assert "documents" not in call_kwargs


# ── collection_info ──────────────────────────────────────────────────────────

def test_collection_info_returns_count_and_metadata(local_t3: T3Database) -> None:
    """collection_info() returns a dict with count and metadata after a put."""
    local_t3.put(
        collection="knowledge__info_test",
        content="Some knowledge content",
        title="info-doc.md",
        tags="test",
    )

    info = local_t3.collection_info("knowledge__info_test")

    assert info["count"] == 1
    assert isinstance(info["metadata"], dict)


def test_collection_info_nonexistent_collection_raises(local_t3: T3Database) -> None:
    """collection_info() raises an error for a non-existent collection."""
    with pytest.raises(Exception):
        local_t3.collection_info("knowledge__does_not_exist")


def test_collection_info_count_increases(local_t3: T3Database) -> None:
    """collection_info() count reflects the actual number of documents."""
    local_t3.put(
        collection="knowledge__count_test",
        content="first doc",
        title="doc-1.md",
    )
    local_t3.put(
        collection="knowledge__count_test",
        content="second doc",
        title="doc-2.md",
    )

    info = local_t3.collection_info("knowledge__count_test")
    assert info["count"] == 2


# ── list_store ───────────────────────────────────────────────────────────────

def test_list_store_returns_entries_with_metadata(local_t3: T3Database) -> None:
    """list_store() returns a list of dicts with id, title, tags, etc."""
    local_t3.put(
        collection="knowledge__ls_test",
        content="stored content",
        title="ls-doc.md",
        tags="alpha,beta",
        ttl_days=30,
    )

    entries = local_t3.list_store("knowledge__ls_test")

    assert len(entries) == 1
    entry = entries[0]
    assert "id" in entry
    assert entry["title"] == "ls-doc.md"
    assert entry["tags"] == "alpha,beta"
    assert entry["ttl_days"] == 30


def test_list_store_nonexistent_collection_returns_empty(local_t3: T3Database) -> None:
    """list_store() returns [] for a collection that does not exist."""
    result = local_t3.list_store("knowledge__no_such_coll")
    assert result == []


def test_list_store_multiple_entries(local_t3: T3Database) -> None:
    """list_store() returns all entries in the collection."""
    for i in range(3):
        local_t3.put(
            collection="knowledge__multi_ls",
            content=f"content {i}",
            title=f"doc-{i}.md",
        )

    entries = local_t3.list_store("knowledge__multi_ls")
    assert len(entries) == 3
    titles = {e["title"] for e in entries}
    assert titles == {"doc-0.md", "doc-1.md", "doc-2.md"}


# ── collection_exists ────────────────────────────────────────────────────────

def test_collection_exists_true_after_put(local_t3: T3Database) -> None:
    """collection_exists() returns True for a collection that has been created."""
    local_t3.put(
        collection="knowledge__exists_test",
        content="some content",
        title="exists.md",
    )

    assert local_t3.collection_exists("knowledge__exists_test") is True


def test_collection_exists_false_for_missing(local_t3: T3Database) -> None:
    """collection_exists() returns False for a non-existent collection."""
    assert local_t3.collection_exists("knowledge__never_created") is False


# ── delete_by_source with nonexistent collection ────────────────────────────

def test_delete_by_source_nonexistent_collection_returns_zero(mock_chromadb: tuple) -> None:
    """delete_by_source() returns 0 (not an exception) when collection doesn't exist."""
    chromadb_m, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError(
        "Collection not found"
    )

    db = T3Database(tenant="t", database="d", api_key="k")
    result = db.delete_by_source("code__nonexistent", "src/file.py")

    assert result == 0


# ── T3Database context manager ──────────────────────────────────────────────

def test_t3_context_manager_enter_returns_self(mock_chromadb: tuple) -> None:
    """T3Database used as a context manager returns itself from __enter__."""
    _, mock_client = mock_chromadb
    db = T3Database(tenant="t", database="d", api_key="k")

    with db as ctx:
        assert ctx is db


def test_t3_context_manager_works_end_to_end(mock_chromadb: tuple) -> None:
    """T3Database context manager can be used in a with-block and exits cleanly."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    with T3Database(tenant="t", database="d", api_key="k") as db:
        doc_id = db.put(
            collection="knowledge__cm_test",
            content="context manager test",
            title="cm.md",
        )
        assert isinstance(doc_id, str) and len(doc_id) > 0
    # No exception means __exit__ succeeded


# ── T3 expire guard edge cases ──────────────────────────────────────────────

def test_expire_preserves_entry_with_missing_expires_at(mock_chromadb: tuple) -> None:
    """Entry where expires_at is missing from metadata is preserved (not deleted)."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    # Entry has ttl_days > 0 but no expires_at key at all
    mock_col.get.return_value = {
        "ids": ["id-no-expires"],
        "metadatas": [{"ttl_days": 30}],  # no "expires_at" key
    }
    mock_client.list_collections.return_value = ["knowledge__sec"]
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.expire()

    assert count == 0
    mock_col.delete.assert_not_called()


def test_expire_preserves_entry_with_empty_expires_at(mock_chromadb: tuple) -> None:
    """Entry with expires_at="" (permanent sentinel) is preserved even if ttl_days > 0."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["id-perm"],
        "metadatas": [{"expires_at": "", "ttl_days": 30}],
    }
    mock_client.list_collections.return_value = ["knowledge__sec"]
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.expire()

    assert count == 0
    mock_col.delete.assert_not_called()


def test_expire_preserves_future_entry(mock_chromadb: tuple) -> None:
    """Entry whose expires_at is in the future is preserved."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    future = "2099-12-31T23:59:59+00:00"
    mock_col.get.return_value = {
        "ids": ["id-future"],
        "metadatas": [{"expires_at": future, "ttl_days": 30}],
    }
    mock_client.list_collections.return_value = ["knowledge__sec"]
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.expire()

    assert count == 0
    mock_col.delete.assert_not_called()


def test_expire_mixed_expired_and_permanent(mock_chromadb: tuple) -> None:
    """Only expired entries are deleted; permanent and future entries survive."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    past = "2020-01-01T00:00:00+00:00"
    future = "2099-12-31T23:59:59+00:00"
    mock_col.get.return_value = {
        "ids": ["expired-1", "perm-1", "future-1"],
        "metadatas": [
            {"expires_at": past, "ttl_days": 30},
            {"expires_at": "", "ttl_days": 30},
            {"expires_at": future, "ttl_days": 30},
        ],
    }
    mock_client.list_collections.return_value = ["knowledge__sec"]
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    count = db.expire()

    assert count == 1
    mock_col.delete.assert_called_once_with(ids=["expired-1"])


# ── T3 deterministic ID edge cases ──────────────────────────────────────────

def test_put_empty_title_collision(mock_chromadb: tuple) -> None:
    """Two puts with empty title in same collection produce the same ID (collision)."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    id1 = db.put(collection="knowledge__sec", content="first", title="")
    id2 = db.put(collection="knowledge__sec", content="second", title="")

    assert id1 == id2  # same hash → upsert overwrites


def test_put_same_title_different_collection_different_ids(mock_chromadb: tuple) -> None:
    """Same title in different collections produces different IDs."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    id1 = db.put(collection="knowledge__sec", content="text", title="shared.md")
    id2 = db.put(collection="knowledge__ops", content="text", title="shared.md")

    assert id1 != id2


# ── T3 collection_info edge cases ───────────────────────────────────────────

def test_collection_info_missing_raises_keyerror(mock_chromadb: tuple) -> None:
    """collection_info() raises KeyError for non-existent collection."""
    chromadb_m, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("not found")

    db = T3Database(tenant="t", database="d", api_key="k")
    with pytest.raises(KeyError, match="Collection not found"):
        db.collection_info("knowledge__missing")


def test_collection_metadata_missing_raises_keyerror(mock_chromadb: tuple) -> None:
    """collection_metadata() raises KeyError for non-existent collection."""
    chromadb_m, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("not found")

    db = T3Database(tenant="t", database="d", api_key="k")
    with pytest.raises(KeyError, match="Collection not found"):
        db.collection_metadata("knowledge__missing")


# ── T3 search with where filter ─────────────────────────────────────────────

def test_search_passes_where_filter(mock_chromadb: tuple) -> None:
    """search() passes the where filter to col.query."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.count.return_value = 5
    mock_col.query.return_value = {
        "ids": [["id-1"]],
        "documents": [["content"]],
        "metadatas": [[{"title": "t"}]],
        "distances": [[0.1]],
    }
    mock_client.get_collection.return_value = mock_col

    db = T3Database(tenant="t", database="d", api_key="k")
    where = {"source_agent": "indexer"}
    db.search("query", ["knowledge__sec"], where=where)

    call_kwargs = mock_col.query.call_args.kwargs
    assert call_kwargs["where"] == {"source_agent": "indexer"}


# ── T3 EF override ─────────────────────────────────────────────────────────

def test_ef_override_bypasses_cache(mock_chromadb: tuple) -> None:
    """When _ef_override is set, it's returned directly without caching."""
    _, mock_client = mock_chromadb
    override_ef = MagicMock(name="override")
    db = T3Database(tenant="t", database="d", api_key="k", _ef_override=override_ef)

    ef1 = db._embedding_fn("code__repo")
    ef2 = db._embedding_fn("knowledge__sec")

    assert ef1 is override_ef
    assert ef2 is override_ef
    assert db._ef_cache == {}  # cache not populated
