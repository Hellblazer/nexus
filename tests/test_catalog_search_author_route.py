# SPDX-License-Identifier: AGPL-3.0-or-later
"""MCP ``catalog_search`` with ``author`` alone asks the engine's author route (RDR-241 Phase 2, nexus-uwggq.2.12).

The reader is the real :class:`HttpCatalogClient` over a stubbed transport that plays the route (a fake engine that
records every path it is asked and refuses any other), so the tool never lists the catalog. Branches that carry
another filter keep their sources and are checked against a reader whose ``all_documents`` and
``documents_by_author`` both fail."""
from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

import nexus.mcp.catalog as mcp_catalog
from nexus.catalog.engine_reads.route_unavailable import PHASE2_ROUTES_ENGINE
from nexus.catalog.http_catalog_client import HttpCatalogClient, _to_entry

FLAG = "catalog_docs_by_author_supported"
ROUTE_PATH = "/docs/by-author"


def _docs(n_filler: int, *, target_at: int | None = None) -> list[dict]:
    docs = [{"tumbler": f"1.1.{i + 1}", "title": f"scale doc {i}", "author": "Filler Person"} for i in range(n_filler)]
    if target_at is not None:
        docs.insert(target_at, {"tumbler": "1.1.9999", "title": "target", "author": "Leslie Lamport"})
    return docs


class FakeEngine:
    """Plays ``GET /v1/catalog/docs/by-author``: substring, case-insensitive, document order, exact count."""

    def __init__(self, docs: list[dict], *, flags: dict | None = None, fail: int | None = None,
                 fail_from_offset: int = 0, rewrite: Callable[[dict], dict] | None = None) -> None:
        self.docs, self.fail, self.fail_from_offset, self.rewrite = docs, fail, fail_from_offset, rewrite
        self.calls: list[tuple[str, dict]] = []
        self.flags = {FLAG: True} if flags is None else flags

    def client(self, monkeypatch: pytest.MonkeyPatch) -> HttpCatalogClient:
        c = object.__new__(HttpCatalogClient)
        c._version_probe_failed = False
        monkeypatch.setattr(c, "_version_flags", lambda: self.flags, raising=False)
        monkeypatch.setattr(c, "_get", self._get, raising=False)
        return c

    def _get(self, path: str, **kw):
        self.calls.append((path, kw))
        assert path == ROUTE_PATH, f"the tool asked {path}: only the author route is allowed"
        assert 1 <= kw["limit"] <= 1000, f"the route caps limit at 1000, asked {kw['limit']}"
        if self.fail is not None and kw["offset"] >= self.fail_from_offset:
            request = httpx.Request("GET", f"http://engine/v1/catalog{path}")
            raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(self.fail, request=request))
        hits = [d for d in self.docs if kw["author"].lower() in d["author"].lower()]
        off, lim = kw["offset"], kw["limit"]
        body = {"count": len(hits), "limit": lim, "offset": off, "documents": hits[off:off + lim]}
        return self.rewrite(body) if self.rewrite else body

    def calls_as_limit_offset(self) -> list[tuple[int, int]]:
        return [(kw["limit"], kw["offset"]) for _, kw in self.calls]


@pytest.fixture
def serve(monkeypatch: pytest.MonkeyPatch):
    def install(engine: FakeEngine) -> FakeEngine:
        cat = engine.client(monkeypatch)
        monkeypatch.setattr(mcp_catalog, "_require_catalog", lambda: (cat, None))
        return engine
    return install


def _tumblers(rows: list[dict]) -> list[str]:
    assert not any("error" in r for r in rows), rows
    return [r["tumbler"] for r in rows if "tumbler" in r]


def test_author_alone_finds_a_late_document_with_one_route_request(serve) -> None:
    engine = serve(FakeEngine(_docs(1200, target_at=1200)))
    assert _tumblers(mcp_catalog.catalog_search(author="lamport")) == ["1.1.9999"]
    assert engine.calls_as_limit_offset() == [(21, 0)]


def test_author_alone_short_last_page_has_no_pagination(serve) -> None:
    engine = serve(FakeEngine(_docs(1201)))
    rows = mcp_catalog.catalog_search(author="Filler Person", limit=50, offset=1190)
    assert len(_tumblers(rows)) == 11 and not any("_pagination" in r for r in rows)
    assert engine.calls_as_limit_offset() == [(51, 1190)]


def test_author_alone_full_page_reports_next_offset(serve) -> None:
    serve(FakeEngine(_docs(30)))
    rows = mcp_catalog.catalog_search(author="Filler", limit=10, offset=5)
    assert len(_tumblers(rows)) == 10
    assert rows[-1] == {"_pagination": {"next_offset": 15, "limit": 10}}


def test_a_limit_past_the_route_cap_pages_the_route(serve) -> None:
    engine = serve(FakeEngine(_docs(2500)))
    rows = mcp_catalog.catalog_search(author="Filler", limit=1500, offset=100)
    assert len(_tumblers(rows)) == 1500
    assert rows[-1] == {"_pagination": {"next_offset": 1600, "limit": 1500}}
    assert engine.calls_as_limit_offset() == [(1000, 100), (501, 1100)]


def test_paging_stops_when_the_route_runs_out(serve) -> None:
    engine = serve(FakeEngine(_docs(1200)))
    rows = mcp_catalog.catalog_search(author="Filler", limit=5000)
    assert len(_tumblers(rows)) == 1200 and not any("_pagination" in r for r in rows)
    assert engine.calls_as_limit_offset() == [(1000, 0), (1000, 1000)]


