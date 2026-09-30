# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 P2.0 (nexus-z0o2p.10): the client half of Phase 1's wire additions.

Wire-shape tests against a mocked ``_post`` (the engine side is pinned Java-side by
AppendWithChunksTest, AppendSweepChashesTest, AppendManyTest, WriteManyDroppedChashesTest and
SuppliedVectorsTest; the real-engine journeys are in
``tests/integration/test_rdr223_multi_batch_writer_journey.py``):

* ``write_manifest_many`` returns each document's ``dropped_chashes``;
* ``append_manifest_chunks`` takes inline ``chunk_payload`` and ``sweep_chashes``;
* ``append_manifest_many`` is the multi-document form;
* client-supplied vectors need ``embedding_model``;
* every op is reachable through the writer proxy's whitelist (the nexus-dcv2k class).
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from nexus.catalog.catalog_protocol import CATALOG_WRITE_OPS
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.errors import CombinedWriteEmbedTimeoutError

_COLLECTION = "knowledge__rdr223-client__voyage-context-3__v1"
_A = "a" * 64
_B = "b" * 64
_C = "c" * 64


@pytest.fixture(autouse=True)
def _no_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.corpus as corpus

    monkeypatch.setattr(corpus, "ensure_collection_registered", lambda *a, **k: None)


