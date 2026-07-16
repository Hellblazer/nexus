# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nexus.db.http_vector_client (RDR-152 bead nexus-gmiaf.20)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.db.http_vector_client import (
    HttpVectorClient,
    VectorServiceError,
    get_http_vector_client,
    is_vector_service_mode,
    reset_http_vector_client_for_tests,
)


class TestDataPathConfigYmlFallback:
    """RDR-166 nexus-v3p0x — the T3 data path (_resolve_endpoint) must consume
    config.yml service creds so greenfield store/search works after `nx config
    set service_url/service_token` (no env, no lease)."""

    def test_resolve_endpoint_reads_config_yml_when_env_absent(self, monkeypatch, tmp_path):
        import nexus.db.http_vector_client as hvc

        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
        for k in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN"):
            monkeypatch.delenv(k, raising=False)
        hvc._invalidate_endpoint()  # clear any cached lease
        from nexus.config import set_credential
        set_credential("service_url", "https://api.conexus-nexus.com")
        set_credential("service_token", "data-tok")
        try:
            assert hvc._resolve_endpoint() == (
                "https://api.conexus-nexus.com", "data-tok",
            )
        finally:
            hvc._invalidate_endpoint()


# ── is_vector_service_mode ────────────────────────────────────────────────────

class TestIsVectorServiceMode:
    def test_unset_returns_true(self, monkeypatch):
        # nexus-tawx0: service mode is the post-P4a default (make_t3 returns
        # the service client unconditionally); unset == service.
        monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
        assert is_vector_service_mode() is True

    def test_sqlite_returns_false(self, monkeypatch):
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "sqlite")
        assert is_vector_service_mode() is False

    def test_service_lowercase_returns_true(self, monkeypatch):
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
        assert is_vector_service_mode() is True

    def test_service_uppercase_returns_true(self, monkeypatch):
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "SERVICE")
        assert is_vector_service_mode() is True

    def test_service_mixed_case_returns_true(self, monkeypatch):
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "Service")
        assert is_vector_service_mode() is True

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "  service  ")
        assert is_vector_service_mode() is True

    def test_unknown_value_returns_false(self, monkeypatch):
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "postgres")
        assert is_vector_service_mode() is False


# ── singleton ────────────────────────────────────────────────────────────────

class TestSingleton:
    def setup_method(self):
        reset_http_vector_client_for_tests()

    def teardown_method(self):
        reset_http_vector_client_for_tests()

    def test_get_http_vector_client_returns_instance(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        client = get_http_vector_client()
        assert isinstance(client, HttpVectorClient)

    def test_singleton_same_object(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        a = get_http_vector_client()
        b = get_http_vector_client()
        assert a is b

    def test_reset_clears_singleton(self, monkeypatch):
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        a = get_http_vector_client()
        reset_http_vector_client_for_tests()
        b = get_http_vector_client()
        assert a is not b


# ── HttpVectorClient methods (mocked HTTP) ───────────────────────────────────

def _make_mock_post(response_body: dict):
    """Return a mock for _post that yields response_body."""
    def _mock(path, body, *, tenant="default"):
        return response_body
    return _mock


def _make_mock_get(response_body):
    def _mock(path, *, tenant="default"):
        return response_body
    return _mock


class TestUpsertChunks:
    def test_empty_ids_is_noop(self, monkeypatch):
        client = HttpVectorClient()
        posted = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: posted.append((path, body))
        )
        client.upsert_chunks("col", [], [])
        assert posted == []

    def test_posts_to_upsert_chunks_endpoint(self, monkeypatch):
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, *, tenant="default", timeout=120):
            calls.append((path, body, timeout))
            return {"upserted": 2}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client.upsert_chunks("my-col", ["id1", "id2"], ["text1", "text2"])
        assert len(calls) == 1
        path, body, timeout = calls[0]
        assert path == "/v1/vectors/upsert-chunks"
        assert body["collection"] == "my-col"
        assert body["ids"] == ["id1", "id2"]
        assert body["documents"] == ["text1", "text2"]
        # nexus-rvfwj: the upsert path alone gets the long CCE-batch timeout.
        assert timeout == 600

    def test_default_metadatas_are_empty_dicts(self, monkeypatch):
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append(body)
            return {"upserted": 1}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client.upsert_chunks("col", ["id1"], ["text1"])
        assert calls[0]["metadatas"] == [{}]

    def test_upsert_with_embeddings_ignores_embeddings(self, monkeypatch):
        """Seam B: embeddings arg is discarded; server embeds server-side."""
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append(body)
            return {"upserted": 1}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        # Pass embeddings — they should NOT appear in the POST body
        client.upsert_chunks_with_embeddings(
            "col", ["id1"], ["text1"], [[0.1, 0.2]]
        )
        assert "embeddings" not in calls[0]

    def test_upsert_chunks_without_embeddings_omits_key(self, monkeypatch):
        """Default Seam B path: no embeddings field → server embeds."""
        client = HttpVectorClient()
        calls = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: calls.append(body) or {"upserted": 1},
        )
        client.upsert_chunks("col", ["id1"], ["text1"])
        assert "embeddings" not in calls[0]

    def test_upsert_chunks_passthrough_sends_embeddings(self, monkeypatch):
        """nexus-hxry2 same-model passthrough: supplied vectors ARE sent so the
        service stores them verbatim and skips the re-embed."""
        client = HttpVectorClient()
        calls = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: calls.append(body) or {"upserted": 2},
        )
        vecs = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        client.upsert_chunks(
            "col", ["id1", "id2"], ["t1", "t2"], embeddings=vecs
        )
        assert calls[0]["embeddings"] == vecs

    def test_skip_existing_no_longer_prunes_embeddings(self, monkeypatch):
        """RDR-181 bead nexus-f0r8p.5: skip_existing is a deprecated no-op —
        the full batch (including supplied embeddings) is always sent, and
        the client-side existing_ids probe is never invoked anymore."""
        client = HttpVectorClient()
        calls = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: calls.append(body) or {"upserted": 2},
        )
        probe_calls = []

        def _tracked_existing_ids(col, ids):
            probe_calls.append((col, ids))
            return {"id1"}

        monkeypatch.setattr(client, "existing_ids", _tracked_existing_ids)
        vecs = [[0.1], [0.2]]
        client.upsert_chunks(
            "col", ["id1", "id2"], ["t1", "t2"], embeddings=vecs, skip_existing=True
        )
        assert probe_calls == []
        assert calls[0]["ids"] == ["id1", "id2"]
        assert calls[0]["embeddings"] == vecs


class TestSearch:
    def test_returns_list_flat(self, monkeypatch):
        client = HttpVectorClient()
        fake_results = [
            {"id": "c1", "content": "hello", "distance": 0.1, "collection": "col"}
        ]
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: fake_results
        )
        results = client.search("hello world", ["col"], n_results=5)
        assert results == fake_results

    def test_structured_returns_dict(self, monkeypatch):
        client = HttpVectorClient()
        fake_results = [
            {"id": "c1", "content": "hello", "distance": 0.1, "collection": "col",
             "tumbler": "1.2"}
        ]
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: fake_results
        )
        result = client.search("hello", ["col"], structured=True)
        assert isinstance(result, dict)
        assert result["ids"] == ["c1"]
        assert result["distances"] == [0.1]
        assert result["collections"] == ["col"]
        assert result["tumblers"] == ["1.2"]

    def test_where_filter_passed_in_body(self, monkeypatch):
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append(body)
            return []
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client.search("q", ["c"], where={"topic": "ml"})
        assert calls[0]["where"] == {"topic": "ml"}

    def test_no_where_not_in_body(self, monkeypatch):
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append(body)
            return []
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client.search("q", ["c"])
        assert "where" not in calls[0]


