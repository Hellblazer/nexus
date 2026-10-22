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

from types import SimpleNamespace

import httpx
import pytest

import nexus.catalog.http_catalog_client as hcc
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.store_hook import _find_ghost_by_title
from nexus.catalog.tumbler import Tumbler


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


class TestTheChunkCountFilter:
    def test_chunk_count_zero_is_sent_to_the_route_not_dropped_as_falsy(self, monkeypatch) -> None:
        c = _Client(monkeypatch, {"/by_title": {"documents": [], "count": 0}})
        c.client.find_by_title_exact("The Note", content_type="knowledge", chunk_count=0)
        assert c.calls == [("/by_title", {"title": "The Note", "content_type": "knowledge", "chunk_count": 0})]

    def test_no_chunk_count_sends_no_chunk_count(self, monkeypatch) -> None:
        c = _Client(monkeypatch, {"/by_title": {"documents": [], "count": 0}})
        c.client.find_by_title_exact("The Note")
        assert "chunk_count" not in c.calls[0][1]

    def test_a_row_with_the_wrong_chunk_count_is_dropped_defensively(self, monkeypatch) -> None:
        c = _Client(monkeypatch, {"/by_title": {"documents": [
            {**_doc("1.1.1", "The Note"), "chunk_count": 0},
            {**_doc("1.1.2", "The Note"), "chunk_count": 4},
        ]}})
        assert [str(e.tumbler) for e in c.client.find_by_title_exact("The Note", chunk_count=0)] == ["1.1.1"]

    def test_the_fallback_filters_chunk_count_client_side(self, monkeypatch) -> None:
        rows = [
            {**_doc("1.1.1", "The Note"), "chunk_count": 0},
            {**_doc("1.1.2", "The Note"), "chunk_count": 4},
        ]
        monkeypatch.setattr(hcc, "_monotonic", lambda: 1000.0)
        c = _Client(monkeypatch, {"/by_title": _status_error(404), "/search": {"documents": rows}})
        assert [str(e.tumbler) for e in c.client.find_by_title_exact("The Note", chunk_count=0)] == ["1.1.1"]
        assert [str(e.tumbler) for e in c.client.find_by_title_exact("The Note", chunk_count=4)] == ["1.1.2"]
        assert len(c.client.find_by_title_exact("The Note")) == 2


class TestTheGhostReconcileAsksForGhostsOnly:
    """``store_hook._find_ghost_by_title`` hands the ghost filter to the reader (nexus-gacz0)
    and keeps its own checks as the guard."""

    class _Reader:
        def __init__(self, entries) -> None:
            self.entries = entries
            self.calls: list[tuple] = []

        def find_by_title_exact(self, title, **kw):
            self.calls.append((title, kw))
            return self.entries

    @staticmethod
    def _entry(tumbler: str, chunk_count: int):
        return SimpleNamespace(tumbler=Tumbler.parse(tumbler), chunk_count=chunk_count)

    def test_the_reader_is_asked_for_the_owners_ghosts_of_that_title(self) -> None:
        owner = Tumbler.parse("1.1")
        ghost = self._entry("1.1.7", 0)
        reader = self._Reader([ghost])
        assert _find_ghost_by_title(reader, owner, "The Note") is ghost
        assert reader.calls == [("The Note", {"content_type": "knowledge", "owner": owner, "chunk_count": 0})]

    def test_a_reader_that_ignores_the_filters_cannot_widen_the_match(self) -> None:
        owner = Tumbler.parse("1.1")
        reader = self._Reader([self._entry("1.1.3", 5), self._entry("2.1.1", 0)])
        assert _find_ghost_by_title(reader, owner, "The Note") is None


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
