# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P3.5a, first commit (nexus-p2m1x.23): the client reads ``collection_id``.

The engine's collection rows on ``/collections/list``, ``/collections/get`` and
``/collections/for_tuple`` carry ``collection_id``: always a JSON number on an engine
that has the column (the registry's identity is NOT NULL, vectors-068-1), and no key at
all from an older engine. The client also accepts ``null`` defensively. It answers
``int`` or ``None`` in every state and never raises on an absent key. Read-only: no
route takes an id.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient

_MODEL = "bge-base-en-v15-768"
_OWNER = "1-1"
_STAMPED = "code__1-1__bge-base-en-v15-768__v1"
_DEFENSIVE_NULL = "docs__1-1__bge-base-en-v15-768__v1"

_ABSENT = object()


def _row(name: str, cid: object) -> dict:
    row = {
        "name": name, "content_type": name.split("__")[0], "owner_id": _OWNER,
        "embedding_model": _MODEL, "model_version": "v1", "legacy_grandfathered": False,
    }
    if cid is not _ABSENT:
        row["collection_id"] = cid
    return row


def _client(monkeypatch: pytest.MonkeyPatch, rows: dict[str, dict]) -> HttpCatalogClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/catalog/collections/list":
            return httpx.Response(200, json={"collections": list(rows.values())})
        if path == "/v1/catalog/collections/get":
            row = rows.get(request.url.params["name"])
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"error": "not found"})
        if path == "/v1/catalog/collections/for_tuple":
            ctype = request.url.params["content_type"]
            row = next((r for r in rows.values() if r["content_type"] == ctype), None)
            return httpx.Response(200, json=row) if row else httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json={})

    monkeypatch.setenv("NX_SERVICE_TOKEN", "test-token")
    client = HttpCatalogClient(base_url="http://mock.test")
    client._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return client


_STATES = pytest.mark.parametrize(
    ("wire", "want"),
    [(41, 41), (None, None), (_ABSENT, None)],
    ids=["number", "defensive-null", "older-engine-absent"],
)


@_STATES
def test_get_collection_exposes_int_or_none(monkeypatch, wire, want) -> None:
    cat = _client(monkeypatch, {_STAMPED: _row(_STAMPED, wire)})
    row = cat.get_collection(_STAMPED)
    assert row is not None
    assert "collection_id" in row, "present in every state in the client's row"
    assert row["collection_id"] == want
    assert type(row["collection_id"]) is type(want)


@_STATES
def test_list_collections_exposes_int_or_none(monkeypatch, wire, want) -> None:
    cat = _client(monkeypatch, {_STAMPED: _row(_STAMPED, wire)})
    rows = cat.list_collections()
    assert len(rows) == 1
    assert "collection_id" in rows[0]
    assert rows[0]["collection_id"] == want


def test_list_collections_mixes_a_number_and_a_defensive_null(monkeypatch) -> None:
    cat = _client(monkeypatch, {_STAMPED: _row(_STAMPED, 41), _DEFENSIVE_NULL: _row(_DEFENSIVE_NULL, None)})
    got = {r["name"]: r["collection_id"] for r in cat.list_collections()}
    assert got == {_STAMPED: 41, _DEFENSIVE_NULL: None}


@_STATES
def test_for_tuple_reader_exposes_int_or_none(monkeypatch, wire, want) -> None:
    cat = _client(monkeypatch, {_STAMPED: _row(_STAMPED, wire)})
    attrs = cat.find_collection("code", _OWNER, _MODEL)
    assert attrs is not None
    assert attrs.collection_id == want
    assert attrs.label == _STAMPED


@pytest.mark.parametrize("bad", [True, "41", 4.5, [41]], ids=["bool", "str", "float", "list"])
def test_a_malformed_id_reads_as_none_never_as_a_value(monkeypatch, bad) -> None:
    cat = _client(monkeypatch, {_STAMPED: _row(_STAMPED, bad)})
    assert cat.get_collection(_STAMPED)["collection_id"] is None
    assert cat.list_collections()[0]["collection_id"] is None
    assert cat.find_collection("code", _OWNER, _MODEL).collection_id is None


def test_a_malformed_id_logs_a_warning_and_a_good_one_does_not(monkeypatch) -> None:
    from structlog.testing import capture_logs

    cat = _client(monkeypatch, {_STAMPED: _row(_STAMPED, "41")})
    with capture_logs() as logs:
        cat.get_collection(_STAMPED)
        cat.list_collections()
        cat.find_collection("code", _OWNER, _MODEL)
    bad = [e for e in logs if e["event"] == "collection_row_collection_id_malformed"]
    assert len(bad) == 3 and all(e["log_level"] == "warning" and e["value_type"] == "str" for e in bad)

    good = _client(monkeypatch, {_STAMPED: _row(_STAMPED, 41), _DEFENSIVE_NULL: _row(_DEFENSIVE_NULL, None)})
    with capture_logs() as logs:
        good.get_collection(_STAMPED)
        good.list_collections()
        good.find_collection("code", _OWNER, _MODEL)
    assert [e for e in logs if e["event"] == "collection_row_collection_id_malformed"] == []