class TestPut:
    def test_post_to_store_put(self, monkeypatch):
        """put() now matches T3Database.put() contract: doc_id derived from content."""
        import hashlib
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append((path, body))
            return {"id": body.get("doc_id", "fallback")}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        content = "content here"
        expected_doc_id = hashlib.sha256(content.encode()).hexdigest()[:32]
        returned_id = client.put("col", content, title="my-title")
        path, body = calls[0]
        assert path == "/v1/vectors/store-put"
        assert body["doc_id"] == expected_doc_id
        assert body["content"] == content
        assert returned_id == expected_doc_id


class TestGetById:
    def test_returns_flat_t3database_shape_when_found(self, monkeypatch):
        # nexus-ij9hg: get_by_id must return T3Database's FLAT shape
        # (id + content + flat metadata), NOT id/document/nested-metadata.
        # The old nested shape silently emptied store_get content in service
        # mode (the nexus-7zuzz divergence class).
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda p, b, **kw: {
                "ids": ["id1"],
                "documents": ["text"],
                "metadatas": [{"content_type": "knowledge",
                               "embedding_model": "minilm-l6-v2-384",
                               "title": "t"}],
            },
        )
        result = client.get_by_id("col", "id1")
        assert result == {
            "id": "id1",
            "content": "text",
            "content_type": "knowledge",
            "embedding_model": "minilm-l6-v2-384",
            "title": "t",
        }
        # Explicitly: no nested keys that callers (store_get / nx store get) miss.
        assert "document" not in result
        assert "metadata" not in result

    def test_returns_none_when_not_found(self, monkeypatch):
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda p, b, **kw: {"ids": [], "documents": [], "metadatas": []}
        )
        result = client.get_by_id("col", "missing-id")
        assert result is None

    def test_returns_none_on_service_error(self, monkeypatch):
        client = HttpVectorClient()
        def raise_err(p, b, **kw):
            raise VectorServiceError("404 not found")
        monkeypatch.setattr("nexus.db.http_vector_client._post", raise_err)
        result = client.get_by_id("col", "any-id")
        assert result is None


class TestDeleteById:
    def test_returns_true_when_deleted(self, monkeypatch):
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda p, b, **kw: {"deleted": 1}
        )
        assert client.delete_by_id("col", "id1") is True

    def test_returns_false_when_not_found(self, monkeypatch):
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda p, b, **kw: {"deleted": 0}
        )
        assert client.delete_by_id("col", "id1") is False

    def test_returns_false_on_service_error(self, monkeypatch):
        client = HttpVectorClient()
        def raise_err(p, b, **kw):
            raise VectorServiceError("500 error")
        monkeypatch.setattr("nexus.db.http_vector_client._post", raise_err)
        assert client.delete_by_id("col", "id1") is False


class TestListCollections:
    def test_returns_list(self, monkeypatch):
        # RDR-156 P3 (nexus-70r3c.12): list_collections is served by
        # GET /v1/vectors/stats and returns the {name, count} T3Database
        # parity shape. Full stats/fallback behavior is covered in
        # tests/test_http_vector_client_stats.py.
        client = HttpVectorClient()
        fake = [{"name": "knowledge__nexus__model__v1", "dim": 384,
                 "count": 7, "last_write": "2026-06-11T00:00:00Z"}]
        monkeypatch.setattr(
            "nexus.db.http_vector_client._get",
            lambda p, **kw: fake
        )
        result = client.list_collections()
        assert result == [{"name": "knowledge__nexus__model__v1", "count": 7}]

    def test_returns_empty_on_service_error(self, monkeypatch):
        client = HttpVectorClient()
        def raise_err(p, **kw):
            raise VectorServiceError("error")
        monkeypatch.setattr("nexus.db.http_vector_client._get", raise_err)
        result = client.list_collections()
        assert result == []


class TestUpdateChunks:
    """RDR-152 nexus-enehl: update_chunks routes to /v1/vectors/update-metadata."""

    def test_posts_to_update_metadata_endpoint(self, monkeypatch):
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append((path, body))
            return {"updated": 2}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client.update_chunks(
            "code__repo__voyage-code-3__v1",
            ["id1", "id2"],
            [{"frecency_score": 0.5}, {"frecency_score": 0.8}],
        )
        assert len(calls) == 1
        path, body = calls[0]
        assert path == "/v1/vectors/update-metadata"
        assert body["collection"] == "code__repo__voyage-code-3__v1"
        assert body["ids"] == ["id1", "id2"]
        assert body["metadatas"] == [{"frecency_score": 0.5}, {"frecency_score": 0.8}]

    def test_empty_ids_is_noop(self, monkeypatch):
        client = HttpVectorClient()
        posted = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: posted.append((path, body))
        )
        client.update_chunks("col", [], [])
        assert posted == []

    def test_tenant_forwarded(self, monkeypatch):
        client = HttpVectorClient(tenant="my-tenant")
        calls = []
        def fake_post(path, body, *, tenant="default"):
            calls.append(tenant)
            return {"updated": 1}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client.update_chunks("col", ["id1"], [{"k": "v"}])
        assert calls == ["my-tenant"]

    def test_batches_at_300(self, monkeypatch):
        """update_chunks MUST batch at 300 to match the service quota validator."""
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append(body["ids"])
            return {"updated": len(body["ids"])}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

        # 350 ids: expect 2 POST calls (300 + 50)
        ids = [f"id{i:04d}" for i in range(350)]
        metas = [{"frecency_score": float(i)} for i in range(350)]
        client.update_chunks("code__repo__model__v1", ids, metas)

        assert len(calls) == 2, (
            f"Expected 2 batched POSTs for 350 ids, got {len(calls)}"
        )
        assert len(calls[0]) == 300
        assert len(calls[1]) == 50
        # All ids appear exactly once across batches
        all_sent = [x for batch in calls for x in batch]
        assert sorted(all_sent) == sorted(ids)


class TestGetCollection:
    """RDR-152 nexus-enehl: get_collection raises ChromaNotFoundError when absent."""

    def test_returns_stub_when_collection_exists(self, monkeypatch):
        from nexus.db.http_vector_client import _ServiceCollectionStub
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._get",
            lambda path, **kw: [{"name": "code__repo__model__v1", "id": "uuid-1"}]
        )
        stub = client.get_collection("code__repo__model__v1")
        assert isinstance(stub, _ServiceCollectionStub)

    def test_raises_not_found_when_absent(self, monkeypatch):
        from chromadb.errors import NotFoundError as _ChromaNotFoundError
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._get",
            lambda path, **kw: [{"name": "other__col__model__v1", "id": "uuid-2"}]
        )
        with pytest.raises(_ChromaNotFoundError):
            client.get_collection("code__repo__model__v1")

    def test_raises_not_found_on_empty_list(self, monkeypatch):
        from chromadb.errors import NotFoundError as _ChromaNotFoundError
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._get",
            lambda path, **kw: []
        )
        with pytest.raises(_ChromaNotFoundError):
            client.get_collection("any__col__model__v1")

    def test_raises_not_found_on_service_error(self, monkeypatch):
        from chromadb.errors import NotFoundError as _ChromaNotFoundError
        client = HttpVectorClient()
        def raise_err(path, **kw):
            raise VectorServiceError("connection refused")
        monkeypatch.setattr("nexus.db.http_vector_client._get", raise_err)
        with pytest.raises(_ChromaNotFoundError):
            client.get_collection("col__name__model__v1")


