# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tu8wp.3: the hermetic half of ``scripts/measure_search_per_collection.py``.

The driver measures the live cloud; what makes its numbers mean anything does
not need the cloud. Driven here through the fake transport of
``tests/test_search_per_collection_route.py``: a route run that never reached
the route is refused (exit 3 at the command line, never timed as the route),
the printed request estimate matches what a search really sends, load is
bounded by default (K in 1, 2, 3; 5 needs a flag; a pause between runs), and
the per-leg query strings are distinct.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

import nexus.db.http_vector_client as hvc
import nexus.search_engine as se
from nexus.db.http_vector_client import HttpVectorClient
from nexus.search_engine import SearchDiagnostics, search_cross_corpus
from tests import _route_parity as rp
from tests.test_search_fanout_recall_parity import _QUERIES_NORERANK, _QUERIES_RR300, _QUERIES_RR1000
from tests.test_search_per_collection_route import _BGE, _MINI, _cols, _FakeEngine, _rows

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_search_per_collection.py"
_spec = importlib.util.spec_from_file_location("measure_search_per_collection", _SCRIPT)
drv = importlib.util.module_from_spec(_spec)
# registered before exec: the dataclasses in the script resolve their module by name
import sys  # noqa: E402
from tests._module_seam import module_time

sys.modules[_spec.name] = drv
_spec.loader.exec_module(drv)


@pytest.fixture(autouse=True)
def _fresh_process_state(monkeypatch):
    monkeypatch.setattr(se, "_mixed_model_groups", {})
    monkeypatch.setattr(hvc, "_per_collection_absent_warned", False)
    # the route is opt-in (nexus-tu8wp.3) and the script must opt in itself for its route runs, so the
    # tests start from the default: unset
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)


def _world(monkeypatch, *, route: bool):
    bge = _cols("code", _BGE, 3) + _cols("docs", _BGE, 2)
    mini = _cols("docs", _MINI, 3)
    cols = bge + mini
    engine = _FakeEngine(
        monkeypatch, {c: _rows(f"c{i}_", 4, 0.2 + 0.01 * i) for i, c in enumerate(cols)},
        route=route,
    )

    def make_client(route_on: bool) -> HttpVectorClient:
        c = HttpVectorClient()
        c.supports_per_collection_search = route_on  # type: ignore[misc]
        return c

    return engine, cols, make_client


def _estimate(cols):
    def estimate(shape):
        n, rerank, _limit = drv.SHAPES[shape]
        return rp.estimate_batched_calls(cols, n, deep=rerank)

    return estimate


# ── the per-leg query strings ────────────────────────────────────────────────


def test_every_leg_sends_its_own_marked_query():
    qs = [drv.latency_query(s) for s in drv.SHAPES]
    qs += [drv.concurrency_query(k, i) for k in (1, 2, 3, 5) for i in range(k)]
    assert len(qs) == len(set(qs)), "two legs (or two concurrent searches) share a query string"
    assert all(drv.LATENCY_MARKERS[s] in drv.latency_query(s) for s in drv.SHAPES)
    assert all(drv.CONCURRENCY_MARKER in q for q in qs[len(drv.SHAPES):])
    # none collides with the parity legs' strings
    parity = {q for qq in (_QUERIES_NORERANK, _QUERIES_RR300, _QUERIES_RR1000) for q, _c in qq}
    assert not parity & set(qs)


# ── load is bounded ──────────────────────────────────────────────────────────


def test_the_default_concurrency_is_one_two_three_and_five_needs_a_flag():
    assert drv.DEFAULT_KS == (1, 2, 3)
    with pytest.raises(SystemExit, match="--include-5"):
        drv.main(["concurrency", "--ks", "1,2,3,5"])
    with pytest.raises(SystemExit, match="K must be in 1..5"):
        drv.main(["concurrency", "--ks", "1,6", "--include-5"])


def test_the_pause_between_runs_defaults_to_a_second_and_is_taken(monkeypatch):
    assert drv.DEFAULT_PAUSE_S == 1.0
    engine, cols, make_client = _world(monkeypatch, route=True)
    slept: list[float] = []
    drv.measure_latency(
        shapes=["mcp"], rounds=2, warmup=1, cols=cols, make_client=make_client,
        search=search_cross_corpus, diag_type=SearchDiagnostics, emit=lambda _l: None,
        pause_s=0.25, sleep=slept.append,
    )
    # 3 route runs + 3 batched runs: a pause before every run but the first
    assert slept == [0.25] * 5


