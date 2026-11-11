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


def _hundred_with_five_copies(n: int) -> list[SearchResult]:
    def sr(i: int) -> SearchResult:
        chash = _A if i <= 4 else f"{i:064x}"
        return _sr(f"c{i}", f"rdr__1-{i}__bge-base-en-v15-768__v1", chash, 0.1 + i * 0.001)

    return [sr(i) for i in range(min(n, 100))]


def test_paging_across_a_window_holding_copies_reaches_every_row(run, monkeypatch: pytest.MonkeyPatch) -> None:
    # The critique of 0f2e04cae: the page cache read "fewer rows than fetched" as "the corpus is
    # exhausted", so a collapsed window ended paging early (offset 30 of 96 distinct rows came back empty).
    asked: list[int] = []

    def fake(query: str, target: list[str], **kw: Any) -> list[SearchResult]:
        asked.append(kw["n_results"])
        return _hundred_with_five_copies(kw["n_results"])

    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake)
    core._page_cache_invalidate()
    seen: list[str] = []
    try:
        for off in range(0, 100, 10):
            out = core.search(query="q", corpus="rdr", threshold=float("inf"), structured=True, offset=off, limit=10)
            seen += out.get("ids", []) if isinstance(out, dict) else []
    finally:
        core._page_cache_invalidate()
    assert len(asked) > 1, f"the window edge refetched: {asked}"
    assert seen == ["c0"] + [f"c{i}" for i in range(5, 100)], (len(seen), seen[-5:])


def test_the_text_footer_says_more_rows_exist_when_a_collapsed_window_is_full(run, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "nexus.search_engine.search_cross_corpus",
        lambda query, target, **kw: _hundred_with_five_copies(kw["n_results"]),
    )
    out, _ = run(offset=20, limit=10)
    assert "(end)" not in out, out[-200:]


def test_a_cache_hit_short_after_the_collapse_refetches_deeper_than_the_entry(run, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    def fake(query: str, target: list[str], **kw: Any) -> list[SearchResult]:
        asked.append(kw["n_results"])
        return _hundred_with_five_copies(kw["n_results"])

    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake)
    core._page_cache_invalidate()
    try:
        core.search(query="q", corpus="rdr", threshold=float("inf"), structured=True, offset=0, limit=10)
        # offset 20 is a cache hit (need 30 of 30 fetched), but the collapse leaves 26 rows
        out = core.search(query="q", corpus="rdr", threshold=float("inf"), structured=True, offset=20, limit=10)
    finally:
        core._page_cache_invalidate()
    assert len(out["ids"]) == 10, out["ids"]
    assert asked[0] == 30 and len(asked) == 2 and asked[1] > 30, asked


def test_a_sensitive_copy_withholds_the_row_it_collapses_into_end_to_end(run, monkeypatch: pytest.MonkeyPatch) -> None:
    clean = _sr("c1", "docs__1-1__bge-base-en-v15-768__v1", _A, 0.1)
    clean.metadata["sensitive"] = False
    secret = _sr("c2", "knowledge__medical__bge-base-en-v15-768__v1", _A, 0.2)
    secret.metadata.update({"sensitive": True, "sensitive_owners": ["1.9.4"]})
    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", lambda query, target, **kw: [clean, secret])
    out, _ = run(structured=True)
    assert out["ids"] == [], "the row stands for the sensitive copy too, so it is withheld"
    assert out.get("sensitive_withheld") == 1, out
    text, _ = run()
    assert "knowledge__medical" not in text and "text of c1" not in text, text


def test_structured_rows_name_the_dropped_copies_documents_like_query(run) -> None:
    out, _ = run(structured=True)
    assert out["also_in_ids"] == [["c2", "c4", "c5"], []], out


def _paged(monkeypatch: pytest.MonkeyPatch, n_rows: int, offsets: list[int]) -> list[dict]:
    def fake(query: str, target: list[str], **kw: Any) -> list[SearchResult]:
        return _hundred_with_five_copies(min(kw["n_results"], n_rows))

    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake)
    core._page_cache_invalidate()
    try:
        return [core.search(query="q", corpus="rdr", threshold=float("inf"), structured=True, offset=o, limit=10)
                for o in offsets]
    finally:
        core._page_cache_invalidate()


def test_a_structured_page_says_where_the_next_page_starts(run, monkeypatch: pytest.MonkeyPatch) -> None:
    (page,) = _paged(monkeypatch, 100, [0])
    assert page["next_offset"] == 10, page


def test_the_last_structured_page_has_no_next_offset(run, monkeypatch: pytest.MonkeyPatch) -> None:
    # 30 rows, 5 of them copies of one chunk: 26 distinct rows, so offset 20 is the last page.
    (page,) = _paged(monkeypatch, 30, [20])
    assert len(page["ids"]) == 6 and "next_offset" not in page, page


def test_following_next_offset_reaches_every_row(run, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(query: str, target: list[str], **kw: Any) -> list[SearchResult]:
        return _hundred_with_five_copies(kw["n_results"])

    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake)
    core._page_cache_invalidate()
    seen: list[str] = []
    off: int | None = 0
    try:
        while off is not None and len(seen) < 200:
            page = core.search(query="q", corpus="rdr", threshold=float("inf"), structured=True, offset=off, limit=10)
            seen += page["ids"]
            off = page.get("next_offset")
    finally:
        core._page_cache_invalidate()
    assert seen == ["c0"] + [f"c{i}" for i in range(5, 100)], (len(seen), seen[-3:])
