# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the pg-source read adapter (nexus-te885.8.1).

Chroma-shaped, read-only HTTP adapter over a LOCAL nexus-service pgvector
store, so ``verify_fill_collections`` (``vector_etl.py``) can reconcile rows
written directly to pgvector post-cutover that exist in no Chroma store at
all (the nexus-te885.1 incident class). Hermetic: ``_post``/``_get`` are
monkeypatched module-level functions, no real HTTP, mirroring the
``tests/db/test_http_vector_client.py`` mocking convention.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from nexus.migration import pg_read
from nexus.migration.chroma_read import iter_collection_chunks
from nexus.migration.pg_read import PgReadClient, PgReadError


class TestConstruction:
    def test_requires_base_url(self) -> None:
        with pytest.raises(ValueError, match="base_url"):
            PgReadClient("", "tok")

    def test_requires_token(self) -> None:
        with pytest.raises(ValueError, match="token"):
            PgReadClient("http://localhost:9999", "")

    def test_explicit_pair_no_env_or_singleton_dependency(self, monkeypatch) -> None:
        """Constructed from an EXPLICIT (base_url, token) pair — no env var
        or lease resolution, so it can be pointed at a second local service
        while HttpVectorClient (env/lease-resolved) is the migration TARGET."""
        monkeypatch.delenv("NX_SERVICE_URL", raising=False)
        monkeypatch.delenv("NX_SERVICE_TOKEN", raising=False)
        client = PgReadClient("http://localhost:9999", "explicit-token")
        assert client is not None

    def test_no_http_vector_client_dependency(self) -> None:
        """Static contract: the adapter must not reuse HttpVectorClient's
        process-wide singleton — it makes its own HTTP calls to the
        explicit URL (locked design decision, T2 nexus/design-te885.8).
        Walks the AST (skipping the module/function docstrings, which
        legitimately name what the design does NOT do) so no import,
        call, or attribute access anywhere touches http_vector_client."""
        tree = ast.parse(inspect.getsource(pg_read))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        assert not any("http_vector_client" in n for n in names)
        assert "HttpVectorClient" not in names
        assert "get_http_vector_client" not in names
        assert "_resolve_endpoint" not in names


class TestListCollections:
    def test_returns_name_objects(self, monkeypatch) -> None:
        def fake_get(base_url, token, path, *, tenant="default", timeout=30):
            assert base_url == "http://localhost:9999"
            assert token == "tok"
            assert path == "/v1/vectors/collections"
            return [
                {"name": "code__nexus-1-1__voyage-code-3__v1"},
                {"name": "knowledge__nexus-1-1__voyage-context-3__v1"},
            ]

        monkeypatch.setattr(pg_read, "_get", fake_get)
        client = PgReadClient("http://localhost:9999", "tok")
        cols = client.list_collections()
        assert [c.name for c in cols] == [
            "code__nexus-1-1__voyage-code-3__v1",
            "knowledge__nexus-1-1__voyage-context-3__v1",
        ]

    def test_skips_nameless_entries(self, monkeypatch) -> None:
        monkeypatch.setattr(
            pg_read, "_get", lambda *a, **k: [{"name": "a"}, {}, {"name": ""}]
        )
        client = PgReadClient("http://localhost:9999", "tok")
        assert [c.name for c in client.list_collections()] == ["a"]


class TestCount:
    def test_count_hits_count_endpoint(self, monkeypatch) -> None:
        def fake_get(base_url, token, path, *, tenant="default", timeout=30):
            assert path == "/v1/vectors/count?collection=code__x"
            return {"count": 42}

        monkeypatch.setattr(pg_read, "_get", fake_get)
        client = PgReadClient("http://localhost:9999", "tok")
        assert client.get_collection("code__x").count() == 42

    def test_count_missing_key_defaults_zero(self, monkeypatch) -> None:
        monkeypatch.setattr(pg_read, "_get", lambda *a, **k: {})
        client = PgReadClient("http://localhost:9999", "tok")
        assert client.get_collection("code__x").count() == 0