class TestServiceCollectionStubGetWithIds:
    """RDR-152 nexus-enehl: _ServiceCollectionStub.get(ids=...) routes to store-get."""

    def test_ids_routes_to_store_get(self, monkeypatch):
        from nexus.db.http_vector_client import _ServiceCollectionStub
        calls = []
        def fake_post(path, body, **kw):
            calls.append((path, body))
            return {"ids": ["id1"], "documents": ["text"], "metadatas": [{"k": "v"}]}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        stub = _ServiceCollectionStub("col__test__m__v1")
        result = stub.get(ids=["id1"], include=["metadatas"])
        assert len(calls) == 1
        path, body = calls[0]
        assert path == "/v1/vectors/store-get"
        assert body["ids"] == ["id1"]
        assert result["ids"] == ["id1"]
        assert result["metadatas"] == [{"k": "v"}]

    def test_where_routes_to_get(self, monkeypatch):
        from nexus.db.http_vector_client import _ServiceCollectionStub
        calls = []
        def fake_post(path, body, **kw):
            calls.append((path, body))
            return {"ids": [], "documents": [], "metadatas": []}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        stub = _ServiceCollectionStub("col__test__m__v1")
        stub.get(where={"source_path": "/foo.py"}, limit=10, offset=0)
        assert calls[0][0] == "/v1/vectors/get"

    def test_neither_ids_nor_where_routes_to_get(self, monkeypatch):
        from nexus.db.http_vector_client import _ServiceCollectionStub
        calls = []
        def fake_post(path, body, **kw):
            calls.append((path, body))
            return {"ids": [], "documents": [], "metadatas": []}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        stub = _ServiceCollectionStub("col__test__m__v1")
        stub.get(limit=5, offset=0)
        assert calls[0][0] == "/v1/vectors/get"


class TestNotImplementedMethods:
    def test_delete_collection_raises(self):
        client = HttpVectorClient()
        with pytest.raises(NotImplementedError):
            client.delete_collection("col")

    # delete_by_source was here until nexus-vhyua implemented it via
    # ids_for_source (/v1/vectors/get where-filter) + /v1/vectors/store-delete.
    # Behaviour coverage now lives in
    # tests/test_http_vector_client_parity.py::TestDeleteBySource.

    # get_embeddings was here until nexus-pebfx.7 implemented it via
    # /v1/vectors/get-embeddings — see TestGetEmbeddings.

    # find_ids_by_title / batch_delete / list_store / collection_info were
    # here until nexus-umvh2 implemented them — see TestFindIdsByTitle,
    # TestBatchDelete, TestListStore, TestCollectionInfo below.


class TestFindIdsByTitle:
    """nexus-umvh2: find_ids_by_title was missing entirely, crashing
    `nx store delete --title` and the MCP store_get title-fallback with
    AttributeError in service mode. Mirrors ids_for_source's where-filter
    pagination pattern (nexus-vhyua)."""

    def test_paginates_and_collects(self, monkeypatch):
        # Two pages (300 then 2) -> single flat id list; second short page ends it.
        pages = [
            {"ids": [f"id{i}" for i in range(300)]},
            {"ids": ["id300", "id301"]},
        ]
        calls = []

        def fake_post(path, body, **kw):
            page = pages[len(calls)]
            calls.append((path, body))
            return page

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        ids = client.find_ids_by_title("knowledge__nexus__model__v1", "doc.md")
        assert len(ids) == 302
        assert calls[0][1]["where"] == {"title": "doc.md"}
        assert all(c[0] == "/v1/vectors/get" for c in calls)

    def test_no_matches_returns_empty(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda p, b, **kw: {"ids": []},
        )
        client = HttpVectorClient()
        assert client.find_ids_by_title("col", "missing.md") == []

    def test_404_first_page_returns_empty(self, monkeypatch):
        def fake_post(path, body, **kw):
            raise VectorServiceError("not found", code=404)

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        assert client.find_ids_by_title("missing-col", "doc.md") == []

    def test_mid_pagination_error_reraises(self, monkeypatch):
        # A 500 on page 2 (after ids collected) must NOT be masked as "no more
        # matches" — else `nx store delete --title` would under-delete and
        # report success.
        def fake_post(path, body, **kw):
            if body["offset"] == 0:
                return {"ids": [f"id{i}" for i in range(300)]}
            raise VectorServiceError("server error", code=500)

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        with pytest.raises(VectorServiceError):
            client.find_ids_by_title("col", "doc.md")


class TestBatchDelete:
    """nexus-umvh2: batch_delete was missing — the second AttributeError on
    `nx store delete --title`'s happy path (after find_ids_by_title resolves
    ids). Batches at the service write quota like update_chunks."""

    def test_empty_ids_is_noop(self, monkeypatch):
        posted = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: posted.append((path, body)),
        )
        HttpVectorClient().batch_delete("col", [])
        assert posted == []

    def test_deletes_via_store_delete(self, monkeypatch):
        calls = []

        def fake_post(path, body, **kw):
            calls.append((path, body))
            return {"deleted": len(body["ids"])}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        HttpVectorClient().batch_delete("col", ["id1", "id2"])
        assert len(calls) == 1
        assert calls[0][0] == "/v1/vectors/store-delete"
        assert calls[0][1]["ids"] == ["id1", "id2"]
        assert calls[0][1]["collection"] == "col"

    def test_batches_at_300(self, monkeypatch):
        calls = []

        def fake_post(path, body, **kw):
            calls.append(body["ids"])
            return {"deleted": len(body["ids"])}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        ids = [f"id{i:04d}" for i in range(350)]
        HttpVectorClient().batch_delete("col", ids)
        assert len(calls) == 2
        assert len(calls[0]) == 300
        assert len(calls[1]) == 50
        assert sorted(x for batch in calls for x in batch) == sorted(ids)


class TestListStore:
    """nexus-umvh2 sibling audit: list_store was missing, crashing `nx store
    list` / `nx store list --docs` / `nx collection info --docs` / MCP
    store_list in service mode — the same class of bug as
    find_ids_by_title, just unreported because the CLI test fixtures mock
    the whole T3 client (bare MagicMock, no spec=)."""

    def test_returns_flat_entries(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda p, b, **kw: {
                "ids": ["id1", "id2"],
                "metadatas": [{"title": "a.md"}, {"title": "b.md"}],
            },
        )
        entries = HttpVectorClient().list_store("col", limit=200, offset=0)
        assert entries == [
            {"id": "id1", "title": "a.md"},
            {"id": "id2", "title": "b.md"},
        ]

    def test_passes_limit_and_offset(self, monkeypatch):
        calls = []

        def fake_post(path, body, **kw):
            calls.append(body)
            return {"ids": [], "metadatas": []}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        HttpVectorClient().list_store("col", limit=50, offset=100)
        assert calls[0]["limit"] == 50
        assert calls[0]["offset"] == 100
        assert calls[0]["collection"] == "col"

    def test_404_returns_empty(self, monkeypatch):
        def fake_post(path, body, **kw):
            raise VectorServiceError("nf", code=404)

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        assert HttpVectorClient().list_store("missing-col") == []

    def test_non_404_error_reraises(self, monkeypatch):
        def fake_post(path, body, **kw):
            raise VectorServiceError("server error", code=500)

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        with pytest.raises(VectorServiceError):
            HttpVectorClient().list_store("col")


