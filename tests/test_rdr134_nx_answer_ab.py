# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 9 (nexus-7fku2.2.3): the nx_answer A/B driver, on stubbed results. No paid call.

The pass rule is the RDR's, verbatim: the default becomes ``top1`` only if NDCG@3 over all questions rises by at
least 0.03 with more questions better than worse, and the generic-answer questions lose no more than 0.02 on
average. That the arm switch reaches the real plan runner is pinned in ``test_rdr134_plan_topic_route``.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from bench import topic_route_nx_answer_ab as ab
from nexus.plans import topic_route_scope as trs


@pytest.fixture(autouse=True)
def _info_events_are_logged():
    """The driver captures the info-level search_topic_route event; the suite's default level filters it."""
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))


def _row(kind: str, off: float, top1: float) -> dict[str, Any]:
    return {"kind": kind, "off": off, "top1": top1}


def _generic(n: int, delta: float) -> list[dict[str, Any]]:
    return [_row("generic", 0.5, 0.5 + delta) for _ in range(n)]


# ── the pass rule ────────────────────────────────────────────────────────────


def test_a_clear_gain_passes_and_every_component_is_read_out() -> None:
    rows = [_row("rdr", 0.3, 0.4) for _ in range(5)] + _generic(10, 0.0)
    out = ab.pass_rule(rows)
    assert out["passed"] is True
    assert out["mean_delta_all"] == pytest.approx(5 * 0.1 / 15)
    assert (out["wins"], out["losses"], out["ties"]) == (5, 0, 10)
    assert out["mean_delta_generic"] == pytest.approx(0.0) and out["n_generic"] == 10
    assert out["checks"] == {"mean_delta_all": True, "more_better_than_worse": True, "generic_loss": True}


def test_a_gain_under_three_hundredths_fails() -> None:
    rows = [_row("rdr", 0.3, 0.32) for _ in range(5)] + _generic(10, 0.02)
    out = ab.pass_rule(rows)
    assert out["checks"]["mean_delta_all"] is False and out["passed"] is False


def test_exactly_three_hundredths_passes() -> None:
    rows = [_row("rdr", 0.3, 0.33) for _ in range(10)] + _generic(10, 0.03)
    assert ab.pass_rule(rows)["checks"]["mean_delta_all"] is True


def test_more_losers_than_winners_fails_even_with_a_mean_gain() -> None:
    rows = [_row("rdr", 0.0, 1.0)] + [_row("rdr", 0.5, 0.45) for _ in range(3)] + _generic(10, 0.0)
    out = ab.pass_rule(rows)
    assert out["mean_delta_all"] > 0.03
    assert out["checks"]["more_better_than_worse"] is False and out["passed"] is False


def test_equal_winners_and_losers_fails() -> None:
    rows = [_row("rdr", 0.0, 1.0), _row("rdr", 0.5, 0.45)] + _generic(10, 0.0)
    assert ab.pass_rule(rows)["checks"]["more_better_than_worse"] is False


def test_generic_questions_losing_more_than_two_hundredths_fails() -> None:
    rows = [_row("rdr", 0.0, 0.5) for _ in range(20)] + _generic(10, -0.021)
    out = ab.pass_rule(rows)
    assert out["checks"]["mean_delta_all"] and out["checks"]["more_better_than_worse"]
    assert out["checks"]["generic_loss"] is False and out["passed"] is False


def test_a_generic_loss_of_exactly_two_hundredths_passes_that_check() -> None:
    rows = [_row("rdr", 0.0, 0.5) for _ in range(20)] + _generic(10, -0.02)
    assert ab.pass_rule(rows)["checks"]["generic_loss"] is True


def test_fewer_than_ten_generic_questions_cannot_pass() -> None:
    rows = [_row("rdr", 0.0, 0.5) for _ in range(20)] + _generic(9, 0.1)
    out = ab.pass_rule(rows)
    assert out["checks"]["generic_loss"] is False and out["passed"] is False
    assert "fewer than 10" in out["generic_note"]


def test_no_rows_is_not_a_pass() -> None:
    assert ab.pass_rule([])["passed"] is False


# ── stubbed nx_answer ────────────────────────────────────────────────────────


class _Catalog:
    """chash -> file path, the two batched calls ``_resolve_paths_via_catalog`` makes."""

    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    def docs_for_chashes(self, chashes):
        return {c: [c] for c in chashes if c in self.paths}

    def resolve_many(self, ids):
        return {i: SimpleNamespace(file_path=self.paths[i]) for i in ids}


