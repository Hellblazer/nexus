# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search latency (nexus-92q1p follow-up, Sam 2026-10-08): the contradiction check runs on the
rows a search DISPLAYS, and fetches a vector only for the rows on that page that could flag.

RDR-057 Phase 3a designed the check over the RETURNED results ("~2 ms for N=10"). It had drifted
onto the up-to-300-row enrichment pool, and that drift put a vector on every pooled row of every
``search-per-collection`` response (233 rows, 1.7 MB, about 1.5 s of transfer and parse per call).
A first repair scoped the check inside ``search_cross_corpus`` by a proxy for "displayed" (the top
``n_results`` by raw distance); the proxy ran before the boosts, the file-diversity cap and the page
slice, so a displayed row could lose its flag and an undisplayed one could cost a fetch. The check
now lives where the page is known: ``mcp.core._search_render`` hands the sliced page to
``flag_displayed_contradictions``.

``_flag_contradictions`` flags a pair only when both rows share a collection and carry different
non-empty ``source_agent`` values with cosine distance under 0.3, so a row is a *candidate* only when
its collection holds, on the same page, a row of a different agent.

Two halves:

* ``TestSearchCrossCorpusMakesNoVectorCallForTheCheck`` runs the real ``HttpVectorClient`` and
  ``search_cross_corpus`` against the fake transport of ``tests/test_search_per_collection_embeddings.py``
  (the batched ``/search`` leg is the route turned off), and pins what that function no longer does.
* ``TestTheRenderedPage`` drives the actual ``_search_render`` / ``query`` code with a spy on
  ``get_embeddings``.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from click.testing import CliRunner

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.cli import main
from nexus.mcp import core as mcp_core
from nexus.mcp_infra import inject_t3
from nexus.search_engine import (
    _contradiction_candidates,
    flag_displayed_contradictions,
    search_cross_corpus,
)
from nexus.types import SearchResult

from tests.conftest import patched_mcp_infra_t3
from tests.test_search_cmd import _LOAD_CFG, _make_result, _mock_t3  # type: ignore[import-not-found]
from tests.test_search_per_collection_embeddings import (  # type: ignore[import-not-found]
    _EmbeddingEngine,
)
from tests.test_search_per_collection_route import _BGE, _cols  # type: ignore[import-not-found]

_FLAG = "[CONTRADICTS ANOTHER RESULT]"
_CHECK_ON = {"search": {"contradiction_check": True}}
_CHECK_OFF = {"search": {"contradiction_check": False}}


# ── client half: search_cross_corpus no longer flags or fetches for the check ──────────────────


@pytest.fixture
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)


class _AgentEngine(_EmbeddingEngine):
    """Rows carry ``source_agent`` from ``agents[(collection, id)]`` or ``default_agent``;
    an empty agent leaves the key off the row, as a chunk without provenance has none."""

    def __init__(self, monkeypatch, data, *, default_agent: str = "nexus-indexer", **kw) -> None:
        super().__init__(monkeypatch, data, **kw)
        self.default_agent = default_agent
        self.agents: dict[tuple[str, str], str] = {}

    def _row(self, col: str, rid: str, dist: float) -> dict:  # type: ignore[override]
        row = {"id": rid, "content": f"text {rid}", "distance": dist, "collection": col}
        agent = self.agents.get((col, rid), self.default_agent)
        if agent:
            row["source_agent"] = agent
        return row


def _rows() -> list[tuple[str, float]]:
    # a0..a3 sit close together in vector space, b0/b1 far from them (see _vector).
    return [("a0", 0.10), ("a1", 0.11), ("a2", 0.12), ("a3", 0.13), ("b0", 0.30), ("b1", 0.31)]


def _run(engine, cols, n: int = 20, **kw):
    kw.setdefault("threshold_override", float("inf"))
    kw.setdefault("cluster_by", None)
    return search_cross_corpus("q", cols, n, engine.client, **kw)


def _mixed_engine(monkeypatch, route: bool, *, check: bool = True):
    mixed, same = _cols("knowledge", _BGE, 2)
    engine = _AgentEngine(monkeypatch, {mixed: _rows(), same: _rows()}, default_agent="agent-x", route=route)
    monkeypatch.setattr(
        "nexus.search_engine.load_config", lambda: _CHECK_ON if check else _CHECK_OFF,
    )
    engine.agents[(mixed, "a0")] = "agent-x"
    engine.agents[(mixed, "a1")] = "agent-y"   # a0/a1: a displayable contradicting pair
    return engine, mixed, same