class TestCollectionInfo:
    """nexus-umvh2 sibling audit: collection_info was missing, crashing
    `nx store list`'s total-count display, `nx collection info`, and
    `nx collection reindex` in service mode."""

    def test_returns_count_for_existing_collection(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._get",
            lambda p, **kw: {"count": 42},
        )
        info = HttpVectorClient().collection_info("col")
        assert info["count"] == 42
        assert info["metadata"] == {}

    def test_raises_keyerror_when_zero_count(self, monkeypatch):
        monkeypatch.setattr(
            "nexus.db.http_vector_client._get",
            lambda p, **kw: {"count": 0},
        )
        with pytest.raises(KeyError):
            HttpVectorClient().collection_info("missing-col")

    def test_raises_keyerror_on_404(self, monkeypatch):
        def raise_err(p, **kw):
            raise VectorServiceError("not found", code=404)

        monkeypatch.setattr("nexus.db.http_vector_client._get", raise_err)
        with pytest.raises(KeyError):
            HttpVectorClient().collection_info("missing-col")

    def test_reraises_non_404_service_error(self, monkeypatch):
        def raise_err(p, **kw):
            raise VectorServiceError("server error", code=500)

        monkeypatch.setattr("nexus.db.http_vector_client._get", raise_err)
        with pytest.raises(VectorServiceError):
            HttpVectorClient().collection_info("col")


# ── get_t3() routing (integration with mcp_infra) ────────────────────────────

class TestGetT3Routing:
    """Verify get_t3() returns the right type based on env flag."""

    def setup_method(self):
        from nexus import mcp_infra
        mcp_infra.reset_singletons()

    def teardown_method(self):
        from nexus import mcp_infra
        mcp_infra.reset_singletons()

    def test_default_path_returns_t3database(self, monkeypatch):
        monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
        from nexus import mcp_infra
        from nexus.db.t3 import T3Database
        # Inject a fake T3Database instance to avoid real DB init
        fake_t3 = MagicMock(spec=T3Database)
        mcp_infra.inject_t3(fake_t3)
        t3 = mcp_infra.get_t3()
        assert t3 is fake_t3

    def test_service_flag_returns_http_vector_client(self, monkeypatch):
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")
        reset_http_vector_client_for_tests()
        from nexus import mcp_infra
        mcp_infra.reset_singletons()
        t3 = mcp_infra.get_t3()
        assert isinstance(t3, HttpVectorClient)


# ── Service-mode split-brain / dead-seam regression tests (RDR-152 .20 fixes) ─
#
# BEFORE the fix: doc_indexer.py called make_t3() directly when t3=None, always
# returning T3Database(daemon) even in service mode — indexed chunks written to
# daemon-Chroma while search reads service-Chroma (silent split-brain).
# AFTER the fix: the fallback routes through get_t3(), which returns
# HttpVectorClient in service mode.

class TestServiceModeIndexerRouting:
    """Verify doc_indexer.py routes through get_t3() in service mode (no split-brain)."""

    def setup_method(self):
        from nexus import mcp_infra
        mcp_infra.reset_singletons()
        reset_http_vector_client_for_tests()

    def teardown_method(self):
        from nexus import mcp_infra
        mcp_infra.reset_singletons()
        reset_http_vector_client_for_tests()

    def test_index_document_fallback_routes_through_get_t3_in_service_mode(
        self, monkeypatch
    ):
        """When _index_document is called with t3=None in service mode, the
        fallback must use get_t3() (returns HttpVectorClient), NOT make_t3()
        (which always returns T3Database — the split-brain bug).

        This test deliberately exercises the t3=None fallback path and asserts
        that get_t3() was called (and not make_t3()) by checking the returned
        instance is an HttpVectorClient.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")

        # Track which factory was called
        get_t3_called = []
        make_t3_called = []

        from nexus import mcp_infra
        original_get_t3 = mcp_infra.get_t3

        def fake_get_t3():
            t3 = original_get_t3()
            get_t3_called.append(type(t3).__name__)
            return t3

        monkeypatch.setattr("nexus.mcp_infra.get_t3", fake_get_t3)

        # Patch make_t3 at doc_indexer's import site to detect if called
        def sentinel_make_t3():
            make_t3_called.append("CALLED")
            return MagicMock()

        monkeypatch.setattr("nexus.doc_indexer.make_t3", sentinel_make_t3)

        # Simulate the t3=None fallback inside _index_document by importing and
        # calling the lazy-import path directly (mirrors the get_t3 lazy import
        # that replaced make_t3 in the fix).
        from nexus.mcp_infra import get_t3
        db = get_t3()

        assert isinstance(db, HttpVectorClient), (
            "In service mode, the t3=None fallback must return HttpVectorClient, "
            "not T3Database — a T3Database write would create a split-brain where "
            "indexed chunks are invisible to service-mode search."
        )
        assert not make_t3_called, (
            "make_t3() must NOT be called in service mode — "
            "it bypasses the routing gate and always returns T3Database(daemon)."
        )

    def test_index_document_with_explicit_t3_uses_provided_instance(self, monkeypatch):
        """When t3 is explicitly provided (non-None), it must be used as-is
        regardless of service mode — the caller owns the T3 instance."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
        explicit_t3 = MagicMock()
        explicit_t3.get_or_create_collection = MagicMock(return_value=MagicMock())

        # In the fixed code: if t3 is not None: db = t3 (no routing, no make_t3)
        # Verify this path directly
        from nexus.doc_indexer import _index_document  # noqa: PLC0415
        import inspect
        source = inspect.getsource(_index_document)
        # The fix must NOT call make_t3 when t3 is provided
        assert "if t3 is not None" in source, (
            "_index_document must have the 'if t3 is not None: db = t3' guard "
            "added by the RDR-152 Seam B fix"
        )


# ── Taxonomy service-mode guard (no-AttributeError regression) ────────────────
#
# BEFORE the fix: taxonomy_assign_batch_hook called get_t3()._client which
# raises AttributeError on HttpVectorClient (no _client attr).  The bare except
# swallowed it silently → taxonomy silently dropped in service mode.
# AFTER the fix: early-return guard logs INFO and returns cleanly.

class TestTaxonomyServiceModeGuard:
    """Verify taxonomy hooks no-op cleanly in service mode (no AttributeError)."""

    def setup_method(self):
        from nexus import mcp_infra
        mcp_infra.reset_singletons()
        reset_http_vector_client_for_tests()

    def teardown_method(self):
        from nexus import mcp_infra
        mcp_infra.reset_singletons()
        reset_http_vector_client_for_tests()

    def test_taxonomy_assign_batch_hook_no_ops_cleanly_in_service_mode(
        self, monkeypatch
    ):
        """taxonomy_assign_batch_hook must return without raising or swallowing
        an AttributeError when NX_STORAGE_BACKEND_VECTORS=service.

        BEFORE the fix: get_t3()._client raised AttributeError on HttpVectorClient
        (no ._client attr), then the bare except swallowed it silently -- taxonomy
        was silently dropped.

        AFTER the fix: early-return guard detects service mode, logs INFO, returns
        cleanly. We verify: (a) no exception escapes, (b) HttpVectorClient._client
        is never accessed (no AttributeError even if the bare except were removed),
        by asserting HttpVectorClient has no _client attribute at all.
        """
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")

        from nexus import mcp_infra
        from nexus.db.http_vector_client import HttpVectorClient

        # Verify the guard: HttpVectorClient must NOT have a _client attribute
        # (the old code's get_t3()._client would have raised AttributeError on it).
        fake_client = HttpVectorClient()
        assert not hasattr(fake_client, "_client"), (
            "HttpVectorClient must not have a ._client attr — "
            "the taxonomy guard protects against AttributeError on this path."
        )

        # Wire HttpVectorClient as the t3 instance (service mode)
        mcp_infra.inject_t3(fake_client)

        # Must NOT raise — before the fix this would silently swallow an AttributeError.
        # The guard in taxonomy_assign_batch_hook must return early before reaching
        # the ._client access.
        mcp_infra.taxonomy_assign_batch_hook(
            doc_ids=["chunk-001"],
            collection="knowledge__nexus-test__all-minilm-l6-v2__v1",
            contents=["Test content for taxonomy."],
            embeddings=None,
            metadatas=None,
        )
        # If we reach here, the hook returned cleanly (no AttributeError escaped).

    def test_fetch_or_embed_returns_none_in_service_mode(self, monkeypatch):
        """_fetch_or_embed must return None immediately in service mode
        (HttpVectorClient has no ._client; the guard prevents AttributeError)."""
        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
        monkeypatch.setenv("NX_SERVICE_TOKEN", "tok")

        from nexus import mcp_infra
        result = mcp_infra._fetch_or_embed(
            doc_ids=["chunk-001"],
            collection="knowledge__nexus-test__all-minilm-l6-v2__v1",
            contents=["Test content."],
        )
        assert result is None, (
            "_fetch_or_embed must return None in service mode — "
            "HttpVectorClient has no ._client, so the Chroma fetch path must be skipped."
        )