class TestGetNoEmbeddings:
    def test_single_call_no_embeddings_stitch(self, monkeypatch) -> None:
        calls = []

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            calls.append((path, dict(body)))
            return {
                "ids": ["a", "b"],
                "documents": ["doc-a", "doc-b"],
                "metadatas": [{"m": 1}, {"m": 2}],
            }

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        result = client.get_collection("code__x").get(
            include=["documents", "metadatas"], limit=300, offset=0
        )
        assert result["ids"] == ["a", "b"]
        assert result["documents"] == ["doc-a", "doc-b"]
        assert result["metadatas"] == [{"m": 1}, {"m": 2}]
        assert result["embeddings"] is None
        assert len(calls) == 1
        assert calls[0] == (
            "/v1/vectors/get",
            {"collection": "code__x", "limit": 300, "offset": 0},
        )

    def test_empty_page_no_embeddings_call(self, monkeypatch) -> None:
        calls = []

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            calls.append(path)
            return {"ids": [], "documents": [], "metadatas": []}

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        result = client.get_collection("code__x").get(
            include=["embeddings"], limit=300, offset=0
        )
        assert calls == ["/v1/vectors/get"]
        assert result["ids"] == []
        assert result["embeddings"] == []


class TestGetEmbeddingsStitch:
    """Plan-audit CRITICAL finding: /v1/vectors/get accepts-and-ignores
    ``include`` and NEVER returns embeddings — the same-model passthrough
    path needs a second call to /v1/vectors/get-embeddings or every chunk
    silently loses its vector and trips a billed Voyage re-embed."""

    def test_two_endpoint_stitch_fires(self, monkeypatch) -> None:
        calls = []

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            calls.append((path, dict(body)))
            if path == "/v1/vectors/get":
                return {
                    "ids": ["a", "b", "c"],
                    "documents": ["da", "db", "dc"],
                    "metadatas": [{}, {}, {}],
                }
            if path == "/v1/vectors/get-embeddings":
                return {"ids": ["a", "b", "c"], "embeddings": [[1.0], [2.0], [3.0]]}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        result = client.get_collection("code__x").get(
            include=["documents", "metadatas", "embeddings"], limit=300, offset=0
        )
        # BOTH calls genuinely fired, in order, with the correct id set forwarded.
        assert [c[0] for c in calls] == ["/v1/vectors/get", "/v1/vectors/get-embeddings"]
        assert calls[1][1] == {"collection": "code__x", "ids": ["a", "b", "c"]}
        assert result["embeddings"] == [[1.0], [2.0], [3.0]]

    def test_no_embeddings_requested_skips_second_call(self, monkeypatch) -> None:
        calls = []

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            calls.append(path)
            return {"ids": ["a"], "documents": ["da"], "metadatas": [{}]}

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        result = client.get_collection("code__x").get(
            include=["documents", "metadatas"], limit=300, offset=0
        )
        assert calls == ["/v1/vectors/get"]
        assert result["embeddings"] is None


class TestEmbeddingsAlignment:
    """Plan-audit concern: get-embeddings DROPS ids it cannot find, in
    request order, with no explicit id-correlation beyond ordering. The
    stitch must key back to the correct id — never silently misalign a
    vector onto the wrong chunk."""

    def test_dropped_id_yields_none_not_a_shift(self, monkeypatch) -> None:
        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            if path == "/v1/vectors/get":
                return {
                    "ids": ["a", "b", "c"],
                    "documents": ["da", "db", "dc"],
                    "metadatas": [{}, {}, {}],
                }
            if path == "/v1/vectors/get-embeddings":
                # 'b' dropped by the server — 'c's embedding must NOT shift
                # into 'b's slot.
                return {"ids": ["a", "c"], "embeddings": [[1.0], [3.0]]}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        result = client.get_collection("code__x").get(
            include=["embeddings"], limit=300, offset=0
        )
        assert result["ids"] == ["a", "b", "c"]
        assert result["embeddings"] == [[1.0], None, [3.0]]

    def test_multiple_dropped_ids_each_independently_none(self, monkeypatch) -> None:
        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            if path == "/v1/vectors/get":
                return {
                    "ids": ["a", "b", "c", "d"],
                    "documents": ["da", "db", "dc", "dd"],
                    "metadatas": [{}, {}, {}, {}],
                }
            if path == "/v1/vectors/get-embeddings":
                # 'a' and 'c' dropped.
                return {"ids": ["b", "d"], "embeddings": [[2.0], [4.0]]}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        result = client.get_collection("code__x").get(
            include=["embeddings"], limit=300, offset=0
        )
        assert result["embeddings"] == [None, [2.0], None, [4.0]]