def test_the_total_expected_request_count_is_printed_before_anything_is_sent(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=True)
    lines: list[str] = []
    seen_calls_at_first_line: list[int] = []

    def emit(line: str) -> None:
        if not lines:
            seen_calls_at_first_line.append(len(engine.calls))
        lines.append(line)

    drv.measure_latency(
        shapes=["mcp"], rounds=2, warmup=0, cols=cols, make_client=make_client,
        search=search_cross_corpus, diag_type=SearchDiagnostics, emit=emit,
        pause_s=0.0, estimate=_estimate(cols), sleep=lambda _s: None,
    )
    assert lines[0].startswith("EXPECTED REQUESTS (estimate)")
    assert seen_calls_at_first_line == [0]
    route_per, batched_per = _estimate(cols)("mcp")
    total = 2 * (route_per + batched_per)
    assert f"{total} total" in lines[0]
    # the estimate is the real count on a healthy engine
    routed = [p for p in engine.paths() if p == "/v1/vectors/search-per-collection"]
    flat = [p for p in engine.paths() if p == "/v1/vectors/search"]
    assert (len(routed), len(flat)) == (2 * route_per, 2 * batched_per)
    assert any(line.startswith("[mcp] WINDOW utc ") for line in lines), "the UTC window is printed at the end"


def test_the_concurrency_estimate_and_window_are_printed(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=True)
    lines: list[str] = []
    drv.measure_concurrency(
        ks=[1, 2], shape="mcp", cols=cols, make_client=make_client,
        search=search_cross_corpus, diag_type=SearchDiagnostics, emit=lines.append,
        pause_s=0.0, estimate=_estimate(cols), sleep=lambda _s: None,
    )
    route_per, _ = _estimate(cols)("mcp")
    assert lines[0] == (
        f"EXPECTED REQUESTS (estimate) {3 * route_per} total route requests  "
        f"[K in [1, 2]: 3 searches x {route_per}]"
    )
    assert [line for line in lines if "WINDOW utc" in line] and len(
        [line for line in lines if "WINDOW utc" in line]) == 2
    assert len([p for p in engine.paths() if p == "/v1/vectors/search-per-collection"]) == 3 * route_per


def test_the_parity_cell_estimate_counts_the_reference_twice_and_the_loader_only_when_asked(monkeypatch):
    engine, cols, _ = _world(monkeypatch, route=True)
    queries = [("q1", "all"), ("q2", "all")]
    off = rp.expected_cell_calls(queries, {"all": cols}, 5, loader=False)
    route_per, batched_per = rp.estimate_batched_calls(cols, 5)
    assert f"{2 * route_per} route + {2 * 2 * batched_per} batched" in off
    assert "loader off" in off
    on = rp.expected_cell_calls(queries, {"all": cols}, 5, loader=True)
    assert f"{2 * len(cols)} pre-batching per-collection" in on


def test_the_estimate_matches_what_a_healthy_search_really_sends(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=True)
    for n in (5, 30):
        engine.calls.clear()
        search_cross_corpus("q", cols, n, make_client(False), cluster_by=None,
                            threshold_override=float("inf"), deep_candidates=True, sensitivity="include")
        route_per, batched_per = rp.estimate_batched_calls(cols, n, deep=True)
        assert engine.paths().count("/v1/vectors/search") == batched_per


# ── a route run that never reached the route is refused ──────────────────────


def test_a_route_run_against_an_engine_without_the_route_is_refused(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=False)
    with pytest.raises(drv.RouteNotServedError, match="not a route measurement"):
        drv.measure_latency(
            shapes=["mcp"], rounds=1, warmup=0, cols=cols, make_client=make_client,
            search=search_cross_corpus, diag_type=SearchDiagnostics, emit=lambda _l: None,
            pause_s=0.0, sleep=lambda _s: None,
        )


def test_a_concurrent_run_against_an_engine_without_the_route_is_refused(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=False)
    with pytest.raises(drv.RouteNotServedError, match="never reached the route"):
        drv.measure_concurrency(
            ks=[2], shape="mcp", cols=cols, make_client=make_client,
            search=search_cross_corpus, diag_type=SearchDiagnostics, emit=lambda _l: None,
            pause_s=0.0, sleep=lambda _s: None,
        )