class _Recorder:
    """A ``_post`` double: records every call, answers from a per-path queue."""

    def __init__(self, *responses: Any) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self._responses = list(responses)

    def __call__(self, path: str, body: dict, **kwargs: Any) -> Any:
        self.calls.append((path, body, kwargs))
        if not self._responses:
            return {}
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _client(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> tuple[HttpCatalogClient, _Recorder]:
    c = HttpCatalogClient.__new__(HttpCatalogClient)
    rec = _Recorder(*responses)
    monkeypatch.setattr(c, "_post", rec, raising=False)
    return c, rec


def _row(chash: str, position: int) -> dict:
    return {"chash": chash, "position": position}


def _chunk(chash: str, text: str = "t") -> dict:
    return {"chash": chash, "text": text, "metadata": {}}


# ── write_manifest_many: dropped_chashes ──────────────────────────────────────


class TestWriteManyDroppedChashes:
    def test_dropped_chashes_returned_per_document(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, {
            "docs": 2, "rows": 2, "failed_doc_ids": [], "chunks_written": 2,
            "dropped_chashes": {"1.1.1": [_B, _C], "1.1.2": []},
        })
        out = c.write_manifest_many(
            [("1.1.1", [_row(_A, 0)]), ("1.1.2", [_row(_A, 0)])],
            chunks=[_chunk(_A)], collection=_COLLECTION,
        )
        assert out["dropped_chashes"] == {"1.1.1": [_B, _C], "1.1.2": []}

    def test_dropped_chashes_merge_across_pages(self, monkeypatch) -> None:
        docs = [(f"1.9.{i}", [_row(_A, 0)]) for i in range(1500)]
        c, rec = _client(
            monkeypatch,
            {"docs": 1000, "failed_doc_ids": [], "dropped_chashes": {"1.9.0": [_B]}},
            {"docs": 500, "failed_doc_ids": [], "dropped_chashes": {"1.9.1400": [_C]}},
        )
        out = c.write_manifest_many(docs, collection=_COLLECTION)
        assert len(rec.calls) == 2
        assert out["dropped_chashes"] == {"1.9.0": [_B], "1.9.1400": [_C]}

    def test_a_response_without_the_field_leaves_the_key_absent(self, monkeypatch) -> None:
        """The client does not invent ``dropped_chashes``: absent is not the same as empty, and the
        multi-batch writer refuses a committed document with no entry."""
        c, _ = _client(monkeypatch, {"docs": 1, "rows": 1, "failed_doc_ids": []})
        out = c.write_manifest_many([("1.1.1", [_row(_A, 0)])], collection=_COLLECTION)
        assert "dropped_chashes" not in out

    def test_dropped_count_and_unknown_pass_through(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, {
            "docs": 2, "failed_doc_ids": [],
            "dropped_chashes": {"1.1.1": [_B]}, "dropped_count": {"1.1.1": 1},
            "dropped_unknown": ["1.1.2"],
        })
        out = c.write_manifest_many(
            [("1.1.1", [_row(_A, 0)]), ("1.1.2", [_row(_A, 0)])], collection=_COLLECTION)
        assert out["dropped_count"] == {"1.1.1": 1}
        assert out["dropped_unknown"] == ["1.1.2"]

    def test_embed_counts_surface_when_chunks_were_sent(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, {
            "docs": 1, "failed_doc_ids": [], "chunks_written": 3,
            "embed_embedded": 2, "embed_skipped": 1, "chunks_deduped": 0,
        })
        out = c.write_manifest_many(
            [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION)
        assert (out["embed_embedded"], out["embed_skipped"], out["chunks_deduped"]) == (2, 1, 0)


# ── client-supplied vectors ───────────────────────────────────────────────────


class TestSuppliedVectors:
    def test_write_many_sends_embedding_model_with_chunks(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {
            "docs": 1, "failed_doc_ids": [], "chunks_written": 1, "vectors_supplied": 1})
        chunk = {**_chunk(_A), "embedding": [0.1, 0.2]}
        c.write_manifest_many(
            [("1.1.1", [_row(_A, 0)])], chunks=[chunk],
            embedding_model="bge-base-en-v15-768", collection=_COLLECTION)
        body = rec.calls[0][1]
        assert body["embedding_model"] == "bge-base-en-v15-768"
        assert body["chunks"][0]["embedding"] == [0.1, 0.2]

    def test_write_many_refuses_a_vector_without_a_model_before_any_post(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        chunk = {**_chunk(_A), "embedding": [0.1]}
        with pytest.raises(ValueError, match="embedding_model"):
            c.write_manifest_many(
                [("1.1.1", [_row(_A, 0)])], chunks=[chunk], collection=_COLLECTION)
        assert rec.calls == []

    def test_append_refuses_a_vector_without_a_model_before_any_post(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        chunk = {**_chunk(_A), "embedding": [0.1]}
        with pytest.raises(ValueError, match="embedding_model"):
            c.append_manifest_chunks(
                "1.1.1", [_row(_A, 0)], chunk_payload=[chunk], collection=_COLLECTION)
        assert rec.calls == []

    def test_append_many_refuses_a_vector_without_a_model_before_any_post(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        chunk = {**_chunk(_A), "embedding": [0.1]}
        with pytest.raises(ValueError, match="embedding_model"):
            c.append_manifest_many(
                [("1.1.1", [_row(_A, 0)])], chunks=[chunk], collection=_COLLECTION)
        assert rec.calls == []

    def test_supplied_vectors_not_acknowledged_are_a_hard_error(self, monkeypatch) -> None:
        """An engine that does not know ``embedding`` embeds the text itself and answers without
        ``vectors_supplied``: content the caller chose was silently replaced."""
        chunk = {**_chunk(_A), "embedding": [0.1]}
        c, _ = _client(monkeypatch, {"docs": 1, "failed_doc_ids": [], "chunks_written": 1})
        with pytest.raises(RuntimeError, match="vectors_supplied"):
            c.write_manifest_many(
                [("1.1.1", [_row(_A, 0)])], chunks=[chunk], embedding_model="m",
                collection=_COLLECTION)
        c, _ = _client(monkeypatch, {"ok": True, "count": 1, "chunks_written": 1})
        with pytest.raises(RuntimeError, match="vectors_supplied"):
            c.append_manifest_chunks(
                "1.1.1", [_row(_A, 0)], chunk_payload=[chunk], embedding_model="m",
                collection=_COLLECTION)
        c, _ = _client(monkeypatch, {"docs": 1, "results": [], "chunks_written": 1})
        with pytest.raises(RuntimeError, match="vectors_supplied"):
            c.append_manifest_many(
                [("1.1.1", [_row(_A, 0)])], chunks=[chunk], embedding_model="m",
                collection=_COLLECTION)

    def test_acknowledged_supplied_vectors_pass(self, monkeypatch) -> None:
        chunk = {**_chunk(_A), "embedding": [0.1]}
        c, _ = _client(monkeypatch, {
            "ok": True, "count": 1, "chunks_written": 1, "vectors_supplied": 1,
            "vector_mismatches": 0})
        out = c.append_manifest_chunks(
            "1.1.1", [_row(_A, 0)], chunk_payload=[chunk], embedding_model="m",
            collection=_COLLECTION)
        assert out["vectors_supplied"] == 1

    def test_a_chunk_without_a_vector_needs_no_model(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"ok": True, "count": 1, "chunks_written": 1})
        c.append_manifest_chunks(
            "1.1.1", [_row(_A, 0)], chunk_payload=[_chunk(_A)], collection=_COLLECTION)
        assert "embedding_model" not in rec.calls[0][1]


# ── append_manifest_chunks ────────────────────────────────────────────────────


class TestAppendManifestChunks:
    def test_plain_append_body_is_unchanged(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"ok": True, "count": 1})
        out = c.append_manifest_chunks("1.1.1", [_row(_A, 0)], collection=_COLLECTION)
        path, body, _ = rec.calls[0]
        assert path == "/manifest/append"
        assert body == {"doc_id": "1.1.1", "collection": _COLLECTION, "rows": [_row(_A, 0)]}
        assert out["count"] == 1

    def test_chunks_ride_the_append_with_the_embed_budget(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {
            "ok": True, "count": 1, "chunks_written": 1, "embed_embedded": 1,
            "embed_skipped": 0, "chunks_deduped": 0})
        out = c.append_manifest_chunks(
            "1.1.1", [_row(_A, 0)], chunk_payload=[_chunk(_A)],
            force_re_embed=True, collection=_COLLECTION)
        path, body, kwargs = rec.calls[0]
        assert path == "/manifest/append"
        assert body["chunks"] == [_chunk(_A)]
        assert body["force_re_embed"] is True
        # A synchronous server-side embed: the long budget, and no blind ReadTimeout retry.
        assert kwargs["timeout"] > 30 and kwargs["retry_read_timeout"] is False
        assert out["chunks_written"] == 1 and out["embed_embedded"] == 1

    def test_missing_chunks_written_is_an_ack_mismatch(self, monkeypatch) -> None:
        """An old engine drops the unknown ``chunks`` field and answers {ok,count}: the chunks were
        NOT written, and proceeding would leave the manifest naming chunks that never landed."""
        c, _ = _client(monkeypatch, {"ok": True, "count": 1})
        with pytest.raises(RuntimeError, match="chunks_written"):
            c.append_manifest_chunks(
                "1.1.1", [_row(_A, 0)], chunk_payload=[_chunk(_A)], collection=_COLLECTION)

    def test_read_timeout_on_the_embed_becomes_the_typed_error(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, httpx.ReadTimeout("slow"))
        with pytest.raises(CombinedWriteEmbedTimeoutError):
            c.append_manifest_chunks(
                "1.1.1", [_row(_A, 0)], chunk_payload=[_chunk(_A)], collection=_COLLECTION)

    def test_sweep_chashes_ride_the_append_and_report_the_sweep(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {
            "ok": True, "count": 0, "swept": 2, "sweep_skipped": 0, "sweep_detail": {}})
        out = c.append_manifest_chunks(
            "1.1.1", [], sweep_chashes=[_B, _C], collection=_COLLECTION)
        assert rec.calls[0][1]["sweep_chashes"] == [_B, _C]
        assert rec.calls[0][1]["rows"] == []      # a sweep-only append
        assert out["swept"] == 2

    def test_a_response_without_swept_is_an_ack_mismatch(self, monkeypatch) -> None:
        """No old-engine fallback: the client and engine ship as a pair, so an append that sent
        sweep_chashes and got no ``swept`` back did not sweep and must not read as done."""
        c, _ = _client(monkeypatch, {"ok": True, "count": 0})
        with pytest.raises(RuntimeError, match="swept"):
            c.append_manifest_chunks(
                "1.1.1", [], sweep_chashes=[_B], collection=_COLLECTION)

    def test_more_than_300_sweep_chashes_refused_locally(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        with pytest.raises(ValueError, match="300"):
            c.append_manifest_chunks(
                "1.1.1", [], sweep_chashes=[f"{i:064x}" for i in range(301)],
                collection=_COLLECTION)
        assert rec.calls == []

    def test_more_than_300_chunks_refused_locally(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        payload = [_chunk(f"{i:064x}") for i in range(301)]
        with pytest.raises(ValueError, match="300"):
            c.append_manifest_chunks(
                "1.1.1", [_row(_A, 0)], chunk_payload=payload, collection=_COLLECTION)
        assert rec.calls == []

    def test_blank_collection_refused_locally(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        with pytest.raises(ValueError, match="collection"):
            c.append_manifest_chunks("1.1.1", [_row(_A, 0)], collection="")
        assert rec.calls == []


# ── append_manifest_many ──────────────────────────────────────────────────────


class TestAppendManifestMany:
    def test_body_shape_and_response(self, monkeypatch) -> None:
        resp = {
            "docs": 2, "rows": 2, "failed_doc_ids": [], "failed": [], "chunks_written": 2,
            "swept": 1, "sweep_skipped": 0, "sweep_detail": [],
            "results": [{"doc_id": "1.1.1", "ok": True}, {"doc_id": "1.1.2", "ok": True}],
        }
        c, rec = _client(monkeypatch, resp)
        out = c.append_manifest_many(
            [("1.1.1", [_row(_A, 0)]), ("1.1.2", [_row(_B, 0)])],
            chunks=[_chunk(_A), _chunk(_B)], sweep_chashes={"1.1.2": [_C]},
            force_re_embed=True, collection=_COLLECTION)
        path, body, kwargs = rec.calls[0]
        assert path == "/manifest/append_many"
        assert body["collection"] == _COLLECTION
        assert body["docs"] == [
            {"doc_id": "1.1.1", "rows": [_row(_A, 0)]},
            {"doc_id": "1.1.2", "rows": [_row(_B, 0)], "sweep_chashes": [_C]},
        ]
        assert body["chunks"] == [_chunk(_A), _chunk(_B)] and body["force_re_embed"] is True
        assert kwargs["timeout"] > 30 and kwargs["retry_read_timeout"] is False
        assert out["results"] == resp["results"] and out["chunks_written"] == 2

    def test_no_chunks_no_embed_budget(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"docs": 1, "results": [], "failed_doc_ids": []})
        c.append_manifest_many([("1.1.1", [_row(_A, 0)])], collection=_COLLECTION)
        assert "chunks" not in rec.calls[0][1]
        assert "timeout" not in rec.calls[0][2]

    def test_missing_chunks_written_is_an_ack_mismatch(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, {"docs": 1, "results": [], "failed_doc_ids": []})
        with pytest.raises(RuntimeError, match="chunks_written"):
            c.append_manifest_many(
                [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION)

    @pytest.mark.parametrize(("what", "kwargs"), [
        ("docs", {"docs": [(f"1.1.{i}", []) for i in range(1001)]}),
        ("chunks", {"docs": [("1.1.1", [])], "chunks": [_chunk(f"{i:064x}") for i in range(301)]}),
        ("sweep_chashes", {"docs": [("1.1.1", [])],
                           "sweep_chashes": {"1.1.1": [f"{i:064x}" for i in range(301)]}}),
    ])
    def test_engine_caps_refused_locally_before_any_post(self, monkeypatch, what, kwargs) -> None:
        c, rec = _client(monkeypatch)
        with pytest.raises(ValueError, match=what):
            c.append_manifest_many(collection=_COLLECTION, **kwargs)
        assert rec.calls == []

    def test_sweep_chashes_naming_an_unknown_doc_refused(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        with pytest.raises(ValueError, match="sweep_chashes"):
            c.append_manifest_many(
                [("1.1.1", [])], sweep_chashes={"9.9.9": [_A]}, collection=_COLLECTION)
        assert rec.calls == []

    def test_old_engine_404_is_a_typed_refusal_not_a_fallback(self, monkeypatch) -> None:
        """No per-document append fallback: it would orphan chunks, which is what the route exists
        to prevent."""
        from nexus.errors import ManifestAppendManyUnsupportedError

        req = httpx.Request("POST", "http://x/v1/catalog/manifest/append_many")
        err = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        c, rec = _client(monkeypatch, err)
        with pytest.raises(ManifestAppendManyUnsupportedError):
            c.append_manifest_many([("1.1.1", [_row(_A, 0)])], collection=_COLLECTION)
        assert len(rec.calls) == 1


# ── the whitelist, the protocol, the gateway classifier ───────────────────────


def test_append_manifest_many_is_a_whitelisted_write_op() -> None:
    assert "append_manifest_many" in CATALOG_WRITE_OPS


def test_writer_proxy_reaches_every_new_op() -> None:
    """The nexus-dcv2k trap: an op missing from the closed whitelist makes a capability check read
    False on every real run."""
    from nexus.catalog.factory import _ServiceCatalogWriter

    class _Stub:
        def append_manifest_many(self): return "many"
        def append_manifest_chunks(self): return "append"
        def write_manifest_many(self): return "wm"

    w = _ServiceCatalogWriter(_Stub())
    assert callable(getattr(w, "append_manifest_many", None))
    assert callable(getattr(w, "append_manifest_chunks", None))
    assert callable(getattr(w, "write_manifest_many", None))


def test_protocol_carries_the_new_signatures() -> None:
    import inspect

    from nexus.catalog.catalog_protocol import CatalogWriter

    params = set(inspect.signature(CatalogWriter.append_manifest_chunks).parameters)
    assert {"chunk_payload", "sweep_chashes", "force_re_embed", "embedding_model"} <= params
    assert "embedding_model" in inspect.signature(CatalogWriter.write_manifest_many).parameters
    many = set(inspect.signature(CatalogWriter.append_manifest_many).parameters)
    assert {"docs", "chunks", "sweep_chashes", "collection", "force_re_embed", "embedding_model"} <= many


@pytest.mark.parametrize("path", ["/v1/catalog/manifest/append", "/v1/catalog/manifest/append_many"])
def test_chunk_carrying_appends_get_the_embed_504_floor(path: str) -> None:
    from nexus.db.gateway_backoff import _is_embed_server_side_write_path

    assert _is_embed_server_side_write_path(path, {"chunks": [{"chash": _A}]}) is True
    assert _is_embed_server_side_write_path(path, {"rows": []}) is False
    assert _is_embed_server_side_write_path(path, {"chunks": []}) is False