_CAT = _Catalog({"good": "docs/benchmarking.md", "bad": "docs/other.md", "far": "docs/far.md"})
_OURS = "docs__1-1__bge-base-en-v1-5__v1"
_log = structlog.get_logger("tests.ab")


def _answer(by_arm: dict[str, list], *, cost: float | None = 0.01, step_cost: float | None = 0.01,
            routed: int = 2, seen: list | None = None, plan: dict[str, str] | None = None,
            exhausted: dict[str, int] | None = None):
    """An nx_answer stand-in. ``by_arm[arm]`` lists ``(chash, distance)``; the top1 arm logs the event a routed
    search would."""

    async def fn(*, question: str, structured: bool, trace: bool, **kw: Any) -> dict:
        route = trs.configured_route()
        arm = "off" if route is None else route["mode"]
        if seen is not None:
            seen.append((question, arm, kw))
        if arm == "top1":
            _log.info(ab.ROUTE_EVENT, routed=routed, no_topics=0, fallback=0, skipped=0, unreported=0,
                      unrouted_reason=None if routed else "capability_absent")
        return {
            "chunks": [{"chash": h, "collection": _OURS, "distance": d} for h, d in by_arm[arm]],
            "cost_usd": cost, "steps": [{"cost_usd": step_cost}],
            "plan_id": (plan or {}).get(arm, "p1"), "step_count": 2,
            "budget_exhausted_at_step": (exhausted or {}).get(arm),
        }

    return fn


def _q(i: int, kind: str = "generic") -> dict[str, Any]:
    return {"qid": f"Q{i}", "text": f"q{i}", "gt": {"benchmarkingmd": 3}, "kind": kind, "owner": "1-1",
            "exact": kind == "generic"}


_Q = [_q(i) for i in range(3)]
_BETTER = {"off": [("bad", 0.1)], "top1": [("good", 0.1)]}


def _run(queries=_Q, answer=None, **kw):
    kw.setdefault("cap_usd", 10.0)
    kw.setdefault("call_budget_usd", 0.05)
    return ab.run(queries, answer_fn=answer or _answer(_BETTER), catalog=_CAT, scope=kw.pop("scope", None), **kw)


def test_each_question_is_run_under_both_arms_and_paired() -> None:
    seen: list = []
    out = _run(answer=_answer(_BETTER, seen=seen))
    assert sorted((q, arm) for q, arm, _ in seen) == sorted((q["text"], a) for q in _Q for a in ab.ARMS)
    assert [r["off"]["ndcg3"] for r in out["rows"]] == [0.0] * 3
    assert [r["top1"]["ndcg3"] for r in out["rows"]] == [1.0] * 3
    assert out["stopped"] is None and out["spent_usd"] == pytest.approx(0.06)


def test_the_raw_distance_ordering_sorts_nearest_first_and_the_envelope_order_is_kept_beside_it() -> None:
    answer = _answer({"off": [("far", 0.9), ("good", 0.1)], "top1": [("far", 0.9), ("good", 0.1)]})
    (row,) = _run(_Q[:1], answer)["rows"]
    assert row["off"]["distance"]["top3"][0] == "benchmarking.md", "nearest first"
    assert row["off"]["envelope"]["top3"][0] == "far.md", "as the envelope listed them"
    assert row["off"]["distance"]["ndcg3"] > row["off"]["envelope"]["ndcg3"]


def test_the_primary_is_the_calibrated_order_and_raw_distance_is_kept_beside_it(cloud_mode: None) -> None:  # voyage per-model calibration factors
    # Sam 2026-11-19 after the first partial run: raw distance put voyage-code-3 rows ahead of every
    # voyage-context-3 row on a mixed envelope, whatever routing did (134-research-17).
    assert ab.PRIMARY == "calibrated" and ab.SECONDARY == "envelope" and "distance" in ab.ORDERINGS
    answer = _answer({"off": [("far", 0.9), ("good", 0.1)], "top1": [("far", 0.9), ("good", 0.1)]})
    (row,) = _run(_Q[:1], answer)["rows"]
    assert row["off"]["ndcg3"] == row["off"]["calibrated"]["ndcg3"]
    assert set(ab.ORDERINGS) <= set(row["off"])


