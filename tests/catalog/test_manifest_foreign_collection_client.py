# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpCatalogClient.manifest_foreign_collection`` (GH #1611): the bounded read of the live documents whose manifest
names chunks outside their ``physical_collection`` (``GET /v1/catalog/manifest/foreign_collection``).

``None`` is "cannot say", never "none": a 404 (an engine from before the route), a 503 (the engine's statement
timeout), a transport error, or a malformed or inconsistent body.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient

_DOC_A = {"tumbler": "1.23.96", "physical_collection": "knowledge__ads__bge-base-en-v15-768__v1",
          "collections": ["docs__1-48__bge-base-en-v15-768__v1"], "chunks": 56}
_DOC_B = {"tumbler": "1.23.97", "physical_collection": "knowledge__ads__bge-base-en-v15-768__v1",
          "collections": ["docs__1-48__bge-base-en-v15-768__v1", "docs__2-1__bge-base-en-v15-768__v1"], "chunks": 9}
_BODY = {"count": 2, "limit": 1000, "capped": False, "documents": [_DOC_A, _DOC_B]}


def _client(monkeypatch: pytest.MonkeyPatch, *, get=None) -> tuple[HttpCatalogClient, list]:
    c = object.__new__(HttpCatalogClient)
    calls: list = []

    def record(path, **kw):
        calls.append((path, kw))
        return get(path, **kw) if callable(get) else get

    monkeypatch.setattr(c, "_get", record, raising=False)
    return c, calls


def test_the_documents_and_the_bound_travel_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_BODY)
    assert c.manifest_foreign_collection(1000) == {"count": 2, "capped": False, "documents": [_DOC_A, _DOC_B]}
    ((path, kw),) = calls
    assert path == "/manifest/foreign_collection" and kw["limit"] == 1000
    assert kw["idempotent"] is False, "sent once: a resend of a timed-out read would run it again"
    assert 0 < kw["timeout"] < 30


@pytest.mark.parametrize("status", [404, 503])
def test_a_404_or_503_is_cannot_say_never_none(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def fail(path, **kw):
        request = httpx.Request("GET", "http://engine/v1/catalog/manifest/foreign_collection")
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))

    c, _calls = _client(monkeypatch, get=fail)
    assert c.manifest_foreign_collection() is None


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_a_transport_error_is_cannot_say(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fail(path, **kw):
        raise exc

    c, _calls = _client(monkeypatch, get=fail)
    assert c.manifest_foreign_collection() is None


@pytest.mark.parametrize("body", [
    None, [], {"count": 2, "capped": False},
    {"count": "2", "capped": False, "documents": []},
    {"count": True, "capped": False, "documents": []},
    {"count": -1, "capped": False, "documents": []},
    {"count": 1, "capped": "no", "documents": []},
    {"count": 1, "capped": False, "documents": ["1.2.3"]},
    {"count": 1, "capped": False, "documents": [{k: v for k, v in _DOC_A.items() if k != "collections"}]},
    {"count": 1, "capped": False, "documents": [{**_DOC_A, "collections": "docs__x"}]},
    {"count": 1, "capped": False, "documents": [{**_DOC_A, "collections": [1]}]},
    {"count": 1, "capped": False, "documents": [{**_DOC_A, "chunks": "56"}]},
    {"count": 1, "capped": False, "documents": [{**_DOC_A, "physical_collection": None}]},
    {"count": 3, "capped": False, "documents": [_DOC_A, _DOC_B]},
])
def test_a_malformed_or_inconsistent_body_is_cannot_say(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    c, _calls = _client(monkeypatch, get=body)
    assert c.manifest_foreign_collection() is None


def test_a_capped_answer_keeps_the_exact_total(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"count": 5, "limit": 1, "capped": True, "documents": [_DOC_A]}
    c, _calls = _client(monkeypatch, get=body)
    assert c.manifest_foreign_collection(1) == {"count": 5, "capped": True, "documents": [_DOC_A]}


def test_an_empty_catalog_is_an_answer_of_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one body that means "none": distinct from every None above."""
    c, _calls = _client(monkeypatch, get={"count": 0, "limit": 1000, "capped": False, "documents": []})
    assert c.manifest_foreign_collection() == {"count": 0, "capped": False, "documents": []}
