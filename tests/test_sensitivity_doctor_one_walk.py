# SPDX-License-Identifier: AGPL-3.0-or-later
"""F30 (nexus-f3yqd.6): `nx doctor`'s three sensitivity rows share one read of the sensitive set.

Measured 2026-11-10 on a laptop tenant: a default `nx doctor` took 2 m 16 s, and the three rows
sensitive-in-aspect-eligible, sensitive-with-aspects and sensitive-in-external-embedder took 22.2 s, 23.1 s
and 23.4 s, each walking the whole catalog (``all_documents(limit=0)``) to find no sensitive document. The walk
cost scales with the catalog, so on a large one the three repeats are most of the run.

RDR-241 Phase 1 (nexus-uwggq.1.8): the walk is gone. The rows read the engine's bounded list (``GET /sensitive``)
once; when it is unavailable or capped, each row reads "not checked" with the cause and no catalog walk is made.
"""
from __future__ import annotations

import itertools
from typing import Any

import httpx
import pytest

from nexus.catalog.http_catalog_client import HttpCatalogClient

from nexus.catalog import sensitivity_doctor as sd
from nexus.catalog.sensitivity_doctor import SensitiveListUnavailable
from nexus.commands import doctor as doctor_mod


class _Doc:
    def __init__(self, sensitive: bool) -> None:
        self.sensitive = sensitive
        self.collection = "knowledge__c__voyage-context-3__v1"
        self.physical_collection = self.collection
        self.tumbler = "1.1.1"
        self.title = "t"


class _Reader:
    """A catalog reader with the bounded list. ``walks`` counts ``all_documents`` calls: a row must make none."""

    def __init__(self, docs: list[_Doc]) -> None:
        self.docs = docs
        self.walks = 0
        self.route_calls = 0

    def catalog_sensitivity_probe(self) -> tuple[bool, str]:
        return True, "9.9.9"

    def sensitive_documents(self, limit: int = 1000, *, strict: bool = False) -> dict | None:
        self.route_calls += 1
        sensitive = [d for d in self.docs if d.sensitive]
        return {"count": len(sensitive), "capped": False,
                "documents": [{"tumbler": d.tumbler, "physical_collection": d.physical_collection} for d in sensitive]}

    def all_documents(self, limit: int = 0) -> list[_Doc]:
        self.walks += 1
        return list(self.docs)


@pytest.fixture
def reader(monkeypatch: pytest.MonkeyPatch) -> _Reader:
    r = _Reader([_Doc(False), _Doc(False)])
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    return r


def _three() -> list[dict]:
    return [
        sd.check_sensitive_in_aspect_eligible(),
        sd.check_sensitive_with_aspects(),
        sd.check_sensitive_in_external_embedder(),
    ]


def test_inside_one_walk_the_three_rows_read_the_sensitive_list_once(reader: _Reader) -> None:
    with sd.one_walk():
        reports = _three()
    assert reader.route_calls == 1 and reader.walks == 0
    assert [r["status"] for r in reports] == [sd.NOT_APPLICABLE] * 3
    assert {r["reason"] for r in reports} == {"no sensitive documents in this catalog"}
    assert [r["name"] for r in reports] == [sd.IN_ELIGIBLE, sd.WITH_ASPECTS, sd.IN_EXTERNAL_EMBEDDER]


def test_outside_one_walk_each_row_reads_the_list_for_itself(reader: _Reader) -> None:
    _three()
    assert reader.route_calls == 3 and reader.walks == 0


def test_the_shared_read_ends_with_its_scope(reader: _Reader) -> None:
    with sd.one_walk():
        sd.check_sensitive_with_aspects()
    reader.docs.append(_Doc(True))
    no_aspects = type("NoAspects", (), {"get_by_doc_id": lambda self, _t: None})()
    with sd.one_walk():
        report = sd.check_sensitive_with_aspects(aspects=no_aspects, queue=object(), queued_doc_ids=lambda _q: set())
        assert report["sensitive_documents"] == 1, report
    assert reader.route_calls == 2 and reader.walks == 0


def test_doctors_supplementary_sweep_reads_the_list_once(reader: _Reader, monkeypatch: pytest.MonkeyPatch) -> None:
    keep = {"sensitive-in-aspect-eligible", "sensitive-with-aspects", "sensitive-in-external-embedder"}
    monkeypatch.setattr(doctor_mod, "_SUPPLEMENTARY_CHECK_NAMES", tuple(n for n in doctor_mod._SUPPLEMENTARY_CHECK_NAMES if n in keep))
    doctor_mod._run_supplementary_checks()
    assert reader.route_calls == 1 and reader.walks == 0