def test_calibration_lifts_a_context_3_row_over_a_nearer_raw_code_row(cloud_mode: None) -> None:  # voyage per-model calibration factors
    code, docs = "code__1-77__voyage-code-3__v1", "docs__1-1__voyage-context-3__v1"
    chunks = [{"chash": "c", "collection": code, "distance": 0.38},
              {"chash": "d", "collection": docs, "distance": 0.50}]
    assert [c["chash"] for c in ab.distance_order(chunks)] == ["c", "d"]
    assert [c["chash"] for c in ab.calibrated_order(chunks)] == ["d", "c"], "0.50 * 0.45/0.65 = 0.346 < 0.38"


def test_a_mixed_envelope_is_scored_on_the_calibrated_order(cloud_mode: None) -> None:  # voyage per-model calibration factors
    # The run's decision reads the calibrated order: the labelled docs row wins it, while raw distance (printed
    # beside it) still puts the nearer code row first.
    rows = [("bad", "code__1-1__voyage-code-3__v1", 0.38), ("good", "docs__1-1__voyage-context-3__v1", 0.50)]

    async def fn(*, question: str, structured: bool, trace: bool, **kw: Any) -> dict:
        if trs.configured_route() is not None:
            _log.info(ab.ROUTE_EVENT, routed=1, no_topics=0, fallback=0, skipped=0, unreported=0, unrouted_reason=None)
        return {"chunks": [{"chash": h, "collection": c, "distance": d} for h, c, d in rows], "cost_usd": 0.01,
                "steps": [{"cost_usd": 0.01}], "plan_id": "p1", "step_count": 1, "budget_exhausted_at_step": None}

    (row,) = _run(_Q[:1], fn)["rows"]
    off = row["off"]
    assert off["calibrated"]["top3"][0] == "benchmarking.md" and off["distance"]["top3"][0] == "other.md"
    assert off["ndcg3"] == off["calibrated"]["ndcg3"] > off["distance"]["ndcg3"]


def test_a_catalog_row_model_wins_over_the_name_and_each_arm_records_the_models_used(cloud_mode: None) -> None:  # voyage per-model calibration factors
    # Search asks the catalog row first (nexus-ml9vu); a legacy name carries no model token.
    legacy, code = "docs__legacy-papers", "code__1-1__voyage-code-3__v1"
    rows = [("bad", code, 0.38), ("good", legacy, 0.50)]
    asked: list = []

    def resolver(colls):
        asked.append(sorted(colls))
        return lambda c: "voyage-context-3" if c == legacy else None

    async def fn(*, question: str, structured: bool, trace: bool, **kw: Any) -> dict:
        if trs.configured_route() is not None:
            _log.info(ab.ROUTE_EVENT, routed=1, no_topics=0, fallback=0, skipped=0, unreported=0, unrouted_reason=None)
        return {"chunks": [{"chash": h, "collection": c, "distance": d} for h, c, d in rows], "cost_usd": 0.01,
                "steps": [{"cost_usd": 0.01}], "plan_id": "p1", "step_count": 1, "budget_exhausted_at_step": None}

    (row,) = _run(_Q[:1], fn, model_resolver=resolver)["rows"]
    assert row["off"]["calibration_models"] == {legacy: "voyage-context-3", code: "voyage-code-3"}
    assert row["off"]["calibrated"]["top3"][0].endswith("benchmarking.md"), "the context-3 factor put it first"
    assert asked and asked[0] == sorted({legacy, code})


def test_the_raw_ordering_is_summarized_beside_the_secondary() -> None:
    s = ab.summarize([_paired(f"R{i}", "rdr", 0.0, 0.5) for i in range(3)])
    assert set(s["informational"]) >= {"secondary_ordering_rule", "raw_ordering_rule"}


def test_one_model_calibrates_to_the_raw_order(cloud_mode: None) -> None:  # voyage per-model calibration factors
    docs = "docs__1-1__voyage-context-3__v1"
    chunks = [{"chash": h, "collection": docs, "distance": d} for h, d in (("a", 0.6), ("b", 0.2), ("c", None))]
    assert ab.calibrated_order(chunks) == ab.distance_order(chunks)


def test_a_chunk_with_no_distance_sorts_after_the_rest() -> None:
    chunks = [{"chash": "a", "distance": None}, {"chash": "b", "distance": 0.5}, {"chash": "c", "distance": 0.1}]
    assert [c["chash"] for c in ab.distance_order(chunks)] == ["c", "b", "a"]


