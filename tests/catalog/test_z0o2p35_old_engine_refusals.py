# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every old-engine ack mismatch names the remedy (RDR-223 Phase 2 gate, nexus-z0o2p.35, finding I3).

An answer that lacks a field this client's protocol needs means the engine predates the client.
Each such refusal is an ``EngineOlderThanClientError`` that names the cause and the remedy, so an
operator on a lagging engine is told to upgrade, not shown a bare ack-mismatch string. The type
stays a ``RuntimeError``: the existing ``pytest.raises(RuntimeError, match=...)`` callers hold.
"""
from __future__ import annotations

from typing import Any

import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.errors import EngineOlderThanClientError

_COLLECTION = "knowledge__z0o2p35-client__voyage-context-3__v1"
_A = "a" * 64
_B = "b" * 64


@pytest.fixture(autouse=True)
def _no_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.corpus as corpus

    monkeypatch.setattr(corpus, "ensure_collection_registered", lambda *a, **k: None)


class _Recorder:
    def __init__(self, *responses: Any) -> None:
        self.calls: list[tuple[str, dict, dict]] = []
        self._responses = list(responses)

    def __call__(self, path: str, body: dict, **kwargs: Any) -> Any:
        self.calls.append((path, body, kwargs))
        return self._responses.pop(0) if self._responses else {}


def _client(monkeypatch: pytest.MonkeyPatch, *responses: Any) -> tuple[HttpCatalogClient, _Recorder]:
    c = HttpCatalogClient.__new__(HttpCatalogClient)
    rec = _Recorder(*responses)
    monkeypatch.setattr(c, "_post", rec, raising=False)
    return c, rec


def _row(chash: str, position: int) -> dict:
    return {"chash": chash, "position": position}


def _chunk(chash: str) -> dict:
    return {"chash": chash, "text": "t", "metadata": {}}


def _raises_older(call, *, match: str) -> None:
    with pytest.raises(EngineOlderThanClientError, match=match) as exc:
        call()
    text = str(exc.value)
    assert "older than this client" in text, text
    assert "Upgrade the local engine" in text and "cloud engine deploy" in text, text


_ACK = {"docs": 1, "rows": 1, "count": 1, "failed_doc_ids": [], "chunks_written": 1,
        "complete_refused": [], "complete_refused_count": 0, "swept": 0}


def test_write_many_without_chunks_written(monkeypatch) -> None:
    c, _ = _client(monkeypatch, {"failed_doc_ids": []})
    _raises_older(lambda: c.write_manifest_many(
        [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION),
        match="chunks_written")


def test_write_many_without_the_metadata_merge_echo(monkeypatch) -> None:
    c, _ = _client(monkeypatch, {"failed_doc_ids": [], "chunks_written": 1})
    _raises_older(lambda: c.write_manifest_many(
        [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION,
        metadata_merge=True), match="metadata_merge")


def test_write_many_without_vectors_supplied(monkeypatch) -> None:
    chunk = {**_chunk(_A), "embedding": [0.1]}
    c, _ = _client(monkeypatch, {"failed_doc_ids": [], "chunks_written": 1})
    _raises_older(lambda: c.write_manifest_many(
        [("1.1.1", [_row(_A, 0)])], chunks=[chunk], embedding_model="m", collection=_COLLECTION),
        match="vectors_supplied")


def test_write_many_that_sent_complete_needs_the_stamp_echo(monkeypatch) -> None:
    """An engine that ignores ``complete`` answers without ``complete_refused_count``; reading that
    as "stamped" would report a note STORED that was never stamped."""
    c, _ = _client(monkeypatch, {"failed_doc_ids": [], "chunks_written": 1})
    _raises_older(lambda: c.write_manifest_many(
        [("1.1.1", [_row(_A, 0)])], complete={"1.1.1": "h"}, chunks=[_chunk(_A)],
        collection=_COLLECTION), match="complete_refused_count")
    c, _ = _client(monkeypatch, {"failed_doc_ids": []})      # no chunks on the request
    _raises_older(lambda: c.write_manifest_many(
        [("1.1.1", [_row(_A, 0)])], complete={"1.1.1": "h"}, collection=_COLLECTION),
        match="complete_refused_count")


def test_write_many_with_the_stamp_echo_passes(monkeypatch) -> None:
    c, _ = _client(monkeypatch, dict(_ACK))
    out = c.write_manifest_many(
        [("1.1.1", [_row(_A, 0)])], complete={"1.1.1": "h"}, chunks=[_chunk(_A)],
        collection=_COLLECTION)
    assert out["complete_refused_count"] == 0


def test_write_many_checks_the_stamp_echo_on_every_page_that_carried_one(monkeypatch) -> None:
    from nexus.catalog import http_catalog_client as hc

    monkeypatch.setattr(hc, "_MANIFEST_GET_MANY_PAGE", 1)
    c, _ = _client(monkeypatch, {"failed_doc_ids": [], "complete_refused_count": 0},
                   {"failed_doc_ids": []})
    _raises_older(lambda: c.write_manifest_many(
        [("1.1.1", []), ("1.1.2", [])], complete={"1.1.1": "h", "1.1.2": "h"},
        collection=_COLLECTION), match="complete_refused_count")


def test_write_many_with_no_stamp_needs_no_echo(monkeypatch) -> None:
    c, _ = _client(monkeypatch, {"failed_doc_ids": [], "chunks_written": 1})
    c.write_manifest_many([("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION)


def test_append_without_chunks_written_or_swept(monkeypatch) -> None:
    c, _ = _client(monkeypatch, {"ok": True, "count": 1})
    _raises_older(lambda: c.append_manifest_chunks(
        "1.1.1", [_row(_A, 0)], chunk_payload=[_chunk(_A)], collection=_COLLECTION),
        match="chunks_written")
    c, _ = _client(monkeypatch, {"ok": True, "count": 0})
    _raises_older(lambda: c.append_manifest_chunks(
        "1.1.1", [], sweep_chashes=[_B], collection=_COLLECTION), match="swept")


def test_append_without_the_metadata_merge_echo_or_vectors_supplied(monkeypatch) -> None:
    c, _ = _client(monkeypatch, {"ok": True, "count": 1, "chunks_written": 1})
    _raises_older(lambda: c.append_manifest_chunks(
        "1.1.1", [_row(_A, 0)], chunk_payload=[_chunk(_A)], collection=_COLLECTION,
        metadata_merge=True), match="metadata_merge")
    chunk = {**_chunk(_A), "embedding": [0.1]}
    c, _ = _client(monkeypatch, {"ok": True, "count": 1, "chunks_written": 1})
    _raises_older(lambda: c.append_manifest_chunks(
        "1.1.1", [_row(_A, 0)], chunk_payload=[chunk], embedding_model="m",
        collection=_COLLECTION), match="vectors_supplied")


def test_append_many_without_chunks_written_swept_or_vectors_supplied(monkeypatch) -> None:
    c, _ = _client(monkeypatch, {"docs": 1, "results": [], "failed_doc_ids": []})
    _raises_older(lambda: c.append_manifest_many(
        [("1.1.1", [_row(_A, 0)])], chunks=[_chunk(_A)], collection=_COLLECTION),
        match="chunks_written")
    c, _ = _client(monkeypatch, {"docs": 1, "results": [], "failed_doc_ids": []})
    _raises_older(lambda: c.append_manifest_many(
        [("1.1.1", [_row(_A, 0)])], sweep_chashes={"1.1.1": [_B]}, collection=_COLLECTION),
        match="swept")
    chunk = {**_chunk(_A), "embedding": [0.1]}
    c, _ = _client(monkeypatch, {"docs": 1, "results": [], "chunks_written": 1})
    _raises_older(lambda: c.append_manifest_many(
        [("1.1.1", [_row(_A, 0)])], chunks=[chunk], embedding_model="m", collection=_COLLECTION),
        match="vectors_supplied")


def test_a_pre_send_check_is_a_value_error_the_classifier_calls_unsent(monkeypatch) -> None:
    from nexus.catalog.write_outcome import PreSendArgumentError, judge

    c, rec = _client(monkeypatch, {})
    with pytest.raises(PreSendArgumentError) as exc:
        c.write_manifest_many([("1.1.1", [])], collection="")
    assert isinstance(exc.value, ValueError) and rec.calls == []
    assert judge(exc.value)[0] == "unsent"
