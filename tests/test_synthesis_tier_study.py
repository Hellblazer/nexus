# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure parts of the synthesis-operator three-arm study driver
(scripts/bench/synthesis_tier_study.py). No dispatch, no I/O."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.synthesis_tier_study import (  # noqa: E402
    ARMS,
    analyze,
    BUDGET_CEILING_USD,
    OPERATORS,
    cell_verdict,
    grounding,
    judge_pairs,
    tally_judgments,
)

TAGS = ["src:aaaaaaaa", "src:bbbbbbbb", "src:cccccccc"]


class TestDesignConstants:
    def test_arms_and_operators(self) -> None:
        assert [a.name for a in ARMS] == ["fable", "sonnet", "haiku"]
        assert ARMS[0].model is None
        assert OPERATORS == ("summarize", "generate", "compare", "aggregate")
        assert BUDGET_CEILING_USD == 80.0


class TestGrounding:
    def test_cited_operators_fraction_of_resolvable_citations(self) -> None:
        out = {"summary": "x", "citations": ["[src:aaaaaaaa] p1", "bbbbbbbb — title", "made-up"]}
        assert grounding("summarize", out, TAGS, expected_keys=None) == pytest.approx(2 / 3)
        out2 = {"output": "x", "citations": []}
        assert grounding("generate", out2, TAGS, expected_keys=None) == 0.0

    def test_aggregate_key_value_jaccard_times_nonempty(self) -> None:
        out = {"aggregates": [{"key_value": "k1", "summary": "s"}, {"key_value": "k2", "summary": "s"}]}
        assert grounding("aggregate", out, TAGS, expected_keys=["k1", "k2"]) == 1.0
        out_drop = {"aggregates": [{"key_value": "k1", "summary": "s"}]}
        assert grounding("aggregate", out_drop, TAGS, expected_keys=["k1", "k2"]) == pytest.approx(0.5)
        out_empty = {"aggregates": [{"key_value": "k1", "summary": ""}, {"key_value": "k2", "summary": "s"}]}
        assert grounding("aggregate", out_empty, TAGS, expected_keys=["k1", "k2"]) == pytest.approx(0.5)

    def test_compare_needs_two_distinct_tags(self) -> None:
        assert grounding("compare", {"comparison": "src:aaaaaaaa vs bbbbbbbb"}, TAGS, expected_keys=None) == 1.0
        assert grounding("compare", {"comparison": "only src:aaaaaaaa"}, TAGS, expected_keys=None) == 0.0
        assert grounding("compare", {"comparison": ""}, TAGS, expected_keys=None) == 0.0

    def test_missing_output_is_zero_never_none(self) -> None:
        assert grounding("summarize", None, TAGS, expected_keys=None) == 0.0


class TestJudgePairs:
    def test_incumbent_pairs_both_positions_only(self) -> None:
        pairs = judge_pairs(["fable", "sonnet", "haiku"])
        assert len(pairs) == 4
        assert ("fable", "sonnet") in pairs and ("sonnet", "fable") in pairs
        assert ("sonnet", "haiku") not in pairs and ("haiku", "sonnet") not in pairs


class TestTally:
    def test_preference_rate_and_swap_instability(self) -> None:
        # judgments: (left_arm, right_arm, winner in {"A","B","tie"}, input_name)
        js = [
            ("fable", "haiku", "A", "q1"), ("haiku", "fable", "B", "q1"),   # consistent: fable preferred
            ("fable", "haiku", "tie", "q2"), ("haiku", "fable", "tie", "q2"),
            ("fable", "haiku", "A", "q3"), ("haiku", "fable", "A", "q3"),   # position-dependent: unstable
        ]
        t = tally_judgments(js, incumbent="fable", arm="haiku")
        assert t["n"] == 6
        assert t["incumbent_preferred"] == 3   # q1 x2, q3 left-position only
        assert t["arm_preferred"] == 1
        assert t["ties"] == 2
        assert t["incumbent_preferred_rate"] == pytest.approx(0.5)
        assert t["swap_disagreement_rate"] == pytest.approx(1 / 3)