def test_the_route_events_of_each_arm_are_counted() -> None:
    (row,) = _run(_Q[:1])["rows"]
    assert row["top1"]["route"]["routed"] == 2 and row["top1"]["route"]["events"] == 1
    assert row["off"]["route"]["events"] == 0


def test_a_run_whose_top1_arm_routed_nothing_is_vacuous_not_a_verdict() -> None:
    out = _run(answer=_answer(_BETTER, routed=0))
    s = ab.summarize(out["rows"], stopped=out["stopped"])
    assert s["vacuous"] is True and s["verdict"].startswith("vacuous")
    assert s["n_routed_questions"] == 0


def test_the_arm_order_alternates_by_question() -> None:
    seen: list = []
    _run(answer=_answer(_BETTER, seen=seen))
    assert [seen[i][1] for i in range(0, len(seen), 2)] == ["off", "top1", "off"]


def test_generic_questions_run_first() -> None:
    seen: list = []
    _run([_q(0, "rdr"), _q(1, "title"), _q(2, "generic")], _answer(_BETTER, seen=seen))
    assert seen[0][0] == "q2"


def test_the_setting_is_restored_after_each_call() -> None:
    before = trs.configured_route
    _run(_Q[:1])
    assert trs.configured_route is before


def test_every_call_gets_the_same_budget_and_the_scope() -> None:
    seen: list = []
    _run(_Q[:2], _answer(_BETTER, seen=seen), scope="docs", call_budget_usd=0.25)
    assert {kw["budget_usd"] for _, _, kw in seen} == {0.25}
    assert {kw["scope"] for _, _, kw in seen} == {"docs"}


def test_a_vacuous_answer_is_an_error_never_a_zero_and_the_question_is_not_paired() -> None:
    out = ab.run(_Q[:1], answer_fn=_answer({"off": [("x", 0.1)], "top1": [("y", 0.1)]}), catalog=_Catalog({}),
                 cap_usd=1.0, scope=None, call_budget_usd=0.05)
    (row,) = out["rows"]
    assert row["off"]["error"].startswith("vacuous") and row["paired"] is False
    s = ab.summarize(out["rows"])
    assert s["errors"] == 1 and s["verdict"].startswith(("incomplete", "vacuous"))


def test_a_budget_exhausted_arm_is_unpaired() -> None:
    (row,) = _run(_Q[:1], _answer(_BETTER, exhausted={"top1": 3}))["rows"]
    assert row["top1"]["error"].startswith("budget exhausted") and row["paired"] is False


def test_pairs_that_ran_different_plans_are_flagged() -> None:
    out = _run(_Q[:1], _answer(_BETTER, plan={"off": "p1", "top1": "p2"}))
    assert ab.summarize(out["rows"])["plan_mismatch_qids"] == ["Q0"]


def test_the_run_stops_before_a_question_that_could_exceed_the_cap() -> None:
    seen: list = []
    out = _run(answer=_answer(_BETTER, cost=0.4, seen=seen), cap_usd=1.0, call_budget_usd=0.1)
    assert len(seen) == 2, "after 0.8 spent, a question at twice the largest cost seen (0.4) could reach 1.6"
    assert out["stopped"] == "cap" and out["spent_usd"] == pytest.approx(0.8) and len(out["rows"]) == 1


def test_the_call_budget_bounds_the_first_question_too() -> None:
    seen: list = []
    out = _run(answer=_answer(_BETTER, seen=seen), cap_usd=1.0, call_budget_usd=0.6)
    assert seen == [] and out["stopped"] == "cap", "two calls at 0.6 could exceed 1.0"


@pytest.mark.parametrize("which", ["envelope", "step"])
def test_an_unknown_cost_stops_the_run(which: str) -> None:
    answer = _answer(_BETTER, cost=None) if which == "envelope" else _answer(_BETTER, step_cost=None)
    out = _run(answer=answer)
    assert out["stopped"] == "cost_unknown" and out["rows"] == []