class TestOffsetPagination:
    """Multi-page ``.get()`` calls with offset advancing correctly — the
    single-page happy path alone is insufficient per the plan audit.
    Exercised through the real consumer (``iter_collection_chunks``) to
    prove genuine Chroma-shape duck-type compatibility, not just a mock
    matching the adapter's own internal assumptions."""

    def test_offset_advances_across_pages_no_embeddings(self, monkeypatch) -> None:
        all_ids = ["id0", "id1", "id2", "id3", "id4"]
        get_bodies = []

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            assert path == "/v1/vectors/get"
            get_bodies.append(dict(body))
            offset, limit = body["offset"], body["limit"]
            page = all_ids[offset : offset + limit]
            return {
                "ids": page,
                "documents": [f"doc-{i}" for i in page],
                "metadatas": [{} for _ in page],
            }

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        rows = list(iter_collection_chunks(client, "code__x", page_size=2))
        assert [r["id"] for r in rows] == all_ids
        # offset must genuinely advance: 0, 2, 4 (third call returns <page, stops)
        assert [b["offset"] for b in get_bodies] == [0, 2, 4]

    def test_offset_advances_across_pages_with_embeddings_stitch(self, monkeypatch) -> None:
        all_ids = ["id0", "id1", "id2"]
        all_embs = {"id0": [0.0], "id1": [1.0], "id2": [2.0]}
        get_calls, emb_calls = [], []

        def fake_post(base_url, token, path, body, *, tenant="default", timeout=120):
            if path == "/v1/vectors/get":
                get_calls.append(dict(body))
                offset, limit = body["offset"], body["limit"]
                page = all_ids[offset : offset + limit]
                return {
                    "ids": page,
                    "documents": [f"d-{i}" for i in page],
                    "metadatas": [{} for _ in page],
                }
            if path == "/v1/vectors/get-embeddings":
                emb_calls.append(dict(body))
                ids = body["ids"]
                return {"ids": ids, "embeddings": [all_embs[i] for i in ids]}
            raise AssertionError(f"unexpected path {path}")

        monkeypatch.setattr(pg_read, "_post", fake_post)
        client = PgReadClient("http://localhost:9999", "tok")
        rows = list(
            iter_collection_chunks(
                client, "code__x", page_size=2, include_embeddings=True
            )
        )
        assert len(rows) == 3
        # page1 (offset 0, 2 ids, ==page -> continue), page2 (offset 2, 1 id, <page -> stop)
        assert [b["offset"] for b in get_calls] == [0, 2]
        assert len(emb_calls) == 2
        by_id = {r["id"]: r for r in rows}
        assert by_id["id1"]["embedding"] == [1.0]
        assert by_id["id2"]["embedding"] == [2.0]


class TestErrorPropagation:
    def test_http_error_raises_pg_read_error(self, monkeypatch) -> None:
        def fake_get(base_url, token, path, *, tenant="default", timeout=30):
            raise PgReadError("GET /v1/vectors/collections -> HTTP 401: bad token", code=401)

        monkeypatch.setattr(pg_read, "_get", fake_get)
        client = PgReadClient("http://localhost:9999", "bad-token")
        with pytest.raises(PgReadError, match="401"):
            client.list_collections()
