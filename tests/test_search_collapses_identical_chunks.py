# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1524: the ``search`` MCP tool returns an identical chunk once, not once per collection.

A chunk is content-addressed, so the same text indexed in five collections (the RDR template's
README) is one ``chunk_text_hash`` in five collections and ranked first five times. ``query()`` and
``nx_answer`` have collapsed it since 7.38.0 (nexus-20uv3, tests/test_query_collapses_identical_chunks.py);
``search()`` never did. It now keeps the best-ranked row and names the other collections on it.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.mcp import core
from nexus.types import SearchResult

_A = "aa" * 32
_B = "bb" * 32


def _sr(rid: str, coll: str, chash: str, dist: float) -> SearchResult:
    return SearchResult(id=rid, content=f"text of {rid}", distance=dist, collection=coll,
                        metadata={"chunk_text_hash": chash})


def _five_copies() -> list[SearchResult]:
    # Distance-ordered, as the retrieval returns them: one chunk in four collections, one other chunk.
    return [
        _sr("c1", "rdr__1-53__bge-base-en-v15-768__v1", _A, 0.109),
        _sr("c2", "rdr__1-46__bge-base-en-v15-768__v1", _A, 0.111),
        _sr("c3", "rdr__1-61__bge-base-en-v15-768__v1", _B, 0.115),
        _sr("c4", "rdr__1-63__bge-base-en-v15-768__v1", _A, 0.118),
        _sr("c5", "rdr__1-2__bge-base-en-v15-768__v1", _A, 0.120),
    ]


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    def fake_search(query: str, target: list[str], **kw: Any) -> list[SearchResult]:
        calls.append(1)
        return _five_copies()

    class _T3:
        supports_server_rerank = False

    monkeypatch.setattr(core, "_get_t3", lambda: _T3())
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **kw: ["rdr__1-53"])
    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake_search)
    # Ranking is not under test: keep the retrieval's order.
    monkeypatch.setattr("nexus.search_engine.apply_ranking_boosts", lambda results, **kw: results)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    monkeypatch.setattr(core, "_get_catalog", lambda: None)

    def _no_t1():
        raise RuntimeError("no T1 in this test")  # the search trace is best-effort and not under test

    monkeypatch.setattr(core, "_get_t1", _no_t1)

    def _run(**kw: Any):
        core._page_cache_invalidate()
        try:
            return core.search(query="rdr readme", corpus="rdr", threshold=float("inf"), **kw), calls
        finally:
            core._page_cache_invalidate()

    return _run


def test_structured_search_returns_one_row_per_identical_chunk(run) -> None:
    out, calls = run(structured=True)
    assert calls, "the retrieval ran: not vacuous"
    assert out["ids"] == ["c1", "c3"], out
    assert out["chunk_text_hash"] == [_A, _B]
    assert out["also_in"] == [
        ["rdr__1-46__bge-base-en-v15-768__v1", "rdr__1-63__bge-base-en-v15-768__v1",
         "rdr__1-2__bge-base-en-v15-768__v1"],
        [],
    ], "the kept row names the other collections, in rank order"


def test_text_search_shows_the_chunk_once_and_names_the_other_collections(run) -> None:
    out, _ = run()
    assert out.count("text of c1") == 1
    for dropped in ("text of c2", "text of c4", "text of c5"):
        assert dropped not in out, out
    assert "text of c3" in out
    assert "also in: rdr__1-46__bge-base-en-v15-768__v1, rdr__1-63__bge-base-en-v15-768__v1, " \
           "rdr__1-2__bge-base-en-v15-768__v1" in out, out


def test_a_page_without_copies_keeps_its_structured_shape(run, monkeypatch: pytest.MonkeyPatch) -> None:
    # Distinct chunks only: no also_in key, so the documented shape is unchanged for a clean page.
    monkeypatch.setattr(
        "nexus.search_engine.search_cross_corpus",
        lambda query, target, **kw: [_sr("c1", "rdr__1-53", _A, 0.1), _sr("c3", "rdr__1-61", _B, 0.2)],
    )
    out, _ = run(structured=True)
    assert out["ids"] == ["c1", "c3"]
    assert "also_in" not in out