def test_has_more_is_read_from_the_raw_rows_not_from_what_the_filter_keeps(serve) -> None:
    """An engine row the client's final filter drops must not hide the following page."""
    def first_row_is_not_a_match(body: dict) -> dict:
        if body["offset"] == 0:
            body["documents"][0] = dict(body["documents"][0], author="Somebody Else")
        return body

    serve(FakeEngine(_docs(3), rewrite=first_row_is_not_a_match))
    rows = mcp_catalog.catalog_search(author="Filler", limit=2)
    assert len(_tumblers(rows)) == 1, "the dropped row is not shown"
    assert rows[-1] == {"_pagination": {"next_offset": 2, "limit": 2}}, "and the third raw row still means more"


def test_a_page_the_filter_empties_with_more_behind_it_still_points_on(serve) -> None:
    def every_row_is_not_a_match(body: dict) -> dict:
        body["documents"] = [dict(d, author="Somebody Else") for d in body["documents"]]
        return body

    serve(FakeEngine(_docs(5), rewrite=every_row_is_not_a_match))
    rows = mcp_catalog.catalog_search(author="Filler", limit=2)
    assert rows[-1] == {"_pagination": {"next_offset": 2, "limit": 2}} and not _tumblers(rows)


def test_offset_past_the_matches_names_the_route_count(serve) -> None:
    serve(FakeEngine(_docs(3)))
    rows = mcp_catalog.catalog_search(author="Filler", offset=50)
    assert rows == [{"note": "No rows at offset 50: this search has at most 3 matches, all before that offset."}]
    serve(FakeEngine(_docs(1)))
    assert "has at most 1 match," in mcp_catalog.catalog_search(author="Filler", offset=7)[0]["note"]


def test_no_match_at_offset_zero_is_the_zero_match_note(serve) -> None:
    serve(FakeEngine(_docs(3)))
    assert mcp_catalog.catalog_search(author="nobody") == [{"note": mcp_catalog._ZERO_MATCH_NOTE}]


@pytest.mark.parametrize("flags", [{}, {FLAG: "true"}, {FLAG: False}])
def test_an_engine_without_the_flag_answers_an_error_row_naming_the_route(serve, flags) -> None:
    engine = serve(FakeEngine(_docs(3), flags=flags))
    rows = mcp_catalog.catalog_search(author="Filler")
    assert engine.calls == [], "no request without the flag, and never a walk"
    assert len(rows) == 1 and set(rows[0]) == {"error"}
    assert "GET /v1/catalog/docs/by-author" in rows[0]["error"] and PHASE2_ROUTES_ENGINE in rows[0]["error"]


def test_a_503_answers_an_error_row_saying_the_engine_could_not_answer_in_time(serve) -> None:
    serve(FakeEngine(_docs(3), fail=503))
    rows = mcp_catalog.catalog_search(author="Filler")
    assert len(rows) == 1 and "could not answer" in rows[0]["error"] and "in time" in rows[0]["error"]


@pytest.mark.parametrize("fail", [404, 500])
def test_other_route_failures_are_error_rows_never_empty(serve, fail) -> None:
    serve(FakeEngine(_docs(3), fail=fail))
    rows = mcp_catalog.catalog_search(author="Filler")
    assert len(rows) == 1 and "GET /v1/catalog/docs/by-author" in rows[0]["error"]


def test_a_failure_on_a_later_page_is_an_error_row_not_a_partial_page(serve) -> None:
    serve(FakeEngine(_docs(2500), fail=503, fail_from_offset=1000))
    rows = mcp_catalog.catalog_search(author="Filler", limit=1500)
    assert len(rows) == 1 and "in time" in rows[0]["error"]


def test_a_reader_without_the_author_route_is_an_error_row_not_a_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    class OldReader:
        def all_documents(self):
            raise AssertionError("the author-alone branch must not walk the catalog")

    monkeypatch.setattr(mcp_catalog, "_require_catalog", lambda: (OldReader(), None))
    rows = mcp_catalog.catalog_search(author="Filler")
    assert len(rows) == 1 and "/docs/by-author" in rows[0]["error"]


# ── the branches that keep their sources ───────────────────────────────────────


class _OtherSourcesReader:
    """Every walk and the author route fail; only the sources the other branches own answer."""

    def __init__(self, docs: list) -> None:
        self.docs = docs

    def all_documents(self):
        raise AssertionError("walk")

    def documents_by_author(self, *a, **kw):
        raise AssertionError("the author route is only for author alone")

    def find_all(self, query, content_type=None):
        return self.docs

    def by_corpus(self, corpus):
        return [e for e in self.docs if e.corpus == corpus]

    def by_content_type(self, content_type):
        return self.docs


def _entries() -> list:
    return [
        _to_entry({"tumbler": t, "title": t, "author": a, "corpus": corpus, "content_type": "code"})
        for t, a, corpus in (("1.1.1", "Leslie Lamport", "x"), ("1.1.2", "Other", "x"), ("1.1.3", "Leslie Lamport", "y"))
    ]


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({"query": "doc"}, ["1.1.1", "1.1.3"]),
    ({"corpus": "x"}, ["1.1.1"]),
    ({"content_type": "code"}, ["1.1.1", "1.1.3"]),
])
def test_author_with_another_filter_keeps_that_filters_source(monkeypatch, kwargs, expected) -> None:
    monkeypatch.setattr(mcp_catalog, "_require_catalog", lambda: (_OtherSourcesReader(_entries()), None))
    assert _tumblers(mcp_catalog.catalog_search(author="lamport", **kwargs)) == expected