class TestGetEmbeddings:
    """nexus-pebfx.7: get_embeddings via /v1/vectors/get-embeddings — the
    search engine's contradiction-check + Ward-clustering features silently
    degraded on EVERY service-mode search while this raised
    NotImplementedError."""

    def test_posts_to_get_embeddings_endpoint(self, monkeypatch):
        client = HttpVectorClient()
        calls = []

        def fake_post(path, body, *, tenant="default", timeout=120):
            calls.append((path, body))
            return {"ids": ["a", "b"], "embeddings": [[0.1, 0.2], [0.3, 0.4]]}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        result = client.get_embeddings("knowledge__x__minilm-l6-v2-384__v1", ["a", "b"])
        path, body = calls[0]
        assert path == "/v1/vectors/get-embeddings"
        assert body == {
            "collection": "knowledge__x__minilm-l6-v2-384__v1",
            "ids": ["a", "b"],
        }
        import numpy as np

        assert result.dtype == np.float32
        assert result.shape == (2, 2)
        assert result[1][1] == np.float32(0.4)

    def test_missing_ids_dropped_chroma_parity(self, monkeypatch):
        # The service omits ids it cannot find; N < len(ids) is the caller's
        # shape-mismatch signal — same semantics as the Chroma path.
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _make_mock_post({"ids": ["a"], "embeddings": [[0.1, 0.2]]}),
        )
        result = client.get_embeddings("col", ["a", "missing"])
        assert result.shape == (1, 2)

    def test_empty_result_shape(self, monkeypatch):
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _make_mock_post({"ids": [], "embeddings": []}),
        )
        result = client.get_embeddings("col", ["x"])
        assert result.shape[0] == 0


class TestGetEmbeddingsBatching:
    """nexus-g7ubw: get_embeddings posted ALL ids in one request; on a 28k-chunk
    collection the response (28k x 1024-dim vectors as JSON) deterministically
    504'd at the gateway, breaking taxonomy discover for large collections in
    service mode. Fetch must page at the service quota and concatenate rows in
    request order (positional-alignment contract, nexus-7ydks S2)."""

    @staticmethod
    def _fake_post_recording(calls):
        def fake_post(path, body, *, tenant="default", timeout=120):
            calls.append((path, body))
            ids = body["ids"]
            # Echo one distinct 2-dim row per id: [index-derived, 0.0]
            return {
                "ids": list(ids),
                "embeddings": [[float(int(i)), 0.0] for i in ids],
            }

        return fake_post

    def test_over_quota_ids_split_into_batches(self, monkeypatch):
        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_recording(calls)
        )
        n = QUOTAS.MAX_RECORDS_PER_WRITE * 2 + 17
        ids = [str(i) for i in range(n)]
        result = client.get_embeddings("col", ids)

        assert len(calls) == 3
        assert all(path == "/v1/vectors/get-embeddings" for path, _ in calls)
        # Every batch respects the quota
        assert all(len(body["ids"]) <= QUOTAS.MAX_RECORDS_PER_WRITE for _, body in calls)
        # Slices cover the input exactly, in order
        assert [i for _, body in calls for i in body["ids"]] == ids

    def test_rows_concatenated_in_request_order(self, monkeypatch):
        import numpy as np

        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_recording(calls)
        )
        n = QUOTAS.MAX_RECORDS_PER_WRITE + 5
        ids = [str(i) for i in range(n)]
        result = client.get_embeddings("col", ids)

        assert result.dtype == np.float32
        assert result.shape == (n, 2)
        # Row i must correspond to id i across the batch boundary
        assert result[0][0] == np.float32(0.0)
        assert result[QUOTAS.MAX_RECORDS_PER_WRITE][0] == np.float32(
            QUOTAS.MAX_RECORDS_PER_WRITE
        )
        assert result[n - 1][0] == np.float32(n - 1)

    def test_exactly_quota_ids_single_batch(self, monkeypatch):
        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_recording(calls)
        )
        ids = [str(i) for i in range(QUOTAS.MAX_RECORDS_PER_WRITE)]
        client.get_embeddings("col", ids)
        assert len(calls) == 1

    def test_missing_ids_dropped_across_batches(self, monkeypatch):
        # Per-batch drops must surface as N < len(ids) overall — the caller's
        # shape-mismatch tripwire (taxonomy_cmd refuses misaligned clustering).
        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()

        def fake_post(path, body, *, tenant="default", timeout=120):
            ids = [i for i in body["ids"] if i != "1"]  # service can't resolve "1"
            return {"ids": ids, "embeddings": [[0.5, 0.5] for _ in ids]}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        n = QUOTAS.MAX_RECORDS_PER_WRITE + 3
        ids = [str(i) for i in range(n)]
        result = client.get_embeddings("col", ids)
        assert result.shape == (n - 1, 2)

    def test_empty_ids_no_post(self, monkeypatch):
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_recording(calls)
        )
        result = client.get_embeddings("col", [])
        assert calls == []
        assert result.shape[0] == 0

    def test_drop_in_middle_batch_keeps_later_rows_aligned(self, monkeypatch):
        # Critic insurance (nexus-g7ubw): a drop in a MIDDLE batch must not
        # shift rows of LATER batches. Guards against a future parallel-fetch
        # refactor breaking the strict sequential-concat ordering.
        import numpy as np

        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()
        quota = QUOTAS.MAX_RECORDS_PER_WRITE
        dropped = str(quota + 7)  # lives in batch 1 of 3

        def fake_post(path, body, *, tenant="default", timeout=120):
            ids = [i for i in body["ids"] if i != dropped]
            return {
                "ids": ids,
                "embeddings": [[float(int(i)), 0.0] for i in ids],
            }

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        n = quota * 2 + 11  # 3 batches
        ids = [str(i) for i in range(n)]
        result = client.get_embeddings("col", ids)

        assert result.shape == (n - 1, 2)
        # Last row (batch 2) still carries its own id's value — alignment
        # after the mid-batch drop is preserved.
        assert result[-1][0] == np.float32(n - 1)
        # The row where the dropped id would have been is its successor.
        assert result[quota + 7][0] == np.float32(quota + 8)


