# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ztunv: pure parts of the three-arm model-tier study driver
(scripts/bench/model_tier_study.py). No dispatch, no I/O."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.model_tier_study import (  # noqa: E402
    ARMS,
    BUDGET_CEILING_USD,
    analyze,
    BudgetExceeded,
    arm_verdict,
    cross_arm_pairs,
    dispatch_order,
    guard_budget,
    summarize_scores,
    within_arm_pairs,
)


def test_arms_are_fable_sonnet_haiku_with_fable_unforced() -> None:
    assert [a.name for a in ARMS] == ["fable", "sonnet", "haiku"]
    assert ARMS[0].model is None  # production HOLD path: no override
    assert ARMS[1].model == "sonnet" and ARMS[2].model == "haiku"


def test_dispatch_order_interleaves_arms_within_each_round() -> None:
    order = dispatch_order(["operator_filter", "operator_rank"], n=2)
    assert order[:6] == [
        ("operator_filter", 0, "fable"), ("operator_filter", 0, "sonnet"),
        ("operator_filter", 0, "haiku"), ("operator_filter", 1, "fable"),
        ("operator_filter", 1, "sonnet"), ("operator_filter", 1, "haiku"),
    ]
    assert len(order) == 12


def test_pairing_counts() -> None:
    assert len(within_arm_pairs(3)) == 3
    assert len(cross_arm_pairs(3, 3)) == 9
    assert within_arm_pairs(1) == []


def test_summarize_scores_reports_none_scores_not_as_zero() -> None:
    s = summarize_scores([1.0, 0.5, None])
    assert s["n"] == 2 and s["n_none"] == 1
    assert s["mean"] == pytest.approx(0.75) and s["min"] == 0.5 and s["max"] == 1.0
    assert summarize_scores([None]) == {"n": 0, "n_none": 1, "mean": None, "min": None, "max": None}


def test_budget_guard_aborts_before_the_dispatch_that_would_exceed() -> None:
    guard_budget(spent=BUDGET_CEILING_USD - 0.01)
    with pytest.raises(BudgetExceeded):
        guard_budget(spent=BUDGET_CEILING_USD)


class TestArmVerdict:
    def test_not_refuted_when_min_vs_fable_clears_and_no_failures(self) -> None:
        v = arm_verdict(min_vs_fable=0.85, threshold=0.80, failures=0, fable_noise_min=0.9)
        assert v == "NOT_REFUTED"

    def test_refuted_on_low_agreement_or_any_failure(self) -> None:
        assert arm_verdict(min_vs_fable=0.7, threshold=0.80, failures=0, fable_noise_min=0.9) == "REFUTED"
        assert arm_verdict(min_vs_fable=0.95, threshold=0.80, failures=1, fable_noise_min=0.9) == "REFUTED"

    def test_noise_limited_when_fable_cannot_agree_with_itself(self) -> None:
        assert arm_verdict(min_vs_fable=0.95, threshold=0.80, failures=0, fable_noise_min=0.5) == "NOISE_LIMITED"

    def test_none_agreement_is_refuted_never_passed(self) -> None:
        assert arm_verdict(min_vs_fable=None, threshold=0.60, failures=0, fable_noise_min=0.9) == "REFUTED"


def _rec(op: str, arm: str, rnd: int, output: dict | None, *, cost: float = 1.0,
         model: str | None = None, error: str | None = None) -> dict:
    model = model or {"fable": "claude-fable-5", "sonnet": "claude-sonnet-5",
                      "haiku": "claude-haiku-4-5"}[arm]
    return {
        "operator": op, "arm": arm, "round": rnd, "requested_model": None,
        "canonical_model": model, "family_ok": arm in model, "elapsed_s": 1.0,
        "cost_usd": cost, "input_tokens": 1, "output_tokens": 1,
        "output": output, "error": error,
    }


def _filter_out(ids: list[str]) -> dict:
    return {"items": [{"id": i} for i in ids]}