@pytest.mark.usefixtures("_fresh_process_state")
@pytest.mark.parametrize("route", [True, False], ids=["per-collection-route", "batched-search"])
class TestSearchCrossCorpusMakesNoVectorCallForTheCheck:
    def test_no_vector_is_requested_or_fetched_and_no_row_is_flagged(self, monkeypatch, route):
        engine, mixed, same = _mixed_engine(monkeypatch, route)
        results = _run(engine, [mixed, same])

        assert len(results) == 12
        assert engine.get_embedding_calls() == [], "a mixed-agent knowledge pair costs search_cross_corpus nothing"
        assert all("include_embeddings" not in b for b in engine.route_calls())
        assert not any(r.metadata.get("_contradiction_flag") for r in results)

    def test_the_rows_still_carry_source_agent_for_the_render_to_read(self, monkeypatch, route):
        engine, mixed, same = _mixed_engine(monkeypatch, route)
        results = _run(engine, [mixed, same])
        agents = {(r.collection, r.id): r.metadata.get("source_agent") for r in results}
        assert agents[(mixed, "a0")] == "agent-x"
        assert agents[(mixed, "a1")] == "agent-y"

    def test_the_result_set_does_not_depend_on_the_check_setting(self, monkeypatch, route):
        def _ids(check: bool) -> list[tuple[str, str]]:
            engine, mixed, same = _mixed_engine(monkeypatch, route, check=check)
            return [(r.collection, r.id) for r in _run(engine, [mixed, same], n=4)]

        on, off = _ids(True), _ids(False)
        assert on == off
        assert len(on) == 12, "all 12 pooled rows come back, as before the check was moved"
        # Strongest available ordering claim: rows come back in non-decreasing distance within a collection.
        by_col: dict[str, list[str]] = {}
        for col, rid in on:
            by_col.setdefault(col, []).append(rid)
        assert all(ids == [r for r, _ in _rows()] for ids in by_col.values())

    def test_indexed_collections_request_and_fetch_no_vectors(self, monkeypatch, route):
        cols = _cols("code", _BGE, 2) + _cols("docs", _BGE, 1)
        engine = _AgentEngine(monkeypatch, {c: _rows() for c in cols}, route=route)
        monkeypatch.setattr("nexus.search_engine.load_config", lambda: _CHECK_ON)
        results = _run(engine, cols)

        assert len(results) == 18
        assert all("include_embeddings" not in b for b in engine.route_calls())
        assert engine.get_embedding_calls() == []


