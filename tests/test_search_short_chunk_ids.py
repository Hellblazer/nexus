# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2k66t.6 (1a): text ``search`` prints a short chunk id on each hit, and ``store_get_many`` resolves it.

Before this, the default text output carried no chunk id, so going from a snippet to the whole chunk took a second
search with ``structured=True``. The id is the chash's first eight characters; the MCP server process records the
full chashes its text search showed and ``store_get_many`` expands a unique prefix of one of them.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.mcp import _shown_chunks, core
from nexus.types import SearchResult

_A = "ab12cd34" + "0" * 56
_B = "ab12cd34" + "1" * 56  # shares the 8-character id with _A
_C = "c0ffee00" + "2" * 56


@pytest.fixture(autouse=True)
def _fresh_record():
    _shown_chunks._clear()
    yield
    _shown_chunks._clear()


def _sr(rid: str, chash: str, dist: float) -> SearchResult:
    return SearchResult(id=rid, content=f"text of {rid}", distance=dist, collection="knowledge__1-1__m__v1",
                        metadata={"chunk_text_hash": chash, "_display_title": f"doc {rid}"})


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch):
    rows: list[SearchResult] = []

    class _T3:
        supports_server_rerank = False

    monkeypatch.setattr(core, "_get_t3", lambda: _T3())
    monkeypatch.setattr(core, "_resolve_corpus_target", lambda corpus, t3, **kw: ["knowledge__1-1"])
    def fake_scc(q, target, **kw):
        calls.append(kw)
        return list(rows)

    calls: list[dict] = []
    monkeypatch.setattr("nexus.search_engine.search_cross_corpus", fake_scc)
    monkeypatch.setattr("nexus.search_engine.apply_ranking_boosts", lambda results, **kw: results)
    monkeypatch.setattr(core, "_search_taxonomy", lambda: None)
    monkeypatch.setattr(core, "_get_catalog", lambda: None)

    def _no_t1():
        raise RuntimeError("no T1 in this test")

    monkeypatch.setattr(core, "_get_t1", _no_t1)

    def _run(results: list[SearchResult], **kw: Any):
        _run.calls = calls  # type: ignore[attr-defined]
        rows[:] = results
        core._page_cache_invalidate()
        try:
            return core.search(query="q", corpus="knowledge", threshold=float("inf"), **kw)
        finally:
            core._page_cache_invalidate()

    return _run


def test_each_text_hit_prints_its_short_id_and_records_the_full_chash(search) -> None:
    out = search([_sr("c1", _A, 0.1), _sr("c3", _C, 0.2)])
    assert "] id=ab12cd34 doc c1" in out
    assert "] id=c0ffee00 doc c3" in out
    assert _shown_chunks.expand("ab12cd34,c0ffee00")[0] == f"{_A},{_C}"


def test_a_row_without_a_full_chash_prints_no_id(search) -> None:
    out = search([SearchResult(id="legacy-1", content="x", distance=0.1, collection="knowledge__1-1__m__v1",
                               metadata={"_display_title": "old"})])
    assert "] old" in out and " id " not in out.split("\n", 2)[1]


def test_structured_output_is_unchanged_and_records_nothing(search) -> None:
    out = search([_sr("c1", _A, 0.1)], structured=True)
    assert out["chunk_text_hash"] == [_A]
    assert _shown_chunks.expand("ab12cd34")[1], "structured output shows full ids; nothing is recorded"