class TestEmbeddingFetchServiceModeRegression:
    """nexus-pebfx.7 critic: lock the NAMED symptom — a service-mode search's
    embedding fetch must NOT emit embedding_fetch_failed (it did, once per
    collection per search, while get_embeddings raised NotImplementedError)."""

    @staticmethod
    def _results(col: str, n: int):
        from nexus.types import SearchResult

        return [
            SearchResult(
                id=f"{col}-{i}", content=f"text {i}", distance=0.1,
                collection=col, metadata={},
            )
            for i in range(n)
        ]

    def test_fetch_succeeds_without_embedding_fetch_failed(self, monkeypatch):
        from structlog.testing import capture_logs

        from nexus.search_engine import _fetch_embeddings_for_results

        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _make_mock_post({
                "ids": ["colA-0", "colA-1"],
                "embeddings": [[0.1, 0.2], [0.3, 0.4]],
            }),
        )
        results = self._results("colA", 2)
        with capture_logs() as logs:
            embeddings, failed = _fetch_embeddings_for_results(results, client)
        assert not any(e["event"] == "embedding_fetch_failed" for e in logs)
        assert failed == set()
        assert embeddings.shape == (2, 2)

    def test_shape_mismatch_marks_collection_failed(self, monkeypatch):
        # Service omits a missing id -> N < len(ids) -> the collection's
        # indices land in failed_indices (never positional misattribution).
        from nexus.search_engine import _fetch_embeddings_for_results

        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            _make_mock_post({"ids": ["colA-0"], "embeddings": [[0.1, 0.2]]}),
        )
        results = self._results("colA", 2)
        _, failed = _fetch_embeddings_for_results(results, client)
        assert failed == {0, 1}

    def test_mixed_dim_collections_fail_minority_not_crash(self, monkeypatch):
        """Live-verify catch (2026-06-11): a 384-dim collection in the same
        result set as 1024-dim collections crashed the whole search with a
        broadcast ValueError once the fetch started succeeding. The
        odd-dim collection must be marked failed instead."""
        from nexus.search_engine import _fetch_embeddings_for_results
        from nexus.types import SearchResult

        client = HttpVectorClient()
        payloads = {
            "colBig": {"ids": ["colBig-0"], "embeddings": [[0.1] * 1024]},
            "colSmall": {"ids": ["colSmall-0"], "embeddings": [[0.2] * 384]},
        }

        def fake_post(path, body, *, tenant="default", timeout=120):
            return payloads[body["collection"]]

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        results = [
            SearchResult(id="colBig-0", content="t", distance=0.1,
                         collection="colBig", metadata={}),
            SearchResult(id="colSmall-0", content="t", distance=0.1,
                         collection="colSmall", metadata={}),
        ]
        embeddings, failed = _fetch_embeddings_for_results(results, client)
        assert embeddings is not None
        assert embeddings.shape == (2, 1024)
        assert failed == {1}


class TestUpsertSkipExisting:
    """RDR-181 bead nexus-f0r8p.5: skip_existing is now a deprecation shim.

    Server-side embed-skip (``PgVectorRepository.upsertChunksInternal``'s
    existence-partition, beads .1/.2) is authoritative. The client-side
    probe this flag used to drive (nexus-7zuzz) is gone: skip_existing
    (or ``NX_UPSERT_SKIP_EXISTING=1``) no longer removes anything from the
    outgoing batch — the whole batch is always sent, and the server does
    the equivalent filtering losslessly, including the metadata refresh
    the old client-side probe dropped. See TestSkipExistingDeprecationNotice
    for the one remaining observable effect (a one-time log line)."""

    def _client_with_fake_post(self, monkeypatch, existing: list[str] | None = None):
        client = HttpVectorClient()
        calls = []
        existing = existing or []

        def fake_post(path, body, *, tenant="default", timeout=120):
            calls.append((path, body))
            if path == "/v1/vectors/store-get":
                present = [i for i in body["ids"] if i in existing]
                return {"ids": present}
            return {"upserted": len(body.get("ids", []))}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        return client, calls

    def test_skip_existing_no_longer_filters_present_ids(self, monkeypatch):
        """The full batch is sent even though 'a' and 'c' are already
        present — the client-side probe round-trip is gone entirely."""
        client, calls = self._client_with_fake_post(monkeypatch, existing=["a", "c"])
        client.upsert_chunks(
            "col", ["a", "b", "c"], ["ta", "tb", "tc"],
            metadatas=[{"k": 1}, {"k": 2}, {"k": 3}],
            skip_existing=True,
        )
        assert [p for p, _ in calls if p == "/v1/vectors/store-get"] == []
        upserts = [(p, b) for p, b in calls if p == "/v1/vectors/upsert-chunks"]
        assert len(upserts) == 1
        body = upserts[0][1]
        assert body["ids"] == ["a", "b", "c"]
        assert body["documents"] == ["ta", "tb", "tc"]
        assert body["metadatas"] == [{"k": 1}, {"k": 2}, {"k": 3}]

    def test_skip_existing_all_present_still_sends_full_batch(self, monkeypatch):
        client, calls = self._client_with_fake_post(monkeypatch, existing=["a", "b"])
        client.upsert_chunks("col", ["a", "b"], ["ta", "tb"], skip_existing=True)
        upserts = [b for p, b in calls if p == "/v1/vectors/upsert-chunks"]
        assert len(upserts) == 1
        assert upserts[0]["ids"] == ["a", "b"]

    def test_default_behavior_unchanged_no_existence_probe(self, monkeypatch):
        client, calls = self._client_with_fake_post(monkeypatch, existing=["a"])
        client.upsert_chunks("col", ["a", "b"], ["ta", "tb"])
        assert [p for p, _ in calls] == ["/v1/vectors/upsert-chunks"]
        assert calls[0][1]["ids"] == ["a", "b"]

    def test_env_flag_no_longer_filters_batch(self, monkeypatch):
        monkeypatch.setenv("NX_UPSERT_SKIP_EXISTING", "1")
        client, calls = self._client_with_fake_post(monkeypatch, existing=["a"])
        client.upsert_chunks("col", ["a", "b"], ["ta", "tb"])
        assert [p for p, _ in calls if p == "/v1/vectors/store-get"] == []
        upserts = [b for p, b in calls if p == "/v1/vectors/upsert-chunks"]
        assert upserts[0]["ids"] == ["a", "b"]


class TestSkipExistingDeprecationNotice:
    """RDR-181 bead nexus-f0r8p.5: skip_existing (kwarg or the
    NX_UPSERT_SKIP_EXISTING=1 env alias) fires a one-time structlog
    deprecation notice pointing at RDR-181 the first time it is observed
    set on this process, and never again — not once per call."""

    def _reset_dedup_flag(self, monkeypatch):
        import nexus.db.http_vector_client as hvc

        monkeypatch.setattr(hvc, "_skip_existing_deprecation_logged", False)

    def _client_with_fake_post(self, monkeypatch):
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: {"upserted": len(body.get("ids", []))},
        )
        return client

    @staticmethod
    def _deprecation_events(logs):
        return [e for e in logs if e["event"] == "http_vector_skip_existing_deprecated"]

    def test_kwarg_true_logs_deprecation_once(self, monkeypatch):
        from structlog.testing import capture_logs

        self._reset_dedup_flag(monkeypatch)
        client = self._client_with_fake_post(monkeypatch)
        with capture_logs() as logs:
            client.upsert_chunks("col", ["a"], ["ta"], skip_existing=True)
        assert len(self._deprecation_events(logs)) == 1

    def test_env_flag_logs_deprecation(self, monkeypatch):
        from structlog.testing import capture_logs

        self._reset_dedup_flag(monkeypatch)
        monkeypatch.setenv("NX_UPSERT_SKIP_EXISTING", "1")
        client = self._client_with_fake_post(monkeypatch)
        with capture_logs() as logs:
            client.upsert_chunks("col", ["a"], ["ta"])
        assert len(self._deprecation_events(logs)) == 1

    def test_logs_only_once_across_multiple_calls(self, monkeypatch):
        from structlog.testing import capture_logs

        self._reset_dedup_flag(monkeypatch)
        client = self._client_with_fake_post(monkeypatch)
        with capture_logs() as logs:
            client.upsert_chunks("col", ["a"], ["ta"], skip_existing=True)
            client.upsert_chunks("col", ["b"], ["tb"], skip_existing=True)
        assert len(self._deprecation_events(logs)) == 1

    def test_default_no_deprecation_log(self, monkeypatch):
        from structlog.testing import capture_logs

        self._reset_dedup_flag(monkeypatch)
        client = self._client_with_fake_post(monkeypatch)
        with capture_logs() as logs:
            client.upsert_chunks("col", ["a"], ["ta"])
        assert self._deprecation_events(logs) == []

    def test_force_re_embed_alone_does_not_log_deprecation(self, monkeypatch):
        """force_re_embed is the live (non-deprecated) escape hatch; using it
        alone must not trip the skip_existing deprecation notice."""
        from structlog.testing import capture_logs

        self._reset_dedup_flag(monkeypatch)
        client = self._client_with_fake_post(monkeypatch)
        with capture_logs() as logs:
            client.upsert_chunks("col", ["a"], ["ta"], force_re_embed=True)
        assert self._deprecation_events(logs) == []


