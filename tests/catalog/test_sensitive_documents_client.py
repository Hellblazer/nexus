# SPDX-License-Identifier: AGPL-3.0-or-later
"""``HttpCatalogClient.sensitive_documents`` (nexus-f3yqd.6, F30): the bounded read of a tenant's sensitive
documents (``GET /v1/catalog/sensitive``, engine 0.1.188, capability ``catalog_sensitive_list_supported``).

``None`` is "cannot say", never "none": an engine that does not advertise the route (0.1.187 is live), a 404, a
503 (the engine's own statement timeout) or a malformed body. The doctor rows then walk the catalog as before.
"""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.sensitivity_doctor import SensitiveListUnavailable

_BODY = {"count": 2, "limit": 1000, "capped": False,
         "documents": [{"tumbler": "1.2.3", "physical_collection": "knowledge__a__voyage-context-3__v1"},
                       {"tumbler": "1.2.9", "physical_collection": "docs__b__bge-768__v1"}]}


def _client(monkeypatch: pytest.MonkeyPatch, *, flag: bool | None = True, get=None) -> tuple[HttpCatalogClient, list]:
    c = object.__new__(HttpCatalogClient)
    calls: list = []
    flags = {} if flag is None else {"catalog_sensitive_list_supported": flag, "release_version": "0.1.188"}
    monkeypatch.setattr(c, "_version_flags", lambda: flags, raising=False)

    def record(path, **kw):
        calls.append((path, kw))
        return get(path, **kw) if callable(get) else get

    monkeypatch.setattr(c, "_get", record, raising=False)
    return c, calls


def test_the_documents_and_the_bound_travel_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_BODY)
    assert c.sensitive_documents(1000) == {"count": 2, "capped": False, "documents": _BODY["documents"]}
    ((path, kw),) = calls
    assert path == "/sensitive" and kw["limit"] == 1000
    assert kw["idempotent"] is False, "sent once: a resend of a timed-out read would run it again"
    assert 0 < kw["timeout"] < 30


@pytest.mark.parametrize("flag", [False, None])
def test_an_engine_without_the_flag_is_not_asked(monkeypatch: pytest.MonkeyPatch, flag: bool | None) -> None:
    c, calls = _client(monkeypatch, flag=flag, get=_BODY)
    assert c.sensitive_documents() is None
    assert calls == [], "an engine that does not advertise the route gets no request"


@pytest.mark.parametrize("status", [404, 503])
def test_a_404_or_503_is_cannot_say(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    def fail(path, **kw):
        request = httpx.Request("GET", "http://engine/v1/catalog/sensitive")
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))

    c, _calls = _client(monkeypatch, get=fail)
    assert c.sensitive_documents() is None


@pytest.mark.parametrize("body", [
    None, [], {"count": 2, "capped": False}, {"count": "2", "capped": False, "documents": []},
    {"count": True, "capped": False, "documents": []}, {"count": 1, "capped": "no", "documents": []},
    {"count": 1, "capped": False, "documents": [{"tumbler": "1.2.3"}]},
    {"count": 1, "capped": False, "documents": ["1.2.3"]},
    {"count": 3, "capped": False, "documents": _BODY["documents"]},
])
def test_a_malformed_or_inconsistent_body_is_cannot_say(monkeypatch: pytest.MonkeyPatch, body: object) -> None:
    c, _calls = _client(monkeypatch, get=body)
    assert c.sensitive_documents() is None


def test_a_capped_answer_is_returned_as_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"count": 5, "limit": 2, "capped": True, "documents": _BODY["documents"]}
    c, _calls = _client(monkeypatch, get=body)
    assert c.sensitive_documents(2) == {"count": 5, "capped": True, "documents": _BODY["documents"]}


def test_a_document_with_no_collection_is_read_as_an_empty_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A document flagged before it was indexed has a NULL collection; the walk's _to_entry reads that as ""."""
    body = {"count": 1, "capped": False, "documents": [{"tumbler": "1.2.3", "physical_collection": None}]}
    c, _calls = _client(monkeypatch, get=body)
    assert c.sensitive_documents() == {"count": 1, "capped": False,
                                       "documents": [{"tumbler": "1.2.3", "physical_collection": ""}]}


@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
def test_a_transport_error_is_cannot_say(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def fail(path, **kw):
        raise exc

    c, _calls = _client(monkeypatch, get=fail)
    assert c.sensitive_documents() is None


# ── strict mode (nexus-uwggq.1.7): the cause of "cannot say", for a caller that must not walk instead ──────────


def _status_error(status: int):
    def fail(path, **kw):
        request = httpx.Request("GET", "http://engine/v1/catalog/sensitive")
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(status, request=request))
    return fail


@pytest.mark.parametrize(("setup", "reason"), [
    (dict(flag=False, get=_BODY), SensitiveListUnavailable.UNSUPPORTED),
    (dict(flag=None, get=_BODY), SensitiveListUnavailable.UNSUPPORTED),
    (dict(get=_status_error(404)), SensitiveListUnavailable.UNSUPPORTED),
    (dict(get=_status_error(503)), SensitiveListUnavailable.TIMEOUT),
    (dict(get=_status_error(500)), SensitiveListUnavailable.FAILED),
    (dict(get=_status_error(403)), SensitiveListUnavailable.FAILED),
    (dict(get={"count": "2"}), SensitiveListUnavailable.MALFORMED),
    (dict(get=[]), SensitiveListUnavailable.MALFORMED),
], ids=["flag-false", "flag-absent", "404", "503", "500", "403", "malformed", "not-a-dict"])
def test_strict_raises_the_cause(monkeypatch: pytest.MonkeyPatch, setup: dict, reason: str) -> None:
    c, _calls = _client(monkeypatch, **setup)
    with pytest.raises(SensitiveListUnavailable) as caught:
        c.sensitive_documents(strict=True)
    assert caught.value.reason == reason


def test_strict_transport_error_is_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(path, **kw):
        raise httpx.ConnectError("refused")

    c, _calls = _client(monkeypatch, get=fail)
    with pytest.raises(SensitiveListUnavailable) as caught:
        c.sensitive_documents(strict=True)
    assert caught.value.reason == SensitiveListUnavailable.FAILED


def test_strict_returns_the_answer_like_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _calls = _client(monkeypatch, get=_BODY)
    assert c.sensitive_documents(1000, strict=True) == c.sensitive_documents(1000)


def test_strict_a_failed_version_probe_is_its_own_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, flag=None, get=_BODY)
    monkeypatch.setattr(c, "_version_probe_failed", True, raising=False)
    with pytest.raises(SensitiveListUnavailable) as caught:
        c.sensitive_documents(strict=True)
    assert caught.value.reason == SensitiveListUnavailable.PROBE_FAILED
    assert calls == []
    assert c.sensitive_documents() is None, "the default contract is unchanged"
