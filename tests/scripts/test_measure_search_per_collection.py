# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tu8wp.3: the latency leg of ``scripts/measure_search_per_collection.py`` measures result
parity as well as speed.

Everything here is offline: synthetic result lists and fake clients, no network and no engine. The
pure functions live in ``scripts/search_page_parity.py``; the last cases drive ``measure_latency``
with fake clients to show the numbers reach the per-run lines and the JSON summary.
"""
from __future__ import annotations

import inspect
import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import measure_search_per_collection as msc  # noqa: E402
import search_page_parity as spp  # noqa: E402

from nexus.commands.search_cmd import search_cmd  # noqa: E402
from nexus.config import TuningConfig  # noqa: E402
from nexus.mcp.core import _MCP_PER_COLLECTION_K  # noqa: E402
from nexus.mcp.core import search as mcp_search  # noqa: E402
from nexus.search_engine import SearchDiagnostics, boost_window_size  # noqa: E402
from nexus.types import SearchResult  # noqa: E402


@dataclass
class Row:
    id: str
    collection: str
    distance: float
    metadata: dict = field(default_factory=dict)


def _dense(collection: str, count: int, *, start: float = 0.10, step: float = 0.001) -> list[Row]:
    return [Row(f"{collection}-{i}", collection, start + i * step) for i in range(count)]


def _k(code: int = 60, other: int = 120):
    return lambda col: code if col.startswith("code__") else other


# -- collection_ranks ---------------------------------------------------------------------------


def test_collection_ranks_are_one_based_per_collection_by_distance_then_id():
    pool = [
        Row("b", "code__x", 0.30), Row("a", "code__x", 0.30), Row("c", "code__x", 0.10),
        Row("d", "docs__y", 0.50), Row("e", "docs__y", 0.05),
    ]
    ranks = spp.collection_ranks(pool)
    assert ranks[spp.row_key(pool[2])] == 1  # c
    assert ranks[spp.row_key(pool[1])] == 2  # a beats b on the id tie-break
    assert ranks[spp.row_key(pool[0])] == 3  # b
    assert ranks[spp.row_key(pool[4])] == 1  # e, its own collection's nearest
    assert ranks[spp.row_key(pool[3])] == 2


def test_row_key_separates_the_same_id_in_two_collections():
    assert spp.row_key(Row("x", "code__a", 0.1)) != spp.row_key(Row("x", "docs__a", 0.1))


# -- kept_counts / kept_diff --------------------------------------------------------------------


def test_kept_counts_reads_the_diagnostics_kept_figure_and_falls_back_to_raw_minus_dropped():
    d = SearchDiagnostics(
        per_collection={"code__a": (80, 20, 0.5, None), "docs__b": (10, 4, 0.5, 0.6)},
        kept={"code__a": 60},
    )
    assert spp.kept_counts([d]) == {"code__a": 60, "docs__b": 6}


def test_kept_counts_sums_across_diagnostics_and_ignores_non_diagnostics():
    d1 = SearchDiagnostics(kept={"code__a": 3, "docs__b": 4})
    d2 = SearchDiagnostics(kept={"code__a": 5})
    assert spp.kept_counts([d1, "junk", d2]) == {"code__a": 8, "docs__b": 4}


def test_kept_diff_lists_only_collections_that_differ_largest_gap_first():
    route = {"a": 60, "b": 10, "c": 0}
    batched = {"a": 150, "b": 10, "d": 7}
    assert spp.kept_diff(route, batched) == [("a", 60, 150), ("d", 0, 7)]


# -- page_rows ----------------------------------------------------------------------------------


def test_page_rows_carry_each_rows_rank_in_its_own_collection_of_the_pool():
    pool = _dense("code__a", 5) + _dense("docs__b", 2)
    page = [pool[1], pool[5], pool[3]]
    rows = spp.page_rows(page, pool)
    assert [(r.collection, r.rank) for r in rows] == [("code__a", 2), ("docs__b", 1), ("code__a", 4)]


# -- compare_pages ------------------------------------------------------------------------------


def test_identical_pages_overlap_fully_with_no_exclusive_rows():
    pool = _dense("code__a", 10)
    rows = spp.page_rows(pool[:5], pool)
    p = spp.compare_pages(rows, rows, 5, _k())
    assert (p.n, p.overlap, p.route_only, p.batched_only, p.past_k) == (5, 5, [], [], [])


def test_partial_overlap_names_the_route_only_and_batched_only_ids():
    pool = _dense("code__a", 10)
    route = spp.page_rows(pool[:4], pool)
    batched = spp.page_rows([pool[0], pool[1], pool[2], pool[5]], pool)
    p = spp.compare_pages(route, batched, 4, _k())
    assert p.overlap == 3
    assert p.route_only == [spp.row_key(pool[3])]
    assert p.batched_only == [spp.row_key(pool[5])]


def test_batched_only_row_past_the_route_k_in_its_collection_is_flagged():
    # code k = 60: a code row at rank 61 can never come from the route; rank 60 can.
    pool = _dense("code__a", 90)
    route = spp.page_rows(pool[:3], pool)
    batched = spp.page_rows([pool[0], pool[1], pool[59], pool[60]], pool)
    p = spp.compare_pages(route, batched, 4, _k())
    assert [(f["key"], f["rank"], f["k"]) for f in p.past_k] == [(spp.row_key(pool[60]), 61, 60)]
    assert p.past_k[0]["collection"] == "code__a"


def test_the_route_k_is_per_collection_so_a_docs_row_at_rank_100_is_within_k():
    docs = _dense("docs__b", 130)
    batched = spp.page_rows([docs[99], docs[120]], docs)  # ranks 100 and 121
    p = spp.compare_pages([], batched, 2, _k(code=60, other=120))
    assert [f["rank"] for f in p.past_k] == [121]


def test_a_row_past_k_that_the_route_also_returned_is_not_flagged():
    pool = _dense("code__a", 90)
    both = spp.page_rows([pool[70]], pool)  # rank 71, on both pages
    p = spp.compare_pages(both, both, 1, _k())
    assert p.batched_only == [] and p.past_k == []


def test_empty_pages_compare_without_error():
    p = spp.compare_pages([], [], 30, _k())
    assert (p.overlap, p.route_only, p.batched_only, p.past_k) == (0, [], [], [])


# -- aggregate_parity ---------------------------------------------------------------------------


def _parity(overlap: int, n: int, past: list[int]) -> spp.PageParity:
    return spp.PageParity(
        n=n, route_size=n, batched_size=n, overlap=overlap,
        route_only=[f"r{i}" for i in range(n - overlap)],
        batched_only=[f"b{i}" for i in range(n - overlap)],
        past_k=[{"key": f"b{i}", "collection": "code__a", "rank": r, "k": 60} for i, r in enumerate(past)],
    )


def test_aggregate_reports_mean_overlap_runs_with_past_k_and_the_max():
    agg = spp.aggregate_parity([_parity(30, 30, []), _parity(28, 30, [61]), _parity(26, 30, [62, 75])])
    assert agg["runs"] == 3
    assert agg["n"] == 30
    assert agg["mean_overlap"] == 28.0
    assert agg["min_overlap"] == 26
    assert agg["runs_with_past_k"] == 2
    assert agg["max_past_k_rows"] == 2
    assert agg["max_past_k_rank"] == 75
    assert agg["mean_batched_only"] == 2.0


def test_aggregate_of_no_runs_is_zeroed_not_an_error():
    agg = spp.aggregate_parity([])
    assert agg["runs"] == 0 and agg["runs_with_past_k"] == 0 and agg["mean_overlap"] is None


# -- formatting ---------------------------------------------------------------------------------


def test_parity_line_shows_n_overlap_exclusive_ids_and_past_k():
    line = spp.format_parity_line("mcp", 0, _parity(28, 30, [61]))
    assert "mcp" in line and "round  0" in line
    assert "page 30" in line and "overlap 28" in line
    assert "route-only 2" in line and "batched-only 2" in line
    assert "past-k 1" in line and "rank 61>60" in line
    assert "b0" in line and "r0" in line


def test_kept_line_totals_both_paths_and_names_the_collections_that_differ():
    line = spp.format_kept_line("mcp", 0, {"code__a": 60, "docs__b": 5}, {"code__a": 150, "docs__b": 5})
    assert "kept route 65 batched 155" in line
    assert "code__a 60/150" in line
    assert "docs__b" not in line


# -- measure_latency wiring ---------------------------------------------------------------------


class _FakeClient:
    def __init__(self, route: bool) -> None:
        self.supports_per_collection_search = route

    def search(self, *a, **kw):  # the batched path's request
        return None

    def search_per_collection(self, query, names, **kw):
        return {"per_collection": []}


def _fake_world(
    route_pool: list[Row], batched_pool: list[Row], route_kept: dict, batched_kept: dict,
    *, route_failed: dict | None = None, batched_failed: dict | None = None, degraded: bool = False,
    clients: list | None = None,
):
    def make_client(route: bool) -> _FakeClient:
        c = _FakeClient(route)
        if clients is not None:
            clients.append(c)
        return c

    def search(query, cols, *, n_results, t3, diagnostics_out, **kw):
        if kw.get("model_of_out") is not None:
            kw["model_of_out"].append(lambda name: "fake-model")
        if kw.get("rerank_meta_out") is not None and degraded:
            kw["rerank_meta_out"]["code__a"] = {"degraded": True}
        if t3.supports_per_collection_search:
            t3.search_per_collection(query, cols)
            diagnostics_out.append(
                SearchDiagnostics(kept=dict(route_kept), failed_collections=dict(route_failed or {})),
            )
            return list(route_pool)
        t3.search(query, cols)
        diagnostics_out.append(
            SearchDiagnostics(kept=dict(batched_kept), failed_collections=dict(batched_failed or {})),
        )
        return list(batched_pool)

    return make_client, search


def _by_distance(results: list, n: int, model_of=None) -> list:
    return sorted(results, key=lambda r: (r.distance, r.id))[:n]


def _run_latency(route_pool, batched_pool, route_kept, batched_kept, *, rounds=2, warmup=1, shapes=("mcp",),
                 **world_kw):
    make_client, search = _fake_world(route_pool, batched_pool, route_kept, batched_kept, **world_kw)
    lines: list[str] = []
    out = msc.measure_latency(
        shapes=list(shapes), rounds=rounds, warmup=warmup, cols=["code__a"], make_client=make_client,
        search=search, diag_type=SearchDiagnostics, emit=lines.append, pause_s=0.0,
        sleep=lambda s: None, page_of=_by_distance, route_k_of=lambda shape: _k(),
    )
    return out, lines


def test_latency_leg_emits_parity_and_kept_lines_and_a_json_summary():
    dense = _dense("code__a", 150)
    deep = Row("code__a-deep", "code__a", 0.0001)  # nearest overall: lands on the batched page only
    out, lines = _run_latency(
        dense[:60], [deep, *dense], {"code__a": 60}, {"code__a": 151}, rounds=2, warmup=1,
    )
    text = "\n".join(lines)
    assert "parity" in text and "kept route 60 batched 151" in text
    assert "0 of 2 pairs excluded (failed collections), 0 skipped" in text and "counted 2" in text
    summary = out["mcp"]["parity_summary"]
    assert summary["runs"] == 2  # the warm-up pair is recorded but not aggregated
    assert len(out["mcp"]["parity"]) == 3
    assert summary["n"] == 10  # the mcp page: limit 10, not the 30 rows fetched
    assert summary["mean_overlap"] == 9.0  # the deep row displaces the route page's 10th row
    assert summary["top1_agree_rate"] == 0.0  # the deep row leads the batched page only
    assert summary["mean_top3_overlap"] == 2.0
    assert summary["runs_with_past_k"] == 0  # rank 1 in its collection: within k
    json.dumps(out["mcp"]["parity"])
    json.dumps(summary)


def test_latency_leg_flags_a_batched_only_page_row_ranked_past_k():
    # Route pool = the 60 nearest of a dense code collection; batched pool = all 90 plus a page that,
    # by construction of the ranker below, takes rank-70 rows: the ranker prefers LARGER ids' rows.
    dense = _dense("code__a", 90)
    route_pool, batched_pool = dense[:60], dense

    def prefer_deep(results: list, n: int, model_of=None) -> list:
        return sorted(results, key=lambda r: -r.distance)[:n]  # the farthest rows lead

    make_client, search = _fake_world(route_pool, batched_pool, {"code__a": 60}, {"code__a": 90})
    lines: list[str] = []
    out = msc.measure_latency(
        shapes=["mcp"], rounds=1, warmup=0, cols=["code__a"], make_client=make_client, search=search,
        diag_type=SearchDiagnostics, emit=lines.append, pause_s=0.0, sleep=lambda s: None,
        page_of=prefer_deep, route_k_of=lambda shape: _k(),
    )
    summary = out["mcp"]["parity_summary"]
    # route page: ranks 51..60; batched page: ranks 81..90 (10 rows), all past k = 60.
    assert summary["runs_with_past_k"] == 1
    assert summary["max_past_k_rows"] == 10
    assert summary["max_past_k_rank"] == 90
    assert summary["mean_overlap"] == 0.0
    assert any("past-k 10" in ln for ln in lines)


def test_latency_leg_reports_no_past_k_when_the_pages_agree():
    dense = _dense("code__a", 90)
    out, lines = _run_latency(dense[:60], dense, {"code__a": 60}, {"code__a": 90}, rounds=2, warmup=0)
    summary = out["mcp"]["parity_summary"]
    assert summary["mean_overlap"] == 10.0
    assert summary["top1_agree_rate"] == 1.0 and summary["mean_top3_overlap"] == 3.0
    assert summary["runs_with_past_k"] == 0 and summary["max_past_k_rows"] == 0
    assert any("overlap 10" in ln and "top1 same" in ln and "top3 3/3" in ln and "past-k 0" in ln for ln in lines)


def test_runs_record_their_kept_counts_and_page_in_the_json_payload():
    dense = _dense("code__a", 90)
    out, _ = _run_latency(dense[:60], dense, {"code__a": 60}, {"code__a": 90}, rounds=1, warmup=0)
    by_path = {r.path: r for r in out["mcp"]["runs"]}
    assert by_path["route"].kept == {"code__a": 60}
    assert by_path["batched"].kept == {"code__a": 90}
    assert len(by_path["route"].page) == 10
    json.dumps([r.__dict__ for r in out["mcp"]["runs"]], default=str)


# -- make_route_k -------------------------------------------------------------------------------


def test_mcp_route_k_is_the_mcp_tools_own_cap_and_cli_stays_uncapped():
    cols = ["code__1-1__voyage-code-3__v1", "docs__1-1__voyage-context-3__v1"]
    route_k_of = spp.make_route_k(cols, msc.SHAPES)
    mcp = route_k_of("mcp")  # n=30: 60 code / 120 docs uncapped, lowered to the cap
    assert spp.shape_k_cap("mcp") == _MCP_PER_COLLECTION_K
    assert (mcp(cols[0]), mcp(cols[1])) == (_MCP_PER_COLLECTION_K, _MCP_PER_COLLECTION_K)
    # the cli shape is uncapped: n=10 gives 20 code / 40 docs (n*mult)
    assert spp.shape_k_cap("cli") is None
    cli = route_k_of("cli")
    assert (cli(cols[0]), cli(cols[1])) == (20, 40)


def test_uncapped_route_k_at_n_30_is_60_code_120_docs():
    cols = ["code__1-1__voyage-code-3__v1", "docs__1-1__voyage-context-3__v1"]
    route_k_of = spp.make_route_k(cols, {"x": (30, False, 10)})  # a shape with no cap
    uncapped = route_k_of("x")
    assert (uncapped(cols[0]), uncapped(cols[1])) == (60, 120)


def test_the_cap_never_lowers_k_below_n():
    # min(k, max(cap, n)): a page deeper than the cap still fills
    cols = ["code__1-1__voyage-code-3__v1"]
    route_k_of = spp.make_route_k(cols, {"mcp": (100, False, 10)})
    assert route_k_of("mcp")(cols[0]) == 100


def test_the_measured_searches_pass_the_shapes_cap_to_search_cross_corpus():
    seen: list[dict] = []

    def make_client(route: bool) -> _FakeClient:
        return _FakeClient(route)

    def search(query, cols, *, n_results, t3, diagnostics_out, **kw):
        seen.append({"route": t3.supports_per_collection_search, **kw})
        if t3.supports_per_collection_search:
            t3.search_per_collection(query, cols)
        else:
            t3.search(query, cols)
        diagnostics_out.append(SearchDiagnostics())
        return []

    msc.measure_latency(
        shapes=["mcp", "cli"], rounds=1, warmup=0, cols=["code__a"], make_client=make_client, search=search,
        diag_type=SearchDiagnostics, emit=lambda s: None, pause_s=0.0, sleep=lambda s: None,
        page_of=_by_distance, route_k_of=lambda shape: _k(),
    )
    caps = {(kw["route"], kw["rerank"]): kw["per_collection_k_cap"] for kw in seen}
    assert caps == {
        (True, False): _MCP_PER_COLLECTION_K, (False, False): _MCP_PER_COLLECTION_K,  # mcp: both paths
        (True, True): None, (False, True): None,  # cli
    }


# -- top-1 / top-3 agreement --------------------------------------------------------------------


def test_top1_and_top3_agreement_ignore_order_within_the_top_three_but_not_the_top_row():
    pool = _dense("code__a", 10)
    route = spp.page_rows([pool[0], pool[1], pool[2], pool[3]], pool)
    batched = spp.page_rows([pool[1], pool[0], pool[4], pool[2]], pool)
    p = spp.compare_pages(route, batched, 4, _k())
    assert p.top1_agree is False  # route leads with pool[0], batched with pool[1]
    assert p.top3_overlap == 2  # {0,1,2} vs {1,0,4}
    same = spp.compare_pages(route, route, 4, _k())
    assert same.top1_agree is True and same.top3_overlap == 3


def test_top1_agreement_is_false_when_a_page_is_empty():
    pool = _dense("code__a", 3)
    rows = spp.page_rows(pool, pool)
    assert spp.compare_pages([], rows, 3, _k()).top1_agree is False
    assert spp.compare_pages([], [], 3, _k()).top3_overlap == 0


# -- failed collections exclude a pair (C3) -----------------------------------------------------


def test_a_pair_with_failed_collections_is_excluded_from_the_summary_but_kept_in_the_json():
    dense = _dense("code__a", 90)
    out, lines = _run_latency(
        dense[:60], dense, {"code__a": 60}, {"code__a": 90}, rounds=2, warmup=0,
        route_failed={"docs__z": "fanout_budget_exhausted"},
    )
    summary = out["mcp"]["parity_summary"]
    assert (summary["pairs"], summary["pairs_excluded_failed"], summary["runs"]) == (2, 2, 0)
    assert summary["mean_overlap"] is None  # nothing counted: not a zero overlap
    entries = out["mcp"]["parity"]
    assert [(e["route_failed"], e["batched_failed"], e["excluded"]) for e in entries] == [(1, 0, True)] * 2
    assert all("overlap" in e for e in entries)  # the numbers stay visible
    text = "\n".join(lines)
    assert "2 of 2 pairs excluded (failed collections)" in text
    assert any("EXCLUDED, failed collections: route 1 batched 0" in ln for ln in lines)


def test_only_the_pairs_with_failures_are_excluded_and_the_rest_are_counted():
    dense = _dense("code__a", 90)
    make_client, search = _fake_world(dense[:60], dense, {"code__a": 60}, {"code__a": 90})
    calls = {"n": 0}

    def flaky(query, cols, *, t3, diagnostics_out, **kw):
        out = search(query, cols, t3=t3, diagnostics_out=diagnostics_out, **kw)
        if t3.supports_per_collection_search:
            calls["n"] += 1
            if calls["n"] == 1:  # only the first route run (round 0) fails a collection
                diagnostics_out[-1].failed_collections["docs__z"] = "statement_timeout"
        return out

    lines: list[str] = []
    out = msc.measure_latency(
        shapes=["mcp"], rounds=3, warmup=0, cols=["code__a"], make_client=make_client, search=flaky,
        diag_type=SearchDiagnostics, emit=lines.append, pause_s=0.0, sleep=lambda s: None,
        page_of=_by_distance, route_k_of=lambda shape: _k(),
    )
    s = out["mcp"]["parity_summary"]
    assert (s["pairs"], s["pairs_excluded_failed"], s["runs"]) == (3, 1, 2)
    assert any("1 of 3 pairs excluded (failed collections)" in ln for ln in lines)


# -- the cli page is labelled, and skipped when a rerank degraded (C2) --------------------------


def test_cli_parity_lines_say_the_page_is_the_pre_rerank_pool_order():
    dense = _dense("code__a", 40)
    out, lines = _run_latency(dense, dense, {"code__a": 40}, {"code__a": 40}, rounds=1, warmup=0,
                              shapes=("cli",))
    parity_lines = [ln for ln in lines if " parity " in ln]
    assert parity_lines and all("pre-rerank pool order" in ln for ln in parity_lines)
    assert out["cli"]["parity_summary"]["runs"] == 1


def test_the_mcp_parity_lines_carry_no_pre_rerank_label():
    dense = _dense("code__a", 40)
    _, lines = _run_latency(dense, dense, {"code__a": 40}, {"code__a": 40}, rounds=1, warmup=0)
    assert not any("pre-rerank" in ln for ln in lines)


def test_a_cli_pair_whose_rerank_degraded_is_skipped_not_compared():
    dense = _dense("code__a", 40)
    out, lines = _run_latency(dense, dense, {"code__a": 40}, {"code__a": 40}, rounds=2, warmup=0,
                              shapes=("cli",), degraded=True)
    s = out["cli"]["parity_summary"]
    assert (s["pairs"], s["pairs_skipped"], s["runs"]) == (2, 2, 0)
    assert all(e["skipped"] == "rerank degraded" and "overlap" not in e for e in out["cli"]["parity"])
    assert any("SKIPPED (rerank degraded" in ln for ln in lines)
    assert all(r.rerank_degraded for r in out["cli"]["runs"])


def test_a_degrade_in_a_shape_without_rerank_is_never_recorded():
    dense = _dense("code__a", 40)
    out, _ = _run_latency(dense, dense, {"code__a": 40}, {"code__a": 40}, rounds=1, warmup=0, degraded=True)
    assert not any(r.rerank_degraded for r in out["mcp"]["runs"])  # mcp asks for no rerank_meta_out


# -- page_of is guarded (I2) and runs outside the timed window ----------------------------------


def test_a_page_of_that_raises_keeps_the_timing_records_page_error_and_carries_on():
    dense = _dense("code__a", 40)

    def boom(results, n, model_of=None):
        raise RuntimeError("no catalog")

    make_client, search = _fake_world(dense, dense, {"code__a": 40}, {"code__a": 40})
    lines: list[str] = []
    out = msc.measure_latency(
        shapes=["mcp"], rounds=2, warmup=0, cols=["code__a"], make_client=make_client, search=search,
        diag_type=SearchDiagnostics, emit=lines.append, pause_s=0.0, sleep=lambda s: None,
        page_of=boom, route_k_of=lambda shape: _k(),
    )
    runs = out["mcp"]["runs"]
    assert len(runs) == 4 and all(r.page == [] and "RuntimeError: no catalog" in r.page_error for r in runs)
    assert all(r.wall_s >= 0 and r.rows == 40 for r in runs)  # the search itself was measured
    s = out["mcp"]["parity_summary"]
    assert (s["pairs"], s["pairs_skipped"], s["runs"]) == (2, 2, 0)
    assert any("SKIPPED (page error" in ln for ln in lines)
    assert any("MEDIAN wall" in ln for ln in lines)  # the timing summary still prints


def test_page_of_runs_after_the_recorder_is_restored_and_the_search_ran_inside_it():
    dense = _dense("code__a", 40)
    clients: list[_FakeClient] = []
    inside: list[bool] = []
    outside: list[bool] = []
    make_client, base_search = _fake_world(dense, dense, {"code__a": 40}, {"code__a": 40}, clients=clients)

    def search(query, cols, *, t3, **kw):
        inside.append("search" in vars(t3))  # the Recorder wraps the instance while the search runs
        return base_search(query, cols, t3=t3, **kw)

    def page_of(results, n, model_of=None):
        outside.append(any("search" in vars(c) or "search_per_collection" in vars(c) for c in clients))
        return _by_distance(results, n)

    msc.measure_latency(
        shapes=["mcp"], rounds=1, warmup=0, cols=["code__a"], make_client=make_client, search=search,
        diag_type=SearchDiagnostics, emit=lambda s: None, pause_s=0.0, sleep=lambda s: None,
        page_of=page_of, route_k_of=lambda shape: _k(),
    )
    assert inside == [True, True]  # control: the probe does see the Recorder when it is live
    assert outside == [False, False]


def test_the_searchs_model_of_is_handed_to_the_page_as_the_surface_does():
    dense = _dense("code__a", 40)
    seen: list = []

    def page_of(results, n, model_of=None):
        seen.append(model_of)
        return _by_distance(results, n)

    make_client, search = _fake_world(dense, dense, {}, {})
    msc.measure_latency(
        shapes=["mcp"], rounds=1, warmup=0, cols=["code__a"], make_client=make_client, search=search,
        diag_type=SearchDiagnostics, emit=lambda s: None, pause_s=0.0, sleep=lambda s: None,
        page_of=page_of, route_k_of=lambda shape: _k(),
    )
    assert len(seen) == 2 and all(callable(m) and m("x") == "fake-model" for m in seen)


# -- the cap is resolved outside the timed window (I1) ------------------------------------------


def test_the_k_cap_is_resolved_before_the_client_is_wrapped_and_the_clock_starts(monkeypatch):
    # The first call imports nexus.mcp.core, which must not land inside the timed window. The
    # window opens when the Recorder wraps the client, so the cap must be resolved while no client
    # is wrapped (the same probe test_page_of_runs_after_the_recorder_is_restored uses, reversed).
    clients: list[_FakeClient] = []
    wrapped_at_cap: list[bool] = []

    def cap(shape):
        wrapped_at_cap.append(any("search" in vars(c) or "search_per_collection" in vars(c) for c in clients))
        return 40

    monkeypatch.setattr(msc, "shape_k_cap", cap)
    dense = _dense("code__a", 5)
    inside: list[bool] = []
    make_client, base = _fake_world(dense, dense, {}, {}, clients=clients)

    def search(query, cols, *, t3, **kw):
        inside.append("search" in vars(t3))
        return base(query, cols, t3=t3, **kw)

    msc.measure_latency(
        shapes=["mcp"], rounds=1, warmup=0, cols=["code__a"], make_client=make_client, search=search,
        diag_type=SearchDiagnostics, emit=lambda s: None, pause_s=0.0, sleep=lambda s: None,
        page_of=_by_distance, route_k_of=lambda shape: _k(),
    )
    assert inside == [True, True]  # control: the probe does see a wrapped client during the search
    assert wrapped_at_cap == [False, False]


def test_concurrency_resolves_the_k_cap_once_outside_the_workers(monkeypatch):
    threads: list[str] = []
    monkeypatch.setattr(
        msc, "shape_k_cap", lambda shape: threads.append(threading.current_thread().name) or 40,
    )
    dense = _dense("code__a", 5)
    make_client, search = _fake_world(dense, dense, {}, {})
    msc.measure_concurrency(
        ks=[3], shape="mcp", cols=["code__a"], make_client=make_client, search=search,
        diag_type=SearchDiagnostics, emit=lambda s: None, pause_s=0.0, sleep=lambda s: None,
    )
    assert threads == [threading.current_thread().name]  # once, on the caller's thread, not in a worker


# -- boosted_page: the MCP tool's first page ----------------------------------------------------


def test_boosted_page_applies_the_boosts_and_the_file_diversity_cap_in_the_tools_order(monkeypatch):
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name: None)
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("nexus.config.get_tuning_config", lambda *a, **k: TuningConfig())
    # Four nearest rows are chunks of ONE document; the cap (2 per file) pushes the rest behind
    # the other files' rows, so the page is NOT the plain distance order.
    pool = [
        SearchResult(id=f"r{i}", content="", distance=0.10 + i * 0.01, collection="docs__x",
                     metadata={"doc_id": "1.1.1"} if i < 4 else {})
        for i in range(12)
    ]
    plain = [r.id for r in sorted(pool, key=lambda r: r.distance)[:5]]
    page = [r.id for r in spp.boosted_page(pool, 5)]
    assert plain == ["r0", "r1", "r2", "r3", "r4"]
    assert page == ["r0", "r1", "r4", "r5", "r6"]  # r2, r3 are the document's third and fourth chunk


def test_boosted_page_cuts_to_the_limit_and_does_not_mutate_the_input_order(monkeypatch):
    monkeypatch.setattr("nexus.mcp_infra.get_collection_row", lambda name: None)
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: {})
    monkeypatch.setattr("nexus.config.get_tuning_config", lambda *a, **k: TuningConfig())
    pool = [SearchResult(id=f"r{i}", content="", distance=0.1 + i * 0.01, collection="docs__x") for i in range(40)]
    before = [r.id for r in pool]
    assert len(spp.boosted_page(pool, 10)) == 10
    assert [r.id for r in pool] == before


# -- boosted_page passes the tool's arguments (nexus.mcp.core.search) ---------------------------


@pytest.fixture
def spied(monkeypatch):
    """``boosted_page`` with its three seams replaced: the config reads return known values and the
    two engine functions (imported when ``boosted_page`` runs) record the kwargs they get."""
    tuning = object()
    calls: dict[str, dict] = {}
    monkeypatch.setattr("nexus.config.load_config", lambda *a, **k: {"search": {"hybrid_default": True}})
    monkeypatch.setattr("nexus.config.get_tuning_config", lambda *a, **k: tuning)

    def boosts(results, **kw):
        calls["boosts"] = kw
        return list(results)

    def cap(results, **kw):
        calls["cap"] = kw
        return list(results)

    monkeypatch.setattr("nexus.search_engine.apply_ranking_boosts", boosts)
    monkeypatch.setattr("nexus.search_engine.apply_file_diversity_cap", cap)
    return calls, tuning


def test_boosted_page_passes_the_configs_hybrid_default(spied):
    calls, _ = spied
    spp.boosted_page([], 10)
    assert calls["boosts"].get("hybrid") is True, "hybrid must be the config's search.hybrid_default"


def test_boosted_page_passes_the_tuning_config(spied):
    calls, tuning = spied
    spp.boosted_page([], 10)
    assert calls["boosts"].get("tuning") is tuning, "tuning must be get_tuning_config()'s result"


@pytest.mark.parametrize("limit", [10, 20])
def test_boosted_page_boosts_over_the_boost_window_of_the_limit(spied, limit):
    calls, _ = spied
    spp.boosted_page([], limit)
    assert calls["boosts"].get("window") == boost_window_size(limit), "boost window"


@pytest.mark.parametrize("limit", [10, 20])
def test_boosted_page_caps_files_over_the_same_window(spied, limit):
    calls, _ = spied
    spp.boosted_page([], limit)
    assert calls["cap"].get("window") == boost_window_size(limit), "diversity-cap window"
    assert calls["cap"].get("window") == calls["boosts"].get("window")


def test_the_boost_window_differs_between_the_two_limits():
    # a window the same for 10 and 20 would let a hard-coded constant pass the two cases above
    assert boost_window_size(10) != boost_window_size(20)


def test_boosted_page_hands_on_the_searchs_model_of(spied):
    calls, _ = spied

    def model_of(name):
        return "m"

    spp.boosted_page([], 10, model_of=model_of)
    assert calls["boosts"].get("model_of") is model_of, "model_of"


# -- make_route_k over a multi-collection group -------------------------------------------------


def test_a_group_takes_its_widest_multiplier_so_a_code_collection_beside_docs_gets_the_docs_k():
    same_model = ["code__1-1__voyage-context-3__v1", "docs__1-1__voyage-context-3__v1"]
    own_model = "code__2-1__voyage-code-3__v1"
    route_k_of = spp.make_route_k([*same_model, own_model], {"x": (30, False, 10)})
    k = route_k_of("x")
    assert k(same_model[1]) == 120
    assert k(same_model[0]) == 120  # max(mult) of its group (4), not its own (2)
    assert k(own_model) == 60  # a code collection alone in its model group keeps 2 * 30


# -- each shape searches its own surface's default corpora --------------------------------------


def test_the_mcp_shape_searches_the_mcp_tools_default_corpus_and_the_cli_shape_the_clis():
    mcp_default = inspect.signature(mcp_search).parameters["corpus"].default
    cli_default = next(p.default for p in search_cmd.params if p.name == "corpus")
    assert spp.shape_corpora("mcp") == tuple(mcp_default.split(","))
    assert spp.shape_corpora("cli") == tuple(cli_default)
    # neither surface searches rdr by default: the four-corpus runs before this change did
    assert "rdr" not in spp.shape_corpora("mcp") and "rdr" not in spp.shape_corpora("cli")
    assert spp.shape_corpora("mcp") != tuple(msc.DEFAULT_CORPORA)


def test_an_unknown_shape_has_no_corpora():
    with pytest.raises(ValueError):
        spp.shape_corpora("web")


def test_cols_for_takes_the_shapes_own_list_or_one_shared_list():
    assert spp.cols_for({"mcp": ["a"], "cli": ["b", "c"]}, "cli") == ["b", "c"]
    assert spp.cols_for(["x", "y"], "mcp") == ["x", "y"]


def test_the_latency_leg_searches_each_shape_over_its_own_collections():
    dense = _dense("code__a", 20)
    seen: dict[str, set[tuple]] = {"mcp": set(), "cli": set()}
    make_client, base = _fake_world(dense, dense, {}, {})

    def search(query, cols, *, n_results, **kw):
        seen["mcp" if n_results == 30 else "cli"].add(tuple(cols))
        return base(query, cols, n_results=n_results, **kw)

    msc.measure_latency(
        shapes=["mcp", "cli"], rounds=1, warmup=0, cols={"mcp": ["code__m"], "cli": ["code__c", "docs__c"]},
        make_client=make_client, search=search, diag_type=SearchDiagnostics, emit=lambda s: None,
        pause_s=0.0, sleep=lambda s: None, page_of=_by_distance, route_k_of=lambda shape: _k(),
    )
    assert seen == {"mcp": {("code__m",)}, "cli": {("code__c", "docs__c")}}


def test_route_k_and_the_request_estimate_follow_each_shapes_collections():
    alone = ["code__1-1__voyage-code-3__v1"]
    grouped = ["code__1-1__voyage-context-3__v1", "docs__1-1__voyage-context-3__v1"]
    # shapes with no cap, so the multiplier is what differs: x holds the code collection alone,
    # y puts it beside a docs collection of its model
    route_k_of = spp.make_route_k({"x": alone, "y": grouped}, {"x": (30, False, 10), "y": (30, False, 10)})
    assert route_k_of("x")(alone[0]) == 60
    assert route_k_of("y")(grouped[0]) == 120
    estimate = msc.make_estimator({"mcp": alone, "cli": [*alone, *grouped]})
    assert estimate("mcp")[0] == 1  # one model group, one route request
    assert estimate("cli")[0] == 2  # two model groups


def test_the_concurrency_leg_searches_its_shapes_collections():
    dense = _dense("code__a", 5)
    seen: list[tuple] = []
    make_client, base = _fake_world(dense, dense, {}, {})

    def search(query, cols, **kw):
        seen.append(tuple(cols))
        return base(query, cols, **kw)

    msc.measure_concurrency(
        ks=[2], shape="mcp", cols={"mcp": ["code__m"], "cli": ["docs__c"]}, make_client=make_client,
        search=search, diag_type=SearchDiagnostics, emit=lambda s: None, pause_s=0.0, sleep=lambda s: None,
    )
    assert seen == [("code__m",)] * 2
