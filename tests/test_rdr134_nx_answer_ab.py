# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-134 Step 9 (nexus-7fku2.2.3): the nx_answer A/B driver, on stubbed results. No paid call.

The pass rule is the RDR's, verbatim: the default becomes ``top1`` only if NDCG@3 over all questions rises by at
least 0.03 with more questions better than worse, and the generic-answer questions lose no more than 0.02 on
average.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bench import topic_route_nx_answer_ab as ab
from nexus.plans import topic_route_scope as trs


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
    out = ab.pass_rule([])
    assert out["passed"] is False


# ── paired scoring on stubbed nx_answer ─────────────────────────────────────


class _Catalog:
    """chash -> file path, the two batched calls ``_resolve_paths_via_catalog`` makes."""

    def __init__(self, paths: dict[str, str]) -> None:
        self.paths = paths

    def docs_for_chashes(self, chashes):
        return {c: [c] for c in chashes if c in self.paths}

    def resolve_many(self, ids):
        return {i: SimpleNamespace(file_path=self.paths[i]) for i in ids}


_CAT = _Catalog({"good": "docs/benchmarking.md", "bad": "docs/other.md", "rdr": "docs/rdr/rdr-134-x.md"})


def _answer(by_arm: dict[str, list[str]], *, cost: float | None = 0.01, seen: list | None = None):
    """An nx_answer stand-in: the chunks depend on the route the runner would read for this call."""

    async def fn(*, question: str, structured: bool, trace: bool, **kw: Any) -> dict:
        route = trs.configured_route()
        arm = "off" if route is None else route["mode"]
        if seen is not None:
            seen.append((question, arm, kw))
        return {"chunks": [{"chash": h, "collection": "docs__x"} for h in by_arm[arm]], "cost_usd": cost}

    return fn


_Q = [{"qid": f"G{i}", "text": f"q{i}", "gt": {"benchmarkingmd": 3}, "kind": "generic"} for i in range(3)]


def test_each_question_is_run_under_both_arms_and_paired() -> None:
    seen: list = []
    out = ab.run(_Q, answer_fn=_answer({"off": ["bad"], "top1": ["good"]}, seen=seen), catalog=_CAT,
                 cap_usd=1.0, scope=None)
    assert sorted((q, arm) for q, arm, _ in seen) == sorted((q["text"], a) for q in _Q for a in ("off", "top1"))
    assert [r["off"]["ndcg3"] for r in out["rows"]] == [0.0] * 3
    assert [r["top1"]["ndcg3"] for r in out["rows"]] == [1.0] * 3
    assert out["stopped"] is None and out["spent_usd"] == pytest.approx(0.06)


def test_the_arm_order_alternates_by_question() -> None:
    seen: list = []
    ab.run(_Q, answer_fn=_answer({"off": [], "top1": []}, seen=seen), catalog=_CAT, cap_usd=1.0, scope=None)
    firsts = [seen[i][1] for i in range(0, len(seen), 2)]
    assert firsts == ["off", "top1", "off"]


def test_the_setting_is_restored_after_each_call() -> None:
    before = trs.configured_route
    ab.run(_Q[:1], answer_fn=_answer({"off": [], "top1": []}), catalog=_CAT, cap_usd=1.0, scope=None)
    assert trs.configured_route is before


def test_scope_and_the_remaining_budget_are_passed_to_nx_answer() -> None:
    seen: list = []
    ab.run(_Q[:1], answer_fn=_answer({"off": [], "top1": []}, seen=seen), catalog=_CAT, cap_usd=1.0, scope="docs")
    (_, _, kw1), (_, _, kw2) = seen
    assert kw1["scope"] == "docs" and kw1["budget_usd"] == pytest.approx(1.0)
    assert kw2["budget_usd"] == pytest.approx(0.99), "the second call may spend only what the first left"


def test_a_vacuous_answer_is_an_error_never_a_zero_and_the_question_is_not_paired() -> None:
    cat = _Catalog({})  # nothing resolves
    out = ab.run(_Q[:1], answer_fn=_answer({"off": ["x"], "top1": ["y"]}), catalog=cat, cap_usd=1.0, scope=None)
    (row,) = out["rows"]
    assert row["off"]["error"].startswith("vacuous") and row["paired"] is False
    s = ab.summarize(out["rows"])
    assert s["errors"] == 1 and s["pass_rule"]["passed"] is False


def test_the_run_stops_before_a_call_that_could_exceed_the_cap() -> None:
    seen: list = []
    out = ab.run(_Q, answer_fn=_answer({"off": [], "top1": []}, cost=0.4, seen=seen), catalog=_CAT,
                 cap_usd=1.0, scope=None)
    assert len(seen) == 2, "after 0.8 spent, a third call at the largest cost seen (0.4) could reach 1.2"
    assert out["stopped"] == "cap" and out["spent_usd"] == pytest.approx(0.8)
    assert len(out["rows"]) == 1


def test_an_unknown_cost_stops_the_run() -> None:
    out = ab.run(_Q, answer_fn=_answer({"off": [], "top1": []}, cost=None), catalog=_CAT, cap_usd=1.0, scope=None)
    assert out["stopped"] == "cost_unknown" and out["rows"] == []


def test_summary_splits_in_sample_from_out_of_sample() -> None:
    rows = [
        {"qid": "R", "kind": "rdr", "paired": True, "off": {"ndcg3": 0.2, "recall10": 0.0}, "top1": {"ndcg3": 0.4, "recall10": 1.0}},
        {"qid": "T", "kind": "title", "paired": True, "off": {"ndcg3": 0.5, "recall10": 1.0}, "top1": {"ndcg3": 0.5, "recall10": 1.0}},
        {"qid": "G", "kind": "generic", "paired": True, "off": {"ndcg3": 0.6, "recall10": 1.0}, "top1": {"ndcg3": 0.3, "recall10": 0.0}},
    ]
    s = ab.summarize(rows)
    assert s["in_sample"]["n"] == 1 and s["in_sample"]["mean_delta_ndcg3"] == pytest.approx(0.2)
    assert s["out_of_sample"]["n"] == 2 and s["out_of_sample"]["mean_delta_ndcg3"] == pytest.approx(-0.15)
    assert s["by_kind"]["generic"]["top1"]["ndcg3"] == pytest.approx(0.3)
    assert s["ordering"] == ab.ORDERING


def test_main_refuses_without_a_spend_cap() -> None:
    with pytest.raises(SystemExit) as exc:
        ab.main(["--out", "/dev/null"])
    assert exc.value.code == 2
