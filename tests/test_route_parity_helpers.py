# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tu8wp.3: the hermetic half of the route-parity measurement.

The live cells in ``tests/test_search_fanout_recall_parity.py`` need the cloud
and the route; what makes their numbers mean anything does not. This pins it
against the fake transport of ``tests/test_search_per_collection_route.py``:
a route that is not served fails loudly (never passes by running the batched
path against itself), a reference that never touches ``/search`` is refused,
the verdict is noise-aware, and the per-leg query strings are distinct.
"""
from __future__ import annotations

import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.db.http_vector_client import HttpVectorClient
from nexus.search_engine import search_cross_corpus
from tests import _route_parity as rp
from tests.test_search_fanout_recall_parity import (
    _QUERIES,
    _QUERIES_NORERANK,
    _QUERIES_RR300,
    _QUERIES_RR1000,
)
from tests.test_search_per_collection_route import (
    _BGE,
    _MINI,
    _cols,
    _FakeEngine,
    _rows,
)


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)


# ── the per-leg query strings ────────────────────────────────────────────────


def test_each_leg_sends_its_own_query_strings():
    legs = {"norerank": _QUERIES_NORERANK, "rr1000": _QUERIES_RR1000, "rr300": _QUERIES_RR300}
    seen: dict[str, str] = {}
    for leg, queries in legs.items():
        assert len(queries) == len(_QUERIES)
        for (q, _corpus), (base, _c) in zip(queries, _QUERIES, strict=True):
            assert q != base, "a leg that sends the bare base query cannot be told from the floor cells"
            assert q.startswith(base) and rp.LEG_MARKERS[leg] in q
            assert q not in seen, f"{q!r} is sent by both {seen[q]} and {leg}"
            seen[q] = leg
    assert set(legs) == set(rp.LEG_MARKERS) == set(rp.LEG_RERANK_LIMIT)
    assert len(set(rp.LEG_MARKERS.values())) == len(rp.LEG_MARKERS)


def test_markers_can_be_dropped_and_corpus_specs_survive():
    assert rp.mark_queries(_QUERIES, None) == list(_QUERIES)
    assert [c for _q, c in rp.mark_queries(_QUERIES, "m")] == [c for _q, c in _QUERIES]


def test_the_sweep_legs_name_what_the_client_really_sends():
    """``rr1000`` is the SHIPPED rerank limit, not a patched one: if the client's
    sizing changes, the leg's name (and its marker) must change with it."""
    for n in (10, 30):
        assert se._per_collection_request_sizes(n, 4, rerank=True)[1] == 1000
        assert se._per_collection_request_sizes(n, 4, rerank=False)[1] == 300
    assert rp.LEG_RERANK_LIMIT == {"norerank": None, "rr1000": None, "rr300": 300}


def test_the_limit_patch_touches_the_rerank_limit_only():
    real = se._per_collection_request_sizes
    assert rp.sized_with_rerank_limit(real, None) is real
    sized = rp.sized_with_rerank_limit(real, 300)
    assert sized(10, 4, rerank=True) == (real(10, 4, rerank=True)[0], 300)
    assert sized(10, 4, rerank=False) == real(10, 4, rerank=False)


# ── the verdict ──────────────────────────────────────────────────────────────


def _row(route=1.0, noise=1.0, raw_route=None, raw_noise=None, query="q"):
    return rp.QueryRow(
        query=query, corpus="docs",
        page_route_ref1=route, page_route_ref2=route, page_noise=noise,
        raw_route_ref1=route if raw_route is None else raw_route,
        raw_route_ref2=route if raw_route is None else raw_route,
        raw_noise=noise if raw_noise is None else raw_noise,
        page_route_loader=None, page_ref_loader=None, route_pool=1, ref_pool=1,
        route_calls=1, route_shapes=[], ref_search_calls=1, loader_search_calls=None,
        start_utc="", end_utc="",
    )


def test_a_route_page_holds_at_the_floor_or_within_the_references_own_noise():
    assert rp.query_holds(0.9, 1.0)
    assert rp.query_holds(0.818, 0.818), "reference jitter (nexus-e9sux): the route cannot beat its reference"
    assert not rp.query_holds(0.818, 1.0), "a stable reference and a drifting route is a loss"
    assert not rp.query_holds(0.7, 0.818)


def test_rerank_off_is_asserted_and_rerank_on_is_reported():
    drifted = [_row(route=0.6, noise=1.0)] * 3
    assert rp.cell_failures(drifted, rerank=False, assert_rerank=False)
    assert rp.cell_failures(drifted, rerank=True, assert_rerank=False) == []
    assert rp.cell_failures(drifted, rerank=True, assert_rerank=True)


def test_the_raw_top_ten_is_asserted_beside_the_final_page_when_rerank_is_off():
    rows = [_row(route=1.0, noise=1.0, raw_route=0.5, raw_noise=1.0)] * 2
    problems = rp.cell_failures(rows, rerank=False, assert_rerank=False)
    assert any(p.startswith("raw page") for p in problems)


def test_a_cell_that_measured_nothing_fails():
    assert rp.cell_failures([], rerank=False, assert_rerank=False) == [
        "vacuous cell: no query was measured",
    ]