def test_a_batched_run_that_reaches_the_route_is_not_a_baseline(monkeypatch):
    engine, cols, _make = _world(monkeypatch, route=True)

    def always_route(_route_on: bool) -> HttpVectorClient:
        return HttpVectorClient()  # supports_per_collection_search left True for both paths

    def opted_in_search(*a, **kw):
        # the script clears the opt-in for a batched run (nexus-tu8wp.3), so a baseline that still
        # reaches the route needs something to put it back: the guard under test is the Recorder's
        os.environ[hvc.PER_COLLECTION_ROUTE_ENV] = "1"
        return search_cross_corpus(*a, **kw)

    with pytest.raises(drv.RouteNotServedError, match="not a batched baseline"):
        drv.measure_latency(
            shapes=["mcp"], rounds=1, warmup=0, cols=cols, make_client=always_route,
            search=opted_in_search, diag_type=SearchDiagnostics, emit=lambda _l: None,
            pause_s=0.0, sleep=lambda _s: None,
        )


def test_a_batched_run_on_a_route_capable_client_stays_off_the_route_because_the_env_is_cleared(monkeypatch):
    engine, cols, _make = _world(monkeypatch, route=True)
    monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, "1")  # the caller opted in; a batched leg must not inherit it

    def always_route(_route_on: bool) -> HttpVectorClient:
        return HttpVectorClient()

    run = drv.run_one(
        path="batched", shape="mcp", round_no=0, query="q", cols=cols, make_client=always_route,
        search=search_cross_corpus, diag_type=SearchDiagnostics,
    )
    assert run.search_calls >= 1 and run.route_calls == 0


def test_the_exit_code_for_an_unserved_route_is_three(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=False)
    monkeypatch.setattr(drv, "_live_world", lambda: (cols, {"mcp": cols, "cli": cols}, make_client, search_cross_corpus, SearchDiagnostics))
    module_time(monkeypatch, drv).sleep = lambda _s: None
    assert drv.main(["latency", "--rounds", "1", "--warmup", "0", "--shapes", "mcp", "--pause", "0"]) == 3
    assert drv.EXIT_ROUTE_NOT_SERVED == 3


# ── what is recorded ─────────────────────────────────────────────────────────


def test_failed_collections_are_recorded_with_their_error_kind(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=True)
    engine.errors[cols[0]] = ("statement_timeout", "search statement exceeded its bound")
    lines: list[str] = []
    out = drv.measure_concurrency(
        ks=[1], shape="mcp", cols=cols, make_client=make_client,
        search=search_cross_corpus, diag_type=SearchDiagnostics, emit=lines.append,
        pause_s=0.0, sleep=lambda _s: None,
    )
    res = out[1]["results"][0]
    assert res["failed"] and res["isolated"] == [(cols[0], "statement_timeout")]
    assert any("statement_timeout" in line for line in lines)
    assert res["route_served"] == 2 and res["search_calls"] == 0


# ── nexus-tu8wp.3: the route is opt-in, so the script owns the env ───────────


def _env_spy(seen: list[tuple[bool, str | None]]):
    """A search that records ``(client asks for the route, NX_SEARCH_PER_COLLECTION)`` as it runs."""
    def spy(*a, **kw):
        seen.append((kw["t3"].supports_per_collection_search, os.environ.get(hvc.PER_COLLECTION_ROUTE_ENV)))
        return search_cross_corpus(*a, **kw)

    return spy


def test_the_scripts_env_name_is_the_clients_opt_in():
    assert drv.ROUTE_ENV == hvc.PER_COLLECTION_ROUTE_ENV


@pytest.mark.parametrize("prior", [None, "0", "1", "garbage"])
@pytest.mark.parametrize("on", [True, False])
def test_route_env_sets_or_clears_for_the_block_and_restores_the_callers_value(monkeypatch, prior, on):
    if prior is None:
        monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)
    else:
        monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, prior)
    with drv.route_env(on):
        assert hvc.per_collection_route_enabled() is on
        assert os.environ.get(hvc.PER_COLLECTION_ROUTE_ENV) == ("1" if on else None)
    assert os.environ.get(hvc.PER_COLLECTION_ROUTE_ENV) == prior


def test_route_env_restores_when_the_block_raises(monkeypatch):
    monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, "0")
    with pytest.raises(RuntimeError), drv.route_env(True):
        raise RuntimeError("boom")
    assert os.environ[hvc.PER_COLLECTION_ROUTE_ENV] == "0"


