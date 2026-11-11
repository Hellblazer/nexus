# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpCatalogClient.stub_metadata`` (nexus-f3yqd.6, F30): the bounded per-collection count of PDF chunks still carrying
the upload placeholder metadata (``GET /v1/catalog/stub-metadata``, engine 0.1.189, capability
``catalog_stub_metadata_supported``).

``None`` means scan as before: no flag (no request), a 404 or 403, a transport error or a malformed body. A 503 is the
engine's own statement timeout: the client answers ``STUB_METADATA_TIMED_OUT`` so the doctor reports the row not
checked, with the reason, instead of rerunning the slow scan.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.http_catalog_client import STUB_METADATA_TIMED_OUT, HttpCatalogClient

_BODY = {"limit": 1000, "collections": [
    {"collection": "knowledge__a__voyage-context-3__v1", "stub_chunks": 0, "capped": False, "examples": []},
    {"collection": "docs__b__bge-768__v1", "stub_chunks": 12, "capped": False, "examples": ["file:///x.pdf", "y.pdf"]},
]}


def _client(monkeypatch: pytest.MonkeyPatch, *, flag: bool | None = True, get=None) -> tuple[HttpCatalogClient, list]:
    c = object.__new__(HttpCatalogClient)
    calls: list = []
    flags = {} if flag is None else {"catalog_stub_metadata_supported": flag}
    monkeypatch.setattr(c, "_version_flags", lambda: flags, raising=False)

    def record(path, **kw):
        calls.append((path, kw))
        return get(path, **kw) if callable(get) else get

    monkeypatch.setattr(c, "_get", record, raising=False)
    return c, calls


def _status(status: int):
    def fail(path, **kw):
        request = httpx.Request("GET", "http://engine/v1/catalog/stub-metadata")
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
    return fail


def test_the_counts_and_the_bound_travel_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_BODY)
    assert c.stub_metadata(1000) == _BODY["collections"]
    ((path, kw),) = calls
    assert path == "/stub-metadata" and kw["limit"] == 1000 and kw["idempotent"] is False and 0 < kw["timeout"] < 30


@pytest.mark.parametrize("flag", [False, None])
def test_an_engine_without_the_flag_is_not_asked(monkeypatch: pytest.MonkeyPatch, flag: bool | None) -> None:
    c, calls = _client(monkeypatch, flag=flag, get=_BODY)
    assert c.stub_metadata() is None and calls == []


@pytest.mark.parametrize("status", [403, 404, 500])
def test_a_refusal_or_missing_route_is_scan_as_before(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    c, _calls = _client(monkeypatch, get=_status(status))
    assert c.stub_metadata() is None


def test_a_503_is_the_engines_timeout_not_a_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _calls = _client(monkeypatch, get=_status(503))
    assert c.stub_metadata() is STUB_METADATA_TIMED_OUT


def test_a_transport_error_is_scan_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(path, **kw):
        raise httpx.ConnectError("refused")

    c, _calls = _client(monkeypatch, get=fail)
    assert c.stub_metadata() is None


@pytest.mark.parametrize("body", [
    None, [], {"collections": "x"}, {"collections": [{"collection": "c", "stub_chunks": "1", "capped": False, "examples": []}]},
    {"collections": [{"collection": "c", "stub_chunks": True, "capped": False, "examples": []}]},
    {"collections": [{"collection": "c", "stub_chunks": -1, "capped": False, "examples": []}]},
    {"collections": [{"collection": "c", "stub_chunks": 1, "capped": "no", "examples": []}]},
    {"collections": [{"collection": "c", "stub_chunks": 1, "capped": False, "examples": [3]}]},
    {"collections": [{"stub_chunks": 1, "capped": False, "examples": []}]},
])
def test_a_malformed_body_is_scan_as_before(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    c, _calls = _client(monkeypatch, get=body)
    assert c.stub_metadata() is None
