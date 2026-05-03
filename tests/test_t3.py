# SPDX-License-Identifier: AGPL-3.0-or-later
"""T3Database single-client init, store, expire, search, collection list."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import chromadb.errors
import pytest

from nexus.db.t3 import T3Database


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_chromadb():
    with patch("nexus.db.t3.chromadb") as m, \
         patch("nexus.db.t3.get_credential", return_value=""):
        mock_client = MagicMock()
        m.CloudClient.return_value = mock_client
        yield m, mock_client


@pytest.fixture
def mock_db(mock_chromadb):
    """Pre-built T3Database with standard mock wiring."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col
    mock_client.get_collection.return_value = mock_col
    db = T3Database(tenant="t", database="d", api_key="k")
    return db, mock_col, mock_client


@pytest.fixture
def mock_db_voyage(mock_chromadb):
    """Pre-built T3Database with voyage_api_key set."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col
    mock_client.get_collection.return_value = mock_col
    db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vkey")
    return db, mock_col, mock_client


@pytest.fixture
def expire_db(mock_chromadb):
    """T3Database wired for expire() tests with a single knowledge__ collection."""
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.list_collections.return_value = ["knowledge__sec"]
    mock_client.get_collection.return_value = mock_col
    db = T3Database(tenant="t", database="d", api_key="k")
    return db, mock_col


# ── AC1: CloudClient init ──────────────────────────────────────────────────


def test_cloudclient_init(mock_chromadb):
    chromadb_m, _ = mock_chromadb
    T3Database(tenant="my-tenant", database="my-db", api_key="secret")
    assert chromadb_m.CloudClient.call_count == 1
    call = chromadb_m.CloudClient.call_args
    assert call.kwargs["database"] == "my-db"
    assert call.kwargs["tenant"] == "my-tenant"
    assert call.kwargs["api_key"] == "secret"


def test_cloudclient_receives_none_for_empty_tenant(mock_chromadb):
    chromadb_m, _ = mock_chromadb
    T3Database(tenant="", database="mydb", api_key="key")
    for c in chromadb_m.CloudClient.call_args_list:
        assert c.kwargs["tenant"] is None


def test_bare_constructor_falls_back_to_get_credential(mock_chromadb):
    """Bare T3Database() (no args) must pick up credentials via
    nexus.config.get_credential — otherwise scripts and research
    probes hit 'Permission denied' from CloudClient auth because the
    constructor forwards empty strings downstream.

    Discovered 2026-04-17 during RDR-086 research; prior behaviour
    required every caller to know about make_t3() in nexus.db.__init__.
    """
    chromadb_m, _ = mock_chromadb

    fake = {
        "chroma_tenant": "tenant-from-config",
        "chroma_database": "db-from-config",
        "chroma_api_key": "key-from-config",
        "voyage_api_key": "voyage-key-from-config",
    }
    with patch(
        "nexus.db.t3.get_credential",
        side_effect=lambda k: fake.get(k, ""),
    ):
        T3Database()

    # CloudClient must have been called with the config values, not ""
    call = chromadb_m.CloudClient.call_args
    assert call.kwargs["tenant"] == "tenant-from-config"
    assert call.kwargs["database"] == "db-from-config"
    assert call.kwargs["api_key"] == "key-from-config"


def test_bare_constructor_explicit_args_override_fallback(mock_chromadb):
    """Explicit args still win over the config fallback."""
    chromadb_m, _ = mock_chromadb
    with patch(
        "nexus.db.t3.get_credential",
        side_effect=lambda k: "should-not-see-this" if k != "migrated" else "1",
    ):
        T3Database(tenant="explicit-t", database="explicit-db", api_key="explicit-k")

    call = chromadb_m.CloudClient.call_args
    assert call.kwargs["tenant"] == "explicit-t"
    assert call.kwargs["database"] == "explicit-db"
    assert call.kwargs["api_key"] == "explicit-k"


def test_fallback_skipped_when_client_injected(mock_chromadb):
    """Tests that pass _client= must not trigger the credential
    fallback — otherwise get_credential would get called on every
    test that injects an EphemeralClient or MagicMock."""
    from unittest.mock import MagicMock
    chromadb_m, _ = mock_chromadb
    sentinel = MagicMock()
    with patch(
        "nexus.db.t3.get_credential",
    ) as gc:
        T3Database(_client=sentinel)
    assert not gc.called, (
        "get_credential must not be called when _client is injected"
    )


def test_fallback_skipped_in_local_mode(tmp_path):
    """Local mode skips the credential fallback — no cloud creds needed."""
    from unittest.mock import patch, MagicMock
    with patch("nexus.db.t3.get_credential") as gc, \
         patch("chromadb.PersistentClient", return_value=MagicMock()):
        T3Database(local_mode=True, local_path=str(tmp_path))
    assert not gc.called


def test_cloud_init_uses_single_database(mock_chromadb):
    """Cloud init connects to the configured database directly — no probe.
    RDR-037 consolidation removed the legacy four-database layout; the
    transitional probe was retired once the migration window closed.
    """
    chromadb_m, _ = mock_chromadb
    db = T3Database(tenant="t", database="mydb", api_key="k")
    assert chromadb_m.CloudClient.call_count == 1
    assert chromadb_m.CloudClient.call_args.kwargs["database"] == "mydb"
    assert db._client is not None


def test_client_injection_sets_single_client(mock_chromadb):
    chromadb_m, _ = mock_chromadb
    injected = MagicMock(name="injected")
    db = T3Database(_client=injected)
    chromadb_m.CloudClient.assert_not_called()
    assert db._client is injected


# ── AC2: VoyageAI embedding function selection ──────────────────────────────


@pytest.mark.parametrize("collection,expected_model", [
    ("code__myrepo", "voyage-code-3"),
    ("knowledge__security", "voyage-context-3"),
])
def test_voyage_embedding_fn_selects_model(mock_chromadb, collection, expected_model):
    chromadb_m, _ = mock_chromadb
    db = T3Database(tenant="t", database="d", api_key="key", voyage_api_key="vkey")
    db.get_or_create_collection(collection)
    chromadb_m.utils.embedding_functions.VoyageAIEmbeddingFunction.assert_called_with(
        model_name=expected_model, api_key="vkey"
    )


# ── AC3: store put ──────────────────────────────────────────────────────────


def test_store_put_permanent_returns_id(mock_db):
    db, mock_col, _ = mock_db
    doc_id = db.put(collection="knowledge__security", content="text", title="sec.md", tags="security,audit")
    assert isinstance(doc_id, str) and len(doc_id) > 0


def test_store_put_permanent_metadata(mock_db):
    db, mock_col, _ = mock_db
    db.put(
        collection="knowledge__security", content="security finding text", title="sec.md",
        tags="security,audit", category="security", session_id="sess-001",
        source_agent="codebase-deep-analyzer",
    )
    meta = mock_col.upsert.call_args.kwargs["metadatas"][0]
    assert meta["title"] == "sec.md"
    assert meta["tags"] == "security,audit"
    assert meta["category"] == "security"
    assert meta["session_id"] == "sess-001"
    assert meta["source_agent"] == "codebase-deep-analyzer"
    assert meta["ttl_days"] == 0
    # expires_at removed; expiry derived from indexed_at + ttl_days
    assert "expires_at" not in meta
    assert meta["indexed_at"]  # ISO timestamp
    # RDR-101 Phase 5c (nexus-o6aa.13) dropped store_type from the schema;
    # content_type is the canonical routing field. ``knowledge__`` →
    # ``prose`` per _STORE_TYPE_TO_CONTENT_TYPE in db/t3.py.
    assert meta["content_type"] == "prose"
    assert "store_type" not in meta
    assert meta["embedding_model"] == "voyage-context-3"


def test_store_put_with_ttl_metadata(mock_db):
    db, mock_col, _ = mock_db
    db.put(collection="knowledge__security", content="temp finding", title="temp.md", ttl_days=30)
    meta = mock_col.upsert.call_args.kwargs["metadatas"][0]
    assert meta["ttl_days"] == 30
    # expires_at no longer stored; verify the inputs to is_expired() are present
    assert "expires_at" not in meta
    assert meta["indexed_at"]
    assert datetime.fromisoformat(meta["indexed_at"]) <= datetime.now(UTC)


# ── AC4: expire ─────────────────────────────────────────────────────────────


def test_expire_guards_permanent_entries(expire_db):
    db, mock_col = expire_db
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    count = db.expire()
    assert count == 0
    assert mock_col.get.call_args.kwargs["where"] == {"ttl_days": {"$gt": 0}}
    mock_col.delete.assert_not_called()


def test_expire_deletes_expired_entries(expire_db):
    db, mock_col = expire_db
    # Old fixtures: indexed 100 days ago with 30-day ttl → expired now.
    old_indexed = "2020-01-01T00:00:00+00:00"
    mock_col.get.return_value = {
        "ids": ["id-1", "id-2"],
        "metadatas": [
            {"indexed_at": old_indexed, "ttl_days": 30},
            {"indexed_at": old_indexed, "ttl_days": 30},
        ],
    }
    assert db.expire() == 2
    mock_col.delete.assert_called_once_with(ids=["id-1", "id-2"])


def test_expire_skips_non_knowledge_collections(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "ids": ["stale-id"],
        "metadatas": [{"indexed_at": "2020-01-01T00:00:00+00:00", "ttl_days": 30}],
    }
    mock_client.list_collections.return_value = ["code__myrepo", "docs__papers", "knowledge__sec"]
    mock_client.get_collection.return_value = mock_col
    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.expire() == 1
    mock_client.get_collection.assert_called_once_with("knowledge__sec")


@pytest.mark.parametrize("ids,metadatas,expected_count,desc", [
    (["id-no-indexed"], [{"ttl_days": 30}], 0, "missing indexed_at"),
    (["id-perm"], [{"indexed_at": "2020-01-01T00:00:00+00:00", "ttl_days": 0}], 0, "ttl=0 is permanent sentinel"),
    (["id-future"], [{"indexed_at": "2099-01-01T00:00:00+00:00", "ttl_days": 30}], 0, "indexed in the future"),
    (["id-fresh"], [{"indexed_at": datetime.now(UTC).isoformat(), "ttl_days": 30}], 0, "fresh entry not yet expired"),
])
def test_expire_preserves_non_expired(expire_db, ids, metadatas, expected_count, desc):
    db, mock_col = expire_db
    mock_col.get.return_value = {"ids": ids, "metadatas": metadatas}
    assert db.expire() == expected_count
    mock_col.delete.assert_not_called()


def test_expire_mixed_expired_and_permanent(expire_db):
    db, mock_col = expire_db
    old = "2020-01-01T00:00:00+00:00"
    fresh = datetime.now(UTC).isoformat()
    mock_col.get.return_value = {
        "ids": ["expired-1", "perm-1", "fresh-1"],
        "metadatas": [
            {"indexed_at": old, "ttl_days": 30},
            {"indexed_at": old, "ttl_days": 0},  # permanent (ttl_days=0)
            {"indexed_at": fresh, "ttl_days": 365},
        ],
    }
    assert db.expire() == 1
    mock_col.delete.assert_called_once_with(ids=["expired-1"])


# ── AC5: search ─────────────────────────────────────────────────────────────


def test_search_single_corpus_results_ordered(mock_db):
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 3
    mock_col.query.return_value = {
        "ids": [["id-1", "id-2"]], "documents": [["content one", "content two"]],
        "metadatas": [[{"title": "t1", "tags": "x"}, {"title": "t2", "tags": "y"}]],
        "distances": [[0.1, 0.5]],
    }
    results = db.search("my query", ["knowledge__security"], n_results=5)
    assert len(results) == 2
    assert results[0]["id"] == "id-1"
    assert results[0]["distance"] == 0.1
    assert results[1]["id"] == "id-2"


def test_search_caps_n_results_to_collection_count(mock_db):
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 2
    mock_col.query.return_value = {
        "ids": [["id-a", "id-b"]], "documents": [["doc a", "doc b"]],
        "metadatas": [[{}, {}]], "distances": [[0.2, 0.8]],
    }
    db.search("query", ["knowledge__sec"], n_results=10)
    mock_col.query.assert_called_once_with(
        query_texts=["query"], n_results=2, include=["documents", "metadatas", "distances"],
    )


def test_search_empty_collection_returns_empty(mock_db):
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 0
    results = db.search("query", ["knowledge__sec"], n_results=10)
    assert results == []
    mock_col.query.assert_not_called()


def test_search_skips_missing_collection_without_creating(mock_chromadb):
    chromadb_m, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("Collection not found")
    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.search("query", ["knowledge__missing"], n_results=10) == []
    mock_client.get_or_create_collection.assert_not_called()


# ── Dimension-mismatch guard (issue #190 follow-up) ─────────────────────────


def test_search_skips_dimension_mismatch_rather_than_crashing(mock_db):
    """Collection indexed at a different embedding dimension than the
    current model emits must be skipped with a warning, not crash search.

    Issue #190 follow-up: users who upgraded the local embedding model
    kept older collections around at the previous dimension; ChromaDB
    raises ``InvalidArgumentError`` on ``col.query`` because the query
    embedding dimension doesn't match the stored vectors. Previously
    this crashed the whole multi-collection search. Now we log a
    structured warning and continue with the remaining collections.
    """
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 3
    mock_col.query.side_effect = chromadb.errors.InvalidArgumentError(
        "Collection expecting embedding with dimension of 384, got 768"
    )
    # Single-collection call: search must return empty, not raise.
    results = db.search("q", ["knowledge__stale"], n_results=5)
    assert results == []


def test_search_continues_past_dimension_mismatch_to_next_collection(
    mock_chromadb,
):
    """Mismatch on collection A must not prevent collection B from being queried."""
    _, mock_client = mock_chromadb

    col_bad = MagicMock()
    col_bad.count.return_value = 3
    col_bad.query.side_effect = chromadb.errors.InvalidArgumentError(
        "Collection expecting embedding with dimension of 384, got 768"
    )

    col_good = MagicMock()
    col_good.count.return_value = 2
    col_good.query.return_value = {
        "ids": [["g-1"]],
        "documents": [["good content"]],
        "metadatas": [[{}]],
        "distances": [[0.1]],
    }

    def _get_collection(name, **kw):
        return col_bad if name == "knowledge__stale" else col_good

    mock_client.get_collection.side_effect = _get_collection
    db = T3Database(tenant="t", database="d", api_key="k")
    results = db.search(
        "q", ["knowledge__stale", "code__healthy"], n_results=5,
    )
    assert len(results) == 1
    assert results[0]["id"] == "g-1"


def test_search_propagates_non_dimension_invalid_argument_errors(mock_db):
    """Only dimension-mismatch InvalidArgumentErrors are swallowed.

    A different ``InvalidArgumentError`` (e.g. bad ``where`` clause)
    must still surface — it points at a real caller bug.
    """
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 3
    mock_col.query.side_effect = chromadb.errors.InvalidArgumentError(
        "invalid where clause: unknown operator $weird"
    )
    with pytest.raises(chromadb.errors.InvalidArgumentError):
        db.search("q", ["code__x"], n_results=5)


def test_search_cce_collection_uses_query_embeddings(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_col.count.return_value = 3
    mock_col.query.return_value = {
        "ids": [["id-cce"]], "documents": [["cce content"]],
        "metadatas": [[{"title": "rdr-004"}]], "distances": [[0.12]],
    }
    mock_client.get_collection.return_value = mock_col

    # voyageai.Client is constructed lazily inside T3Database.__init__
    # now (PR #293 cold-start fix); patch the canonical voyageai.Client
    # rather than the nexus.db.t3 module attribute (which no longer
    # exists at module scope).
    with patch("voyageai.Client") as mock_vo_ctor:
        mock_vo_inst = MagicMock()
        mock_vo_ctor.return_value = mock_vo_inst
        mock_vo_inst.contextualized_embed.return_value = MagicMock()
        db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vkey")
        results = db.search("four store t3 architecture", ["rdr__nexus-abc123"], n_results=5)

    mock_vo_ctor.assert_called_once_with(api_key="vkey", timeout=120.0, max_retries=0)
    mock_vo_inst.contextualized_embed.assert_called_once_with(
        inputs=[["four store t3 architecture"]], model="voyage-context-3", input_type="query",
    )
    call_kwargs = mock_col.query.call_args.kwargs
    assert "query_embeddings" in call_kwargs
    assert "query_texts" not in call_kwargs
    assert len(results) == 1 and results[0]["id"] == "id-cce"


def test_search_cce_skipped_without_voyage_api_key(mock_db):
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 2
    mock_col.query.return_value = {
        "ids": [["id-1"]], "documents": [["doc"]], "metadatas": [[{}]], "distances": [[0.2]],
    }
    db.search("query", ["docs__manual"], n_results=5)
    call_kwargs = mock_col.query.call_args.kwargs
    assert "query_texts" in call_kwargs
    assert "query_embeddings" not in call_kwargs


def test_search_passes_where_filter(mock_db):
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 5
    mock_col.query.return_value = {
        "ids": [["id-1"]], "documents": [["content"]],
        "metadatas": [[{"title": "t"}]], "distances": [[0.1]],
    }
    db.search("query", ["knowledge__sec"], where={"source_agent": "indexer"})
    assert mock_col.query.call_args.kwargs["where"] == {"source_agent": "indexer"}


# ── CCE put ─────────────────────────────────────────────────────────────────


def test_put_cce_collection_uses_document_input_type(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col

    # See PR #293 cold-start note above.
    with patch("voyageai.Client") as mock_vo_ctor:
        mock_vo_inst = MagicMock()
        mock_vo_ctor.return_value = mock_vo_inst
        mock_vo_inst.contextualized_embed.return_value = MagicMock()
        db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vkey")
        db.put(collection="knowledge__security", content="some document text about security findings", title="finding.md")

    mock_vo_inst.contextualized_embed.assert_called_once_with(
        inputs=[["some document text about security findings"]], model="voyage-context-3", input_type="document",
    )
    assert "embeddings" in mock_col.upsert.call_args.kwargs


# ── AC7: collection list ────────────────────────────────────────────────────


def test_list_collections_returns_names_and_counts(mock_chromadb):
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


def test_list_collections_empty(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_client.list_collections.return_value = []
    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.list_collections() == []


def test_list_collections_skips_failed_count(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_ok = MagicMock()
    mock_ok.count.return_value = 10
    mock_fail = MagicMock()
    mock_fail.count.side_effect = RuntimeError("network error")
    mock_client.list_collections.return_value = ["knowledge__good", "knowledge__broken"]
    mock_client.get_collection.side_effect = lambda name: mock_ok if name == "knowledge__good" else mock_fail
    db = T3Database(tenant="t", database="d", api_key="k")
    result = db.list_collections()
    names = [r["name"] for r in result]
    assert "knowledge__good" in names
    assert "knowledge__broken" not in names


# ── Deterministic ID ────────────────────────────────────────────────────────


@pytest.mark.parametrize("col,title1,title2,expect_same", [
    ("knowledge__sec", "finding.md", "finding.md", True),
    ("knowledge__sec", "chunk-a.md", "chunk-b.md", False),
    ("code__repo", "file.py:1-50", "file.py:1-50", True),
])
def test_put_deterministic_id(mock_db, col, title1, title2, expect_same):
    db, mock_col, _ = mock_db
    id1 = db.put(collection=col, content="first", title=title1)
    id2 = db.put(collection=col, content="second", title=title2)
    assert (id1 == id2) == expect_same


def test_put_empty_title_collision(mock_db):
    db, mock_col, _ = mock_db
    id1 = db.put(collection="knowledge__sec", content="first", title="")
    id2 = db.put(collection="knowledge__sec", content="second", title="")
    assert id1 == id2


def test_put_same_title_different_collection_different_ids(mock_db):
    db, mock_col, _ = mock_db
    id1 = db.put(collection="knowledge__sec", content="text", title="shared.md")
    id2 = db.put(collection="knowledge__ops", content="text", title="shared.md")
    assert id1 != id2


# ── Oversized put raises (GH #244 + nexus-akof) ─────────────────────────────


def test_put_raises_on_oversized_content(mock_db):
    """``put()`` raises ``PutOversizedError`` instead of silently dropping
    the write. Previously ``_write_batch`` logged a warning and returned,
    but the caller (``store_put`` MCP tool) went on to register the
    doc_id in the catalog, producing a ghost entry: catalog says the doc
    exists, but ChromaDB has no row for the id."""
    from nexus.db.chroma_quotas import QUOTAS
    from nexus.errors import PutOversizedError

    db, mock_col, _ = mock_db
    oversized = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)

    with pytest.raises(PutOversizedError) as exc_info:
        db.put(collection="knowledge__sec", content=oversized, title="big.md")

    assert exc_info.value.doc_bytes > QUOTAS.MAX_DOCUMENT_BYTES
    assert exc_info.value.max_bytes == QUOTAS.MAX_DOCUMENT_BYTES
    assert exc_info.value.collection == "knowledge__sec"
    # Upsert must NOT have been called: no ChromaDB row written, so
    # downstream catalog registration at the caller level would also be
    # skipped.
    mock_col.upsert.assert_not_called()


def test_put_under_cap_still_succeeds(mock_db):
    """Regression guard: documents under the cap write normally and
    the cap check doesn't false-positive on ordinary content."""
    db, mock_col, _ = mock_db
    doc_id = db.put(
        collection="knowledge__sec", content="small body", title="ok.md",
    )
    assert isinstance(doc_id, str) and len(doc_id) == 16
    mock_col.upsert.assert_called_once()


