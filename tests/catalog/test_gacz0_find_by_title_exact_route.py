# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-gacz0: ``find_by_title_exact`` asks the engine's exact-title route
(``GET /v1/catalog/by_title``) instead of paging a full-text search, and
falls back to that search on an engine without the route.

The route's own behaviour (exactness, owner scope, tenant scope, no page cap)
is pinned engine-side by ``CatalogHandlerByTitleTest``. These tests pin the
client's use of it: which request goes out, that an old engine (404/405) costs
one probe per interval and still answers correctly, and that any other
failure is not mistaken for "route absent".
"""
from __future__ import annotations

import httpx
import pytest

import nexus.catalog.http_catalog_client as hcc
from nexus.catalog.http_catalog_client import HttpCatalogClient


def _doc(tumbler: str, title: str, content_type: str = "knowledge") -> dict:
    return {"tumbler": tumbler, "title": title, "content_type": content_type}


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://engine/v1/catalog/by_title")
    return httpx.HTTPStatusError(
        f"{code}", request=request, response=httpx.Response(code, request=request),
    )


class _Client:
    """An HttpCatalogClient with ``_get`` replaced by a recorded script."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, routes: dict) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.client = object.__new__(HttpCatalogClient)

        def fake_get(path: str, *, idempotent: bool = True, **params):
            # The real _get drops None/"" params before they reach the wire; record what it sends.
            params = {k: v for k, v in params.items() if v is not None and v != ""}
            self.calls.append((path, params))
            handler = routes[path]
            if isinstance(handler, Exception):
                raise handler
            return handler(params) if callable(handler) else handler

        monkeypatch.setattr(self.client, "_get", fake_get)

    @property
    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]


class TestTheRouteIsUsed:
    def test_a_present_route_answers_in_one_request_and_never_searches(self, monkeypatch) -> None:
        c = _Client(monkeypatch, {"/by_title": {"documents": [_doc("1.1.5", "The Note")], "count": 1}})
        got = c.client.find_by_title_exact("The Note", content_type="knowledge")
        assert [str(e.tumbler) for e in got] == ["1.1.5"]
        assert c.calls == [("/by_title", {"title": "The Note", "content_type": "knowledge"})]

    def test_the_owner_is_sent_as_its_string_form(self, monkeypatch) -> None:
        from nexus.catalog.tumbler import Tumbler

        c = _Client(monkeypatch, {"/by_title": {"documents": [], "count": 0}})
        assert c.client.find_by_title_exact("The Note", owner=Tumbler.parse("1.1")) == []
        assert c.calls == [("/by_title", {"title": "The Note", "owner": "1.1"})]

    def test_a_row_whose_title_is_not_exact_is_dropped_defensively(self, monkeypatch) -> None:
        # The engine filters exactly; the client still refuses a row that is not,
        # the way by_file_path does, so a regressed route cannot widen a match.
        c = _Client(monkeypatch, {"/by_title": {"documents": [
            _doc("1.1.1", "The Note"), _doc("1.1.2", "The Note, Part 2"),
        ]}})
        assert [str(e.tumbler) for e in c.client.find_by_title_exact("The Note")] == ["1.1.1"]


class TestAnOldEngine:
    @pytest.fixture(autouse=True)
    def _fresh_backoff(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(hcc, "_monotonic", lambda: now[0])
        self.now = now

    @pytest.mark.parametrize("code", [404, 405])
    def test_an_engine_without_the_route_falls_back_to_the_full_text_search(self, monkeypatch, code) -> None:
        search_rows = [_doc("1.1.1", "The Note"), _doc("1.1.2", "The Note, Part 2")]
        c = _Client(monkeypatch, {"/by_title": _status_error(code), "/search": {"documents": search_rows}})
        got = c.client.find_by_title_exact("The Note", content_type="knowledge")
        assert [str(e.tumbler) for e in got] == ["1.1.1"]
        assert c.paths == ["/by_title", "/search"]

    def test_the_owner_filter_still_applies_on_the_fallback(self, monkeypatch) -> None:
        from nexus.catalog.tumbler import Tumbler

        rows = [_doc("1.1.1", "The Note"), _doc("2.1.1", "The Note")]
        c = _Client(monkeypatch, {"/by_title": _status_error(404), "/search": {"documents": rows}})
        got = c.client.find_by_title_exact("The Note", owner=Tumbler.parse("1.1"))
        assert [str(e.tumbler) for e in got] == ["1.1.1"]

    def test_the_miss_is_remembered_so_an_old_engine_costs_one_probe_per_interval(self, monkeypatch) -> None:
        c = _Client(monkeypatch, {"/by_title": _status_error(404), "/search": {"documents": []}})
        c.client.find_by_title_exact("a")
        c.client.find_by_title_exact("b")
        assert c.paths == ["/by_title", "/search", "/search"]
        # After the interval the route is probed again, so an engine upgrade is picked up
        # by a long-lived process without a restart.
        self.now[0] += hcc._BY_TITLE_RETRY_S + 1
        c.client.find_by_title_exact("c")
        assert c.paths == ["/by_title", "/search", "/search", "/by_title", "/search"]

    def test_a_present_route_after_the_interval_is_used_again(self, monkeypatch) -> None:
        answers = iter([_status_error(404), {"documents": [_doc("1.1.1", "The Note")]}])
        calls: list[str] = []

        def by_title(params):
            calls.append("by_title")
            nxt = next(answers)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        c = _Client(monkeypatch, {"/by_title": by_title, "/search": {"documents": []}})
        c.client.find_by_title_exact("The Note")
        self.now[0] += hcc._BY_TITLE_RETRY_S + 1
        got = c.client.find_by_title_exact("The Note")
        assert [str(e.tumbler) for e in got] == ["1.1.1"]
        assert calls == ["by_title", "by_title"]


class TestOtherFailuresAreNotRouteAbsence:
    def test_a_server_error_propagates_and_does_not_poison_the_route(self, monkeypatch) -> None:
        c = _Client(monkeypatch, {"/by_title": _status_error(500), "/search": {"documents": []}})
        with pytest.raises(httpx.HTTPStatusError):
            c.client.find_by_title_exact("The Note")
        assert c.paths == ["/by_title"], "a 500 must not fall back to the search"
        # And it left no 'route absent' memory: the next call asks the route again.
        with pytest.raises(httpx.HTTPStatusError):
            c.client.find_by_title_exact("The Note")
        assert c.paths == ["/by_title", "/by_title"]

    def test_an_empty_title_is_not_sent_to_the_route(self, monkeypatch) -> None:
        # An empty title would be a 400 from the route; the old path's own behaviour for it
        # is unchanged, so the route is never asked.
        c = _Client(monkeypatch, {"/search": {"documents": []}})
        assert c.client.find_by_title_exact("") == []
        assert "/by_title" not in c.paths