def test_a_failed_read_is_shared_too_and_is_not_checked_with_its_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    class _Broken(_Reader):
        def sensitive_documents(self, limit: int = 1000, *, strict: bool = False) -> dict | None:
            calls.append(1)
            raise ConnectionError("engine gone")

    broken = _Broken([])
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: broken)
    with sd.one_walk():
        reports = _three()
    assert len(calls) == 1 and broken.walks == 0
    for r in reports:
        assert r["status"] == sd.NOT_APPLICABLE
        assert r["reason"] == _FAILED, r  # the cause is the fixed text; the exception text is not passed through


class _RouteReader(_Reader):
    """An engine with ``GET /sensitive`` that answers *answer*; a :class:`SensitiveListUnavailable` is raised, as
    the strict client raises it."""

    def __init__(self, docs: list[_Doc], answer: dict | SensitiveListUnavailable | None) -> None:
        super().__init__(docs)
        self.answer = answer
        self.route_calls = 0

    def sensitive_documents(self, limit: int = 1000, *, strict: bool = False) -> dict | None:
        self.route_calls += 1
        if isinstance(self.answer, SensitiveListUnavailable):
            raise self.answer
        return self.answer


_ONE = {"count": 1, "capped": False,
        "documents": [{"tumbler": "1.9.9", "physical_collection": "knowledge__c__voyage-context-3__v1"}]}


def test_the_bounded_route_replaces_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _RouteReader([_Doc(False)], _ONE)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    with sd.one_walk():
        no_aspects = type("NoAspects", (), {"list_by_collection": lambda self, _c, limit=1: []})()
        reports = [
            sd.check_sensitive_in_external_embedder(),
            sd.check_sensitive_in_aspect_eligible(aspects=no_aspects, select=lambda _c: None),
        ]
    assert r.walks == 0 and r.route_calls == 1
    assert all(rep["sensitive_documents"] == 1 for rep in reports), reports
    assert reports[0]["status"] == sd.WARN, reports[0]  # a voyage collection is an external embedder


# Each cause is asserted in full, as the operator reads it: true for every input that produces it, and it says
# what to do. The literals are not imported, so a reworded cause fails here.
_UNSUPPORTED = ("not checked: the engine does not offer the sensitive list (a local engine needs engine-service "
                "0.1.188 or later; on a managed engine the public edge must allow it)")
_PROBE_FAILED = "not checked: the engine's capabilities could not be read; run nx doctor again"
_TIMEOUT = "not checked: the engine did not answer in time; run nx doctor again"
_FAILED = "not checked: the engine could not be reached or failed; run nx doctor again"
_MALFORMED = "not checked: the engine's answer could not be read; run nx doctor again"
_CAPPED = ("not checked: more than 10,000 sensitive documents; "
           "list them with nx catalog list --sensitive --owner <owner>")

_CAUSES = [
    (SensitiveListUnavailable.UNSUPPORTED, _UNSUPPORTED),
    (SensitiveListUnavailable.PROBE_FAILED, _PROBE_FAILED),
    (SensitiveListUnavailable.TIMEOUT, _TIMEOUT),
    (SensitiveListUnavailable.FAILED, _FAILED),
    (SensitiveListUnavailable.MALFORMED, _MALFORMED),
]
_ALL_CHECKS = [sd.check_sensitive_in_aspect_eligible, sd.check_sensitive_with_aspects,
               sd.check_sensitive_in_external_embedder]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [(SensitiveListUnavailable(cause), text) for cause, text in _CAUSES]
    + [({"count": 10_001, "capped": True, "documents": _ONE["documents"]}, _CAPPED)],
    ids=[cause for cause, _text in _CAUSES] + ["capped"],
)
@pytest.mark.parametrize("check", _ALL_CHECKS)
def test_each_cause_is_not_checked_with_its_own_text_and_no_walk(
    monkeypatch: pytest.MonkeyPatch, check: Any, answer: Any, reason: str,
) -> None:
    r = _RouteReader([_Doc(True), _Doc(False)], answer)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    report = check()
    assert r.route_calls == 1 and r.walks == 0
    assert report["status"] == sd.NOT_APPLICABLE and report["findings"] == [] and report["sensitive_documents"] == 0
    assert report["reason"] == reason
    assert sd.is_not_checked(report)
    assert sd.render_text(report) == [f"[ ] {report['name']}: {reason}"], "no 'not applicable -' in front of it"


def test_a_reader_answering_with_a_non_dict_is_the_malformed_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _RouteReader([_Doc(True)], None)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    assert sd.check_sensitive_with_aspects()["reason"] == _MALFORMED


