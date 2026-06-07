# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nexus.db.http_vector_client (RDR-152 bead nexus-gmiaf.20)."""
from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from nexus.db.http_vector_client import (
    HttpVectorClient,
    VectorServiceError,
    get_http_vector_client,
    is_vector_service_mode,
    reset_http_vector_client_for_tests,
)


# ── is_vector_service_mode ────────────────────────────────────────────────────

class TestIsVectorServiceMode:
    def test_unset_returns_false(self, monkeypatch):
        monkeypatch.delenv("NX_STORAGE_BACKEND_VECTORS", raising=False)
        assert is_vector_service_mode() is False

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


# ── chunk_id ─────────────────────────────────────────────────────────────────

class TestChunkId:
    def test_matches_sha256_prefix(self):
        import hashlib
        text = "hello world"
        expected = hashlib.sha256(text.encode()).hexdigest()[:32]
        assert HttpVectorClient.chunk_id(text) == expected

    def test_length_is_32(self):
        cid = HttpVectorClient.chunk_id("some text here")
        assert len(cid) == 32

    def test_deterministic(self):
        t = "same text every time"
        assert HttpVectorClient.chunk_id(t) == HttpVectorClient.chunk_id(t)


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
        def fake_post(path, body, *, tenant="default"):
            calls.append((path, body))
            return {"upserted": 2}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client.upsert_chunks("my-col", ["id1", "id2"], ["text1", "text2"])
        assert len(calls) == 1
        path, body = calls[0]
        assert path == "/v1/vectors/upsert-chunks"
        assert body["collection"] == "my-col"
        assert body["ids"] == ["id1", "id2"]
        assert body["documents"] == ["text1", "text2"]

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
        client = HttpVectorClient()
        calls = []
        def fake_post(path, body, **kw):
            calls.append((path, body))
            return {"id": "abc123"}
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        returned_id = client.put("col", "abc123", "content here")
        path, body = calls[0]
        assert path == "/v1/vectors/store-put"
        assert body["doc_id"] == "abc123"
        assert body["content"] == "content here"
        assert returned_id == "abc123"


class TestGetById:
    def test_returns_dict_when_found(self, monkeypatch):
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda p, b, **kw: {"ids": ["id1"], "documents": ["text"], "metadatas": [{}]}
        )
        result = client.get_by_id("col", "id1")
        assert result == {"id": "id1", "document": "text", "metadata": {}}

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
        client = HttpVectorClient()
        fake = [{"name": "knowledge__nexus__model__v1"}]
        monkeypatch.setattr(
            "nexus.db.http_vector_client._get",
            lambda p, **kw: fake
        )
        result = client.list_collections()
        assert result == fake

    def test_returns_empty_on_service_error(self, monkeypatch):
        client = HttpVectorClient()
        def raise_err(p, **kw):
            raise VectorServiceError("error")
        monkeypatch.setattr("nexus.db.http_vector_client._get", raise_err)
        result = client.list_collections()
        assert result == []


class TestNotImplementedMethods:
    def test_delete_collection_raises(self):
        client = HttpVectorClient()
        with pytest.raises(NotImplementedError):
            client.delete_collection("col")

    def test_delete_by_source_raises(self):
        client = HttpVectorClient()
        with pytest.raises(NotImplementedError):
            client.delete_by_source("col", "/path/to/file.py")

    def test_get_embeddings_raises(self):
        client = HttpVectorClient()
        with pytest.raises(NotImplementedError):
            client.get_embeddings("col", ["id1"])


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
        import chromadb
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
