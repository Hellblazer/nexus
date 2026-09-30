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

    def test_a_response_without_swept_to_a_sweep_request_is_an_ack_mismatch(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, {"docs": 1, "results": [], "failed_doc_ids": []})
        with pytest.raises(RuntimeError, match="swept"):
            c.append_manifest_many(
                [("1.1.1", [_row(_A, 0)])], sweep_chashes={"1.1.1": [_B]}, collection=_COLLECTION)

    # ── per-document `complete` (nexus-z0o2p.19): the stamp rides a document's last append ──

    def test_complete_rides_the_document_entry_and_the_refusal_fields_come_back(self, monkeypatch) -> None:
        resp = {"docs": 2, "failed_doc_ids": [], "chunks_written": 1, "results": [],
                "complete_refused": [{"doc_id": "1.1.2", "referenced": 1, "missing": 0, "chunk_count": 9}],
                "complete_refused_count": 1}
        c, rec = _client(monkeypatch, resp)
        out = c.append_manifest_many(
            [("1.1.1", [_row(_A, 0)]), ("1.1.2", [_row(_B, 0)])], chunks=[_chunk(_A), _chunk(_B)],
            complete={"1.1.1": ("h1", 1), "1.1.2": ("h2", 9)}, collection=_COLLECTION)
        body = rec.calls[0][1]
        assert body["docs"] == [
            {"doc_id": "1.1.1", "rows": [_row(_A, 0)], "complete": {"content_hash": "h1", "chunk_count": 1}},
            {"doc_id": "1.1.2", "rows": [_row(_B, 0)], "complete": {"content_hash": "h2", "chunk_count": 9}},
        ]
        assert out["complete_refused_count"] == 1 and out["complete_refused"][0]["doc_id"] == "1.1.2"

    def test_a_request_without_complete_carries_no_complete_key(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"docs": 1, "results": [], "failed_doc_ids": []})
        c.append_manifest_many([("1.1.1", [_row(_A, 0)])], collection=_COLLECTION)
        assert all("complete" not in d for d in rec.calls[0][1]["docs"])

    def test_an_answer_without_complete_refused_count_is_an_ack_mismatch(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, {"docs": 1, "results": [], "failed_doc_ids": []})
        with pytest.raises(RuntimeError, match="complete_refused_count"):
            c.append_manifest_many(
                [("1.1.1", [_row(_A, 0)])], complete={"1.1.1": ("h", 1)}, collection=_COLLECTION)

    @pytest.mark.parametrize("stamp", [("", 1), ("h", -1), ("h", True), ("h", "1")])
    def test_a_malformed_stamp_is_refused_before_any_post(self, monkeypatch, stamp) -> None:
        c, rec = _client(monkeypatch)
        with pytest.raises(ValueError, match="complete"):
            c.append_manifest_many([("1.1.1", [_row(_A, 0)])], complete={"1.1.1": stamp}, collection=_COLLECTION)
        assert rec.calls == []

    def test_complete_naming_an_unknown_doc_is_refused(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch)
        with pytest.raises(ValueError, match="not in docs"):
            c.append_manifest_many([("1.1.1", [_row(_A, 0)])], complete={"9.9.9": ("h", 1)}, collection=_COLLECTION)
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


# ── begin_index_run: the pre-run snapshot ─────────────────────────────────────


class TestBeginIndexRunSnapshot:
    def test_plain_begin_body_is_unchanged_and_returns_the_response(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"ok": True})
        out = c.begin_index_run("1.1.1", "h", "run", _COLLECTION)
        assert rec.calls[0][0] == "/index-run/begin"
        assert rec.calls[0][1] == {
            "doc_id": "1.1.1", "content_hash": "h", "run_id": "run", "collection": _COLLECTION}
        assert out == {"ok": True}

    def test_snapshot_flag_rides_the_body_and_the_fields_come_back(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"ok": True, "prior_chashes": [_A, _B], "prior_count": 3})
        out = c.begin_index_run("1.1.1", "h", "run", _COLLECTION, snapshot_manifest=True)
        assert rec.calls[0][1]["snapshot_manifest"] is True
        assert out["prior_chashes"] == [_A, _B] and out["prior_count"] == 3

    def test_begin_many_with_snapshots_rides_the_flag_and_returns_them(self, monkeypatch) -> None:
        resp = {"docs": 1, "failed_doc_ids": [],
                "snapshots": {"1.1.1": {"prior_chashes": [_A], "prior_count": 2}}}
        c, rec = _client(monkeypatch, resp)
        out = c.begin_index_run_many(
            [{"doc_id": "1.1.1", "content_hash": "h", "run_id": "r"}], _COLLECTION, snapshot_manifest=True)
        assert rec.calls[0][0] == "/index-run/begin-many"
        assert rec.calls[0][1]["snapshot_manifest"] is True
        assert out["snapshots"]["1.1.1"]["prior_count"] == 2

    def test_begin_many_without_the_flag_sends_the_old_body(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"docs": 1, "failed_doc_ids": []})
        c.begin_index_run_many([{"doc_id": "1.1.1", "content_hash": "h", "run_id": "r"}], _COLLECTION)
        assert "snapshot_manifest" not in rec.calls[0][1]

    def test_begin_many_asked_for_snapshots_but_answered_without_is_an_ack_mismatch(self, monkeypatch) -> None:
        c, _ = _client(monkeypatch, {"docs": 1, "failed_doc_ids": []})
        with pytest.raises(RuntimeError, match="snapshots"):
            c.begin_index_run_many(
                [{"doc_id": "1.1.1", "content_hash": "h", "run_id": "r"}], _COLLECTION, snapshot_manifest=True)

    def test_begin_many_404_stays_the_empty_sentinel(self, monkeypatch) -> None:
        req = httpx.Request("POST", "http://x/v1/catalog/index-run/begin-many")
        err = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        c, _ = _client(monkeypatch, err)
        assert c.begin_index_run_many(
            [{"doc_id": "1.1.1", "content_hash": "h", "run_id": "r"}], _COLLECTION, snapshot_manifest=True) == {}

    def test_a_404_returns_none(self, monkeypatch) -> None:
        req = httpx.Request("POST", "http://x/v1/catalog/index-run/begin")
        err = httpx.HTTPStatusError("404", request=req, response=httpx.Response(404, request=req))
        c, _ = _client(monkeypatch, err)
        assert c.begin_index_run("1.1.1", "h", "run", _COLLECTION) is None


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
    assert {"docs", "chunks", "sweep_chashes", "complete", "collection", "force_re_embed",
            "embedding_model"} <= many
    assert "snapshot_manifest" in inspect.signature(CatalogWriter.begin_index_run_many).parameters


