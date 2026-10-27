# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pfgb.36: ``HttpVectorClient.chunk_rows``, the recovery-bundle export's piece reader.

It must raise on a store error (``get_by_id`` reads one as "no such chunk", which made an unreadable
piece look like a missing one), page at 300 ids, and return the flat row shape ``get_by_id`` does.
The engine-backed happy path is ``tests/integration/test_6pfgb36_recovery_bundle_split_note.py``.
"""
from __future__ import annotations

import pytest

from nexus.db.http_vector_client import HttpVectorClient, VectorServiceError

_COLLECTION = "knowledge__1-1__bge-base-en-v15-768__v1"


def _client() -> HttpVectorClient:
    return HttpVectorClient.__new__(HttpVectorClient)


@pytest.fixture
def wire(monkeypatch):
    calls: list[dict] = []
    state: dict = {"error": None, "rows": {}, "drop_documents": False}

    def _post(path, body, *, tenant=None, **_kw):
        assert path == "/v1/vectors/store-get"
        calls.append(body)
        if state["error"] is not None:
            raise state["error"]
        ids = [i for i in body["ids"] if i in state["rows"]]
        out = {"ids": ids, "metadatas": [state["rows"][i]["meta"] for i in ids]}
        if not state["drop_documents"]:
            out["documents"] = [state["rows"][i]["doc"] for i in ids]
        return out

    monkeypatch.setattr("nexus.db.http_vector_client._post", _post)
    monkeypatch.setattr(HttpVectorClient, "_tenant", None, raising=False)
    return calls, state


def test_chunk_rows_raises_on_a_store_error(wire):
    _calls, state = wire
    state["error"] = VectorServiceError("503 from store-get", code=503)
    with pytest.raises(VectorServiceError):
        _client().chunk_rows(_COLLECTION, ["a" * 64])


def test_chunk_rows_returns_the_flat_row_shape_and_leaves_out_absent_ids(wire):
    _calls, state = wire
    state["rows"] = {"a" * 64: {"doc": "piece one", "meta": {"title": "t", "catalog_doc_id": "1.1.1"}}}
    rows = _client().chunk_rows(_COLLECTION, ["a" * 64, "b" * 64])
    assert rows == {"a" * 64: {"id": "a" * 64, "content": "piece one", "title": "t", "catalog_doc_id": "1.1.1"}}


def test_chunk_rows_pages_at_300_ids(wire):
    calls, state = wire
    ids = [f"{i:064x}" for i in range(301)]
    state["rows"] = {i: {"doc": "x", "meta": {}} for i in ids}
    rows = _client().chunk_rows(_COLLECTION, ids)
    assert len(rows) == 301
    assert [len(c["ids"]) for c in calls] == [300, 1]


def test_a_response_without_documents_gives_empty_content(wire):
    """The export reads empty content as a missing piece, never as an empty note."""
    _calls, state = wire
    state["rows"] = {"a" * 64: {"doc": "ignored", "meta": {}}}
    state["drop_documents"] = True
    assert _client().chunk_rows(_COLLECTION, ["a" * 64])["a" * 64]["content"] == ""


@pytest.mark.parametrize("read", ["chunk_rows", "_note_rows"])
def test_a_store_get_with_no_count_warns_once_and_still_returns_the_rows(wire, monkeypatch, read):
    """nexus-6pfgb.33: an engine that predates the store-get ``count`` field (nexus-hdx2u E3) cannot
    tell these reads that rows were dropped; they go on, as the collection stub does, and say so once
    per process with the stub's own warning."""
    from structlog.testing import capture_logs

    import nexus.db.http_vector_client as hvc

    monkeypatch.setattr(hvc, "_store_get_count_unreported_logged", False)
    _calls, state = wire
    state["rows"] = {"a" * 64: {"doc": "x", "meta": {"title": "t"}}}
    with capture_logs() as logs:
        first = getattr(_client(), read)(_COLLECTION, ["a" * 64])
        getattr(_client(), read)(_COLLECTION, ["a" * 64])
    assert "a" * 64 in first
    events = [e for e in logs if e["event"] == "store_get_count_unreported"]
    assert len(events) == 1 and events[0]["log_level"] == "warning"
    assert events[0]["collection"] == _COLLECTION


def test_a_store_get_that_reports_its_count_does_not_warn(wire, monkeypatch):
    from structlog.testing import capture_logs

    import nexus.db.http_vector_client as hvc

    monkeypatch.setattr(hvc, "_store_get_count_unreported_logged", False)
    real_post = hvc._post

    def _with_count(path, body, **kw):
        out = real_post(path, body, **kw)
        return {**out, "count": len(out["ids"])}

    monkeypatch.setattr(hvc, "_post", _with_count)
    _calls, state = wire
    state["rows"] = {"a" * 64: {"doc": "x", "meta": {}}}
    with capture_logs() as logs:
        _client().chunk_rows(_COLLECTION, ["a" * 64])
    assert [e for e in logs if e["event"] == "store_get_count_unreported"] == []