def test_an_unexpected_error_keeps_the_paid_rows() -> None:
    class _Broken(_Catalog):
        calls = 0

        def docs_for_chashes(self, chashes):
            _Broken.calls += 1
            if _Broken.calls > 9:  # four catalog reads an arm: the second question's first arm fails
                raise RuntimeError("catalog hiccup")
            return super().docs_for_chashes(chashes)

    out = ab.run(_Q, answer_fn=_answer(_BETTER), catalog=_Broken(_CAT.paths), cap_usd=10.0, scope=None,
                 call_budget_usd=0.05)
    assert out["stopped"] == "driver_error" and "catalog hiccup" in out["stop_detail"]
    assert [r["qid"] for r in out["rows"]] == ["Q0"], "the finished question is kept"
    assert out["spent_usd"] == pytest.approx(0.03), "the failed arm's call was paid for and counts"


def test_each_finished_question_is_checkpointed_and_a_resume_skips_it() -> None:
    saved: list = []
    _run(_Q[:2], checkpoint=lambda state: saved.append(json.loads(json.dumps(state))))
    assert [len(s["rows"]) for s in saved] == [1, 2]
    seen: list = []
    first = saved[0]
    out = _run(_Q[:2], _answer(_BETTER, seen=seen), done=first["rows"], spent_usd=first["spent_usd"])
    assert {q for q, _, _ in seen} == {"q1"}, "the finished question is not paid again"
    assert [r["qid"] for r in out["rows"]] == ["Q0", "Q1"]
    assert out["spent_usd"] == pytest.approx(0.04)


def test_another_owners_file_earns_nothing() -> None:
    cat = _Catalog({"theirs": "docs/benchmarking.md"})

    async def fn(**kw):
        return {"chunks": [{"chash": "theirs", "collection": "docs__1-94__bge-base-en-v1-5__v1", "distance": 0.1}],
                "cost_usd": 0.01, "steps": [], "plan_id": "p", "step_count": 1, "budget_exhausted_at_step": None}

    (row,) = ab.run(_Q[:1], answer_fn=fn, catalog=cat, cap_usd=1.0, scope=None, call_budget_usd=0.05)["rows"]
    assert row["off"]["ndcg3"] == 0.0


def _paired(qid: str, kind: str, off: float, top1: float) -> dict[str, Any]:
    arm = lambda v: {"calibrated": {"ndcg3": v, "recall10": v}, "distance": {"ndcg3": v, "recall10": v},  # noqa: E731
                     "envelope": {"ndcg3": v, "recall10": v},
                     "elapsed_s": 1.0, "route": {"routed": 1}}
    return {"qid": qid, "kind": kind, "paired": True, "same_plan": True, "off": arm(off), "top1": arm(top1)}


def test_summary_splits_in_sample_from_out_of_sample_with_an_interval() -> None:
    rows = [_paired("R", "rdr", 0.2, 0.4), _paired("T", "title", 0.5, 0.5), _paired("G", "generic", 0.6, 0.3)]
    s = ab.summarize(rows)
    assert s["in_sample"]["n"] == 1 and s["in_sample"]["mean_delta_ndcg3"] == pytest.approx(0.2)
    assert s["out_of_sample"]["n"] == 2 and s["out_of_sample"]["mean_delta_ndcg3"] == pytest.approx(-0.15)
    lo, hi = s["out_of_sample"]["ci95_mean_delta"]
    assert lo <= -0.15 <= hi
    assert s["by_kind"]["generic"]["top1"]["ndcg3"] == pytest.approx(0.3)
    assert s["primary"] == "calibrated" and s["scope_notes"]


def test_a_verdict_needs_a_complete_run() -> None:
    rows = [_paired(f"R{i}", "rdr", 0.0, 0.5) for i in range(5)] + [_paired(f"G{i}", "generic", 0.5, 0.5)
                                                                     for i in range(10)]
    assert ab.summarize(rows)["verdict"].startswith("pass")
    assert ab.summarize(rows, stopped="cap")["verdict"].startswith("incomplete")


def test_the_bootstrap_interval_is_seeded() -> None:
    d = [0.1, -0.2, 0.3, 0.0, 0.05]
    assert ab.bootstrap_ci(d) == ab.bootstrap_ci(d)
    assert ab.bootstrap_ci([0.1]) is None


def test_main_refuses_without_a_spend_cap() -> None:
    with pytest.raises(SystemExit) as exc:
        ab.main(["--out", "/dev/null"])
    assert exc.value.code == 2