def test_a_withheld_row_prints_its_stub_and_no_id(search, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_mcp_output_posture", lambda: "withhold")
    secret = _sr("c1", _A, 0.1)
    secret.metadata["sensitive"] = True
    clean = _sr("c3", _C, 0.2)
    clean.metadata["sensitive"] = False
    out = search([secret, clean])
    assert "ab12cd34" not in out, out
    assert "] id=c0ffee00 doc c3" in out
    assert _shown_chunks.expand("ab12cd34")[1], "a withheld row's chash is not recorded either"


# ── store_get_many ──────────────────────────────────────────────────────────


@pytest.fixture
def get_many(monkeypatch: pytest.MonkeyPatch):
    seen: list[Any] = []

    def impl(*, ids, structured, collections="knowledge", **kw):
        seen.append(ids)
        colls.append(collections)
        if isinstance(ids, list):
            flat = [x for s in ids for x in s] if ids and isinstance(ids[0], list) else list(ids)
        else:
            flat = [s for s in ids.split(",") if s]
        known = {_A, _C}
        missing = [i for i in flat if i not in known]
        if structured:
            return {"contents": ["x" if i in known else "" for i in flat], "missing": missing}
        return "\n".join([f"Hydrated {len(flat) - len(missing)}/{len(flat)} docs",
                          *([f"Missing: {', '.join(missing)}"] if missing else [])])

    monkeypatch.setattr(core, "_store_get_many_impl", impl)
    return seen

colls: list = []


def test_store_get_many_expands_a_recorded_short_id(get_many) -> None:
    _shown_chunks.record([(_A, "knowledge__1-1__m__v1"), (_C, "knowledge__1-1__m__v1")])
    colls.clear()
    out = core.store_get_many(ids="c0ffee00")
    assert get_many == [[_C]] and colls == [["knowledge__1-1__m__v1"]]
    assert "Missing" not in out


def test_list_and_parallel_shapes_are_expanded_in_place(get_many) -> None:
    _shown_chunks.record([(_C, "knowledge__1-1__m__v1")])
    core.store_get_many(ids=["c0ffee00", _A], collections="knowledge__1-1__m__v1")
    core.store_get_many(ids=[["c0ffee00"], [_A]], collections=["knowledge", "knowledge"],
                        limit_per_source=[1, 1])
    assert get_many == [[_C, _A], [[_C], [_A]]]


def test_an_ambiguous_short_id_is_named_not_guessed(get_many) -> None:
    _shown_chunks.record([(_A, "knowledge__1-1__m__v1"), (_B, "knowledge__1-1__m__v1")])
    out = core.store_get_many(ids="ab12cd34")
    assert get_many == ["ab12cd34"]
    assert "id ab12cd34 matches 2 chunks" in out
    s = core.store_get_many(ids="ab12cd34", structured=True)
    assert s["missing"] == ["ab12cd34"] and "matches 2 chunks" in s["id_notes"][0]
    # more characters settle it
    core.store_get_many(ids="ab12cd340")
    assert get_many[-1] == [_A]


def test_a_short_id_never_shown_says_how_to_get_the_full_one(get_many) -> None:
    out = core.store_get_many(ids="deadbeef")
    assert "id deadbeef is not the start of a chunk id this session's searches showed" in out
    assert "structured=True" in out


def test_full_ids_and_non_hex_ids_pass_through_untouched(get_many) -> None:
    out = core.store_get_many(ids=f"{_A},some-title", structured=True)
    assert get_many == [f"{_A},some-title"]
    assert "id_notes" not in out


def test_the_record_is_bounded() -> None:
    def chash(i: int) -> str:
        return f"{i:08x}" + "e" * 56  # a distinct 8-character id each

    _shown_chunks.record((chash(i), "k") for i in range(_shown_chunks.CAPACITY + 10))
    _ids, unresolved, _h = _shown_chunks.expand(chash(0)[:8])
    assert "not the start of a chunk id" in unresolved[chash(0)[:8]], "the oldest fell out"
    newest = chash(_shown_chunks.CAPACITY + 9)
    assert _shown_chunks.expand(newest[:8])[0] == newest


def test_a_docs_hit_is_fetched_from_its_own_collection(search, get_many) -> None:
    docs = SearchResult(id=_C, content="d", distance=0.1, collection="docs__1-1__m__v1",
                        metadata={"chunk_text_hash": _C, "_display_title": "a doc"})
    out = search([docs])
    assert "] id=c0ffee00 a doc" in out
    colls.clear()
    core.store_get_many(ids="c0ffee00")
    assert get_many[-1] == [_C] and colls == [["docs__1-1__m__v1"]]


def test_an_explicit_scope_that_misses_names_where_the_id_was_shown(get_many) -> None:
    _shown_chunks.record([("d" * 64, "docs__1-1__m__v1")])
    out = core.store_get_many(ids="dddddddd", collections="code")
    assert 'id dddddddd was shown from docs__1-1__m__v1; pass collections="docs__1-1__m__v1"' in out


def test_only_text_search_asks_for_the_superseded_mark(search) -> None:
    # nexus-2k66t.6 (2A): text output prints the mark, so only it demotes; structured output keeps its ranking.
    search([_sr("c1", _A, 0.1)])
    search([_sr("c1", _A, 0.1)], structured=True)
    assert [c.get("mark_superseded") for c in search.calls] == [True, False]


def test_a_routed_id_that_is_still_missing_gets_no_looping_advice(get_many) -> None:
    _shown_chunks.record([("d" * 64, "docs__1-1__m__v1")])
    out = core.store_get_many(ids="dddddddd")  # routed to docs__1-1, and the fake does not know the chunk
    assert colls[-1] == ["docs__1-1__m__v1"] and "Missing:" in out and "pass collections" not in out


def test_text_and_structured_searches_never_share_a_cached_page(search, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core._sensitivity, "read_sensitivity", lambda *a, **k: "include")  # "exclude" skips the cache
    search([_sr("c1", _A, 0.1)])  # sets the rows and the stubs; invalidates the cache around itself
    search.calls.clear()
    core._page_cache_invalidate()
    try:
        core.search(query="q", corpus="knowledge", threshold=float("inf"))
        # The cache holds one page; a structured call keyed like the text one would be served the marked page.
        core.search(query="q", corpus="knowledge", threshold=float("inf"), structured=True)
    finally:
        core._page_cache_invalidate()
    assert [c.get("mark_superseded") for c in search.calls] == [True, False]
