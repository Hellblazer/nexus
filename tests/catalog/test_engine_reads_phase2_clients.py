# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RDR-241 Phase 2 client readers, against a stubbed transport (nexus-uwggq.2.6, .2.7, .2.13):
``HttpCatalogClient.documents_by_author``, ``documents_by_meta`` and ``sensitive_locators``.

An engine without the route is never an empty answer and never a walk: no request is sent without the ``/version``
flag, and every failure raises :class:`EngineRouteUnavailable`, whose message names the route and, for an engine
without it, the engine release that has it."""
from __future__ import annotations

import httpx
import pytest

from nexus.catalog.engine_reads.route_unavailable import PHASE2_ROUTES_ENGINE, EngineRouteUnavailable
from nexus.catalog.http_catalog_client import HttpCatalogClient

_FLAGS = {"catalog_docs_by_author_supported": True, "catalog_docs_by_meta_supported": True,
          "catalog_sensitive_locators_supported": True}


def _doc(t: str, **kw) -> dict:
    return {"tumbler": t, "title": f"doc {t}", "author": kw.pop("author", ""), "meta": kw.pop("meta", {}), **kw}


def _page(docs: list[dict], *, count: int | None = None, offset: int = 0, limit: int = 1000) -> dict:
    return {"count": len(docs) if count is None else count, "limit": limit, "offset": offset, "documents": docs}


def _client(monkeypatch: pytest.MonkeyPatch, *, flags: dict | None = None, get=None,
            probe_failed: bool = False) -> tuple[HttpCatalogClient, list]:
    c = object.__new__(HttpCatalogClient)
    c._version_probe_failed = probe_failed
    calls: list = []
    monkeypatch.setattr(c, "_version_flags", lambda: _FLAGS if flags is None else flags, raising=False)

    def record(path, **kw):
        calls.append((path, kw))
        return get(path, **kw) if callable(get) else get

    monkeypatch.setattr(c, "_get", record, raising=False)
    return c, calls


def _status(code: int):
    def fail(path, **kw):
        request = httpx.Request("GET", f"http://engine/v1/catalog{path}")
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))
    return fail


_READS = {
    "author": (lambda c: c.documents_by_author("lamport"), "/docs/by-author", "catalog_docs_by_author_supported"),
    "meta": (lambda c: c.documents_by_meta(meta=[("k", "v")]), "/docs/by-meta", "catalog_docs_by_meta_supported"),
    "locators": (lambda c: c.sensitive_locators(), "/sensitive/locators", "catalog_sensitive_locators_supported"),
}


@pytest.mark.parametrize("name", sorted(_READS))
@pytest.mark.parametrize("flags", [{}, {"catalog_docs_by_author_supported": "true", "catalog_docs_by_meta_supported": 1,
                                        "catalog_sensitive_locators_supported": False}])
def test_an_engine_without_the_flag_is_not_asked_and_the_error_names_the_engine(
        monkeypatch: pytest.MonkeyPatch, name: str, flags: dict) -> None:
    call, route, flag = _READS[name]
    c, calls = _client(monkeypatch, flags=flags, get=_page([]))
    with pytest.raises(EngineRouteUnavailable) as exc:
        call(c)
    assert calls == [], "only a JSON true advertises the route; nothing is sent otherwise"
    assert exc.value.reason == EngineRouteUnavailable.UNSUPPORTED and exc.value.route == route
    assert f"GET /v1/catalog{route}" in str(exc.value) and PHASE2_ROUTES_ENGINE in str(exc.value) and flag in str(exc.value)


@pytest.mark.parametrize("name", sorted(_READS))
def test_a_failed_version_probe_is_unknown_not_an_old_engine(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    c, calls = _client(monkeypatch, flags={}, probe_failed=True, get=_page([]))
    with pytest.raises(EngineRouteUnavailable) as exc:
        _READS[name][0](c)
    assert calls == [] and exc.value.reason == EngineRouteUnavailable.PROBE_FAILED


@pytest.mark.parametrize("name", sorted(_READS))
@pytest.mark.parametrize(("code", "reason"), [(404, "unsupported"), (503, "timeout"), (500, "failed"), (400, "failed")])
def test_an_http_failure_is_cannot_say_with_its_cause(monkeypatch: pytest.MonkeyPatch, name: str, code: int,
                                                     reason: str) -> None:
    c, _calls = _client(monkeypatch, get=_status(code))
    with pytest.raises(EngineRouteUnavailable) as exc:
        _READS[name][0](c)
    assert exc.value.reason == reason
    if code == 503:
        assert "in time" in str(exc.value)


@pytest.mark.parametrize("name", sorted(_READS))
def test_a_transport_error_is_failed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    def boom(path, **kw):
        raise httpx.ConnectError("refused")
    c, _calls = _client(monkeypatch, get=boom)
    with pytest.raises(EngineRouteUnavailable) as exc:
        _READS[name][0](c)
    assert exc.value.reason == EngineRouteUnavailable.FAILED


# ── by-author ───────────────────────────────────────────────────────────────────


def test_author_page_travels_the_wire_and_converts_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _page([_doc("1.1.2", author="Leslie Lamport"), _doc("1.1.10", author="L. Lamport")], count=7, offset=40, limit=2)
    c, calls = _client(monkeypatch, get=body)
    entries, count = c.documents_by_author("lamport", limit=2, offset=40)
    assert count == 7 and [str(e.tumbler) for e in entries] == ["1.1.2", "1.1.10"]
    assert entries[0].author == "Leslie Lamport"
    ((path, kw),) = calls
    assert path == "/docs/by-author" and kw["author"] == "lamport" and kw["limit"] == 2 and kw["offset"] == 40
    assert kw["idempotent"] is False and 10 < kw["timeout"] < 30


@pytest.mark.parametrize("body", [
    None, [], "x", {"count": 1, "offset": 0}, {"count": True, "offset": 0, "documents": []},
    {"count": -1, "offset": 0, "documents": []}, {"count": "1", "offset": 0, "documents": []},
    {"count": 1, "offset": 5, "documents": [_doc("1.1.1")]},
    {"count": 1, "offset": 0, "documents": [{"title": "no tumbler"}]},
    {"count": 1, "offset": 0, "documents": [{"tumbler": ""}]},
    {"count": 1, "offset": 0, "documents": ["1.1.1"]},
])
def test_author_malformed_body_is_rejected(monkeypatch: pytest.MonkeyPatch, body) -> None:
    c, _calls = _client(monkeypatch, get=body)
    with pytest.raises(EngineRouteUnavailable) as exc:
        c.documents_by_author("x")
    assert exc.value.reason == EngineRouteUnavailable.MALFORMED


def test_author_page_longer_than_its_limit_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _calls = _client(monkeypatch, get=_page([_doc("1.1.1"), _doc("1.1.2")], limit=1))
    with pytest.raises(EngineRouteUnavailable):
        c.documents_by_author("x", limit=1)


@pytest.mark.parametrize(("author", "limit", "offset"), [("", 10, 0), ("x", 0, 0), ("x", 1001, 0), ("x", 10, -1)])
def test_author_bad_arguments_send_nothing(monkeypatch: pytest.MonkeyPatch, author: str, limit: int, offset: int) -> None:
    c, calls = _client(monkeypatch, get=_page([]))
    with pytest.raises(ValueError):
        c.documents_by_author(author, limit=limit, offset=offset)
    assert calls == []


# ── by-meta ─────────────────────────────────────────────────────────────────────


def test_meta_clauses_travel_the_wire_with_the_stated_default(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_page([_doc("1.1.3")], count=3, offset=10, limit=5))
    entries, count = c.documents_by_meta(content_type="paper", meta=[("extraction_source", "file"), ("q", "true")],
                                         meta_min=[("miss_count", 2)], limit=5, offset=10)
    assert count == 3 and [str(e.tumbler) for e in entries] == ["1.1.3"]
    ((path, kw),) = calls
    assert path == "/docs/by-meta"
    assert {k: v for k, v in kw.items() if k not in ("idempotent", "timeout")} == {
        "limit": 5, "offset": 10, "content_type": "paper", "meta.extraction_source": "file", "meta.q": "true",
        "meta_default.extraction_source": "file", "meta_min.miss_count": "2"}


def test_meta_default_rides_only_with_its_own_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_page([]))
    c.documents_by_meta(meta_min=[("miss_count", 2)])
    c.documents_by_meta(meta=[("q", "x")])
    assert not any(k.startswith("meta_default.") for _p, kw in calls for k in kw), (
        "the engine answers 400 to a default without its meta.KEY")


def test_meta_sensitive_alone_is_a_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_page([]))
    c.documents_by_meta(sensitive=True, content_type="code")
    ((_path, kw),) = calls
    assert kw["sensitive"] == "true" and kw["content_type"] == "code"


def test_meta_conflicting_pairs_answer_nothing_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    c, calls = _client(monkeypatch, get=_page([_doc("1.1.1")]))
    assert c.documents_by_meta(meta=[("k", "a"), ("k", "b")]) == ([], 0)
    assert calls == []
    c.documents_by_meta(meta=[("k", "a"), ("k", "a")])
    assert calls[0][1]["meta.k"] == "a"


@pytest.mark.parametrize("kwargs", [{}, {"meta": [("k", "")]}, {"meta": [("", "v")]}, {"meta_min": [("m", 2.0)]},
                                    {"meta_min": [("m", True)]}, {"meta_min": [("", 2)]}, {"content_type": "paper"}])
def test_meta_a_clause_that_would_widen_the_answer_is_refused(monkeypatch: pytest.MonkeyPatch, kwargs: dict) -> None:
    c, calls = _client(monkeypatch, get=_page([]))
    with pytest.raises(ValueError):
        c.documents_by_meta(**kwargs)
    assert calls == []


# ── sensitive locators ──────────────────────────────────────────────────────────

_LOC = {"tumbler": "1.1.4", "source_uri": "file:///a/secret.md", "file_path": "secret.md", "alias_of": None,
        "physical_collection": "docs__a__bge-768__v1", "sensitive": True}
_ALIAS = {**_LOC, "tumbler": "1.1.9", "source_uri": "file:///b/copy.md", "alias_of": "1.1.4", "sensitive": False}


def test_locators_travel_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"count": 2, "limit": 10_000, "capped": False, "documents": [_LOC, _ALIAS]}
    c, calls = _client(monkeypatch, get=body)
    assert c.sensitive_locators() == body
    ((path, kw),) = calls
    assert path == "/sensitive/locators" and kw["limit"] == 10_000 and kw["idempotent"] is False


def test_locators_capped_is_returned_for_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    c, _calls = _client(monkeypatch, get={"count": 3, "limit": 1, "capped": True, "documents": [_LOC]})
    assert c.sensitive_locators(1)["capped"] is True


@pytest.mark.parametrize("body", [
    None, {"count": 1, "capped": False}, {"count": 1, "capped": "no", "documents": [_LOC]},
    {"count": 2, "capped": False, "documents": [_LOC]}, {"count": True, "capped": False, "documents": []},
    {"count": 1, "capped": False, "documents": [{**_LOC, "sensitive": None}]},
    {"count": 1, "capped": False, "documents": [{k: v for k, v in _LOC.items() if k != "sensitive"}]},
    {"count": 1, "capped": False, "documents": [{**_LOC, "tumbler": ""}]},
    {"count": 1, "capped": False, "documents": [{**_LOC, "source_uri": 7}]},
])
def test_locators_malformed_body_is_rejected(monkeypatch: pytest.MonkeyPatch, body) -> None:
    c, _calls = _client(monkeypatch, get=body)
    with pytest.raises(EngineRouteUnavailable) as exc:
        c.sensitive_locators()
    assert exc.value.reason == EngineRouteUnavailable.MALFORMED


@pytest.mark.parametrize("limit", [0, 10_001])
def test_locators_bound_is_checked_before_sending(monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    c, calls = _client(monkeypatch, get={})
    with pytest.raises(ValueError):
        c.sensitive_locators(limit)
    assert calls == []
