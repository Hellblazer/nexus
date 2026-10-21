# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Parity tests: HttpVectorClient must be a drop-in for T3Database.

Bead nexus-7zuzz — since RDR-155 P4a.2, nexus.mcp_infra.get_t3() always
returns HttpVectorClient. Signature drift from T3Database silently broke:
  - git-hook indexing (upsert_chunks_with_embeddings collection_name kwarg)
  - get_embeddings / delete_by_source / search param names

These tests are the standing tripwire.  Every exclusion must carry a written
reason in EXCLUSIONS.
"""
from __future__ import annotations

import inspect
from typing import Any

import pytest

from nexus.db.http_vector_client import HttpVectorClient
from nexus.db.t3 import T3Database


@pytest.fixture(autouse=True)
def _no_real_catalog_registration(monkeypatch: pytest.MonkeyPatch):
    """RDR-204 Phase 1 (nexus-f5wwx): see the identical fixture in
    tests/db/test_http_vector_client.py for the full rationale — this
    file's upsert_chunks() tests are pure HTTP-shape tests using
    arbitrary collection name strings, so the registration step is
    stubbed to a bare passthrough here too.
    """
    monkeypatch.setattr(
        "nexus.corpus.write_with_registration_retry",
        lambda name, write_fn, **kwargs: write_fn(),
    )
    yield


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
    # nexus-h8rf6.5: was missing entirely (AttributeError on `nx store expire`
    # in service mode) — implemented client-side with the supported $ne
    # operator (the T3 $gt-0 pre-filter's only job is excluding the
    # permanent ttl_days=0 sentinel; TTLs are never negative).
    "expire",
    # nexus-h8rf6.6: nx doctor fix-paths crashed (unguarded per-row call).
    "update_source_path",
    # nexus-h8rf6.7: nx t3 gc / prune-stale silently no-oped (call sites
    # try/except-wrapped, so the missing methods degraded instead of crashing).
    # (delete_by_chunk_ids was deleted from both classes with nexus-wbfpw.18.)
    "list_unique_source_paths",
    "list_chunks_with_metadata",
    # nexus-h8rf6.8: doctor's model-drift probe degraded to outcome='error'
    # for every collection. Full parity is client-side derivable: the model
    # fields come from the collection NAME, only count needs the server.
    "collection_metadata",
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
    "hybrid_search": (
        "HttpVectorClient.hybrid_search() is an Http-path-only extension "
        "(RDR-217 P2.1, bead nexus-lqo4p.6): POST /v1/vectors/hybrid-search "
        "reads the engine's tsvector and pg_trgm indexes, and T3Database has "
        "no hybrid route for it to be a drop-in for. Deliberately NOT a "
        "drop-in: the route is not a superset of search(), so a caller "
        "reaching retrieval through the duck-typed t3 must gate on the "
        "capability rather than assume the method is there. What --lexical "
        "does against a backend without the leg is RDR-217 bead .5's ask."
    ),
    "ids_for_doc_id": (
        "NOT ported to HttpVectorClient — dead code (wave review, substantive-"
        "critic): no caller outside T3Database itself. Documented here so the "
        "'every live T3 method is ported or excluded-with-reason' claim stays "
        "auditable. If a caller ever appears, port it (ids_for_source pattern) "
        "and move it into DROP_IN_METHODS."
    ),
    "delete_by_doc_id": (
        "NOT ported to HttpVectorClient — dead code (wave review, substantive-"
        "critic): no caller outside T3Database itself. Same disposition as "
        "ids_for_doc_id above."
    ),
    "put": (
        "RETIRED from HttpVectorClient (nexus-nt76j): the engine's POST "
        "/v1/vectors/store-put route is gone and every note writer rides the "
        "combined chunk-plus-owner write (RDR-223), so the single-chunk put has "
        "no caller. T3Database.put stays for injected in-memory clients. Not in "
        "DROP_IN_METHODS; a deliberate deviation, not drift."
    ),
    "rename_collection": (
        "NOT ported to HttpVectorClient — not live in service mode: "
        "collection_rename.py short-circuits to catalog.rename_collection_"
        "cascade() (one atomic server transaction) before ever reaching "
        "t3_db.rename_collection() (wave review, substantive-critic)."
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


# ── Behavior: upsert_chunks_with_embeddings collection_name kwarg ─────────────


@pytest.mark.t2_tenant
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


@pytest.mark.t2_tenant
class TestUpsertChunksOversizedDropAndWarn:
    """nexus-xzyr3 fold-in round 2 (dev-suite-reds-2026-09-05-wave-fold):
    a document over ``QUOTAS.MAX_DOCUMENT_BYTES`` (16384) must NOT be
    silently dropped by ``upsert_chunks``/``upsert_chunks_with_embeddings``
    — that was the wrong fix. MAX_DOCUMENT_BYTES is a ChromaDB-era STORAGE
    quota; the paging path (RDR-195, nexus-nf3n7/nexus-kmtlp,
    ``tests/db/test_http_vector_client.py::TestUpsertChunksPaging``, the
    pre-existing spec) already handles arbitrarily large chunks correctly —
    a chunk over any page's byte budget ships alone in its own page — and a
    chunk genuinely too large for Voyage surfaces as the engine's typed 422
    (``cda82c8a5``), never a silent client-side drop. A prior fold-in
    (f668f9b02) mirrored T3Database._write_batch's drop-and-warn onto this
    method anyway, which emptied every batch in TestUpsertChunksPaging
    (18,000-300,000-byte synthetic chunks, all legitimately shippable) —
    reverted. These tests now pin the CORRECTED contract: no drop, ever,
    regardless of document size. (``put()``, the single-chunk sibling the
    original 9-row evidence pointed at, is retired, nexus-nt76j.)
    """

    @staticmethod
    def _fake_post_ack_matching(calls: list) -> Any:
        def fake(path: str, body: dict, *, tenant: str = "default", timeout: int = 120):
            calls.append(body)
            return {"upserted": len(body.get("ids", []))}
        return fake

    def test_upsert_chunks_ships_oversized_document_never_drops(self, monkeypatch):
        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_ack_matching(calls)
        )

        oversized = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
        good = "y" * 32

        # Must not raise, must not drop either document.
        client.upsert_chunks(
            "knowledge__nexus__minilm-l6-v2-384__v1",
            ids=["a", "b"],
            documents=[oversized, good],
            metadatas=[{"title": "a"}, {"title": "b"}],
        )

        assert len(calls) == 1, f"expected exactly one HTTP call; got {calls}"
        assert calls[0]["ids"] == ["a", "b"], (
            f"both documents must ship, oversized included; got ids={calls[0]['ids']}"
        )
        assert calls[0]["documents"] == [oversized, good]

    def test_upsert_chunks_with_embeddings_ships_oversized_document(self, monkeypatch):
        """upsert_chunks_with_embeddings is a thin forward to upsert_chunks
        (it discards its own embeddings and delegates); this pins that the
        corrected no-drop contract is inherited, not diverged."""
        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_ack_matching(calls)
        )

        oversized = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
        good = "y" * 32

        client.upsert_chunks_with_embeddings(
            collection_name="knowledge__nexus__minilm-l6-v2-384__v1",
            ids=["a", "b"],
            documents=[oversized, good],
            embeddings=[[0.1], [0.2]],
            metadatas=[{"title": "a"}, {"title": "b"}],
        )

        assert len(calls) == 1
        assert calls[0]["ids"] == ["a", "b"]

    def test_upsert_chunks_single_oversized_document_still_sends(self, monkeypatch):
        """A batch of exactly one oversized document ships — mirrors
        TestUpsertChunksPaging::test_single_oversize_chunk_ships_alone at
        the collection-quota boundary rather than the paging byte budget."""
        from nexus.db.limits import QUOTAS

        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_ack_matching(calls)
        )

        oversized = "x" * (QUOTAS.MAX_DOCUMENT_BYTES + 1)
        client.upsert_chunks(
            "knowledge__nexus__minilm-l6-v2-384__v1",
            ids=["a"],
            documents=[oversized],
            metadatas=[{"title": "a"}],
        )
        assert len(calls) == 1
        assert calls[0]["ids"] == ["a"]

    def test_upsert_chunks_under_cap_unaffected(self, monkeypatch):
        """Regression guard: ordinary batches are not touched by the new check."""
        client = HttpVectorClient()
        calls: list = []
        monkeypatch.setattr(
            "nexus.db.http_vector_client._post", self._fake_post_ack_matching(calls)
        )

        client.upsert_chunks(
            "knowledge__nexus__minilm-l6-v2-384__v1",
            ids=["a", "b"],
            documents=["small one", "small two"],
            metadatas=[{"title": "a"}, {"title": "b"}],
        )
        assert len(calls) == 1
        assert calls[0]["ids"] == ["a", "b"]


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


# ── Behavior: the source_path-keyed methods are RETIRED (nexus-bm8dd) ────────
#
# TestDeleteBySource / TestUpdateSourcePath used to live here, pinning the wire
# behaviour of ids_for_source, delete_by_source and update_source_path: their
# pagination, their 404 handling, their mid-pagination re-raise. Every one of
# those pins passed, and the methods were dead the whole time.
#
# They asserted what the CLIENT SENT. RDR-102 D2 had hard-removed source_path
# from the chunk schema, so what came BACK was always empty, and each method
# dutifully returned its no-rows value — [] / 0 / [] / 0 — which is
# indistinguishable from "there was nothing to do". `nx t3 prune-stale`
# reported "0 stale" on every corpus, `nx doctor fix-paths` reported "0 T3
# chunks updated" as if that were a result, and the documented "delete this
# document's chunks and re-index" recovery deleted nothing.
#
# The methods now raise. These pins replace the wire-shape ones, and they are
# deliberately about the RAISE, not the message: a test that asserts a method
# still paginates correctly is exactly what let this run for months.


class TestSourcePathMethodsRetired:
    """nexus-bm8dd: the four source_path-keyed methods must FAIL LOUD.

    Returning the empty/zero value was the defect — it let callers, and their
    tests, read "cannot look" as "looked, found nothing".
    """

    @staticmethod
    def _client(monkeypatch) -> HttpVectorClient:
        # No transport needed: the guard is the first statement in each method,
        # so nothing may reach the wire. If a future edit moves the raise below
        # a _post, these tests fail on the network instead of passing — which is
        # the correct signal.
        return HttpVectorClient()

    def test_ids_for_source_raises(self, monkeypatch):
        c = self._client(monkeypatch)
        with pytest.raises(NotImplementedError) as e:
            c.ids_for_source("code__x__minilm-l6-v2-384__v1", "/src/foo.py")
        assert "source_path" in str(e.value)
        assert "nexus-bm8dd" in str(e.value)

    def test_delete_by_source_raises(self, monkeypatch):
        c = self._client(monkeypatch)
        with pytest.raises(NotImplementedError):
            c.delete_by_source("code__x__minilm-l6-v2-384__v1", "/src/foo.py")

    def test_update_source_path_raises(self, monkeypatch):
        c = self._client(monkeypatch)
        with pytest.raises(NotImplementedError):
            c.update_source_path("code__x__minilm-l6-v2-384__v1", "/old.py", "/new.py")

    def test_list_unique_source_paths_raises(self, monkeypatch):
        c = self._client(monkeypatch)
        with pytest.raises(NotImplementedError):
            c.list_unique_source_paths("code__x__minilm-l6-v2-384__v1")

    def test_the_message_names_the_replacement_addressing(self, monkeypatch):
        """An operator or a future caller hitting this needs to be told what
        replaced it, not merely that it is gone."""
        c = self._client(monkeypatch)
        with pytest.raises(NotImplementedError) as e:
            c.list_unique_source_paths("c")
        msg = str(e.value)
        assert "manifest" in msg
        assert "list_by_collection" in msg


class TestExpire:
    """nexus-h8rf6.5: expire() was missing entirely — `nx store expire`
    crashed with AttributeError in service mode. Implemented client-side.

    nexus-tk070.p6b (RDR-194 D5) UPDATE: originally the server's
    where-translator supported $eq/$ne/$in/$nin only (no $gt), so this
    method used the NULL-inclusive `{"ttl_days": {"$ne": 0}}` as a
    numeric-comparison workaround for T3Database.expire's own
    `{"ttl_days": {"$gt": 0}}` (equivalent then: TTLs are never negative,
    and rows with absent ttl_days were kept by the server but rejected by
    is_expired Python-side either way). Range operators landed later
    (nexus-4l80g) but this method was never retrofitted — until this pass,
    which retires the divergence: this method now sends the IDENTICAL
    `{"ttl_days": {"$gt": 0}}` T3Database.expire always used. See
    HttpVectorClient.expire's own docstring for why `$ne: null` (the
    literal spelling the D5 sentinel flip might suggest) does NOT work as
    a substitute — it is vacuous server-side by construction
    (PgVectorRepository.appendWherePredicate's $ne binds
    String.valueOf(operand), and String.valueOf(None) is the four-char
    string "null", not SQL NULL).
    """

    _KNOWLEDGE = "knowledge__nexus-1-1__voyage-context-3__v1"

    @staticmethod
    def _meta(ttl_days: int, indexed_at: str) -> dict:
        return {"ttl_days": ttl_days, "indexed_at": indexed_at}

    def _patch(self, monkeypatch, collections, fake_post):
        # RDR-204 Phase 3 fixture-seam fix (nexus-ft04v.26): expire() now
        # filters list_collections() entries by their row's content_type
        # (class (c) repoint) instead of a name prefix -- a fixture entry
        # with none of the row fields silently reads as unregistered and
        # is skipped every pass.
        from tests.conftest import catalog_row_for_collection_name

        monkeypatch.setattr(
            HttpVectorClient, "list_collections",
            lambda self: [
                {"name": n, "count": 1, **catalog_row_for_collection_name(n)}
                for n in collections
            ],
        )
        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)

    @pytest.mark.t2_tenant
    def test_expire_deletes_only_expired_knowledge_rows(self, monkeypatch):
        """Neutral model token on purpose (RDR-109 mode lint): the code__
        collection below is included only to prove it is EXCLUDED from
        expire's knowledge-only scan; list_collections() is monkeypatched
        in `_patch`, so no embedder or catalog probe is ever reached."""
        posted = []

        def fake_post(path, body, **kw):
            posted.append((path, body))
            if path == "/v1/vectors/get":
                if body["offset"] > 0:
                    return {"ids": [], "metadatas": []}
                return {
                    "ids": ["expired1", "permanent", "fresh", "no-ttl"],
                    "metadatas": [
                        self._meta(1, "2020-01-01T00:00:00+00:00"),   # expired
                        # nexus-tk070.p6b: a real server's $gt:0 predicate
                        # structurally excludes ttl_days=0/null/missing rows
                        # (jsonb_typeof(...)='number' AND >0) — this fake
                        # deliberately ignores the `where` it was sent and
                        # returns them anyway, so this test also proves
                        # is_expired() is a correct defense-in-depth second
                        # check, not merely that the server's own filtering
                        # (exercised for real by the Java integration suite)
                        # works.
                        self._meta(0, "2020-01-01T00:00:00+00:00"),   # permanent
                        self._meta(36500, "2026-01-01T00:00:00+00:00"),  # not yet
                        {},                                            # absent ttl_days
                    ],
                }
            return {"deleted": len(body["ids"])}

        self._patch(
            monkeypatch,
            [self._KNOWLEDGE, "code__nexus-1-1__model-code__v1"],
            fake_post,
        )
        n = HttpVectorClient().expire()
        assert n == 1
        gets = [b for p, b in posted if p == "/v1/vectors/get"]
        # only the knowledge__ collection is queried, with the $gt pre-filter
        assert all(b["collection"] == self._KNOWLEDGE for b in gets)
        assert gets[0]["where"] == {"ttl_days": {"$gt": 0}}
        deletes = [b for p, b in posted if p == "/v1/vectors/store-delete"]
        assert deletes == [{"collection": self._KNOWLEDGE, "ids": ["expired1"]}]

    @pytest.mark.t2_tenant
    def test_expire_paginates_and_accumulates_before_deleting(self, monkeypatch):
        posted = []

        def fake_post(path, body, **kw):
            posted.append((path, body))
            if path == "/v1/vectors/get":
                if body["offset"] == 0:
                    page_ids = [f"id{i}" for i in range(300)]
                    return {
                        "ids": page_ids,
                        "metadatas": [
                            self._meta(1, "2020-01-01T00:00:00+00:00")
                        ] * 300,
                    }
                return {
                    "ids": ["last"],
                    "metadatas": [self._meta(1, "2020-01-01T00:00:00+00:00")],
                }
            return {"deleted": len(body["ids"])}

        self._patch(monkeypatch, [self._KNOWLEDGE], fake_post)
        n = HttpVectorClient().expire()
        assert n == 301
        # all gets precede the first delete (accumulate-then-delete: deleting
        # mid-pagination would shift offsets and skip rows)
        paths = [p for p, _ in posted]
        assert paths.index("/v1/vectors/store-delete") > max(
            i for i, p in enumerate(paths) if p == "/v1/vectors/get"
        )
        # deletes are batched at the 300-record write quota
        deletes = [b for p, b in posted if p == "/v1/vectors/store-delete"]
        assert [len(b["ids"]) for b in deletes] == [300, 1]

    def test_expire_no_knowledge_collections_returns_zero(self, monkeypatch):
        """Neutral model token on purpose (RDR-109 mode lint): same
        reason as the sibling test above."""
        def fake_post(path, body, **kw):  # pragma: no cover — must not be called
            raise AssertionError("no HTTP call expected")

        self._patch(monkeypatch, ["code__nexus-1-1__model-code__v1"], fake_post)
        assert HttpVectorClient().expire() == 0

    def test_expire_predicate_matches_t3database_exactly(self, monkeypatch):
        """nexus-tk070.p6b (RDR-194 D5): the subtlest part of this phase —
        BEFORE this pass, HttpVectorClient.expire's `{"ttl_days": {"$ne":
        0}}` and T3Database.expire's `{"ttl_days": {"$gt": 0}}` were only
        EQUIVALENT (same fetched row set for every shape this repo's data
        takes), never IDENTICAL — two different spellings, kept in sync by
        hand. AFTER this pass they are the literal same dict.

        Mechanism (code-review cosmetic finding, 2026-08-20 fix-pass — the
        prior wording overclaimed a live import/reflection binding): two
        INDEPENDENT literal assertions, not a shared constant. The first
        greps T3Database.expire's OWN source text via inspect.getsource for
        the exact string; the second checks HttpVectorClient.expire's
        actually-produced dict. Both directions of drift are still caught
        (either assertion fails on its own if only one side changes), so
        the protection is real — it just isn't a single shared source of
        truth the way "sourced from ... at import time" would imply.
        """
        from nexus.db.t3 import T3Database

        t3_ttl_where = inspect.getsource(T3Database.expire)
        assert '{"ttl_days": {"$gt": 0}}' in t3_ttl_where, (
            "T3Database.expire's own predicate must still be $gt:0 — if this "
            "assertion ever fails because T3Database changed, HttpVectorClient "
            "must change with it, not silently diverge again"
        )

        def fake_post(path, body, **kw):
            if path == "/v1/vectors/get":
                return {"ids": [], "metadatas": []}
            return {"deleted": 0}

        posted = []

        def capturing_post(path, body, **kw):
            posted.append((path, body))
            return fake_post(path, body, **kw)

        self._patch(monkeypatch, [self._KNOWLEDGE], fake_post)
        monkeypatch.setattr("nexus.db.http_vector_client._post", capturing_post)
        HttpVectorClient().expire()
        gets = [b for p, b in posted if p == "/v1/vectors/get"]
        assert gets[0]["where"] == {"ttl_days": {"$gt": 0}}
        assert "$ne" not in str(gets[0]["where"]), "the retired $ne:0 predicate must not reappear"

    def test_expire_server_error_reraises(self, monkeypatch):
        # A failure must NOT be swallowed as "0 expired" — the CLI would
        # report success while expired rows survive.
        from nexus.db.http_vector_client import VectorServiceError

        def fake_post(path, body, **kw):
            raise VectorServiceError("server error", code=500)

        self._patch(monkeypatch, [self._KNOWLEDGE], fake_post)
        with pytest.raises(VectorServiceError):
            HttpVectorClient().expire()


class TestT3GcPrimitives:
    """nexus-h8rf6.7: list_unique_source_paths / list_chunks_with_metadata (and a third method,
    deleted with nexus-wbfpw.18 when `nx t3 gc` moved onto the engine's quarantine route) were
    missing — `nx t3 gc` and `nx t3 prune-stale` degrade to silent no-ops in service mode (call sites are
    try/except-wrapped, so no traceback, just zero effect).

    The two ``list_unique_source_paths`` cases MOVED to
    TestSourcePathMethodsRetired (nexus-bm8dd): the method addresses a chunk
    metadata key RDR-102 D2 removed, so it now raises. `nx t3 gc` never used
    it — that was `nx t3 prune-stale`, which is retired.
    """

    def test_list_chunks_with_metadata_yields_field_subset(self, monkeypatch):
        def fake_post(path, body, **kw):
            if body["offset"] > 0:
                return {"ids": [], "metadatas": []}
            return {
                "ids": ["x", "y"],
                "metadatas": [
                    {"doc_id": "1.2", "indexed_at": "2026-01-01", "extra": "drop"},
                    {},  # all fields missing -> empty strings
                ],
            }

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        rows = list(HttpVectorClient().list_chunks_with_metadata("c"))
        assert rows == [
            ("x", {"doc_id": "1.2", "indexed_at": "2026-01-01"}),
            ("y", {"doc_id": "", "indexed_at": ""}),
        ]

    def test_list_chunks_with_metadata_paginates(self, monkeypatch):
        def fake_post(path, body, **kw):
            if body["offset"] == 0:
                return {
                    "ids": [f"id{i}" for i in range(300)],
                    "metadatas": [{"doc_id": "d"}] * 300,
                }
            return {"ids": ["last"], "metadatas": [{"doc_id": "d"}]}

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        rows = list(
            HttpVectorClient().list_chunks_with_metadata("c", fields=("doc_id",))
        )
        assert len(rows) == 301
        assert rows[-1] == ("last", {"doc_id": "d"})

    def test_list_chunks_with_metadata_missing_collection_empty(self, monkeypatch):
        from nexus.db.http_vector_client import VectorServiceError

        def fake_post(path, body, **kw):
            raise VectorServiceError("not found", code=404)

        monkeypatch.setattr("nexus.db.http_vector_client._post", fake_post)
        assert list(HttpVectorClient().list_chunks_with_metadata("gone")) == []


class TestCollectionMetadata:
    """nexus-h8rf6.8: collection_metadata was missing — doctor's model-drift
    probe reported ProbeResult(outcome='error') for EVERY collection in
    service mode. Full T3 parity turns out to be achievable client-side:
    T3Database derives embedding_model / index_model from the collection
    NAME (conformant names embed the model; index_model_for_collection is
    an alias of embedding_model_for_collection) — only `count` needs the
    server. KeyError semantics match collection_info (RDR-156 Decision 6:
    zero live rows is indistinguishable from absent on pgvector).
    """

    _CONFORMANT = "code__nexus-1-1__voyage-code-3__v1"

    def test_returns_t3_parity_keys(self, monkeypatch):
        """Neutral model token on purpose (RDR-109 mode lint): the model
        is derived purely positionally from the collection NAME's own
        segment (index_model_for_collection reads whatever sits between
        the "__" delimiters), never from a real embedder call."""
        name = "code__nexus-1-1__model-code__v1"
        monkeypatch.setattr(HttpVectorClient, "count", lambda self, c: 42)
        meta = HttpVectorClient().collection_metadata(name)
        assert meta == {
            "name": name,
            "count": 42,
            "embedding_model": "model-code",
            "index_model": "model-code",
        }

    def test_missing_collection_raises_keyerror(self, monkeypatch):
        from nexus.db.http_vector_client import VectorServiceError

        def fake_count(self, c):
            raise VectorServiceError("not found", code=404)

        monkeypatch.setattr(HttpVectorClient, "count", fake_count)
        with pytest.raises(KeyError):
            HttpVectorClient().collection_metadata("gone__x__y__v1")

    def test_zero_rows_raises_keyerror(self, monkeypatch):
        # pgvector: zero live rows == absent (collection_info semantics).
        monkeypatch.setattr(HttpVectorClient, "count", lambda self, c: 0)
        with pytest.raises(KeyError):
            HttpVectorClient().collection_metadata(self._CONFORMANT)

    @pytest.mark.parametrize("reason, message", [
        ("unregistered_collection", "collection 'gone__x__y__v1' is not registered in this tenant"),
        (None, "collection 'gone__x__y__v1' is not registered in this tenant"),  # an engine that predates reason
    ])
    def test_an_unregistered_collection_422_raises_keyerror(self, monkeypatch, reason, message):
        """The engine answers /count for a name with no catalog row with a typed 422, not a 404. It is
        the same "absent" as a 404, so the KeyError contract holds for it too (the RDR hook's t3-exists
        probe logged it as a resolution failure every few seconds for a never-indexed repo)."""
        from nexus.db.http_vector_client import VectorServiceError

        def fake_count(self, c):
            raise VectorServiceError(message, code=422, reason=reason)

        monkeypatch.setattr(HttpVectorClient, "count", fake_count)
        with pytest.raises(KeyError):
            HttpVectorClient().collection_metadata("gone__x__y__v1")
        with pytest.raises(KeyError):
            HttpVectorClient().collection_info("gone__x__y__v1")

    def test_a_422_with_another_reason_reraises(self, monkeypatch):
        from nexus.db.http_vector_client import VectorServiceError

        def fake_count(self, c):
            raise VectorServiceError("collection is not registered (worded alike)", code=422,
                                     reason="model_unavailable")

        monkeypatch.setattr(HttpVectorClient, "count", fake_count)
        with pytest.raises(VectorServiceError):
            HttpVectorClient().collection_info(self._CONFORMANT)

    def test_non_404_error_reraises(self, monkeypatch):
        from nexus.db.http_vector_client import VectorServiceError

        def fake_count(self, c):
            raise VectorServiceError("server error", code=500)

        monkeypatch.setattr(HttpVectorClient, "count", fake_count)
        with pytest.raises(VectorServiceError):
            HttpVectorClient().collection_metadata(self._CONFORMANT)