@pytest.mark.parametrize("caller_env", [None, "1", "0"], ids=["unset", "caller-on", "caller-off"])
def test_latency_route_runs_see_the_opt_in_and_batched_runs_do_not(monkeypatch, caller_env):
    """Whatever the caller's environment says, a route run runs with the opt-in set and a batched run
    without it, and the caller's value is back afterwards."""
    engine, cols, make_client = _world(monkeypatch, route=True)
    if caller_env is not None:
        monkeypatch.setenv(hvc.PER_COLLECTION_ROUTE_ENV, caller_env)
    seen: list[tuple[bool, str | None]] = []
    drv.measure_latency(
        shapes=["mcp"], rounds=2, warmup=0, cols=cols, make_client=make_client,
        search=_env_spy(seen), diag_type=SearchDiagnostics, emit=lambda _l: None,
        pause_s=0.0, sleep=lambda _s: None,
    )
    assert len(seen) == 4
    assert [env for route, env in seen if route] == ["1", "1"]
    assert [env for route, env in seen if not route] == [None, None]
    assert os.environ.get(hvc.PER_COLLECTION_ROUTE_ENV) == caller_env


def test_concurrency_workers_see_the_opt_in_and_it_is_restored(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=True)
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)
    seen: list[tuple[bool, str | None]] = []
    drv.measure_concurrency(
        ks=[3], shape="mcp", cols=cols, make_client=make_client,
        search=_env_spy(seen), diag_type=SearchDiagnostics, emit=lambda _l: None,
        pause_s=0.0, sleep=lambda _s: None,
    )
    assert seen == [(True, "1")] * 3
    assert hvc.PER_COLLECTION_ROUTE_ENV not in os.environ


def test_lexical_searches_see_the_opt_in_and_it_is_restored(monkeypatch):
    engine, cols, make_client = _world(monkeypatch, route=True)
    engine.serve_lexical = True
    monkeypatch.setenv(hvc.ROUTE_LEXICAL_ENV, "")
    monkeypatch.delenv(hvc.PER_COLLECTION_ROUTE_ENV, raising=False)
    seen: list[tuple[bool, str | None]] = []
    drv.measure_lexical(
        rounds=1, warmup=0, ks=(2,), cols=cols, make_client=make_client, search=_env_spy(seen),
        diag_type=SearchDiagnostics, emit=lambda _l: None, pause_s=0.0, sleep=lambda _s: None,
    )
    assert len(seen) == 4, "one field and one nofield round, then two concurrent searches"
    assert {env for _route, env in seen} == {"1"}
    assert hvc.PER_COLLECTION_ROUTE_ENV not in os.environ


# ── nexus-pq3wr: the --lexical mode ──────────────────────────────────────────


def test_the_lexical_mode_times_the_field_against_the_hybrid_fallback(monkeypatch):
    """With the field the lexical arms ride the route (no /hybrid-search call); without
    it the engine echoes no lexical and the client runs the leg on /hybrid-search. Both
    paths are recorded per round, with each arm's lexical_error_kind."""
    engine, cols, make_client = _world(monkeypatch, route=True)
    engine.serve_lexical = True
    # the mode opts itself in (nexus-n51v1); setenv, not delenv, so the "1" it writes is undone
    monkeypatch.setenv(hvc.ROUTE_LEXICAL_ENV, "")
    engine.lexical_errors = {cols[0]: ("statement_timeout", "the statement bound")}
    out: list[str] = []
    res = drv.measure_lexical(
        rounds=2, warmup=0, ks=(), cols=cols, make_client=make_client, search=search_cross_corpus,
        diag_type=SearchDiagnostics, emit=out.append, pause_s=0.0, sleep=lambda s: None,
    )
    runs = res["runs"]
    assert {r["path"] for r in runs} == {"field", "nofield"} and len(runs) == 4
    field = [r for r in runs if r["path"] == "field"]
    nofield = [r for r in runs if r["path"] == "nofield"]
    assert all(r["lexical_served"] for r in field) and not any(r["lexical_served"] for r in nofield)
    # the one timed-out arm is retried on /hybrid-search; without the field every group's leg is
    assert all(r["hybrid_calls"] >= 1 for r in field) and all(r["lexical_kinds"] == {"statement_timeout": 1} for r in field)
    assert all(r["hybrid_calls"] > r_f["hybrid_calls"] for r, r_f in zip(nofield, field)) or all(
        r["hybrid_calls"] >= 1 for r in nofield)
    assert any("MEDIAN wall" in line for line in out) and any("WINDOW utc" in line for line in out)


def test_the_lexical_mode_queries_are_marked_and_distinct():
    qs = [drv.lexical_query(p) for p in ("field", "nofield")] + [drv.lexical_query("conc", k=3, i=i) for i in range(3)]
    assert len(qs) == len(set(qs)) and all(drv.LEXICAL_MARKER in q for q in qs)
