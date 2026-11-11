# SPDX-License-Identifier: AGPL-3.0-or-later
"""F30 (nexus-f3yqd.6): the PDF chunk metadata row reads the engine's bounded count when it has one (0.1.189).

The scan read every knowledge/docs collection's method-less PDF documents chunk by chunk (about 5 s on a laptop
tenant). With the route, a collection the route lists is not scanned; one it does not list still is; a capped count
reads "at least" (no SAMPLED/PARTIAL wording, which is the scan's); the route's own timeout reports the row not checked,
with the reason, instead of rerunning the scan; and no route means the scan as before.
"""
from __future__ import annotations

from typing import Any

import pytest

import nexus.health as h
from nexus.catalog.http_catalog_client import STUB_METADATA_TIMED_OUT

_NAMES = ["docs__b__bge-768__v1", "knowledge__a__voyage-context-3__v1", "knowledge__new__voyage-context-3__v1"]


class _T3:
    def list_collections(self) -> list[dict]:
        return [{"name": n} for n in _NAMES] + [{"name": "code__x__voyage-code-3__v1"}]


def _setup(monkeypatch: pytest.MonkeyPatch, answer: Any) -> list[str]:
    scanned: list[str] = []
    monkeypatch.setattr("nexus.db.make_t3", lambda *a, **k: _T3())
    monkeypatch.setattr("nexus.catalog.factory.make_catalog_reader",
                        lambda *a, **k: type("R", (), {"stub_metadata": lambda self, limit=1000: answer})())

    def scan(_t3: Any, name: str, _page: int) -> tuple[dict[str, int], h._StubScanCoverage]:
        scanned.append(name)
        return {}, h._StubScanCoverage()

    monkeypatch.setattr(h, "_pdf_stubs_in_collection", scan)
    return scanned


def test_the_route_replaces_the_scan_for_the_collections_it_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    route = [
        {"collection": _NAMES[0], "stub_chunks": 12, "capped": False, "examples": ["file:///x.pdf"]},
        {"collection": _NAMES[1], "stub_chunks": 1000, "capped": True, "examples": ["y.pdf", "z.pdf"]},
    ]
    scanned = _setup(monkeypatch, route)
    r = h._check_pdf_stub_metadata()[0]
    assert scanned == [_NAMES[2]], "only the collection the route did not list is scanned"
    assert r.ok is False and r.warn is True
    assert r.detail.startswith("at least 1012 PDF chunk(s) in 2 collection(s)"), r.detail
    assert f"{_NAMES[1]}: 1000 or more chunk(s), e.g. y.pdf, z.pdf" in r.detail
    assert "SAMPLED" not in r.detail and "PARTIAL" not in r.detail
    assert any(f.startswith("nx index pdf <path> --force") for f in r.fix_suggestions)


def test_a_clean_route_answer_is_clean_without_a_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    route = [{"collection": n, "stub_chunks": 0, "capped": False, "examples": []} for n in _NAMES]
    scanned = _setup(monkeypatch, route)
    r = h._check_pdf_stub_metadata()[0]
    assert scanned == [] and r.ok is True, r.detail
    assert "3 knowledge/docs collection(s) checked" in r.detail


def test_the_routes_timeout_is_not_checked_and_not_a_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    scanned = _setup(monkeypatch, STUB_METADATA_TIMED_OUT)
    r = h._check_pdf_stub_metadata()[0]
    assert scanned == [] and r.ok is False and r.detail.startswith("NOT CHECKED: the engine answered 503"), r.detail


def test_no_route_scans_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    scanned = _setup(monkeypatch, None)
    r = h._check_pdf_stub_metadata()[0]
    assert sorted(scanned) == sorted(_NAMES) and r.ok is True


def test_a_collection_the_engine_cut_off_is_not_checked_and_not_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    route = [
        {"collection": _NAMES[0], "stub_chunks": 3, "capped": False, "examples": ["a.pdf"]},
        {"collection": _NAMES[1], "timed_out": True},
        {"collection": _NAMES[2], "stub_chunks": 0, "capped": False, "examples": []},
    ]
    scanned = _setup(monkeypatch, route)
    r = h._check_pdf_stub_metadata()[0]
    assert scanned == [], "a timed-out collection is not rescanned"
    assert r.ok is False and r.detail.startswith("3 PDF chunk(s) in 1 collection(s)"), r.detail
    assert f"NOT CHECKED: the engine's bounded read did not finish in time for 1 collection(s): {_NAMES[1]}" in r.detail


def test_only_timed_out_rows_still_say_not_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    route = [{"collection": n, "stub_chunks": 0, "capped": False, "examples": []} for n in _NAMES[:2]]
    route.append({"collection": _NAMES[2], "timed_out": True})
    scanned = _setup(monkeypatch, route)
    r = h._check_pdf_stub_metadata()[0]
    assert scanned == [] and r.ok is False and "did not finish in time" in r.detail, r.detail


def test_a_route_row_for_a_collection_the_scan_would_skip_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    route = [{"collection": n, "stub_chunks": 0, "capped": False, "examples": []} for n in _NAMES]
    route.append({"collection": "other__z__voyage-context-3__v1", "stub_chunks": 9, "capped": False, "examples": ["q.pdf"]})
    _setup(monkeypatch, route)
    r = h._check_pdf_stub_metadata()[0]
    assert r.ok is True, r.detail
