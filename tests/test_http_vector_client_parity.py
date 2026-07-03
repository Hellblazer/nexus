# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Parity tests: HttpVectorClient must be a drop-in for T3Database.

Bead nexus-7zuzz — since RDR-155 P4a.2, nexus.mcp_infra.get_t3() always
returns HttpVectorClient. Signature drift from T3Database silently broke:
  - git-hook indexing (upsert_chunks_with_embeddings collection_name kwarg)
  - MCP store_put (put() lacked T3Database.put()'s public contract)
  - get_embeddings / delete_by_source / search param names

These tests are the standing tripwire.  Every exclusion must carry a written
reason in EXCLUSIONS.
"""
from __future__ import annotations

import hashlib
import inspect
from datetime import UTC, datetime
from typing import Any

import pytest

from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.t3 import T3Database


# ── Parity PIN ───────────────────────────────────────────────────────────────


# All methods whose shared prefix we pin.  Http may have EXTRA TRAILING params
# (superset is fine); the shared prefix must be identical in name and order.
# Methods that are verified parity-OK are included even if not recently changed —
# the pin is free and catches future drift.  (CRE L2 — nexus-7zuzz review.)
DROP_IN_METHODS = [
    # Primary drift targets (nexus-7zuzz fix)
    "upsert_chunks_with_embeddings",
    "delete_by_source",
    "get_embeddings",
    "put",
    "search",
    "upsert_chunks",
    # Already-verified parity-OK methods — pinned as free tripwires
    "ids_for_source",  # nexus-vhyua: implemented to match T3Database
    "get_by_id",
    "delete_by_id",
    "list_collections",
    "collection_exists",
    "existing_ids",
    "update_chunks",
    "get_collection",
    # nexus-umvh2: were missing entirely (AttributeError in service mode) —
    # implemented + pinned so the drift class cannot silently reopen.
    "find_ids_by_title",
    "batch_delete",
    "list_store",
    "collection_info",
    # get_or_create_collection is excluded (see EXCLUSIONS below)
]

# Documented exclusions: method -> reason.
# Every entry here means we explicitly accept the deviation and have written why.
EXCLUSIONS: dict[str, str] = {
    "get_or_create_collection": (
        "T3Database.get_or_create_collection has 'strict: bool | None = None' "
        "which performs a conformant-collection-name validation gate that requires "
        "ChromaDB internals (corpus module, is_conformant_collection_name). "
        "HttpVectorClient.get_or_create_collection returns a lightweight "
        "_ServiceCollectionStub — the 'strict' gate is not implementable "
        "server-side without an extra round-trip that would degrade every "
        "doc_indexer call. The method is excluded from the name-prefix parity "
        "pin with this documented reason. (bead nexus-7zuzz)"
    ),
    "count": (
        "HttpVectorClient.count() is an Http-path-only extension: T3Database has no "
        "count() method (not in the ChromaDB T3 facade contract). The Http method "
        "provides a cheap collection record count via the Java service. No T3 parity "
        "pin possible — it is Http-only surface, not a shared-prefix method."
    ),
    "collection_stats": (
        "HttpVectorClient.collection_stats() is an Http-path-only extension "
        "(RDR-156 P3, nexus-70r3c.12): one-round-trip per-collection live stats "
        "from the nexus.collection_vector_stats view. T3Database has no equivalent "
        "(its list_collections fans out N col.count() calls). list_collections() "
        "itself IS pinned above; its return shape {name, count} is asserted in "
        "tests/test_http_vector_client_stats.py."
    ),
}


class TestSignatureParity:
    """For each method in DROP_IN_METHODS, Http's params must MATCH T3's prefix.

    Http is allowed to have EXTRA TRAILING params (superset) — the shared prefix
    must be identical in name and order.
    """

    @pytest.mark.parametrize("method_name", DROP_IN_METHODS)
    def test_shared_prefix_param_names_match(self, method_name: str):
        if method_name in EXCLUSIONS:
            pytest.skip(
                f"{method_name} excluded: {EXCLUSIONS[method_name][:80]}..."
            )

        t3_sig = inspect.signature(getattr(T3Database, method_name))
        http_sig = inspect.signature(getattr(HttpVectorClient, method_name))

        t3_params = [
            name for name in t3_sig.parameters
            if name != "self"
        ]
        http_params = [
            name for name in http_sig.parameters
            if name != "self"
        ]

        # Http must have AT LEAST as many params as T3 for the shared prefix
        assert len(http_params) >= len(t3_params), (
            f"{method_name}: HttpVectorClient has FEWER params than T3Database.\n"
            f"  T3:   {t3_params}\n"
            f"  Http: {http_params}\n"
            "The HttpVectorClient signature must cover the full T3 prefix."
        )

        # The shared prefix (first len(t3_params) params) must match exactly.
        shared_prefix = http_params[: len(t3_params)]
        assert shared_prefix == t3_params, (
            f"{method_name}: param names diverge in shared prefix.\n"
            f"  T3:            {t3_params}\n"
            f"  Http (prefix): {shared_prefix}\n"
            f"  Http (full):   {http_params}\n"
            "Fix: rename Http params to match T3 for the shared prefix."
        )

    @pytest.mark.parametrize("method_name", [
        "upsert_chunks",
    ])
    def test_upsert_chunks_has_extra_trailing_param(self, method_name: str):
        """upsert_chunks on Http has a trailing 'embeddings' param T3 lacks.

        This is the approved superset extension (Http: embeddings arg accepted
        but discarded — Seam B contract). The shared prefix must match T3's
        signature; the extra trailing param is documented here.
        """
        t3_sig = inspect.signature(getattr(T3Database, method_name))
        http_sig = inspect.signature(getattr(HttpVectorClient, method_name))

        t3_params = [n for n in t3_sig.parameters if n != "self"]
        http_params = [n for n in http_sig.parameters if n != "self"]

        # Http must have more params (it has the extra 'embeddings')
        assert len(http_params) > len(t3_params), (
            f"{method_name}: expected Http to have extra trailing params vs T3.\n"
            f"T3: {t3_params}\nHttp: {http_params}"
        )
        # Shared prefix must match T3
        prefix = http_params[: len(t3_params)]
        assert prefix == t3_params, (
            f"{method_name}: shared prefix mismatch.\n"
            f"T3: {t3_params}\nHttp prefix: {prefix}"
        )


# ── Behavior: put() must match T3Database.put() public contract ───────────────


def _reference_metadata(
    collection: str,
    content: str,
    *,
    title: str = "",
    tags: str = "",
    category: str = "",
    ttl_days: int = 0,
    session_id: str = "",
    source_agent: str = "",
    now_iso: str | None = None,
) -> dict:
    """Build the reference metadata dict using the SAME factory T3Database.put uses.

    This is the authoritative oracle for what metadata HttpVectorClient.put()
    must produce — parity by construction, not by enumeration.
    """
    from nexus.corpus import (
        embedding_model_for_collection_name,
        index_model_for_collection,
    )
    from nexus.metadata_schema import make_chunk_metadata

    if now_iso is None:
        now_iso = datetime.now(UTC).isoformat()

    content_hash = hashlib.sha256(content.encode()).hexdigest()
    prefix_to_ct = {
        "code__": "code",
        "docs__": "prose",
        "rdr__": "markdown",
        "knowledge__": "prose",
    }
    content_type = "prose"
    for prefix, ct in prefix_to_ct.items():
        if collection.startswith(prefix):
            content_type = ct
            break

    return make_chunk_metadata(
        content_type=content_type,
        chunk_text_hash=content_hash,
        content_hash=content_hash,
        chunk_start_char=0,
        chunk_end_char=len(content),
        indexed_at=now_iso,
        embedding_model=(
            embedding_model_for_collection_name(collection)
            or index_model_for_collection(collection)
        ),
        title=title,
        tags=tags,
        category=category,
        ttl_days=ttl_days,
        source_agent=source_agent,
        session_id=session_id,
    )


class TestPutBehavior:
    """HttpVectorClient.put() must match T3Database.put()'s public contract.

    T3Database.put() signature:
        put(collection, content, title='', tags='', category='',
            session_id='', source_agent='', store_type='knowledge',
            ttl_days=0, catalog_doc_id='') -> str

    The HTTP wire call is /v1/vectors/store-put. The request body must carry:
      - doc_id: sha256(content)[:32]
      - content: the raw content
      - metadata: every key make_chunk_metadata() produces (the same factory
        T3Database.put uses — parity by construction, not by duplication)

    T3Database.put is single-chunk: fail_on_oversized=True. HttpVectorClient.put
    must NOT multi-chunk. The doc_id is sha256(content)[:32].
    """

    @staticmethod
    def _fake_post_capture(calls: list) -> Any:
        def fake(path: str, body: dict, *, tenant: str = "default", timeout: int = 120):
            calls.append({"path": path, "body": body, "tenant": tenant})
            return {"id": body.get("doc_id", "fake-id")}
        return fake

    def test_put_returns_chash_doc_id(self, monkeypatch):
        """put() must return sha256(content)[:32]."""
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        content = "Hello MCP store_put content"
        expected_doc_id = hashlib.sha256(content.encode()).hexdigest()[:32]

        returned = client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content=content,
            title="test-title",
            tags="rdr-test",
            category="test",
            ttl_days=0,
            catalog_doc_id="",
        )
        assert returned == expected_doc_id, (
            f"put() must return sha256(content)[:32]; got {returned!r}"
        )

    def test_put_sends_chash_as_doc_id_in_body(self, monkeypatch):
        """The HTTP body must carry doc_id = sha256(content)[:32]."""
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        content = "content for doc_id derivation test"
        expected_doc_id = hashlib.sha256(content.encode()).hexdigest()[:32]

        client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content=content,
        )
        assert calls, "no HTTP call was made"
        body = calls[0]["body"]
        assert body["doc_id"] == expected_doc_id, (
            f"expected doc_id={expected_doc_id!r} in body, got {body.get('doc_id')!r}"
        )

    def test_put_kwarg_shape_from_mcp_core(self, monkeypatch):
        """Verify the exact kwarg shape mcp/core.py:1373 uses (the live-broken call).

        mcp/core.py calls:
            t3.put(
                collection=col_name,
                content=content,
                title=title,
                tags=tags,
                category=category,
                ttl_days=ttl_days,
                catalog_doc_id=catalog_doc_id,
            )
        This must not raise TypeError on HttpVectorClient.put().
        """
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        result = client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content="MCP store_put content body",
            title="mcp-test-title",
            tags="tag1,tag2",
            category="prose",
            ttl_days=30,
            catalog_doc_id="1.2.3",
        )
        assert result  # returns doc_id string
        assert calls, "no HTTP call made"
        body = calls[0]["body"]
        meta = body.get("metadata", {})
        assert "title" in meta, f"'title' missing from metadata: {meta}"
        assert meta["title"] == "mcp-test-title"
        assert "tags" in meta, f"'tags' missing from metadata: {meta}"
        assert "category" in meta, f"'category' missing from metadata: {meta}"
        assert meta["category"] == "prose"

    def test_put_metadata_matches_make_chunk_metadata_factory(self, monkeypatch):
        """The metadata dict must contain EVERY key make_chunk_metadata() produces.

        Parity by construction: HttpVectorClient.put() calls the same
        make_chunk_metadata factory as T3Database.put(). This test compares
        the produced metadata against a reference dict from the same factory,
        so future factory changes are automatically caught — no manual enumeration.
        """
        collection = "knowledge__nexus__minilm-l6-v2-384__v1"
        content = "Metadata factory parity test content"
        title = "parity-title"
        tags = "test,parity"
        category = "prose"
        ttl_days = 7
        session_id = "sess-abc"
        source_agent = "test-agent"

        calls: list = []

        def fake_post(path: str, body: dict, *, tenant: str = "default", timeout: int = 120):
            calls.append(body)
            return {"id": body.get("doc_id", "fake")}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

        client = HttpVectorClient()
        client.put(
            collection=collection,
            content=content,
            title=title,
            tags=tags,
            category=category,
            ttl_days=ttl_days,
            session_id=session_id,
            source_agent=source_agent,
        )
        assert calls, "no HTTP call"
        actual_meta = calls[0]["metadata"]

        # Read the timestamp the implementation produced so the reference uses
        # the same value (avoids false mismatch from two datetime.now() calls).
        actual_indexed_at = actual_meta.get("indexed_at")
        assert actual_indexed_at, "put() must stamp indexed_at in metadata"

        # Build the reference using the same factory with the captured timestamp
        reference = _reference_metadata(
            collection,
            content,
            title=title,
            tags=tags,
            category=category,
            ttl_days=ttl_days,
            session_id=session_id,
            source_agent=source_agent,
            now_iso=actual_indexed_at,
        )

        # Every key the factory produces must be present in the actual metadata
        missing_keys = [k for k in reference if k not in actual_meta]
        assert not missing_keys, (
            f"put() metadata is missing factory keys: {missing_keys}\n"
            f"Reference (from make_chunk_metadata): {sorted(reference.keys())}\n"
            f"Actual:                                {sorted(actual_meta.keys())}"
        )

        # Values for factory-produced keys must match
        mismatched = {
            k: (actual_meta[k], reference[k])
            for k in reference
            if k in actual_meta and actual_meta[k] != reference[k]
        }
        assert not mismatched, (
            "put() metadata values diverge from factory reference:\n"
            + "\n".join(f"  {k}: got={v[0]!r}, expected={v[1]!r}" for k, v in mismatched.items())
        )

        # Key fields downstream consumers require — double-check explicitly
        assert "embedding_model" in actual_meta, (
            "embedding_model is missing — search_engine.py routing depends on it"
        )
        assert "content_type" in actual_meta, (
            "content_type is missing — search_engine.py and exporter depend on it"
        )
        assert actual_meta["ttl_days"] == ttl_days, (
            f"ttl_days mismatch: got {actual_meta['ttl_days']!r}"
        )

    def test_put_metadata_carries_catalog_doc_id(self, monkeypatch):
        """catalog_doc_id flows through to metadata (HTTP-path superset).

        NOTE: This is a documented HTTP-path SUPERSET, not T3 parity.
        T3Database.put() accepts catalog_doc_id but normalize() strips it
        (not in ALLOWED_TOP_LEVEL); on the T3 path catalog association goes
        via the hook chain, not chunk metadata. HttpVectorClient stamps it
        into the service request body so the Java layer can persist the
        tumbler cross-reference. See docstring in http_vector_client.py.
        """
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content="catalog doc id test content",
            catalog_doc_id="1.5.17",
        )
        meta = calls[0]["body"].get("metadata", {})
        assert meta.get("catalog_doc_id") == "1.5.17", (
            f"catalog_doc_id not stamped into metadata body: {meta}"
        )

    def test_put_catalog_doc_id_absent_when_empty(self, monkeypatch):
        """When catalog_doc_id='' (legacy path), the key must be absent from metadata."""
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content="no catalog path",
            catalog_doc_id="",
        )
        meta = calls[0]["body"].get("metadata", {})
        assert "catalog_doc_id" not in meta, (
            f"catalog_doc_id must be absent for legacy/no-catalog path: {meta}"
        )

    def test_put_posts_to_store_put_endpoint(self, monkeypatch):
        """put() must POST to /v1/vectors/store-put."""
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content="content here",
        )
        path = calls[0]["path"]
        assert "/store-put" in path, (
            f"put() must POST to a store-put endpoint; got {path!r}"
        )

    def test_put_is_single_chunk_not_multi_chunk(self, monkeypatch):
        """T3Database.put() is single-chunk (fail_on_oversized=True). HttpVectorClient.put()
        must also be single-chunk — one HTTP call per put() call regardless of content size.
        Long content that exceeds SAFE_CHUNK_BYTES is ALLOWED to fail loud (matching T3's
        fail_on_oversized=True contract), or truncate at oversized detection, but must NOT
        silently split into multiple /v1/vectors/store-put calls.
        """
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        content = "A" * 10000  # under SAFE_CHUNK_BYTES (12288 bytes)
        client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content=content,
        )
        store_put_calls = [c for c in calls if "/store-put" in c["path"]]
        assert len(store_put_calls) == 1, (
            f"put() must be single-chunk (1 HTTP call); got {len(store_put_calls)}"
        )

    def test_put_accepts_all_t3_kwargs_without_typeerror(self, monkeypatch):
        """All T3Database.put() parameters must be accepted without TypeError."""
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            self._fake_post_capture([]),
        )
        # Full T3Database.put() call signature — must not raise TypeError
        client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content="full signature test",
            title="full-sig",
            tags="a,b",
            category="prose",
            session_id="session-abc",
            source_agent="developer",
            store_type="knowledge",
            ttl_days=14,
            catalog_doc_id="1.2",
        )

    def test_put_store_type_silently_ignored(self, monkeypatch):
        """store_type is accepted but not forwarded — symmetric with T3Database.put().

        T3Database also ignores store_type (RDR-101 Phase 5c dropped it from
        ALLOWED_TOP_LEVEL; content_type derives from the collection prefix).
        This test pins the intentional-ignore so callers don't expect it to matter.
        """
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr("nexus.db.http_vector_client._post", self._fake_post_capture(calls))

        client.put(
            collection="knowledge__nexus__minilm-l6-v2-384__v1",
            content="store type test",
            store_type="rdr",
        )
        meta = calls[0]["body"].get("metadata", {})
        # store_type must NOT appear in the metadata (it's dropped by design)
        assert "store_type" not in meta, (
            f"store_type must not be forwarded to the service (intentional drop): {meta}"
        )
        # content_type DOES appear — derived from collection prefix, not store_type
        assert "content_type" in meta, "content_type (from prefix) must be in metadata"


# ── Behavior: upsert_chunks_with_embeddings collection_name kwarg ─────────────


class TestUpsertChunksWithEmbeddingsKwarg:
    """The collection_name= kwarg form used by code_indexer, prose_indexer, exporter."""

    def test_collection_name_kwarg_accepted(self, monkeypatch):
        """upsert_chunks_with_embeddings(collection_name=...) must not raise TypeError.

        code_indexer.py:470, prose_indexer.py:233, exporter.py:431,448 all call:
            db.upsert_chunks_with_embeddings(
                collection_name=...,
                ids=..., documents=..., embeddings=..., metadatas=...,
            )
        This was breaking with TypeError on HttpVectorClient (param was 'collection').
        """
        client = HttpVectorClient()
        calls: list = []

        def fake_post(path: str, body: dict, **kw):
            calls.append(body)
            return {"upserted": 1}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

        client.upsert_chunks_with_embeddings(
            collection_name="code__nexus__minilm-l6-v2-384__v1",
            ids=["abc123"],
            documents=["def foo(): pass"],
            embeddings=[[0.1, 0.2, 0.3]],
            metadatas=[{"source_path": "/src/foo.py"}],
        )
        assert len(calls) == 1, "expected one HTTP call"
        assert calls[0].get("collection") == "code__nexus__minilm-l6-v2-384__v1", (
            f"collection name not forwarded: {calls[0]}"
        )

    def test_collection_name_positional_still_works(self, monkeypatch):
        """Positional usage must still work (backward compat with any positional callers)."""
        client = HttpVectorClient()
        calls: list = []

        def fake_post(path: str, body: dict, **kw):
            calls.append(body)
            return {"upserted": 1}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

        client.upsert_chunks_with_embeddings(
            "code__nexus__minilm-l6-v2-384__v1",
            ["id1"],
            ["text1"],
            [[0.1]],
        )
        assert calls[0]["collection"] == "code__nexus__minilm-l6-v2-384__v1"


# ── Behavior: search collection_names kwarg ───────────────────────────────────


class TestSearchCollectionNamesParam:
    """T3Database.search() names the param 'collection_names'; Http had 'collections'."""

    def test_collection_names_kwarg_accepted(self, monkeypatch):
        """search(collection_names=...) must not raise TypeError."""
        client = HttpVectorClient()
        calls: list = []

        def fake_post(path: str, body: dict, **kw):
            calls.append(body)
            return []

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

        client.search(
            query="test query",
            collection_names=["knowledge__nexus__minilm-l6-v2-384__v1"],
        )
        assert calls

    def test_collections_kwarg_still_rejected_after_rename(self, monkeypatch):
        """After renaming to collection_names, the old 'collections' kwarg must raise TypeError.

        This test is the CANARY: if it starts failing it means someone re-added the old name
        or the rename was reverted.
        """
        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda *a, **kw: [],
        )
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            client.search(
                query="q",
                collections=["col"],  # OLD name — must now fail
            )

    def test_search_still_posts_collections_in_body(self, monkeypatch):
        """Even after renaming the param, the HTTP body key must stay 'collections'
        (the Java server reads that key).
        """
        client = HttpVectorClient()
        calls: list = []

        def fake_post(path: str, body: dict, **kw):
            calls.append(body)
            return []

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

        client.search(
            query="q",
            collection_names=["col1", "col2"],
        )
        assert "collections" in calls[0], (
            "HTTP body must still use 'collections' key (Java server reads this)"
        )
        assert calls[0]["collections"] == ["col1", "col2"]


# ── Behavior: get_embeddings collection_name param ────────────────────────────


class TestGetEmbeddingsParam:
    """T3Database.get_embeddings() uses 'collection_name'; Http had 'collection'."""

    def test_collection_name_kwarg_accepted(self, monkeypatch):
        """get_embeddings(collection_name=...) must not raise TypeError."""

        client = HttpVectorClient()
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda *a, **kw: {"embeddings": [[0.1, 0.2]]},
        )
        result = client.get_embeddings(
            collection_name="knowledge__nexus__minilm-l6-v2-384__v1",
            ids=["abc"],
        )
        assert result.shape == (1, 2)

    def test_http_body_still_uses_collection_key(self, monkeypatch):
        """The HTTP body must still send 'collection' (Java endpoint field name)."""
        client = HttpVectorClient()
        calls: list = []

        def fake_post(path: str, body: dict, **kw):
            calls.append(body)
            return {"embeddings": []}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

        client.get_embeddings(
            collection_name="knowledge__nexus__minilm-l6-v2-384__v1",
            ids=["abc"],
        )
        assert "collection" in calls[0], (
            "HTTP body key must be 'collection' (Java endpoint field)"
        )
        assert calls[0]["collection"] == "knowledge__nexus__minilm-l6-v2-384__v1"


# ── Behavior: delete_by_source / ids_for_source (nexus-vhyua) ────────────────


class TestDeleteBySource:
    """nexus-vhyua: delete_by_source was a NotImplementedError stub, making
    ``nx t3 prune-stale`` silently no-op in service mode. It is now built from
    the existing /v1/vectors/get (where-filter) + /v1/vectors/store-delete
    endpoints. Param name ``collection_name`` matches T3Database (nexus-7zuzz).
    """

    def test_ids_for_source_paginates_and_collects(self, monkeypatch):
        # Two pages (300 then 2) -> single flat id list; second short page ends it.
        pages = [
            {"ids": [f"id{i}" for i in range(300)]},
            {"ids": ["id300", "id301"]},
        ]
        calls = []

        def fake_post(path, body, **kw):
            # Index by call order, not a hard-coded page size (robust if the
            # quota constant changes).
            page = pages[len(calls)]
            calls.append((path, body))
            return page

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        ids = client.ids_for_source(
            collection_name="code__nexus__minilm-l6-v2-384__v1",
            source_path="/src/foo.py",
        )
        assert len(ids) == 302
        # where-filter is by source_path; param key is collection_name's value.
        assert calls[0][1]["where"] == {"source_path": "/src/foo.py"}
        assert all(c[0] == "/v1/vectors/get" for c in calls)

    def test_ids_for_source_404_first_page_returns_empty(self, monkeypatch):
        # Collection-not-found (404 on page 0) is the ONLY swallowed case.
        from nexus.db.http_vector_client import VectorServiceError

        def fake_post(path, body, **kw):
            raise VectorServiceError("not found", code=404)

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        assert client.ids_for_source("missing-col", "/src/foo.py") == []

    def test_ids_for_source_mid_pagination_error_reraises(self, monkeypatch):
        # A 500 on page 2 (after ids collected) must NOT be masked as "no chunks"
        # — else delete_by_source would under-delete and report success.
        from nexus.db.http_vector_client import VectorServiceError

        def fake_post(path, body, **kw):
            if body["offset"] == 0:
                return {"ids": [f"id{i}" for i in range(300)]}
            raise VectorServiceError("server error", code=500)

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        with pytest.raises(VectorServiceError):
            client.ids_for_source("code__nexus__minilm-l6-v2-384__v1", "/src/foo.py")

    def test_delete_by_source_deletes_resolved_ids(self, monkeypatch):
        posted = []

        def fake_post(path, body, **kw):
            posted.append((path, body))
            if path == "/v1/vectors/get":
                # one short page of 3 ids
                return {"ids": ["a", "b", "c"]}
            return {"deleted": len(body["ids"])}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        n = client.delete_by_source(
            collection_name="code__nexus__minilm-l6-v2-384__v1",
            source_path="/src/foo.py",
        )
        assert n == 3
        delete_calls = [b for p, b in posted if p == "/v1/vectors/store-delete"]
        assert delete_calls and delete_calls[0]["ids"] == ["a", "b", "c"]

    def test_delete_by_source_no_ids_is_noop(self, monkeypatch):
        posted = []

        def fake_post(path, body, **kw):
            posted.append(path)
            return {"ids": []}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        client = HttpVectorClient()
        n = client.delete_by_source(
            collection_name="c", source_path="/none",
        )
        assert n == 0
        assert "/v1/vectors/store-delete" not in posted  # no delete attempted

    def test_delete_by_source_collection_name_kwarg_no_typeerror(self, monkeypatch):
        # Param-name parity guard (nexus-7zuzz): collection_name must be accepted.
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post",
            lambda *a, **kw: {"ids": []},
        )
        client = HttpVectorClient()
        # Must NOT raise TypeError (wrong param) or NotImplementedError (old stub).
        assert client.delete_by_source(
            collection_name="code__nexus__minilm-l6-v2-384__v1",
            source_path="/src/foo.py",
        ) == 0