@pytest.mark.usefixtures("_fresh_process_state")
class TestSemanticClusteringStillGetsItsVectors:
    def test_the_route_is_asked_for_vectors_exactly_as_before_and_no_get_embeddings_call_is_made(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _AgentEngine(monkeypatch, {c: _rows() for c in cols}, route=True)
        results = _run(engine, cols, cluster_by="semantic")

        assert engine.route_calls()
        assert all(b.get("include_embeddings") is True for b in engine.route_calls())
        assert all(b.get("embeddings_limit") == 300 for b in engine.route_calls())
        assert engine.get_embedding_calls() == []
        assert any(r.metadata.get("_cluster_label") for r in results), "Ward ran on the vectors"

    def test_a_semantic_search_does_not_flag_either(self, monkeypatch):
        engine, mixed, same = _mixed_engine(monkeypatch, True)
        results = _run(engine, [mixed, same], cluster_by="semantic")
        assert engine.get_embedding_calls() == []
        assert not any(r.metadata.get("_contradiction_flag") for r in results)


# ── the helper in isolation ─────────────────────────────────────────────────────────────────────


def _r(i: str, col: str, agent: str = "", distance: float = 0.5, **meta) -> SearchResult:
    m = dict(meta)
    if agent:
        m["source_agent"] = agent
    return SearchResult(id=i, content="", distance=distance, collection=col, metadata=m)


class _Vectors:
    """``get_embeddings`` spy. ``vec`` maps row id to its vector; ``fail`` names collections whose
    fetch raises; ``short`` names collections that answer with too few rows."""

    def __init__(self, vec: dict[str, list[float]]) -> None:
        self.vec = vec
        self.fail: set[str] = set()
        self.short: set[str] = set()
        self.calls: list[tuple[str, list[str]]] = []

    def get_embeddings(self, collection: str, ids: list[str]) -> np.ndarray:
        self.calls.append((collection, list(ids)))
        if collection in self.fail:
            raise RuntimeError("simulated collection fault")
        rows = [self.vec[i] for i in ids]
        if collection in self.short:
            rows = rows[:-1]
        return np.array(rows, dtype=np.float32).reshape(len(rows), -1)


_NEAR = [1.0, 0.0, 0.0, 0.0]
_NEAR2 = [0.99, 0.01, 0.0, 0.0]
_FAR = [0.0, 0.0, 1.0, 0.0]


class TestCandidates:
    def test_a_row_needs_an_agent_and_a_different_agent_in_its_collection(self):
        rows = [
            _r("a", "k1", "x"), _r("b", "k1", "x"),       # one agent
            _r("c", "k2", "x"), _r("d", "k2", "y"),       # two agents
            _r("e", "k2", ""),                            # no agent
            _r("f", "k3", "x"), _r("g", "k4", "y"),       # different collections
        ]
        assert _contradiction_candidates(rows) == [2, 3]

    def test_a_collection_over_the_pairwise_cap_is_skipped(self):
        cap = se._CONTRADICTION_MAX_PER_COLLECTION
        rows = [_r(f"r{i}", "k", "x" if i % 2 else "y") for i in range(cap + 1)]
        assert _contradiction_candidates(rows) == [], "over the cap: neither fetched for nor checked"
        assert len(_contradiction_candidates(rows[:cap])) == cap

    def test_no_candidates_means_no_fetch_and_the_same_rows_back(self):
        spy = _Vectors({})
        rows = [_r("a", "k", "x"), _r("b", "k", "x"), _r("c", "c", "")]
        out = flag_displayed_contradictions(rows, spy)
        assert spy.calls == []
        assert [r.id for r in out] == ["a", "b", "c"]
        assert out is not rows


class TestFlaggingAPage:
    def test_the_pair_is_flagged_and_the_input_rows_are_not_mutated(self):
        spy = _Vectors({"a": _NEAR, "b": _NEAR2, "c": _FAR})
        rows = [_r("a", "k", "x"), _r("b", "k", "y"), _r("c", "k", "z")]
        out = flag_displayed_contradictions(rows, spy)

        assert [bool(r.metadata.get("_contradiction_flag")) for r in out] == [True, True, False]
        assert not any(r.metadata.get("_contradiction_flag") for r in rows), "cached rows stay clean"
        assert spy.calls == [("k", ["a", "b", "c"])]

    def test_partial_failure_leaves_the_other_collections_flags_intact(self):
        spy = _Vectors({"a": _NEAR, "b": _NEAR2, "c": _NEAR, "d": _NEAR2})
        spy.fail = {"broken"}
        rows = [_r("a", "good", "x"), _r("b", "good", "y"), _r("c", "broken", "x"), _r("d", "broken", "y")]
        out = flag_displayed_contradictions(rows, spy)

        assert {(r.collection, r.id) for r in out if r.metadata.get("_contradiction_flag")} == {
            ("good", "a"), ("good", "b"),
        }
        assert {c for c, _ in spy.calls} == {"good", "broken"}

    def test_a_collection_that_answers_the_wrong_shape_is_left_unflagged_the_rest_are_kept(self):
        spy = _Vectors({"a": _NEAR, "b": _NEAR2, "c": _NEAR, "d": _NEAR2})
        spy.short = {"odd"}
        rows = [_r("a", "good", "x"), _r("b", "good", "y"), _r("c", "odd", "x"), _r("d", "odd", "y")]
        out = flag_displayed_contradictions(rows, spy)
        assert {r.id for r in out if r.metadata.get("_contradiction_flag")} == {"a", "b"}

    def test_total_failure_leaves_the_page_unflagged_and_does_not_raise(self):
        spy = _Vectors({"a": _NEAR, "b": _NEAR2})
        spy.fail = {"k"}
        rows = [_r("a", "k", "x"), _r("b", "k", "y")]
        out = flag_displayed_contradictions(rows, spy)
        assert [r.id for r in out] == ["a", "b"]
        assert not any(r.metadata.get("_contradiction_flag") for r in out)

    def test_an_unexpected_failure_in_the_check_leaves_the_page_unflagged(self, monkeypatch):
        def boom(*a, **kw):
            raise ValueError("bad matrix")

        monkeypatch.setattr(se, "_flag_contradictions", boom)
        spy = _Vectors({"a": _NEAR, "b": _NEAR2})
        out = flag_displayed_contradictions([_r("a", "k", "x"), _r("b", "k", "y")], spy)
        assert not any(r.metadata.get("_contradiction_flag") for r in out)


# ── render half: the actual MCP search / query code ─────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _render_state():
    mcp_core._reset_page_cache_for_tests()
    yield
    mcp_core._reset_page_cache_for_tests()
    inject_t3(None)


def _no_taxonomy():
    return None


def _spy_t3(vec: dict[str, list[float]], names: list[str]) -> tuple[_Vectors, MagicMock]:
    spy = _Vectors(vec)
    mock = MagicMock()
    mock.list_collections.return_value = [
        {
            "name": n, "count": 1, "content_type": n.partition("__")[0],
            "owner_id": n.partition("__")[2], "embedding_model": "test-model",
            "lifecycle_state": "live",
        }
        for n in names
    ]
    mock.get_embeddings.side_effect = spy.get_embeddings
    inject_t3(mock)
    return spy, mock


def _render(rows: list[SearchResult], *, names: list[str], cfg=_CHECK_ON, fn="search", **kw):
    """Run the real render over *rows* as the pooled result of ``search_cross_corpus``.

    The rows are copied per call, so a test that renders twice gets a fresh pool each time."""
    def _pool(*a, **k):
        return [
            SearchResult(id=r.id, content=r.content, distance=r.distance, collection=r.collection,
                         metadata=dict(r.metadata), hybrid_score=r.hybrid_score)
            for r in rows
        ]

    with patch("nexus.search_engine.search_cross_corpus", _pool), \
         patch("nexus.config.load_config", return_value=cfg), \
         patch("nexus.mcp.core._search_taxonomy", _no_taxonomy), \
         patch("nexus.mcp.core._get_catalog", return_value=None):
        if fn == "query":
            return mcp_core.query(question="anything", corpus=",".join(names), **kw)
        return mcp_core._search_render(query="anything", corpus=",".join(names), **kw)


def _labels(out: str) -> dict[str, bool]:
    """``{row id: flagged}`` for each rendered row (the title is the row id in these fixtures)."""
    found = re.findall(r"^\[d=[^\]]*\] (\S+)( \[CONTRADICTS ANOTHER RESULT\])?$", out, flags=re.M)
    return {label: bool(flag) for label, flag in found}


def _n(i: str, col: str, agent: str, distance: float, **meta) -> SearchResult:
    return SearchResult(
        id=i, content=f"text {i}", distance=distance, collection=col,
        metadata={"title": i, "source_agent": agent, **meta} if agent else {"title": i, **meta},
    )


_NOTES = "knowledge__notes"
_OTHER = "knowledge__other"
_CODE = "code__repo"

_VEC = {
    "n0": _NEAR, "n1": _NEAR2, "n2": _FAR, "n3": _NEAR, "n4": _NEAR2,
    "o0": _NEAR, "o1": _NEAR2, "c0": _NEAR, "c1": _NEAR2,
}


class TestTheRenderedPage:
    def test_a_mixed_agent_pair_on_the_page_is_flagged_and_exactly_the_candidates_are_fetched(self):
        spy, _ = _spy_t3(_VEC, [_NOTES])
        rows = [
            _n("n0", _NOTES, "agent-x", 0.10),
            _n("n1", _NOTES, "agent-y", 0.11),   # close to n0: the pair
            _n("n2", _NOTES, "agent-x", 0.12, ),  # far from both: a candidate, not flagged
            _n("n3", _NOTES, "", 0.13),           # no provenance: cannot flag, so no vector
        ]
        out = _render(rows, names=[_NOTES])

        assert _labels(out) == {"n0": True, "n1": True, "n2": False, "n3": False}
        assert spy.calls == [(_NOTES, ["n0", "n1", "n2"])]

    def test_a_partner_that_is_not_on_the_rendered_page_is_neither_fetched_nor_flagged(self):
        spy, _ = _spy_t3(_VEC, [_NOTES])
        rows = [_n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11)]
        out = _render(rows, names=[_NOTES], limit=1)

        assert _labels(out) == {"n0": False}
        assert spy.calls == [], "one row on the page cannot contradict another row"

    def test_each_page_turn_computes_its_own_flag_and_the_cache_holds_none(self):
        spy, _ = _spy_t3(_VEC, [_NOTES])
        rows = [_n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11)]

        both = _render(rows, names=[_NOTES], limit=2)
        assert _labels(both) == {"n0": True, "n1": True}
        assert len(spy.calls) == 1

        # The same retrieval identity, served from the page cache: the pair is split across pages.
        first = _render(rows, names=[_NOTES], limit=1, offset=0)
        second = _render(rows, names=[_NOTES], limit=1, offset=1)
        assert _labels(first) == {"n0": False}
        assert _labels(second) == {"n1": False}
        assert len(spy.calls) == 1, "no stale flag was stored on the cached rows, no new fetch was made"

    def test_a_row_lifted_onto_the_page_by_the_file_diversity_cap_is_considered(self):
        """Pool order is a0 a1 a2 b0 (a2 is the third chunk of file A). The render's file-diversity
        cap sends a2 behind b0, so the page of three is a0 a1 b0: b0 was lifted onto it. b0 pairs
        with a0, and a2, which is in the pool but off the page, is not fetched."""
        spy, _ = _spy_t3({"a0": _NEAR, "a1": _NEAR2, "a2": _NEAR, "b0": _NEAR2}, [_NOTES])
        rows = [
            _n("a0", _NOTES, "agent-x", 0.10, source_path="A.md"),
            _n("a1", _NOTES, "agent-x", 0.11, source_path="A.md"),
            _n("a2", _NOTES, "agent-x", 0.12, source_path="A.md"),
            _n("b0", _NOTES, "agent-y", 0.13, source_path="B.md"),
        ]
        out = _render(rows, names=[_NOTES], limit=3)

        assert list(_labels(out)) == ["a0", "a1", "b0"], "the cap lifted b0 over a2"
        assert _labels(out) == {"a0": True, "a1": True, "b0": True}
        assert spy.calls == [(_NOTES, ["a0", "a1", "b0"])]

    def test_a_row_lifted_onto_the_page_by_the_hybrid_score_is_considered(self):
        """code__ rows carry frecency; with ``search.hybrid_default`` on, apply_ranking_boosts
        lifts the high-frecency c2 over c1. The page of two is c0 c2, so c2 pairs with c0 while
        c1, which sat second in the pool, is off the page and is not fetched."""
        spy, _ = _spy_t3({"c0": _NEAR, "c1": _NEAR2, "c2": _NEAR2}, [_CODE])
        rows = [
            _n("c0", _CODE, "agent-x", 0.10, frecency_score=0.0),
            _n("c1", _CODE, "agent-y", 0.55, frecency_score=0.0),
            _n("c2", _CODE, "agent-y", 0.60, frecency_score=1.0),
        ]
        cfg = {"search": {"contradiction_check": True, "hybrid_default": True}}
        out = _render(rows, names=[_CODE], cfg=cfg, limit=2)

        assert list(_labels(out)) == ["c0", "c2"], "the boost lifted c2 over c1"
        assert _labels(out) == {"c0": True, "c2": True}
        assert spy.calls == [(_CODE, ["c0", "c2"])]

    def test_a_page_from_indexed_collections_makes_zero_vector_calls(self):
        spy, mock = _spy_t3(_VEC, [_CODE, "docs__d"])
        rows = [
            _n("c0", _CODE, "nexus-indexer", 0.10),
            _n("c1", _CODE, "nexus-indexer", 0.11),
            _n("d0", "docs__d", "nexus-indexer", 0.12),
        ]
        out = _render(rows, names=[_CODE, "docs__d"])

        assert _FLAG not in out
        assert spy.calls == []
        mock.get_embeddings.assert_not_called()

    def test_one_fetch_per_collection_that_holds_a_mixed_pair_and_none_for_the_rest(self):
        spy, _ = _spy_t3(_VEC, [_NOTES, _OTHER, _CODE])
        rows = [
            _n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11),
            _n("o0", _OTHER, "agent-x", 0.12), _n("o1", _OTHER, "agent-x", 0.13),   # one agent only
            _n("c0", _CODE, "nexus-indexer", 0.14),
        ]
        out = _render(rows, names=[_NOTES, _OTHER, _CODE])

        assert _labels(out) == {"n0": True, "n1": True, "o0": False, "o1": False, "c0": False}
        assert spy.calls == [(_NOTES, ["n0", "n1"])]

    def test_a_partial_get_embeddings_failure_keeps_the_other_collections_flag(self):
        spy, _ = _spy_t3(_VEC, [_NOTES, _OTHER])
        spy.fail = {_OTHER}
        rows = [
            _n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11),
            _n("o0", _OTHER, "agent-x", 0.12), _n("o1", _OTHER, "agent-y", 0.13),
        ]
        out = _render(rows, names=[_NOTES, _OTHER])

        assert not out.startswith("Error"), "a failed flag fetch never fails the search"
        assert _labels(out) == {"n0": True, "n1": True, "o0": False, "o1": False}

    def test_a_total_get_embeddings_failure_returns_the_page_unflagged(self):
        spy, _ = _spy_t3(_VEC, [_NOTES, _OTHER])
        spy.fail = {_NOTES, _OTHER}
        rows = [
            _n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11),
            _n("o0", _OTHER, "agent-x", 0.12), _n("o1", _OTHER, "agent-y", 0.13),
        ]
        out = _render(rows, names=[_NOTES, _OTHER])

        assert not out.startswith("Error")
        assert _labels(out) == {"n0": False, "n1": False, "o0": False, "o1": False}

    def test_the_check_turned_off_fetches_nothing_and_flags_nothing(self):
        spy, _ = _spy_t3(_VEC, [_NOTES])
        rows = [_n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11)]
        out = _render(rows, names=[_NOTES], cfg=_CHECK_OFF)
        assert spy.calls == []
        assert _FLAG not in out

    def test_the_check_is_off_by_default_and_a_flaggable_page_fetches_nothing(self):
        # Sam 2026-10-08: opt-in. The shipped default and a config with no
        # search.contradiction_check key both leave the page unflagged.
        from nexus.config import _DEFAULTS

        assert _DEFAULTS["search"]["contradiction_check"] is False
        spy, _ = _spy_t3(_VEC, [_NOTES])
        rows = [_n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11)]
        out = _render(rows, names=[_NOTES], cfg={})
        assert spy.calls == []
        assert _FLAG not in out

    def test_structured_output_carries_no_flag_and_fetches_nothing(self):
        spy, _ = _spy_t3(_VEC, [_NOTES])
        rows = [_n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11)]
        data = _render(rows, names=[_NOTES], structured=True)
        assert isinstance(data, dict) and data["ids"] == ["n0", "n1"]
        assert spy.calls == []

    def test_query_makes_zero_vector_calls_for_the_check(self):
        spy, _ = _spy_t3(_VEC, [_NOTES])
        rows = [_n("n0", _NOTES, "agent-x", 0.10), _n("n1", _NOTES, "agent-y", 0.11)]
        out = _render(rows, names=[_NOTES], fn="query")
        assert isinstance(out, str) and not out.startswith("Error")
        assert spy.calls == []
        assert _FLAG not in out


def test_the_nx_search_cli_makes_zero_vector_calls_for_the_check(monkeypatch):
    """``nx search`` never rendered the flag; it must not pay for it either."""
    for k, v in {"CHROMA_API_KEY": "k", "VOYAGE_API_KEY": "v", "CHROMA_TENANT": "t", "CHROMA_DATABASE": "d"}.items():
        monkeypatch.setenv(k, v)
    mock_t3 = _mock_t3(["knowledge__test"])
    rows = [
        _make_result("r0", "alpha", metadata={"source_agent": "agent-x"}, distance=0.1),
        _make_result("r1", "alpha", metadata={"source_agent": "agent-y"}, distance=0.11),
    ]
    with patch("nexus.commands.search_cmd._t3", return_value=mock_t3), patched_mcp_infra_t3(mock_t3), \
         patch("nexus.commands.search_cmd.search_cross_corpus", return_value=rows), \
         patch("nexus.commands.search_cmd.load_config", return_value=_LOAD_CFG):
        result = CliRunner().invoke(main, ["search", "alpha", "--corpus", "knowledge", "--no-rerank"])

    assert result.exit_code == 0, result.output
    mock_t3.get_embeddings.assert_not_called()
    assert _FLAG not in result.output