def test_the_mean_is_held_even_when_every_query_is_individually_inside_noise():
    # Every query holds on its own (0.9 against a noise of 1.0 clears the floor,
    # 0.0 against a noise of 0.0 matches its reference), yet the mean of 0.675
    # sits more than the margin under the reference's mean noise of 0.75.
    rows = [_row(route=0.9, noise=1.0)] * 3 + [_row(route=0.0, noise=0.0)]
    assert all(rp.query_holds(r.page_route, r.page_noise) for r in rows)
    problems = rp.cell_failures(rows, rerank=False, assert_rerank=False)
    assert problems and all(p.startswith("mean ") for p in problems)
    # the same shape with every route score equal to its noise passes
    rows = [_row(route=0.6, noise=0.6)] * 4
    assert rp.cell_failures(rows, rerank=False, assert_rerank=False) == []


# ── the measurement: a route that was not served must fail loudly ────────────

_N = 5


def _world(monkeypatch, *, route: bool):
    bge = _cols("code", _BGE, 3) + _cols("docs", _BGE, 2)
    mini = _cols("docs", _MINI, 3)
    cols = bge + mini
    engine = _FakeEngine(
        monkeypatch, {c: _rows(f"c{i}_", 4, 0.2 + 0.01 * i) for i, c in enumerate(cols)},
        route=route,
    )
    return engine, cols


def _page(results, *, rerank):
    """A stand-in for the live module's ``_user_page``, whose ranking boosts
    look collections up on a real service."""
    return rp.raw_top_ids(results, 10)


def _measure(engine, cols, *, route_client, ref_client, search=search_cross_corpus):
    return rp.measure_query(
        query="q", corpus="all", cols=cols, n=_N, threshold=float("inf"), rerank=False,
        route_client=route_client, ref_client=ref_client, search=search, user_page=_page,
    )


def test_a_served_route_is_compared_with_the_batched_path_and_matches_it(monkeypatch):
    engine, cols = _world(monkeypatch, route=True)
    ref = HttpVectorClient()
    ref.supports_per_collection_search = False
    row = _measure(engine, cols, route_client=engine.client, ref_client=ref)
    assert row.page_route == 1.0 and row.raw_route == 1.0 and row.page_noise == 1.0
    assert row.route_calls == 2, "one route request per model group"
    assert row.ref_search_calls >= 1
    assert all(k >= 1 and lim >= 1 for k, lim, _n in row.route_shapes)


def test_a_route_the_engine_does_not_serve_fails_loudly_instead_of_comparing_the_fallback(monkeypatch):
    engine, cols = _world(monkeypatch, route=False)  # an old engine: 404
    ref = HttpVectorClient()
    ref.supports_per_collection_search = False
    with pytest.raises(rp.RouteNotServedError, match="route NOT served"):
        _measure(engine, cols, route_client=engine.client, ref_client=ref)


def test_a_candidate_client_that_never_asks_for_the_route_fails_loudly(monkeypatch):
    engine, cols = _world(monkeypatch, route=True)
    ref = HttpVectorClient()
    ref.supports_per_collection_search = False
    candidate = HttpVectorClient()
    candidate.supports_per_collection_search = False  # the kill switch, or a fixture mix-up
    with pytest.raises(rp.RouteNotServedError, match="route NOT served"):
        _measure(engine, cols, route_client=candidate, ref_client=ref)


def test_a_candidate_that_fell_back_for_one_group_is_refused(monkeypatch):
    engine, cols = _world(monkeypatch, route=True)
    ref = HttpVectorClient()
    ref.supports_per_collection_search = False
    candidate = engine.client
    real = candidate.search_per_collection

    def _one_group_absent(query, names, **kw):
        return None if _MINI in names[0] else real(query, names, **kw)

    candidate.search_per_collection = _one_group_absent
    with pytest.raises(rp.RouteNotServedError, match="partly served"):
        _measure(engine, cols, route_client=candidate, ref_client=ref)


def test_a_reference_that_never_calls_search_is_refused(monkeypatch):
    engine, cols = _world(monkeypatch, route=True)
    ref = HttpVectorClient()
    ref.supports_per_collection_search = False
    calls = {"n": 0}

    def _search_without_ref_traffic(query, columns, n, client, **kw):
        if client is ref:
            calls["n"] += 1
            return []  # a stand-in that never touches /search
        return search_cross_corpus(query, columns, n, client, **kw)

    with pytest.raises(rp.RouteNotServedError, match="made no /search call"):
        _measure(engine, cols, route_client=engine.client, ref_client=ref,
                 search=_search_without_ref_traffic)
    assert calls["n"] == 1


def test_a_reference_that_reaches_the_route_is_not_a_batched_reference(monkeypatch):
    engine, cols = _world(monkeypatch, route=True)
    ref = engine.client  # supports_per_collection_search left True: the same path as the candidate
    other = HttpVectorClient()
    with pytest.raises(rp.RouteNotServedError):
        _measure(engine, cols, route_client=other, ref_client=ref)


def test_the_call_log_restores_the_client_when_it_exits(monkeypatch):
    engine, cols = _world(monkeypatch, route=True)
    client = engine.client
    with rp.CallLog(client) as log:
        search_cross_corpus("q", cols, _N, client, cluster_by=None,
                            threshold_override=float("inf"))
    assert log.route_served == 2 and log.search_calls == 0
    assert "search" not in vars(client) and "search_per_collection" not in vars(client)


def test_raw_top_ids_orders_by_distance_then_id():
    class R:
        def __init__(self, i, d):
            self.id, self.distance = i, d

    assert rp.raw_top_ids([R("b", 0.2), R("a", 0.2), R("c", 0.1)], 2) == ["c", "a"]