def test_a_reader_with_no_sensitive_list_is_not_checked_and_not_walked(monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoRoute:
        walks = 0

        def catalog_sensitivity_probe(self) -> tuple[bool, str]:
            return True, "9.9.9"

        def all_documents(self, limit: int = 0) -> list[_Doc]:
            type(self).walks += 1
            return [_Doc(True)]

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _NoRoute())
    report = sd.check_sensitive_with_aspects()
    assert _NoRoute.walks == 0
    assert report["status"] == sd.NOT_APPLICABLE and report["reason"] == _UNSUPPORTED


def test_a_not_checked_gating_row_does_not_gate_the_doctor(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _RouteReader([_Doc(True)], SensitiveListUnavailable(SensitiveListUnavailable.UNSUPPORTED))
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    assert doctor_mod._run_check_sensitive_with_aspects() is False


def test_an_empty_route_answer_is_not_applicable_without_a_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    r = _RouteReader([_Doc(True)], {"count": 0, "capped": False, "documents": []})
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    report = sd.check_sensitive_with_aspects()
    assert r.walks == 0
    assert report["status"] == sd.NOT_APPLICABLE and report["reason"] == "no sensitive documents in this catalog"


def test_a_bounded_read_that_raises_is_not_checked_with_the_cause_and_no_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Raising(_RouteReader):
        def sensitive_documents(self, limit: int = 1000, *, strict: bool = False) -> dict | None:
            self.route_calls += 1
            raise RuntimeError("proxy blew up")

    r = _Raising([_Doc(True)], None)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    report = sd.check_sensitive_in_external_embedder()
    assert r.route_calls == 1 and r.walks == 0, report
    assert report["status"] == sd.NOT_APPLICABLE
    assert report["reason"] == _FAILED


def test_the_route_is_asked_for_the_engines_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    class _Recording(_RouteReader):
        def sensitive_documents(self, limit: int = 1000, *, strict: bool = False) -> dict | None:
            asked.append(limit)
            return _ONE

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Recording([], _ONE))
    sd.check_sensitive_in_external_embedder()
    assert asked == [10_000]


def test_the_gating_row_fails_on_a_route_built_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """sensitive-with-aspects (the row that gates nx doctor's exit) reaches FAIL from the bounded list."""
    r = _RouteReader([], _ONE)
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    with_row = type("Aspects", (), {"get_by_doc_id": lambda self, t: {"doc_id": t} if t == "1.9.9" else None})()
    report = sd.check_sensitive_with_aspects(aspects=with_row, queue=object(), queued_doc_ids=lambda _q: set())
    assert r.walks == 0
    assert report["status"] == sd.FAIL, report
    assert report["findings"][0]["tumbler"] == "1.9.9", report


# ── the real client: no unfiltered GET /list in any not-checked state ───────