def test_write_batch_indexer_path_still_drops_oversized(mock_db):
    """Indexer callers do not pass ``fail_on_oversized=True``; the
    pipeline should keep running (drop-and-warn) because a chunker
    upstream was supposed to guard against this and a pipeline-wide
    raise is worse than dropping one record."""
    from nexus.db.chroma_quotas import QUOTAS

    db, mock_col, _ = mock_db
    oversized = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
    good = "y" * 32

    # No exception raised.
    db._write_batch(
        mock_col,
        "knowledge__mixed",
        ids=["a", "b"],
        documents=[oversized, good],
        metadatas=[{"title": "a"}, {"title": "b"}],
    )
    # Upsert was called with only the good record.
    assert mock_col.upsert.call_count == 1
    written_ids = mock_col.upsert.call_args.kwargs["ids"]
    assert written_ids == ["b"]


# ── Embedding function cache ────────────────────────────────────────────────


def test_embedding_fn_cached_per_collection_name(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_client.get_or_create_collection.return_value = MagicMock()
    with patch("nexus.db.t3.chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction") as mock_ef_cls:
        mock_ef_cls.return_value = MagicMock(name="ef_instance")
        db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vk")
        ef1 = db._embedding_fn("knowledge__topic")
        ef2 = db._embedding_fn("knowledge__topic")
    assert ef1 is ef2
    assert mock_ef_cls.call_count == 1


def test_embedding_fn_different_names_not_confused(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_client.get_or_create_collection.return_value = MagicMock()
    ef_a, ef_b = MagicMock(name="ef_code"), MagicMock(name="ef_knowledge")
    with patch("nexus.db.t3.chromadb.utils.embedding_functions.VoyageAIEmbeddingFunction", side_effect=[ef_a, ef_b]) as mock_ef_cls:
        db = T3Database(tenant="t", database="d", api_key="k", voyage_api_key="vk")
        r1 = db._embedding_fn("code__repo")
        r2 = db._embedding_fn("knowledge__topic")
        r3 = db._embedding_fn("code__repo")
    assert r1 is ef_a and r2 is ef_b and r3 is ef_a
    assert mock_ef_cls.call_count == 2


def test_ef_override_bypasses_cache(mock_chromadb):
    _, mock_client = mock_chromadb
    override_ef = MagicMock(name="override")
    db = T3Database(tenant="t", database="d", api_key="k", _ef_override=override_ef)
    assert db._embedding_fn("code__repo") is override_ef
    assert db._embedding_fn("knowledge__sec") is override_ef
    assert db._ef_cache == {}


# ── make_t3() factory ───────────────────────────────────────────────────────


def test_make_t3_returns_t3database(mock_chromadb):
    from nexus.db import make_t3
    with patch("nexus.db.get_credential", side_effect=lambda k: f"val-{k}"):
        db = make_t3()
    assert isinstance(db, T3Database)


def test_make_t3_uses_credentials(mock_chromadb):
    from nexus.db import make_t3
    creds = {"chroma_tenant": "my-tenant", "chroma_database": "my-db", "chroma_api_key": "ck-abc", "voyage_api_key": "vk-xyz"}
    with patch("nexus.config.is_local_mode", return_value=False):
        with patch("nexus.db.get_credential", side_effect=lambda k: creds.get(k, "")):
            db = make_t3()
    assert mock_chromadb[0].CloudClient.call_count == 1
    assert mock_chromadb[0].CloudClient.call_args.kwargs["database"] == "my-db"
    assert db._voyage_api_key == "vk-xyz"


def test_make_t3_client_injection(mock_chromadb):
    from nexus.db import make_t3
    fake_client = MagicMock()
    with patch("nexus.db.get_credential", return_value="x"):
        db = make_t3(_client=fake_client)
    mock_chromadb[0].CloudClient.assert_not_called()
    assert db._client is fake_client


# ── upsert_chunks ───────────────────────────────────────────────────────────


def test_upsert_chunks_calls_col_upsert(mock_db):
    db, mock_col, _ = mock_db
    db.upsert_chunks(
        collection="code__myrepo", ids=["id-1", "id-2"],
        documents=["chunk one", "chunk two"],
        metadatas=[{"title": "f.py:1-10"}, {"title": "f.py:11-20"}],
    )
    # Metadata is funnelled through the canonical schema (nexus-40t),
    # which injects ``content_type`` from the collection prefix.
    mock_col.upsert.assert_called_once_with(
        ids=["id-1", "id-2"], documents=["chunk one", "chunk two"],
        metadatas=[
            {"title": "f.py:1-10", "content_type": "code"},
            {"title": "f.py:11-20", "content_type": "code"},
        ],
    )


def test_upsert_chunks_passes_all_metadata_fields(mock_db):
    """Canonical schema fields (nexus-40t) survive the normalize pass;
    ``indexed_at`` is now in ALLOWED_TOP_LEVEL (replaces dropped
    ``expires_at``); ``content_type`` is injected from the collection
    prefix. RDR-102 Phase B drops ``source_path`` — it is normalized
    out and must not appear in the written record."""
    db, mock_col, _ = mock_db
    rich_meta = {
        "title": "f.py:1-5", "tags": "py", "category": "code", "session_id": "",
        "source_agent": "nexus-indexer",
        "indexed_at": "2026-01-01T00:00:00+00:00", "ttl_days": 0,
        "line_start": 1, "line_end": 5, "frecency_score": 0.42,
    }
    db.upsert_chunks(
        collection="code__myrepo", ids=["abc123"],
        documents=["def foo(): pass"], metadatas=[rich_meta],
    )
    written = mock_col.upsert.call_args.kwargs["metadatas"][0]
    assert written["title"] == "f.py:1-5"
    assert written["tags"] == "py"
    assert written["category"] == "code"
    assert written["source_agent"] == "nexus-indexer"
    assert written["content_type"] == "code"  # injected
    assert written["ttl_days"] == 0
    assert written["indexed_at"] == "2026-01-01T00:00:00+00:00"
    assert "expires_at" not in written
    # RDR-102 Phase B dropped source_path; RDR-101 Phase 5c dropped
    # store_type, corpus, git_meta. ``content_type`` is the canonical
    # routing field; the legacy duplicates are normalized out.
    assert "source_path" not in written
    assert "store_type" not in written
    assert "corpus" not in written
    assert "git_meta" not in written
    assert written["line_start"] == 1
    assert written["line_end"] == 5
    assert written["frecency_score"] == 0.42


def test_upsert_chunks_uses_correct_embedding_fn(mock_db_voyage):
    db, mock_col, mock_client = mock_db_voyage
    db.upsert_chunks(collection="docs__corpus", ids=["d1"], documents=["some text"], metadatas=[{"source_path": "doc.pdf"}])
    mock_client.get_or_create_collection.assert_called_once()
    assert mock_client.get_or_create_collection.call_args.args[0] == "docs__corpus"


# ── delete_by_source ────────────────────────────────────────────────────────


@pytest.mark.parametrize("col,path,returned_ids,expected_count,expect_delete", [
    ("code__myrepo", "src/foo.py", ["a1", "a2", "a3"], 3, True),
    ("knowledge__wiki", "notes/page.md", ["x1", "x2"], 2, True),
    ("code__myrepo", "nonexistent/file.py", [], 0, False),
])
def test_delete_by_source(mock_db, col, path, returned_ids, expected_count, expect_delete):
    db, mock_col, _ = mock_db
    mock_col.get.return_value = {"ids": returned_ids}
    result = db.delete_by_source(col, path)
    assert result == expected_count
    if expect_delete:
        mock_col.delete.assert_called_once_with(ids=returned_ids)
    else:
        mock_col.delete.assert_not_called()


def test_delete_by_source_nonexistent_collection_returns_zero(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("Collection not found")
    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.delete_by_source("code__nonexistent", "src/file.py") == 0


# ── ids_for_doc_id / delete_by_doc_id (nexus-dcym) ────────────────────────


@pytest.mark.parametrize("col,doc_id,returned_ids,expected_count", [
    ("code__myrepo", "ART-deadbeef", ["a1", "a2", "a3"], 3),
    ("knowledge__wiki", "ART-cafe1234", ["x1"], 1),
    ("code__myrepo", "ART-nonexistent", [], 0),
])
def test_ids_for_doc_id(mock_db, col, doc_id, returned_ids, expected_count):
    """ids_for_doc_id queries on the doc_id metadata field, not source_path."""
    db, mock_col, _ = mock_db
    mock_col.get.return_value = {"ids": returned_ids}
    result = db.ids_for_doc_id(col, doc_id)
    assert result == returned_ids
    assert len(result) == expected_count
    # The chunk-lookup must use doc_id, NOT source_path. WITH TEETH:
    # if the implementation accidentally falls back to source_path, this fails.
    where = mock_col.get.call_args.kwargs["where"]
    assert where == {"doc_id": doc_id}


def test_ids_for_doc_id_nonexistent_collection_returns_empty(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("Collection not found")
    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.ids_for_doc_id("code__nonexistent", "ART-deadbeef") == []


def test_ids_for_doc_id_paginates_over_300_record_quota(mock_db):
    """Cloud has a 300-record limit per get(); helper must page."""
    db, mock_col, _ = mock_db
    page1 = {"ids": [f"id-{i}" for i in range(300)]}
    page2 = {"ids": [f"id-{i}" for i in range(300, 450)]}
    mock_col.get.side_effect = [page1, page2]
    result = db.ids_for_doc_id("code__myrepo", "ART-large")
    assert len(result) == 450
    assert mock_col.get.call_count == 2
    # offset advances by page size
    assert mock_col.get.call_args_list[0].kwargs["offset"] == 0
    assert mock_col.get.call_args_list[1].kwargs["offset"] == 300


@pytest.mark.parametrize("col,doc_id,returned_ids,expected_count,expect_delete", [
    ("code__myrepo", "ART-deadbeef", ["a1", "a2", "a3"], 3, True),
    ("knowledge__wiki", "ART-cafe1234", ["x1", "x2"], 2, True),
    ("code__myrepo", "ART-nonexistent", [], 0, False),
])
def test_delete_by_doc_id(mock_db, col, doc_id, returned_ids, expected_count, expect_delete):
    db, mock_col, _ = mock_db
    mock_col.get.return_value = {"ids": returned_ids}
    result = db.delete_by_doc_id(col, doc_id)
    assert result == expected_count
    if expect_delete:
        mock_col.delete.assert_called_once_with(ids=returned_ids)
    else:
        mock_col.delete.assert_not_called()
    # The lookup leg must filter on doc_id.
    where = mock_col.get.call_args.kwargs["where"]
    assert where == {"doc_id": doc_id}


def test_delete_by_doc_id_nonexistent_collection_returns_zero(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("Collection not found")
    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.delete_by_doc_id("code__nonexistent", "ART-deadbeef") == 0


# ── update_source_path ─────────────────────────────────────────────────────


def test_update_source_path_basic(mock_db):
    db, mock_col, _ = mock_db
    mock_col.get.return_value = {
        "ids": ["c1", "c2"],
        "metadatas": [
            {"source_path": "/abs/path/f.py", "title": "f.py:1-5"},
            {"source_path": "/abs/path/f.py", "title": "f.py:6-10"},
        ],
    }
    result = db.update_source_path("code__myrepo", "/abs/path/f.py", "src/f.py")
    assert result == 2
    mock_col.update.assert_called_once()
    call_kwargs = mock_col.update.call_args.kwargs
    assert call_kwargs["ids"] == ["c1", "c2"]
    assert call_kwargs["metadatas"][0]["source_path"] == "src/f.py"
    assert call_kwargs["metadatas"][0]["title"] == "f.py:1-5"  # preserved
    assert call_kwargs["metadatas"][1]["source_path"] == "src/f.py"


def test_update_source_path_empty_result(mock_db):
    db, mock_col, _ = mock_db
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    result = db.update_source_path("code__myrepo", "/nonexistent", "relative/f.py")
    assert result == 0
    mock_col.update.assert_not_called()


def test_update_source_path_missing_collection(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("not found")
    db = T3Database(tenant="t", database="d", api_key="k")
    assert db.update_source_path("code__nonexistent", "/old", "new") == 0


def test_update_source_path_preserves_other_metadata(mock_db):
    db, mock_col, _ = mock_db
    mock_col.get.return_value = {
        "ids": ["c1"],
        "metadatas": [{
            "source_path": "/abs/file.py",
            "title": "file.py:1-10",
            "tags": "python",
            "category": "code",
            "frecency_score": 0.5,
            "start_line": 1,
            "end_line": 10,
        }],
    }
    db.update_source_path("code__myrepo", "/abs/file.py", "src/file.py")
    meta = mock_col.update.call_args.kwargs["metadatas"][0]
    assert meta["source_path"] == "src/file.py"
    assert meta["title"] == "file.py:1-10"
    assert meta["tags"] == "python"
    assert meta["frecency_score"] == 0.5
    assert meta["start_line"] == 1


def test_update_source_path_pagination(mock_db):
    """Verify pagination works for >300 chunks."""
    db, mock_col, _ = mock_db
    # First page: 300 results (full page)
    page1_ids = [f"c{i}" for i in range(300)]
    page1_metas = [{"source_path": "/old/f.py"} for _ in range(300)]
    # Second page: 5 results (short page = last)
    page2_ids = [f"c{i}" for i in range(300, 305)]
    page2_metas = [{"source_path": "/old/f.py"} for _ in range(5)]

    mock_col.get.side_effect = [
        {"ids": page1_ids, "metadatas": page1_metas},
        {"ids": page2_ids, "metadatas": page2_metas},
    ]
    result = db.update_source_path("code__myrepo", "/old/f.py", "src/f.py")
    assert result == 305
    # Should be called twice (two batches of ≤300)
    assert mock_col.update.call_count == 2


# ── collection_metadata ─────────────────────────────────────────────────────


@pytest.mark.parametrize("col,expected_model", [
    ("docs__corpus", "voyage-context-3"),
    ("code__myrepo", "voyage-code-3"),
])
def test_collection_metadata_returns_correct_fields(mock_db, col, expected_model):
    db, mock_col, _ = mock_db
    mock_col.count.return_value = 17
    meta = db.collection_metadata(col)
    assert meta["name"] == col
    assert meta["count"] == 17
    assert meta["embedding_model"] == expected_model
    assert meta["index_model"] == expected_model


@pytest.mark.parametrize("method", ["collection_info", "collection_metadata"])
def test_collection_missing_raises_keyerror(mock_chromadb, method):
    _, mock_client = mock_chromadb
    mock_client.get_collection.side_effect = chromadb.errors.NotFoundError("not found")
    db = T3Database(tenant="t", database="d", api_key="k")
    with pytest.raises(KeyError, match="Collection not found"):
        getattr(db, method)("knowledge__missing")


# ── upsert_chunks_with_embeddings ───────────────────────────────────────────


def test_upsert_chunks_with_embeddings_stores(mock_db):
    db, mock_col, _ = mock_db
    embeddings = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    ids = ["chunk-1", "chunk-2"]
    docs = ["first chunk text", "second chunk text"]
    # RDR-102 Phase B: source_path is dropped by normalize. Use page_number
    # as the schema-stable distinguishing field.
    metas = [
        {"page_number": 1},
        {"page_number": 2},
    ]
    db.upsert_chunks_with_embeddings(
        collection_name="docs__corpus", ids=ids, documents=docs,
        embeddings=embeddings, metadatas=metas,
    )
    # docs__ collections route to the prose content_type by default.
    expected_metas = [
        {"page_number": 1, "content_type": "prose"},
        {"page_number": 2, "content_type": "prose"},
    ]
    mock_col.upsert.assert_called_once_with(
        ids=ids, documents=docs, embeddings=embeddings, metadatas=expected_metas,
    )


def test_upsert_chunks_with_embeddings_uses_get_or_create(mock_db):
    db, mock_col, mock_client = mock_db
    db.upsert_chunks_with_embeddings(
        collection_name="knowledge__wiki", ids=["k1"], documents=["some doc"],
        embeddings=[[0.9, 0.1, 0.5]], metadatas=[{"source_path": "wiki/page.md"}],
    )
    mock_client.get_or_create_collection.assert_called_once()
    assert mock_client.get_or_create_collection.call_args.args[0] == "knowledge__wiki"


# ── update_chunks ───────────────────────────────────────────────────────────


def test_update_chunks_calls_col_update_without_documents(mock_db):
    db, mock_col, _ = mock_db
    # RDR-102 Phase B: source_path is dropped by normalize. frecency_score
    # is schema-stable and round-trips through the canonical pass.
    metas = [{"frecency_score": 0.9}] * 2
    db.update_chunks(collection="code__myrepo", ids=["id-1", "id-2"], metadatas=metas)
    # update_chunks funnels through canonical metadata (nexus-40t),
    # injecting ``content_type`` for every record.
    normalised = [
        {"frecency_score": 0.9, "content_type": "code"}
    ] * 2
    mock_col.update.assert_called_once_with(
        ids=["id-1", "id-2"], metadatas=normalised,
    )
    assert "documents" not in mock_col.update.call_args.kwargs


# ── Context manager ─────────────────────────────────────────────────────────


def test_t3_context_manager_enter_returns_self(mock_chromadb):
    _, mock_client = mock_chromadb
    db = T3Database(tenant="t", database="d", api_key="k")
    with db as ctx:
        assert ctx is db


def test_t3_context_manager_works_end_to_end(mock_chromadb):
    _, mock_client = mock_chromadb
    mock_col = MagicMock()
    mock_client.get_or_create_collection.return_value = mock_col
    with T3Database(tenant="t", database="d", api_key="k") as db:
        doc_id = db.put(collection="knowledge__cm_test", content="context manager test", title="cm.md")
        assert isinstance(doc_id, str) and len(doc_id) > 0


# ── local_t3: retrieval quality ─────────────────────────────────────────────


def test_search_returns_closest_document_first(local_t3: T3Database):
    col = "knowledge__quality_test"
    local_t3.put(collection=col, content="Python web framework for building REST APIs with Django and Flask", title="python-web")
    local_t3.put(collection=col, content="Quantum physics experiments studying wave-particle duality and entanglement", title="quantum")
    local_t3.put(collection=col, content="Italian cooking recipes for homemade pasta carbonara and risotto", title="cooking")
    results = local_t3.search(query="Django REST API web development Python", collection_names=[col], n_results=3)
    assert len(results) >= 3
    assert results[0]["title"] == "python-web"


def test_search_unrelated_query_produces_higher_distance(local_t3: T3Database):
    col = "knowledge__distance_test"
    local_t3.put(collection=col, content="Machine learning model training with PyTorch neural networks and gradient descent", title="ml")
    relevant = local_t3.search(query="deep learning neural network training", collection_names=[col], n_results=1)
    unrelated = local_t3.search(query="medieval castle architecture stone walls", collection_names=[col], n_results=1)
    assert relevant and unrelated
    assert unrelated[0]["distance"] > relevant[0]["distance"]


def test_search_single_chunk_document_retrievable(local_t3: T3Database):
    col = "docs__single_chunk_test"
    local_t3.put(collection=col, content="Authentication tokens use RS256 signing with rotating key schedules", title="auth-tokens")
    results = local_t3.search(query="JWT RS256 authentication token signing", collection_names=[col], n_results=1)
    assert results
    assert "RS256" in results[0]["content"] or "authentication" in results[0]["content"].lower()


# ── local_t3: collection_info ───────────────────────────────────────────────


def test_collection_info_returns_count_and_metadata(local_t3: T3Database):
    local_t3.put(collection="knowledge__info_test", content="Some knowledge content", title="info-doc.md", tags="test")
    info = local_t3.collection_info("knowledge__info_test")
    assert info["count"] == 1
    assert isinstance(info["metadata"], dict)


def test_collection_info_nonexistent_collection_raises(local_t3: T3Database):
    with pytest.raises(Exception):
        local_t3.collection_info("knowledge__does_not_exist")


def test_collection_info_count_increases(local_t3: T3Database):
    local_t3.put(collection="knowledge__count_test", content="first doc", title="doc-1.md")
    local_t3.put(collection="knowledge__count_test", content="second doc", title="doc-2.md")
    assert local_t3.collection_info("knowledge__count_test")["count"] == 2


# ── local_t3: list_store ────────────────────────────────────────────────────


def test_list_store_returns_entries_with_metadata(local_t3: T3Database):
    local_t3.put(collection="knowledge__ls_test", content="stored content", title="ls-doc.md", tags="alpha,beta", ttl_days=30)
    entries = local_t3.list_store("knowledge__ls_test")
    assert len(entries) == 1
    entry = entries[0]
    assert "id" in entry
    assert entry["title"] == "ls-doc.md"
    assert entry["tags"] == "alpha,beta"
    assert entry["ttl_days"] == 30


def test_list_store_nonexistent_collection_returns_empty(local_t3: T3Database):
    assert local_t3.list_store("knowledge__no_such_coll") == []


def test_list_store_multiple_entries(local_t3: T3Database):
    for i in range(3):
        local_t3.put(collection="knowledge__multi_ls", content=f"content {i}", title=f"doc-{i}.md")
    entries = local_t3.list_store("knowledge__multi_ls")
    assert len(entries) == 3
    assert {e["title"] for e in entries} == {"doc-0.md", "doc-1.md", "doc-2.md"}


# ── local_t3: collection_exists ─────────────────────────────────────────────


@pytest.mark.parametrize("setup,collection,expected", [
    (True, "knowledge__exists_test", True),
    (False, "knowledge__never_created", False),
])
def test_collection_exists(local_t3: T3Database, setup, collection, expected):
    if setup:
        local_t3.put(collection=collection, content="some content", title="exists.md")
    assert local_t3.collection_exists(collection) is expected


# ── local_t3: verify_collection_deep ────────────────────────────────────────


def test_verify_deep_healthy_collection(local_t3: T3Database):
    col = "knowledge__verify_test"
    local_t3.put(collection=col, content="Semantic search uses vector embeddings for similarity matching", title="search-doc")
    local_t3.put(collection=col, content="Database indexing improves query performance significantly", title="db-doc")
    from nexus.db.t3 import verify_collection_deep
    result = verify_collection_deep(local_t3, col)
    assert result.status == "healthy"
    assert result.doc_count == 2
    assert result.probe_doc_id is not None
    assert result.distance is not None


def test_verify_deep_reports_distance(local_t3: T3Database):
    col = "knowledge__dist_test"
    local_t3.put(collection=col, content="Test document for distance reporting", title="test")
    local_t3.put(collection=col, content="Another document for the collection", title="test2")
    from nexus.db.t3 import verify_collection_deep
    result = verify_collection_deep(local_t3, col)
    assert result.distance >= 0.0
    assert result.metric in ("l2", "cosine", "ip", "unknown")


def test_verify_deep_skips_tiny_collection(local_t3: T3Database):
    col = "knowledge__tiny_test"
    local_t3.get_or_create_collection(col)
    from nexus.db.t3 import verify_collection_deep
    result = verify_collection_deep(local_t3, col)
    assert result.status == "skipped"
    assert result.doc_count <= 1


def test_verify_deep_nonexistent_collection(local_t3: T3Database):
    from nexus.db.t3 import verify_collection_deep
    with pytest.raises(KeyError):
        verify_collection_deep(local_t3, "knowledge__does_not_exist")


# ── existing_ids (GH #249) ─────────────────────────────────────────────────


def test_existing_ids_returns_present_subset(local_t3: T3Database):
    """existing_ids filters the query list to the subset present in the collection."""
    col = "knowledge__existing_ids_test"
    doc1 = local_t3.put(collection=col, content="Document one", title="doc-one")
    doc2 = local_t3.put(collection=col, content="Document two", title="doc-two")

    found = local_t3.existing_ids(col, [doc1, doc2, "ghostghostghost0"])
    assert found == {doc1, doc2}


def test_existing_ids_empty_input_short_circuits(local_t3: T3Database):
    """Empty id list → empty set without a network round trip."""
    assert local_t3.existing_ids("knowledge__anything", []) == set()


def test_existing_ids_missing_collection_is_empty(local_t3: T3Database):
    """existing_ids on an unknown collection returns empty, not raises."""
    assert local_t3.existing_ids(
        "knowledge__never_created", ["aaaa", "bbbb"]
    ) == set()


def test_existing_ids_pagination_respects_300_cap(local_t3: T3Database):
    """existing_ids pages at 300 ids per call (ChromaDB Cloud cap)."""
    col = "knowledge__existing_ids_paging"
    # Seed 305 entries so the sweep must page twice.
    seeded: list[str] = []
    for i in range(305):
        doc_id = local_t3.put(
            collection=col, content=f"body {i}", title=f"doc-{i}",
        )
        seeded.append(doc_id)

    found = local_t3.existing_ids(col, seeded + ["nonexistent11111"])
    assert found == set(seeded)
    assert "nonexistent11111" not in found