def test_a_resume_runs_an_unpaired_question_again() -> None:
    first = _run(_Q[:2], _answer(_BETTER, exhausted={"top1": 3}))
    assert [r["paired"] for r in first["rows"]] == [False, False]
    seen: list = []
    out = _run(_Q[:2], _answer(_BETTER, seen=seen), done=first["rows"], spent_usd=first["spent_usd"])
    assert {q for q, _, _ in seen} == {"q0", "q1"} and all(r["paired"] for r in out["rows"])


def test_a_resume_of_a_different_measurement_is_refused() -> None:
    head = {"queries": {"a.yaml": "1"}, "scope": None, "primary": "calibrated",
            "manifest": {"git_sha": "x", "engine_build": "e1"}}
    assert ab.resume_mismatch(dict(head), head) == ""
    assert "question files" in ab.resume_mismatch({**head, "queries": {"a.yaml": "2"}}, head)
    assert "scope" in ab.resume_mismatch({**head, "scope": "docs"}, head)
    assert "engine_build" in ab.resume_mismatch({**head, "manifest": {"git_sha": "x", "engine_build": "e0"}}, head)
    old = {k: v for k, v in head.items() if k != "primary"}  # a results file from before the primary changed
    assert "primary ordering" in ab.resume_mismatch(old, head)
    assert "git_sha" in ab.resume_mismatch({**head, "manifest": {"git_sha": "y", "engine_build": "e1"}}, head)
    # the off arm's path reads NX_SEARCH_PER_COLLECTION, so a resume under another value is another measurement
    assert "NX_SEARCH_PER_COLLECTION" in ab.resume_mismatch({**head, "per_collection_env": "1"},
                                                           {**head, "per_collection_env": None})


def test_an_engine_without_the_capability_is_refused_before_any_paid_call(monkeypatch, tmp_path) -> None:
    import nexus.db
    import nexus.mcp.core

    monkeypatch.setattr(nexus.db, "make_t3", lambda: SimpleNamespace(supports_topic_route=lambda: False))
    called: list = []
    monkeypatch.setattr(nexus.mcp.core, "nx_answer", lambda **kw: called.append(kw))
    assert ab.main(["--cap-usd", "1", "--out", str(tmp_path / "r.json")]) == 2
    assert called == [] and not (tmp_path / "r.json").exists()


def test_an_answer_with_no_chunks_is_unpaired_not_a_loss() -> None:
    (row,) = _run(_Q[:1], _answer({"off": [("good", 0.1)], "top1": []}))["rows"]
    assert row["top1"]["error"].startswith("no chunks") and row["paired"] is False


def test_an_unknown_step_cost_still_counts_the_known_envelope_cost() -> None:
    out = _run(answer=_answer(_BETTER, step_cost=None, cost=0.07))
    assert out["stopped"] == "cost_unknown" and out["spent_usd"] == pytest.approx(0.07)


def test_three_questions_that_route_nothing_stop_the_run_early() -> None:
    seen: list = []
    out = _run([_q(i) for i in range(6)], _answer(_BETTER, routed=0, seen=seen))
    assert out["stopped"] == "vacuous_early" and len(seen) == 6, "three questions, two arms each, then stop"


def test_a_fail_from_a_diluted_run_is_inconclusive_and_a_pass_is_not_affected() -> None:
    def arm(v: float, routed: int) -> dict:
        return {"calibrated": {"ndcg3": v, "recall10": v}, "distance": {"ndcg3": v, "recall10": v},
                "envelope": {"ndcg3": v, "recall10": v},
                "elapsed_s": 1.0, "route": {"routed": routed}}

    def row(qid: str, kind: str, off: float, top1: float, routed: int) -> dict:
        return {"qid": qid, "kind": kind, "paired": True, "same_plan": True, "off": arm(off, 0),
                "top1": arm(top1, routed)}

    few = [row("R0", "rdr", 0.5, 0.4, 1)] + [row(f"G{i}", "generic", 0.5, 0.5, 0) for i in range(10)]
    s = ab.summarize(few)
    assert s["verdict"].startswith("inconclusive (diluted: 1 of 11")
    many = [row(f"R{i}", "rdr", 0.5, 0.4, 1) for i in range(10)] + [row(f"G{i}", "generic", 0.5, 0.5, 1)
                                                                     for i in range(10)]
    assert ab.summarize(many)["verdict"].startswith("fail")
    wins = [row("R0", "rdr", 0.0, 1.0, 1)] + [row(f"G{i}", "generic", 0.5, 0.5, 0) for i in range(10)]
    assert ab.summarize(wins)["verdict"].startswith("pass")