class TestAnalyze:
    def test_perfect_agreement_is_not_refuted_and_ratios_computed(self) -> None:
        recs = [
            _rec("operator_filter", arm, i, _filter_out(["a", "b"]), cost=c)
            for arm, c in (("fable", 1.0), ("sonnet", 0.3), ("haiku", 0.1))
            for i in range(3)
        ]
        a = analyze(recs)
        e = a["operators"]["operator_filter"]
        assert e["verdicts"] == {"sonnet": "NOT_REFUTED", "haiku": "NOT_REFUTED"}
        assert e["arms"]["sonnet"]["cost_ratio_vs_fable"] == pytest.approx(0.3)
        assert e["cross"]["fable_vs_haiku"]["n"] == 9 and e["arms"]["fable"]["noise"]["n"] == 3
        assert a["total_cost_usd"] == pytest.approx(4.2)
        assert e["fable_void"] is False

    def test_fable_family_violation_voids_every_verdict(self) -> None:
        recs = [
            _rec("operator_filter", arm, i, _filter_out(["a"]))
            for arm in ("fable", "sonnet", "haiku") for i in range(3)
        ]
        recs[1]["canonical_model"] = "claude-sonnet-5"  # box default drifted
        recs[1]["family_ok"] = False
        e = analyze(recs)["operators"]["operator_filter"]
        assert e["fable_void"] is True
        assert e["arms"]["fable"]["family_violations"] == 1
        assert e["verdicts"] == {"sonnet": "VOID", "haiku": "VOID"}

    def test_candidate_failure_refutes_and_shrinks_pairs_visibly(self) -> None:
        recs = [
            _rec("operator_filter", arm, i, _filter_out(["a"]))
            for arm in ("fable", "sonnet", "haiku") for i in range(3)
        ]
        bad = next(r for r in recs if r["arm"] == "haiku")
        bad["output"], bad["error"], bad["cost_usd"] = None, "TimeoutError: x", None
        a = analyze(recs)
        e = a["operators"]["operator_filter"]
        assert e["arms"]["haiku"]["failures"] == 1
        assert e["cross"]["fable_vs_haiku"]["n"] == 6  # 3 x 2 surviving outputs
        assert e["verdicts"]["haiku"] == "REFUTED" and e["verdicts"]["sonnet"] == "NOT_REFUTED"
        assert a["n_cost_unknown"] == 1

    def test_low_agreement_refutes_only_that_arm(self) -> None:
        recs = [_rec("operator_filter", "fable", i, _filter_out(["a", "b"])) for i in range(3)]
        recs += [_rec("operator_filter", "sonnet", i, _filter_out(["a", "b"])) for i in range(3)]
        recs += [_rec("operator_filter", "haiku", i, _filter_out(["z"])) for i in range(3)]
        e = analyze(recs)["operators"]["operator_filter"]
        assert e["verdicts"] == {"sonnet": "NOT_REFUTED", "haiku": "REFUTED"}
        assert e["cross"]["fable_vs_haiku"]["max"] == 0.0


class TestRunResume:
    @pytest.mark.asyncio
    async def test_resume_skips_paid_cells_and_counts_prior_spend(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A resumed run dispatches ONLY the cells not already on disk, and
        the budget guard sees prior spend (no double-count, no reset)."""
        import bench.model_tier_study as mts

        out = tmp_path / "study.json"
        prior = [_rec("operator_filter", "fable", 0, _filter_out(["a"]), cost=5.0),
                 _rec("operator_filter", "sonnet", 0, _filter_out(["a"]), cost=1.0)]
        out.write_text(json.dumps({"started_at": "x", "records": prior}))
        dispatched: list[tuple[str, str]] = []

        async def fake_dispatch(op, arm, *, timeout):
            dispatched.append((op, arm.name))
            return _rec(op, arm.name, -1, _filter_out(["a"]), cost=0.5)

        monkeypatch.setattr(mts, "_dispatch_one", fake_dispatch)
        state = await mts.run(["operator_filter"], n=2, out_path=out, timeout=1)
        assert dispatched == [
            ("operator_filter", "haiku"),
            ("operator_filter", "fable"), ("operator_filter", "sonnet"), ("operator_filter", "haiku"),
        ]
        assert {(r["operator"], r["round"], r["arm"]) for r in state["records"]} == {
            ("operator_filter", i, a) for i in range(2) for a in ("fable", "sonnet", "haiku")
        }
        assert state["analysis"]["total_cost_usd"] == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_ceiling_aborts_before_the_next_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import bench.model_tier_study as mts

        out = tmp_path / "study.json"
        prior = [_rec("operator_filter", "fable", 0, _filter_out(["a"]), cost=BUDGET_CEILING_USD)]
        out.write_text(json.dumps({"started_at": "x", "records": prior}))
        calls = 0

        async def fake_dispatch(op, arm, *, timeout):
            nonlocal calls
            calls += 1
            return _rec(op, arm.name, -1, _filter_out(["a"]))

        monkeypatch.setattr(mts, "_dispatch_one", fake_dispatch)
        with pytest.raises(BudgetExceeded):
            await mts.run(["operator_filter"], n=1, out_path=out, timeout=1)
        assert calls == 0