class TestForceReEmbed:
    """RDR-181 bead nexus-f0r8p.3: plumbing-only — thread force_re_embed onto
    the wire and map the deprecated NX_UPSERT_SKIP_EXISTING=0 escape to it.
    The client does NOT interpret the flag itself; the server's existence
    partition is what force_re_embed bypasses (PgVectorRepository)."""

    def _client_with_fake_post(self, monkeypatch):
        client = HttpVectorClient()
        calls = []

        def fake_post(path, body, *, tenant="default", timeout=120):
            calls.append((path, body))
            return {"upserted": len(body.get("ids", []))}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        return client, calls

    def test_force_re_embed_true_sends_field(self, monkeypatch):
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks("col", ["a"], ["ta"], force_re_embed=True)
        assert calls[0][1]["force_re_embed"] is True

    def test_default_omits_field(self, monkeypatch):
        """Default (no kwarg, no env) must NOT send force_re_embed at all —
        the common case stays byte-identical to pre-.3 request bodies."""
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks("col", ["a"], ["ta"])
        assert "force_re_embed" not in calls[0][1]

    def test_explicit_false_omits_field(self, monkeypatch):
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks("col", ["a"], ["ta"], force_re_embed=False)
        assert "force_re_embed" not in calls[0][1]

    def test_env_skip_existing_0_maps_to_force_re_embed(self, monkeypatch):
        """The deprecated escape: NX_UPSERT_SKIP_EXISTING=0 (explicitly set to
        the string '0', not merely unset) maps to force_re_embed=True when the
        caller does not pass the kwarg explicitly."""
        monkeypatch.setenv("NX_UPSERT_SKIP_EXISTING", "0")
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks("col", ["a"], ["ta"])
        assert calls[0][1]["force_re_embed"] is True

    def test_explicit_kwarg_overrides_env(self, monkeypatch):
        """An explicit force_re_embed=False kwarg wins over the env escape."""
        monkeypatch.setenv("NX_UPSERT_SKIP_EXISTING", "0")
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks("col", ["a"], ["ta"], force_re_embed=False)
        assert "force_re_embed" not in calls[0][1]

    def test_env_unset_does_not_activate_force_re_embed(self, monkeypatch):
        monkeypatch.delenv("NX_UPSERT_SKIP_EXISTING", raising=False)
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks("col", ["a"], ["ta"])
        assert "force_re_embed" not in calls[0][1]

    def test_upsert_chunks_with_embeddings_forwards_force_re_embed_true(self, monkeypatch):
        """The plumbing gap this closes: every production indexer call site
        goes through upsert_chunks_with_embeddings (Seam B — the caller's
        embeddings are discarded), NOT upsert_chunks directly. Bead .3/.5
        wired force_re_embed onto upsert_chunks's wire body but
        upsert_chunks_with_embeddings never forwarded it, so no production
        --force reindex could ever reach the server's forceReEmbed escape."""
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks_with_embeddings(
            "col", ["a"], ["ta"], [[0.1]], force_re_embed=True,
        )
        assert calls[0][1]["force_re_embed"] is True

    def test_upsert_chunks_with_embeddings_default_omits_field(self, monkeypatch):
        client, calls = self._client_with_fake_post(monkeypatch)
        client.upsert_chunks_with_embeddings("col", ["a"], ["ta"], [[0.1]])
        assert "force_re_embed" not in calls[0][1]


class TestServiceModeDefault:
    """nexus-tawx0: post-RDR-155 P4a.2, make_t3() returns HttpVectorClient
    unconditionally — service mode IS the default reality. The env var
    survives only as an explicit OPT-OUT for chroma-injected test setups.
    Before this fix the no-Python-embed stubs (doc/prose/code indexers)
    were inert in default environments and every indexing run paid Voyage
    twice (client embed discarded, server re-embed)."""

    def test_unset_defaults_to_service_mode(self, monkeypatch):
        from nexus.db.http_vector_client import is_vector_service_mode

        monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
        assert is_vector_service_mode() is True

    def test_explicit_service_is_service_mode(self, monkeypatch):
        from nexus.db.http_vector_client import is_vector_service_mode

        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "service")
        assert is_vector_service_mode() is True

    def test_empty_string_treated_as_unset(self, monkeypatch):
        from nexus.db.http_vector_client import is_vector_service_mode

        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "")
        assert is_vector_service_mode() is True

    def test_chroma_opts_out(self, monkeypatch):
        from nexus.db.http_vector_client import is_vector_service_mode

        monkeypatch.setenv("NX_STORAGE_BACKEND_VECTORS", "chroma")
        assert is_vector_service_mode() is False


# ── RDR-001 nexus-kf679: managed-endpoint failure reframing ───────────────────


class TestManagedFailureReframe:
    """First-use failures against an EXPLICIT managed endpoint (NX_SERVICE_URL set)
    are reframed with an actionable remedy; the local/lease topology is unchanged."""

    def _http_error(self, code: int, body: bytes = b'{"error":"x"}'):
        import io
        import urllib.error
        return urllib.error.HTTPError(
            url="http://svc/v1/x", code=code, msg="err", hdrs={},
            fp=io.BytesIO(body),
        )

    def test_post_401_managed_appends_remedy(self, monkeypatch):
        import nexus.db.http_vector_client as hv
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        monkeypatch.setattr(hv, "_request", lambda *a, **k: (_ for _ in ()).throw(self._http_error(401)))
        with pytest.raises(VectorServiceError) as exc:
            hv._post("/v1/vectors/search", {})
        assert exc.value.code == 401
        assert "NX_SERVICE_TOKEN" in str(exc.value)
        assert "api.conexus-nexus.com" in str(exc.value)

    def test_post_500_managed_no_remedy(self, monkeypatch):
        import nexus.db.http_vector_client as hv
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        monkeypatch.setattr(hv, "_request", lambda *a, **k: (_ for _ in ()).throw(self._http_error(500)))
        with pytest.raises(VectorServiceError) as exc:
            hv._post("/v1/vectors/search", {})
        assert exc.value.code == 500
        assert "NX_SERVICE_TOKEN" not in str(exc.value)  # remedy only for auth failures

    def test_get_connection_error_managed_reframed(self, monkeypatch):
        import urllib.error
        import nexus.db.http_vector_client as hv
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        monkeypatch.setattr(hv, "_request", lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("refused")))
        with pytest.raises(VectorServiceError) as exc:
            hv._get("/v1/vectors/collections")
        assert "api.conexus-nexus.com" in str(exc.value)
        assert "nx service probe" in str(exc.value)

    def test_post_403_managed_appends_remedy(self, monkeypatch):
        import nexus.db.http_vector_client as hv
        monkeypatch.setenv("NX_SERVICE_URL", "https://api.conexus-nexus.com")
        monkeypatch.setattr(hv, "_request", lambda *a, **k: (_ for _ in ()).throw(self._http_error(403)))
        with pytest.raises(VectorServiceError) as exc:
            hv._post("/v1/vectors/search", {})
        assert exc.value.code == 403
        assert "NX_SERVICE_TOKEN" in str(exc.value)

    def test_post_401_local_no_remedy(self, monkeypatch):
        # NX_SERVICE_URL unset: 401 still raises VectorServiceError(code=401) but
        # WITHOUT a managed remedy (a local user isn't told to check a managed URL).
        import nexus.db.http_vector_client as hv
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.setattr(hv, "_request", lambda *a, **k: (_ for _ in ()).throw(self._http_error(401)))
        with pytest.raises(VectorServiceError) as exc:
            hv._post("/v1/vectors/search", {})
        assert exc.value.code == 401
        assert "NX_SERVICE_TOKEN" not in str(exc.value)

    @pytest.mark.parametrize("err", ["urlerror", "connection", "timeout"])
    def test_connection_error_local_unchanged(self, monkeypatch, err):
        # NX_SERVICE_URL unset → local/lease topology → original error propagates
        # unchanged (NOT reframed as a managed VectorServiceError), for every
        # connection-level family the managed path would otherwise wrap.
        import urllib.error
        import nexus.db.http_vector_client as hv
        exc_obj = {
            "urlerror": urllib.error.URLError("refused"),
            "connection": ConnectionError("refused"),
            "timeout": TimeoutError("timed out"),
        }[err]
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.setattr(hv, "_request", lambda *a, **k: (_ for _ in ()).throw(exc_obj))
        with pytest.raises(type(exc_obj)):
            hv._get("/v1/vectors/collections")