def _real_client(
    *, version: dict | None, sensitive: httpx.Response | Exception | None,
) -> tuple[HttpCatalogClient, list[str]]:
    """The real client over a mock transport. *version* ``None``: ``GET /version`` answers 500. *sensitive* an
    exception: the transport raises it."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/version":
            return httpx.Response(200, json=version) if version is not None else httpx.Response(500, json={})
        if request.url.path == "/v1/catalog/sensitive" and sensitive is not None:
            if isinstance(sensitive, Exception):
                raise sensitive
            return sensitive
        return httpx.Response(404, json={"error": "unexpected"})

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    return cat, seen


_FLAGS = {"catalog_sensitivity_supported": True, "release_version": "0.1.188"}
_ROUTE = {**_FLAGS, "catalog_sensitive_list_supported": True}
_CAPPED_BODY = {"count": 10_001, "limit": 10_000, "capped": True, "documents": _ONE["documents"]}


@pytest.mark.parametrize(
    ("version", "sensitive", "reason", "asked_route"),
    [
        (_FLAGS, None, _UNSUPPORTED, False),
        (_ROUTE, httpx.Response(404, json={"error": "no route"}), _UNSUPPORTED, True),
        (_ROUTE, httpx.Response(503, json={"error": "statement timeout"}), _TIMEOUT, True),
        (_ROUTE, httpx.Response(500, json={"error": "boom"}), _FAILED, True),
        (_ROUTE, httpx.ConnectError("refused"), _FAILED, True),
        (_ROUTE, httpx.Response(200, json={"count": "many"}), _MALFORMED, True),
        (_ROUTE, httpx.Response(200, json=_CAPPED_BODY), _CAPPED, True),
        (None, None, _PROBE_FAILED, False),
    ],
    ids=["flag-absent", "404", "503", "500", "transport-error", "malformed", "capped", "version-probe-fails"],
)
@pytest.mark.parametrize("check", _ALL_CHECKS)
def test_the_real_client_is_never_asked_for_a_catalog_listing(
    check: Any, version: dict | None, sensitive: Any, reason: str, asked_route: bool,
) -> None:
    cat, seen = _real_client(version=version, sensitive=sensitive)
    report = check(reader=cat)
    assert report["status"] == sd.NOT_APPLICABLE and report["reason"] == reason, report
    assert not [p for p in seen if p.startswith("/v1/catalog/list")], seen
    assert set(seen) <= {"/version", "/v1/catalog/sensitive"}, seen
    assert ("/v1/catalog/sensitive" in seen) is asked_route


def test_a_version_probe_that_fails_after_the_capability_was_cached_is_the_probe_failed_cause() -> None:
    """The doctor's own capability read answers, the cache then expires, and the client's re-read fails: the client's
    PROBE_FAILED cause reaches the row."""
    ticks = itertools.count(0, 1000)  # every read of the clock is 1000 s after the last: past the 300 s trust
    answers = iter([httpx.Response(200, json=_ROUTE), httpx.Response(500, json={})])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path != "/version":
            return httpx.Response(404, json={"error": "unexpected"})
        return next(answers)

    cat = HttpCatalogClient(base_url="http://mock.test", _token="t")
    cat._client = httpx.Client(base_url="http://mock.test", transport=httpx.MockTransport(handler))
    cat._capability_clock = lambda: float(next(ticks))
    report = sd.check_sensitive_with_aspects(reader=cat)
    assert seen == ["/version", "/version"], seen
    assert report["status"] == sd.NOT_APPLICABLE and report["reason"] == _PROBE_FAILED, report


# ── the other not-applicable reasons: each says "not checked:" and what to do ───────────────────────


def test_no_catalog_is_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: None)
    report = sd.check_sensitive_with_aspects()
    assert report["reason"] == "not checked: no catalog is available; run nx doctor again once the service is running"
    assert sd.is_not_checked(report)


@pytest.mark.parametrize(
    ("probe", "reason"),
    [
        ((False, "0.0.1"), "not checked: engine 0.0.1 does not advertise catalog_sensitivity_supported; "
                           "a newer engine-service is needed"),
        ((False, ""), "not checked: the engine's capabilities could not be read; run nx doctor again"),
    ],
    ids=["flag-not-advertised", "probe-failed"],
)
def test_a_capability_that_is_not_advertised_or_not_readable_is_not_checked(
    monkeypatch: pytest.MonkeyPatch, probe: tuple[bool, str], reason: str,
) -> None:
    class _Probe(_Reader):
        def catalog_sensitivity_probe(self) -> tuple[bool, str]:
            return probe

    r = _Probe([_Doc(True)])
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    report = sd.check_sensitive_in_aspect_eligible()
    assert report["reason"] == reason and sd.is_not_checked(report)
    assert r.walks == 0


def test_an_unreadable_catalog_is_not_checked_and_the_exception_text_is_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outer-except path: the capability probe itself raises. A transport error quoting a bearer token reads
    with the token redacted."""

    class _Raises(_Reader):
        _base_url = "http://engine.test"

        def catalog_sensitivity_probe(self) -> tuple[bool, str]:
            raise httpx.LocalProtocolError("Illegal header value b'Bearer s3cr3t-token'")

    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: _Raises([_Doc(True)]))
    report = sd.check_sensitive_with_aspects()
    assert sd.is_not_checked(report) and report["status"] == sd.NOT_APPLICABLE
    assert report["reason"].startswith("not checked: the catalog could not be read (LocalProtocolError: ")
    assert report["reason"].endswith("); run nx doctor again")
    assert "s3cr3t-token" not in report["reason"] and "Bearer <redacted>" in report["reason"]


def test_an_empty_catalog_is_not_applicable_and_not_a_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """It looked and found none: the dedicated check must not read it as "could not check"."""
    r = _RouteReader([], {"count": 0, "capped": False, "documents": []})
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader", lambda: r)
    report = sd.check_sensitive_with_aspects()
    assert report["status"] == sd.NOT_APPLICABLE and not sd.is_not_checked(report)
    assert sd.render_text(report) == [f"[ ] {sd.WITH_ASPECTS}: not applicable - no sensitive documents in this catalog"]


def test_the_real_client_reads_the_list_when_the_route_answers() -> None:
    cat, seen = _real_client(version=_ROUTE, sensitive=httpx.Response(200, json={**_CAPPED_BODY, "count": 1, "capped": False}))
    report = sd.check_sensitive_in_external_embedder(reader=cat)
    assert report["status"] == sd.WARN and report["sensitive_documents"] == 1, report
    assert not [p for p in seen if p.startswith("/v1/catalog/list")], seen
