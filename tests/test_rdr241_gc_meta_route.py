# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx catalog gc`` reads its orphans through the engine's metadata route (RDR-241 Phase 2, nexus-uwggq.2.15).

The command is driven through the real ``HttpCatalogClient`` reader over a stubbed transport that plays the route:
it filters on ``meta_min.miss_count`` as the engine does, pages by ``limit``/``offset`` and answers an exact
``count``. An engine without the route is a non-zero exit naming the route and the engine that has it, never a walk
of the catalog."""
from __future__ import annotations

import math

import httpx
import pytest
from click.testing import CliRunner

from nexus.catalog.engine_reads.route_unavailable import PHASE2_ROUTES_ENGINE
from nexus.catalog.http_catalog_client import HttpCatalogClient
from nexus.catalog.meta_filter import meta_at_least
from nexus.cli import main

_FLAGS = {"catalog_docs_by_meta_supported": True}
ROUTE = "/docs/by-meta"


def _doc(n: int, miss) -> dict:
    return {"tumbler": f"1.1.{n}", "title": f"doc {n}", "file_path": f"src/{n}.py", "meta": {"miss_count": miss}}


class _Writer:
    def __init__(self) -> None:
        self.deleted: list = []

    def delete_many(self, tumblers):
        self.deleted.append([str(t) for t in tumblers])
        return list(tumblers)


class _Engine:
    """The catalog as the engine holds it, and a transport that answers ``GET /docs/by-meta`` over it."""

    def __init__(self, docs: list[dict], *, leak: tuple[dict, ...] = (), flags: dict | None = None) -> None:
        self.docs, self.leak, self.flags = docs, leak, _FLAGS if flags is None else flags
        self.calls: list[tuple[str, dict]] = []

    def matching(self) -> list[dict]:
        rows = [d for d in self.docs if meta_at_least(d["meta"], "miss_count", 2)]
        return rows + list(self.leak)

    def get(self, path: str, **kw):
        self.calls.append((path, kw))
        assert path == ROUTE, f"gc asked {path}: only the metadata route is allowed"
        assert kw["meta_min.miss_count"] == "2"
        rows = self.matching()
        off, lim = kw["offset"], kw["limit"]
        return {"count": len(rows), "limit": lim, "offset": off, "documents": rows[off:off + lim]}


def _wire(monkeypatch: pytest.MonkeyPatch, engine: _Engine, get=None) -> _Writer:
    client = object.__new__(HttpCatalogClient)
    client._version_probe_failed = False
    monkeypatch.setattr(client, "_version_flags", lambda: engine.flags, raising=False)
    monkeypatch.setattr(client, "_get", get or engine.get, raising=False)

    def no_walk(*a, **kw):
        raise AssertionError("gc walked the catalog with all_documents")

    monkeypatch.setattr(client, "all_documents", no_walk, raising=False)
    writer = _Writer()
    monkeypatch.setattr("nexus.commands.catalog._get_catalog", lambda: client)
    monkeypatch.setattr("nexus.commands.catalog._get_catalog_writer", lambda: writer)
    monkeypatch.setattr("nexus.doc_sensitivity._catalog_reader", lambda: client)
    return writer


def _gc(*args: str):
    return CliRunner().invoke(main, ["catalog", "gc", *args])


def _fixture_docs() -> list[dict]:
    return [_doc(1, 0), _doc(2, 2), _doc(3, 3), _doc(4, "2.9"), _doc(5, 1), _doc(6, "7"), _doc(7, True),
            {"tumbler": "1.1.8", "title": "no meta", "meta": {}}]


def test_the_orphan_list_is_the_walk_s_without_the_non_integer_row(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(_fixture_docs())
    _wire(monkeypatch, engine)
    result = _gc()
    assert result.exit_code == 0, result.output
    walk_answer = [d["tumbler"] for d in _fixture_docs() if meta_at_least(d["meta"], "miss_count", 2)]
    assert walk_answer == ["1.1.2", "1.1.3", "1.1.6"]
    assert "Found 3 orphan entries (miss_count >= 2):" in result.output
    for t in walk_answer:
        assert f"{t}: " in result.output
    assert "1.1.4" not in result.output, "a miss_count of 2.9 is not an integer and is skipped"
    assert "would be deleted" in result.output


def test_a_row_the_engine_wrongly_returns_is_dropped_by_the_final_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine([_doc(1, 3)], leak=(_doc(9, 1), _doc(10, "2.9"), _doc(11, None)))
    writer = _wire(monkeypatch, engine)
    result = _gc("--no-dry-run", "--confirm")
    assert result.exit_code == 0, result.output
    assert writer.deleted == [["1.1.1"]], "an engine bug must not widen the delete set"
    assert "Found 1 orphan entry" in result.output


@pytest.mark.parametrize("n", [0, 1, 999, 1000, 1001, 2500])
def test_requests_are_the_count_over_a_page_rounded_up_and_never_unfiltered(monkeypatch: pytest.MonkeyPatch,
                                                                           n: int) -> None:
    engine = _Engine([_doc(i, 2) for i in range(n)])
    _wire(monkeypatch, engine)
    result = _gc()
    assert result.exit_code == 0, result.output
    assert len(engine.calls) == max(1, math.ceil(n / 1000))
    assert [kw["offset"] for _p, kw in engine.calls] == [1000 * i for i in range(len(engine.calls))]
    assert all(p == ROUTE and kw["limit"] == 1000 for p, kw in engine.calls)
    assert ("No orphan entries found." in result.output) == (n == 0)
    if n:
        assert f"Found {n} orphan" in result.output


def test_a_tumbler_that_repeats_across_pages_is_kept_once(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {0: [_doc(i, 2) for i in range(1000)], 1000: [_doc(999, 2), _doc(1000, 2)]}

    def get(path, **kw):
        return {"count": 1002, "limit": kw["limit"], "offset": kw["offset"], "documents": pages[kw["offset"]]}

    engine = _Engine([])
    writer = _wire(monkeypatch, engine, get=get)
    result = _gc("--no-dry-run", "--confirm")
    assert result.exit_code == 0, result.output
    (deleted,) = writer.deleted
    assert len(deleted) == 1001 and deleted.count("1.1.999") == 1


def test_an_empty_page_before_the_count_is_an_error_not_a_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(path, **kw):
        docs = [_doc(i, 2) for i in range(1000)] if kw["offset"] == 0 else []
        return {"count": 1500, "limit": kw["limit"], "offset": kw["offset"], "documents": docs}

    writer = _wire(monkeypatch, _Engine([]), get=get)
    result = _gc("--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "1500" in result.output and "1000" in result.output
    assert writer.deleted == [], "a partial list must never be deleted from"


@pytest.mark.parametrize("flags", [{}, {"catalog_docs_by_meta_supported": "true"}])
def test_an_engine_without_the_flag_exits_naming_the_route_and_the_engine(monkeypatch: pytest.MonkeyPatch,
                                                                          flags: dict) -> None:
    engine = _Engine(_fixture_docs(), flags=flags)
    writer = _wire(monkeypatch, engine)
    result = _gc("--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "GET /v1/catalog/docs/by-meta" in result.output and PHASE2_ROUTES_ENGINE in result.output
    assert engine.calls == [], "no request at all without the flag"
    assert writer.deleted == []


def _status(code: int):
    def fail(path, **kw):
        request = httpx.Request("GET", f"http://engine/v1/catalog{path}")
        raise httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))
    return fail


def test_a_404_names_the_route_and_the_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _wire(monkeypatch, _Engine([]), get=_status(404))
    result = _gc("--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "GET /v1/catalog/docs/by-meta" in result.output and PHASE2_ROUTES_ENGINE in result.output
    assert writer.deleted == []


def test_a_503_says_the_engine_could_not_answer_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = _wire(monkeypatch, _Engine([]), get=_status(503))
    result = _gc("--no-dry-run", "--confirm")
    assert result.exit_code != 0
    assert "could not answer" in result.output and "in time" in result.output
    assert writer.deleted == []


def test_a_failed_version_probe_is_not_a_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine(_fixture_docs(), flags={})
    _wire(monkeypatch, engine)
    client = __import__("nexus.commands.catalog", fromlist=["_get_catalog"])._get_catalog()
    client._version_probe_failed = True
    result = _gc()
    assert result.exit_code != 0 and "GET /version failed" in result.output
    assert engine.calls == []
