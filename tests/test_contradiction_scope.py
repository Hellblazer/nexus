# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Search latency (nexus-92q1p follow-up, Sam 2026-10-08): the contradiction check looks at the
rows a caller can display, and fetches a vector only for the rows that could flag.

RDR-057 Phase 3a designed the check over the RETURNED results. It had drifted onto the up-to-300-row
enrichment pool, and that drift is what put a vector on every pooled row of every
``search-per-collection`` response (233 rows, 1.7 MB, about 1.5 s of transfer and parse per call).

``_flag_contradictions`` flags a pair only when both rows share a collection and carry different
non-empty ``source_agent`` values with cosine distance under 0.3, so a row is a *candidate* only when
its collection holds, inside the scope, a row of a different agent. The indexer stamps every
code/docs/rdr chunk with one agent, so those collections have none.

The real ``HttpVectorClient`` and ``search_cross_corpus`` run against the fake transport of
``tests/test_search_per_collection_embeddings.py``; the batched ``/search`` leg is run with the
route off, because the check reads ``source_agent`` from the row metadata on both.
"""
from __future__ import annotations

import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.search_engine import (
    _contradiction_candidates,
    search_cross_corpus,
)
from nexus.types import SearchResult

from tests.test_search_per_collection_embeddings import (  # type: ignore[import-not-found]
    _EmbeddingEngine,
)
from tests.test_search_per_collection_route import _BGE, _cols  # type: ignore[import-not-found]


@pytest.fixture(autouse=True)
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


def _flags(results) -> dict[tuple[str, str], bool]:
    return {(r.collection, r.id): bool(r.metadata.get("_contradiction_flag")) for r in results}


def _fetched(engine) -> dict[str, list[str]]:
    return {b["collection"]: sorted(b["ids"]) for b in engine.get_embedding_calls()}


@pytest.mark.parametrize("route", [True, False], ids=["per-collection-route", "batched-search"])
class TestWhichRowsGetAVector:
    def test_indexed_collections_request_and_fetch_no_vectors(self, monkeypatch, route):
        cols = _cols("code", _BGE, 2) + _cols("docs", _BGE, 1)
        engine = _AgentEngine(monkeypatch, {c: _rows() for c in cols}, route=route)
        results = _run(engine, cols)

        assert len(results) == 18
        assert all("include_embeddings" not in b for b in engine.route_calls())
        assert engine.get_embedding_calls() == []
        assert not any(_flags(results).values())

    def test_a_mixed_agent_knowledge_collection_fetches_exactly_the_candidates(self, monkeypatch, route):
        mixed, same = _cols("knowledge", _BGE, 2)
        engine = _AgentEngine(monkeypatch, {mixed: _rows(), same: _rows()}, route=route)
        engine.agents[(mixed, "a0")] = "agent-x"
        engine.agents[(mixed, "a1")] = "agent-y"
        engine.agents[(mixed, "a2")] = "agent-x"
        engine.agents[(mixed, "a3")] = ""        # no provenance: cannot flag, so no vector
        engine.agents[(mixed, "b0")] = ""
        engine.agents[(mixed, "b1")] = ""
        for rid, _ in _rows():
            engine.agents[(same, rid)] = "agent-x"
        results = _run(engine, [mixed, same])

        assert len(results) == 12
        assert _fetched(engine) == {mixed: ["a0", "a1", "a2"]}, "only the rows that carry an agent, only where agents differ"
        flagged = {k for k, v in _flags(results).items() if v}
        assert flagged == {(mixed, "a0"), (mixed, "a1"), (mixed, "a2")}
        assert all("include_embeddings" not in b for b in engine.route_calls())

    def test_a_same_agent_knowledge_collection_fetches_none(self, monkeypatch, route):
        cols = _cols("knowledge", _BGE, 2)
        engine = _AgentEngine(monkeypatch, {c: _rows() for c in cols}, default_agent="agent-x", route=route)
        results = _run(engine, cols)

        assert len(results) == 12
        assert engine.get_embedding_calls() == []
        assert all("include_embeddings" not in b for b in engine.route_calls())
        assert not any(_flags(results).values())

    def test_a_displayed_contradicting_pair_is_flagged_as_before(self, monkeypatch, route):
        (col,) = _cols("knowledge", _BGE, 1)
        engine = _AgentEngine(monkeypatch, {col: _rows()}, default_agent="", route=route)
        engine.agents[(col, "a0")] = "agent-x"
        engine.agents[(col, "a1")] = "agent-y"
        engine.agents[(col, "b0")] = "agent-z"    # far from both: a candidate, not flagged
        results = _run(engine, [col])

        assert _flags(results) == {
            (col, "a0"): True, (col, "a1"): True, (col, "a2"): False,
            (col, "a3"): False, (col, "b0"): False, (col, "b1"): False,
        }
        assert _fetched(engine) == {col: ["a0", "a1", "b0"]}


class TestTheScope:
    def test_a_pair_whose_partner_is_not_displayed_is_neither_fetched_nor_flagged(self, monkeypatch):
        (col,) = _cols("knowledge", _BGE, 1)
        engine = _AgentEngine(monkeypatch, {col: _rows()}, default_agent="", route=True)
        engine.agents[(col, "a0")] = "agent-x"
        engine.agents[(col, "a3")] = "agent-y"    # rank 4 of 6; n_results = 3 leaves it out
        results = _run(engine, [col], n=3)

        assert engine.get_embedding_calls() == []
        assert not any(_flags(results).values())

        # The same data with the partner inside the scope is flagged.
        wider = _AgentEngine(monkeypatch, {col: _rows()}, default_agent="", route=True)
        wider.agents[(col, "a0")] = "agent-x"
        wider.agents[(col, "a3")] = "agent-y"
        flagged = _run(wider, [col], n=4)
        assert _flags(flagged)[(col, "a0")] and _flags(flagged)[(col, "a3")]

    def test_result_count_and_order_do_not_depend_on_the_check(self, monkeypatch):
        cols = _cols("knowledge", _BGE, 2)

        def _order(check: bool) -> list[tuple[str, str]]:
            engine = _AgentEngine(monkeypatch, {c: _rows() for c in cols}, default_agent="", route=True)
            monkeypatch.setattr(
                "nexus.search_engine.load_config",
                lambda: {"search": {"contradiction_check": check}},
            )
            engine.agents[(cols[0], "a0")] = "agent-x"
            engine.agents[(cols[0], "a1")] = "agent-y"
            return [(r.collection, r.id) for r in _run(engine, cols, n=4)]

        on, off = _order(True), _order(False)
        assert on == off
        assert len(on) == 12, "scoping the check drops no row"

    def test_the_check_turned_off_fetches_nothing(self, monkeypatch):
        (col,) = _cols("knowledge", _BGE, 1)
        engine = _AgentEngine(monkeypatch, {col: _rows()}, default_agent="", route=True)
        monkeypatch.setattr(
            "nexus.search_engine.load_config",
            lambda: {"search": {"contradiction_check": False}},
        )
        engine.agents[(col, "a0")] = "agent-x"
        engine.agents[(col, "a1")] = "agent-y"
        results = _run(engine, [col])
        assert engine.get_embedding_calls() == []
        assert not any(_flags(results).values())


class TestSemanticClusteringStillGetsItsVectors:
    def test_the_route_is_asked_for_vectors_and_no_get_embeddings_call_is_made(self, monkeypatch):
        cols = _cols("code", _BGE, 2)
        engine = _AgentEngine(monkeypatch, {c: _rows() for c in cols}, route=True)
        results = _run(engine, cols, cluster_by="semantic")

        assert engine.route_calls()
        assert all(b.get("include_embeddings") is True for b in engine.route_calls())
        assert all(b.get("embeddings_limit") == 300 for b in engine.route_calls())
        assert engine.get_embedding_calls() == []
        assert any(r.metadata.get("_cluster_label") for r in results), "Ward ran on the vectors"

    def test_the_check_inside_a_semantic_search_reads_the_same_vectors(self, monkeypatch):
        (col,) = _cols("knowledge", _BGE, 1)
        engine = _AgentEngine(monkeypatch, {col: _rows()}, default_agent="", route=True)
        engine.agents[(col, "a0")] = "agent-x"
        engine.agents[(col, "a1")] = "agent-y"
        results = _run(engine, [col], cluster_by="semantic")

        assert engine.get_embedding_calls() == [], "the matrix already fetched is sliced, not fetched again"
        assert _flags(results)[(col, "a0")] and _flags(results)[(col, "a1")]


def _r(i: str, col: str, agent: str = "", distance: float = 0.5, **meta) -> SearchResult:
    m = dict(meta)
    if agent:
        m["source_agent"] = agent
    return SearchResult(id=i, content="", distance=distance, collection=col, metadata=m)


class TestCandidates:
    def test_the_scope_is_the_best_rows_by_rerank_score_then_distance(self):
        rows = [
            _r("far", "k", "x", distance=0.9),
            _r("near", "k", "y", distance=0.1),
            _r("scored", "k", "x", distance=0.8, rerank_score=0.99),
        ]
        # scope 2 keeps the reranked row, then the nearest unscored one; "far" is out.
        assert _contradiction_candidates(rows, 2) == [1, 2]
        assert _contradiction_candidates(rows, 1) == [], "one row cannot contradict itself"

    def test_a_row_needs_an_agent_and_a_different_agent_in_its_collection(self):
        rows = [
            _r("a", "k1", "x"), _r("b", "k1", "x"),       # one agent
            _r("c", "k2", "x"), _r("d", "k2", "y"),       # two agents
            _r("e", "k2", ""),                            # no agent
            _r("f", "k3", "x"), _r("g", "k4", "y"),       # different collections
        ]
        assert _contradiction_candidates(rows, 10) == [2, 3]

    def test_a_collection_over_the_pairwise_cap_is_skipped(self):
        cap = se._CONTRADICTION_MAX_PER_COLLECTION
        rows = [_r(f"r{i}", "k", "x" if i % 2 else "y") for i in range(cap + 1)]
        assert _contradiction_candidates(rows, cap + 1) == []
        assert len(_contradiction_candidates(rows, cap)) == cap