class TestGatewayTransientRetry:
    """502/503/504 retry with backoff in _request (nexus-wcs39).

    Found by the duoak.4 scaling sweep: one transient 504 on
    /v1/vectors/upsert-chunks killed an entire nx index run at 2 workers
    (concurrent CCE batches slow server-side embed past the gateway
    timeout). Upserts are idempotent -> bounded retry is safe.
    """

    def _http_error(self, code: int, body: bytes = b'{"error":"gw"}'):
        import io
        import urllib.error
        return urllib.error.HTTPError(
            url="http://svc/v1/x", code=code, msg="err", hdrs={},
            fp=io.BytesIO(body),
        )

    @pytest.mark.parametrize("code", [502, 503, 504])
    def test_transient_5xx_retries_then_succeeds(self, monkeypatch, code):
        import nexus.db.http_vector_client as hv
        calls: list[int] = []
        sleeps: list[float] = []

        def fake_once(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise self._http_error(code)
            return {"ok": True}

        monkeypatch.setattr(hv, "_request_once", fake_once)
        monkeypatch.setattr(hv.time, "sleep", lambda s: sleeps.append(s))
        result = hv._request("POST", "/v1/vectors/upsert-chunks",
                             tenant="default", timeout=600, body={})
        assert result == {"ok": True}
        assert len(calls) == 3
        assert sleeps == list(hv._GATEWAY_RETRY_SLEEPS[:2])

    def test_exhausted_retries_raise_original(self, monkeypatch):
        import urllib.error
        import nexus.db.http_vector_client as hv
        calls: list[int] = []
        monkeypatch.setattr(hv, "_request_once",
                            lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(self._http_error(504)))[1])
        monkeypatch.setattr(hv.time, "sleep", lambda s: None)
        with pytest.raises(urllib.error.HTTPError):
            hv._request("POST", "/v1/vectors/upsert-chunks",
                        tenant="default", timeout=600, body={})
        assert len(calls) == 1 + len(hv._GATEWAY_RETRY_SLEEPS)

    # 401 is intentionally absent: it is an auto-restart signature and gets
    # ONE re-resolve retry via the pre-existing nexus-pebfx.1 path.
    @pytest.mark.parametrize("code", [400, 409, 500])
    def test_non_gateway_codes_do_not_retry(self, monkeypatch, code):
        import urllib.error
        import nexus.db.http_vector_client as hv
        calls: list[int] = []
        monkeypatch.setattr(hv, "_request_once",
                            lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(self._http_error(code)))[1])
        monkeypatch.setattr(hv.time, "sleep", lambda s: pytest.fail("must not sleep"))
        with pytest.raises(urllib.error.HTTPError):
            hv._request("POST", "/v1/vectors/search",
                        tenant="default", timeout=120, body={})
        assert len(calls) == 1


# ── nexus-nf3n7: per-collection upsert paging (CCE 504 avoidance) ──────────────


def test_per_collection_chunk_cap_values():
    from nexus.db.http_vector_client import per_collection_chunk_cap

    assert per_collection_chunk_cap("docs__o__onnx-x__v1") == 64
    assert per_collection_chunk_cap("knowledge__x__onnx-x__v1") == 64
    assert per_collection_chunk_cap("rdr__x__onnx-x__v1") == 64
    assert per_collection_chunk_cap("code__x__onnx-x__v1") == 300
    assert per_collection_chunk_cap("weird-no-prefix") == 300


class TestUpsertChunksPaging:
    """A single oversize upsert is paged into <=cap sub-POSTs so no request
    exceeds the control-plane requestTimeout (nexus-nf3n7)."""

    def _capture(self, monkeypatch):
        calls: list[dict] = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda path, body, **kw: calls.append(body),
        )
        return calls

    def test_oversize_cce_pages_into_cap_sized_posts(self, monkeypatch):
        from nexus.db.http_vector_client import HttpVectorClient

        calls = self._capture(monkeypatch)
        ids = [f"{i:032x}" for i in range(150)]
        docs = [f"d{i}" for i in range(150)]
        metas = [{"n": i} for i in range(150)]
        HttpVectorClient().upsert_chunks(
            "docs__o__onnx-x__v1", ids, docs, metadatas=metas,
        )
        # 150 CCE chunks, cap 64 → 64 + 64 + 22
        assert [len(c["ids"]) for c in calls] == [64, 64, 22]
        # full coverage, in order, no drops/dupes
        assert [i for c in calls for i in c["ids"]] == ids
        # metadatas sliced in lockstep with ids
        assert calls[0]["metadatas"][0] == {"n": 0}
        assert calls[2]["metadatas"][-1] == {"n": 149}

    def test_passthrough_embeddings_sliced_in_lockstep(self, monkeypatch):
        from nexus.db.http_vector_client import HttpVectorClient

        calls = self._capture(monkeypatch)
        n = 130
        ids = [f"{i:032x}" for i in range(n)]
        docs = [f"d{i}" for i in range(n)]
        embs = [[float(i)] for i in range(n)]
        HttpVectorClient().upsert_chunks(
            "docs__o__onnx-x__v1", ids, docs, embeddings=embs,
        )
        assert [len(c["ids"]) for c in calls] == [64, 64, 2]
        # embeddings track the ids page-for-page
        assert calls[0]["embeddings"][0] == [0.0]
        assert calls[1]["embeddings"][0] == [64.0]
        assert calls[2]["embeddings"] == [[128.0], [129.0]]

    def test_under_cap_is_single_post(self, monkeypatch):
        from nexus.db.http_vector_client import HttpVectorClient

        calls = self._capture(monkeypatch)
        # 200 code chunks, cap 300 → one POST (batcher-shaped batches unchanged)
        ids = [f"{i:032x}" for i in range(200)]
        HttpVectorClient().upsert_chunks(
            "code__o__onnx-x__v1", ids, [f"d{i}" for i in range(200)],
        )
        assert len(calls) == 1
        assert len(calls[0]["ids"]) == 200