class TestCellVerdict:
    def test_not_refuted_refuted_and_unstable(self) -> None:
        assert cell_verdict(incumbent_rate=0.5, arm_grounding=0.9, incumbent_grounding=0.95,
                            failures=0, swap_disagreement=0.0) == "NOT_REFUTED"
        assert cell_verdict(incumbent_rate=0.67, arm_grounding=0.9, incumbent_grounding=0.95,
                            failures=0, swap_disagreement=0.0) == "REFUTED"
        assert cell_verdict(incumbent_rate=0.5, arm_grounding=0.7, incumbent_grounding=0.95,
                            failures=0, swap_disagreement=0.0) == "REFUTED"
        assert cell_verdict(incumbent_rate=0.5, arm_grounding=0.9, incumbent_grounding=0.95,
                            failures=1, swap_disagreement=0.0) == "REFUTED"
        assert cell_verdict(incumbent_rate=0.5, arm_grounding=0.9, incumbent_grounding=0.95,
                            failures=0, swap_disagreement=0.5) == "NOT_REFUTED/JUDGE_UNSTABLE"
        assert cell_verdict(incumbent_rate=None, arm_grounding=0.9, incumbent_grounding=0.95,
                            failures=0, swap_disagreement=0.0) == "NO_DATA"


class TestResume:
    @pytest.mark.asyncio
    async def test_failed_judgment_is_retried_on_resume_completed_is_not(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A spend-limit-failed judge call (winner None) must be retried by
        a resumed run; a completed judgment and a completed generation must
        not be re-dispatched."""
        import json as _json

        import bench.synthesis_tier_study as sts

        inputs = {"inputs": [{
            "name": "delos", "question": "q",
            "chunks": [{"tag": "src:aaaaaaaa", "chash": "a" * 16, "title": "T", "text": "body"}],
        }]}
        inputs_path = tmp_path / "inputs.json"
        inputs_path.write_text(_json.dumps(inputs))

        def _gen(arm: str) -> dict:
            return {"kind": "generation", "operator": "summarize", "arm": arm, "input": "delos",
                    "requested_model": None, "canonical_model": f"claude-{arm}-5",
                    "family_ok": True, "elapsed_s": 1.0, "cost_usd": 0.1,
                    "input_tokens": 1, "output_tokens": 1,
                    "output": {"summary": "s", "citations": ["aaaaaaaa"]}, "error": None,
                    "grounding": 1.0}

        prior = [_gen(a) for a in ("fable", "sonnet", "haiku")]
        prior[2] = {**prior[2], "output": None, "error": "OperatorError: spend limit",
                    "cost_usd": None, "grounding": 0.0}  # failed haiku generation
        prior.append({"kind": "judgment", "operator": "summarize", "input": "delos",
                      "left": "fable", "right": "sonnet", "judge": "sonnet",
                      "judge_canonical_model": "claude-sonnet-5", "winner": "A",
                      "reason": "r", "error": None, "cost_usd": 0.4, "elapsed_s": 1.0})
        prior.append({"kind": "judgment", "operator": "summarize", "input": "delos",
                      "left": "sonnet", "right": "fable", "judge": "sonnet",
                      "judge_canonical_model": None, "winner": None, "reason": None,
                      "error": "OperatorError: spend limit", "cost_usd": None, "elapsed_s": 1.0})
        out = tmp_path / "study.json"
        out.write_text(_json.dumps({"started_at": "x", "records": prior}))

        gen_calls: list = []
        judge_calls: list = []

        async def fake_gen(op, arm, inp, *, timeout):
            gen_calls.append((op, arm.name))
            return _gen(arm.name)

        async def fake_judge(op, inp, left, right, *, judge_model, judge_name, timeout):
            judge_calls.append((left["arm"], right["arm"], judge_name))
            return {"kind": "judgment", "operator": op, "input": inp["name"],
                    "left": left["arm"], "right": right["arm"], "judge": judge_name,
                    "judge_canonical_model": "m", "winner": "B", "reason": "r",
                    "error": None, "cost_usd": 0.4, "elapsed_s": 1.0}

        monkeypatch.setattr(sts, "_run_operator", fake_gen)
        monkeypatch.setattr(sts, "_judge", fake_judge)
        await sts.run(["summarize"], ["delos"], out_path=out, inputs_path=inputs_path, timeout=1)
        # fable/sonnet generations complete on file; the FAILED haiku one is retried.
        assert gen_calls == [("summarize", "haiku")]
        # fable-involving pairs: (f,s),(s,f),(f,h),(h,f); (f,s) done -> 3 dispatched,
        # including the RETRY of the failed (s,f).
        assert sorted(judge_calls) == [
            ("fable", "haiku", "sonnet"), ("haiku", "fable", "sonnet"), ("sonnet", "fable", "sonnet"),
        ]


def _jrec(op: str, left: str, right: str, winner: str | None, inp: str = "delos",
          judge: str = "sonnet", error: str | None = None) -> dict:
    return {"kind": "judgment", "operator": op, "input": inp, "left": left,
            "right": right, "judge": judge, "judge_canonical_model": "m",
            "winner": winner, "reason": "r", "error": error,
            "cost_usd": 0.4 if winner else None, "elapsed_s": 1.0}


def _grec(op: str, arm: str, inp: str = "delos", *, cost: float = 1.0,
          model: str | None = None, grounding_val: float = 1.0) -> dict:
    model = model or {"fable": "claude-fable-5", "sonnet": "claude-sonnet-5",
                      "haiku": "claude-haiku-4-5"}[arm]
    return {"kind": "generation", "operator": op, "arm": arm, "input": inp,
            "requested_model": None, "canonical_model": model,
            "family_ok": arm in model, "elapsed_s": 1.0, "cost_usd": cost,
            "input_tokens": 1, "output_tokens": 100,
            "output": {"summary": "s"}, "error": None, "grounding": grounding_val}


class TestAnalyze:
    def test_verdicts_ratios_and_judge_error_exclusion(self) -> None:
        recs = [_grec("summarize", a, cost=c)
                for a, c in (("fable", 1.0), ("sonnet", 0.3), ("haiku", 0.1))]
        recs += [
            _jrec("summarize", "fable", "haiku", "A"),
            _jrec("summarize", "haiku", "fable", "B"),
            _jrec("summarize", "fable", "sonnet", "B"),
            _jrec("summarize", "sonnet", "fable", "A"),
            _jrec("summarize", "fable", "sonnet", None, error="spend limit"),  # excluded
        ]
        a = analyze(recs)
        e = a["operators"]["summarize"]
        assert e["fable_void"] is False
        assert e["arms"]["sonnet"]["cost_ratio_vs_fable"] == pytest.approx(0.3)
        assert e["cells"]["haiku"]["incumbent_preferred"] == 2
        assert e["cells"]["haiku"]["verdict"] == "REFUTED"      # fable 2/2 > 0.60
        assert e["cells"]["sonnet"]["arm_preferred"] == 2
        assert e["cells"]["sonnet"]["n"] == 2                    # error row excluded
        assert e["cells"]["sonnet"]["verdict"] == "NOT_REFUTED"
        assert a["judge_cost_usd"] == pytest.approx(1.6)

    def test_fable_family_violation_voids_cells(self) -> None:
        recs = [_grec("generate", a) for a in ("fable", "sonnet", "haiku")]
        recs[0] = {**recs[0], "canonical_model": "claude-sonnet-5", "family_ok": False}
        recs += [_jrec("generate", "fable", "haiku", "A")]
        e = analyze(recs)["operators"]["generate"]
        assert e["fable_void"] is True
        assert e["cells"]["haiku"]["verdict"] == "VOID"

    def test_no_judgments_is_no_data_and_second_judge_agreement_pairs(self) -> None:
        recs = [_grec("aggregate", a) for a in ("fable", "sonnet", "haiku")]
        e = analyze(recs)["operators"]["aggregate"]
        assert e["cells"]["sonnet"]["verdict"] == "NO_DATA"
        recs2 = [_grec("generate", a) for a in ("fable", "sonnet", "haiku")]
        recs2 += [_jrec("generate", "fable", "haiku", "A", judge="sonnet"),
                  _jrec("generate", "fable", "haiku", "A", judge="fable"),
                  _jrec("generate", "haiku", "fable", "B", judge="sonnet"),
                  _jrec("generate", "haiku", "fable", "A", judge="fable")]
        e2 = analyze(recs2)["operators"]["generate"]
        assert e2["judge_agreement"] == {"n": 2, "rate": 0.5}