@pytest.mark.parametrize("path", ["/v1/catalog/manifest/append", "/v1/catalog/manifest/append_many"])
def test_chunk_carrying_appends_get_the_embed_504_floor(path: str) -> None:
    from nexus.db.gateway_backoff import _is_embed_server_side_write_path

    assert _is_embed_server_side_write_path(path, {"chunks": [{"chash": _A}]}) is True
    assert _is_embed_server_side_write_path(path, {"rows": []}) is False
    assert _is_embed_server_side_write_path(path, {"chunks": []}) is False


# ── metadata write mode (nexus-z0o2p.13) ──────────────────────────────────────


class TestMetadataMergeFields:
    """``metadata_merge`` / ``metadata_delete_keys`` ride the chunk-carrying request only, are absent
    (the engine's replace behaviour) by default, and are checked before any round trip."""

    _OK = {"chunks_written": 1, "failed_doc_ids": [], "ok": True, "docs": 1, "rows": 1, "count": 1,
           "metadata_merge": True}

    def test_write_many_sends_the_fields_when_merge_is_on(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, self._OK)
        c.write_manifest_many(
            [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION,
            metadata_merge=True, metadata_delete_keys=["x", "y"])
        body = rec.calls[0][1]
        assert body["metadata_merge"] is True and body["metadata_delete_keys"] == ["x", "y"]

    @pytest.mark.parametrize("route", ["write_many", "append", "append_many"])
    def test_an_engine_that_does_not_echo_the_mode_is_refused(self, monkeypatch, route) -> None:
        """An old engine ignores the fields and REPLACES metadata; the client must not carry on."""
        old_engine = {k: v for k, v in self._OK.items() if k != "metadata_merge"}
        c, _ = _client(monkeypatch, old_engine)
        with pytest.raises(RuntimeError, match="metadata_merge"):
            if route == "write_many":
                c.write_manifest_many(
                    [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION,
                    metadata_merge=True)
            elif route == "append":
                c.append_manifest_chunks(
                    "1.1.1", [_row(_A, 0)], collection=_COLLECTION, chunk_payload=[_chunk(_A)],
                    metadata_merge=True)
            else:
                c.append_manifest_many(
                    [("1.1.1", [_row(_A, 0)])], collection=_COLLECTION, chunks=[_chunk(_A)],
                    metadata_merge=True)

    def test_no_echo_is_needed_when_merge_was_not_asked_for(self, monkeypatch) -> None:
        old_engine = {k: v for k, v in self._OK.items() if k != "metadata_merge"}
        c, _ = _client(monkeypatch, old_engine)
        c.write_manifest_many([("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION)

    def test_write_many_sends_nothing_by_default(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, self._OK)
        c.write_manifest_many([("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION)
        assert "metadata_merge" not in rec.calls[0][1]
        assert "metadata_delete_keys" not in rec.calls[0][1]

    def test_merge_without_keys_sends_only_the_flag(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, self._OK)
        c.write_manifest_many(
            [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION, metadata_merge=True)
        assert rec.calls[0][1]["metadata_merge"] is True
        assert "metadata_delete_keys" not in rec.calls[0][1]

    def test_append_and_append_many_send_them_with_chunks(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, self._OK, self._OK)
        c.append_manifest_chunks(
            "1.1.1", [_row(_A, 0)], collection=_COLLECTION, chunk_payload=[_chunk(_A)],
            metadata_merge=True, metadata_delete_keys=["k"])
        c.append_manifest_many(
            [("1.1.1", [_row(_A, 0)])], collection=_COLLECTION, chunks=[_chunk(_A)],
            metadata_merge=True, metadata_delete_keys=["k"])
        for _, body, _kw in rec.calls:
            assert body["metadata_merge"] is True and body["metadata_delete_keys"] == ["k"]

    def test_a_sweep_only_append_carries_neither(self, monkeypatch) -> None:
        c, rec = _client(monkeypatch, {"ok": True, "swept": 0})
        c.append_manifest_chunks(
            "1.1.1", [], collection=_COLLECTION, sweep_chashes=[_B],
            metadata_merge=True, metadata_delete_keys=["k"])
        assert "metadata_merge" not in rec.calls[0][1]

    @pytest.mark.parametrize("merge, keys", [
        (False, ["k"]),                      # keys without merge
        (True, ["k"] * 65),                  # over the cap
        (True, ["  "]),                      # blank
        (True, [1]),                         # not a string
    ])
    def test_bad_combinations_are_refused_before_any_round_trip(self, monkeypatch, merge, keys) -> None:
        c, rec = _client(monkeypatch, self._OK)
        with pytest.raises(ValueError):
            c.write_manifest_many(
                [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION,
                metadata_merge=merge, metadata_delete_keys=keys)
        assert rec.calls == []
