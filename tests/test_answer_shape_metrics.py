# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-232 Phase 1 (nexus-jypmj.1.1): the seven answer-shape measures, each on a hand-built
result with a known answer.

The measures are RDR-232 section 5 "Evaluation before tuning": eligible-plan recall, wrong-shape
admission rate, primary-source coverage, facet evidence coverage, scope leakage, repair yield and
cost, terminal completeness. A measure that needs a later phase's mechanism reports
``NotMeasurable`` (rendered "not measurable before Phase N"), never zero: a zero would read as a
perfect score for a run nobody could have scored.

Pure arithmetic: no engine, no model call.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.answer_shape_metrics import (
    MEASURE_NAMES,
    EvidenceItem,
    NotMeasurable,
    RepairOutcome,
    RunObservation,
    TerminalOutcome,
    compute_measures,
    measures_to_json,
    observations_from_json,
)
from bench.schema import Query

LOOKUP = "lookup:hybrid-factual-lookup"
THEMES = "query:abstract-themes"
RESEARCH = "research:default"


def _q(qid: str, **over) -> Query:
    base = dict(
        qid=qid, category="factual", text=qid, answer_kind="synthesis",
        required_sources=[], required_facets=[], allowed_corpora=["rdr"],
        unacceptable_plans=[LOOKUP],
    )
    base.update(over)
    return Query(**base)


def _item(corpus="rdr", source="", facets=frozenset(), reason="") -> EvidenceItem:
    return EvidenceItem(source=source, corpus=corpus, facets=frozenset(facets), permitted_reason=reason)


class TestEligiblePlanRecall:
    def test_share_of_requests_with_an_acceptable_candidate(self) -> None:
        qs = [_q("a"), _q("b"), _q("c"), _q("d")]
        obs = [
            RunObservation("a", selected_plan=RESEARCH, candidate_plans=[LOOKUP, RESEARCH]),  # survives
            RunObservation("b", selected_plan=LOOKUP, candidate_plans=[LOOKUP]),              # only the wrong shape
            RunObservation("c", selected_plan=None, candidate_plans=[]),                      # no candidate at all
            RunObservation("d", selected_plan=THEMES, candidate_plans=[THEMES]),              # survives
        ]
        m = compute_measures(qs, obs)["eligible_plan_recall"]
        assert m.value == pytest.approx(2 / 4)
        assert m.n == 4

    def test_a_request_with_no_candidate_observation_is_not_counted(self) -> None:
        qs = [_q("a"), _q("b")]
        obs = [RunObservation("a", candidate_plans=[RESEARCH]), RunObservation("b")]
        m = compute_measures(qs, obs)["eligible_plan_recall"]
        assert m.value == pytest.approx(1.0)
        assert m.n == 1


class TestWrongShapeAdmissionRate:
    def test_share_of_requests_whose_selected_plan_is_unacceptable(self) -> None:
        qs = [_q("a"), _q("b"), _q("c"), _q("d")]
        obs = [
            RunObservation("a", selected_plan=LOOKUP, candidate_plans=[LOOKUP, RESEARCH]),
            RunObservation("b", selected_plan=RESEARCH, candidate_plans=[RESEARCH, LOOKUP]),
            RunObservation("c", selected_plan=None, candidate_plans=[]),
            RunObservation("d", selected_plan=LOOKUP, candidate_plans=[LOOKUP]),
        ]
        m = compute_measures(qs, obs)["wrong_shape_admission_rate"]
        assert m.value == pytest.approx(2 / 4)
        assert m.n == 4
        # The wrong shape was in the candidate pool for a, b and d even where it was not chosen.
        assert m.detail["unacceptable_in_candidates"] == 3

    def test_a_plan_miss_is_not_an_admission(self) -> None:
        qs = [_q("a")]
        obs = [RunObservation("a", selected_plan=None, candidate_plans=[])]
        assert compute_measures(qs, obs)["wrong_shape_admission_rate"].value == 0.0


class TestPrimarySourceCoverage:
    def test_mean_share_of_required_sources_represented(self) -> None:
        qs = [
            _q("a", required_sources=["RDR-084@ART"]),
            _q("b", required_sources=["RDR-070", "RDR-095", "RDR-089", "RDR-086"]),
            _q("c", required_sources=[]),  # nothing required: not scored
        ]
        obs = [
            RunObservation("a", evidence=[_item(corpus="code"), _item(source="RDR-084@ART")]),
            RunObservation("b", evidence=[_item(source="RDR-070"), _item(source="RDR-095"),
                                          _item(source="RDR-070")]),
            RunObservation("c", evidence=[_item()]),
        ]
        m = compute_measures(qs, obs)["primary_source_coverage"]
        assert m.value == pytest.approx((1.0 + 2 / 4) / 2)
        assert m.n == 2

    def test_no_primary_item_scores_zero_not_unmeasured(self) -> None:
        # The incident shape: hundreds of references, none from the named source.
        qs = [_q("a", required_sources=["RDR-084@ART"])]
        obs = [RunObservation("a", evidence=[_item(corpus="code")] * 339)]
        assert compute_measures(qs, obs)["primary_source_coverage"].value == 0.0


class TestFacetEvidenceCoverage:
    def test_mean_share_of_required_facets_with_evidence(self) -> None:
        qs = [
            _q("a", required_facets=["arch", "num", "gate", "open"]),
            _q("b", required_facets=["x", "y"]),
        ]
        obs = [
            RunObservation("a", evidence=[_item(facets={"arch"}), _item(facets={"arch", "gate"}),
                                          _item(facets={"unrelated"})]),
            RunObservation("b", evidence=[_item(facets={"x", "y"})]),
        ]
        m = compute_measures(qs, obs)["facet_evidence_coverage"]
        assert m.value == pytest.approx((2 / 4 + 1.0) / 2)
        assert m.n == 2

    def test_evidence_with_no_facet_labels_is_not_measurable_before_phase_3(self) -> None:
        qs = [_q("a", required_facets=["arch"])]
        obs = [RunObservation("a", evidence=[EvidenceItem(source="", corpus="rdr", facets=None)])]
        m = compute_measures(qs, obs)["facet_evidence_coverage"]
        assert isinstance(m.value, NotMeasurable)
        assert m.value.phase == 3
        assert str(m.value) == "not measurable before Phase 3"


class TestScopeLeakage:
    def test_share_of_evidence_outside_allowed_corpora_without_a_reason(self) -> None:
        qs = [_q("a", allowed_corpora=["rdr"]), _q("b", allowed_corpora=["rdr", "code"])]
        obs = [
            # 1 rdr + 3 code under scope rdr: 3 of 4 leak.
            RunObservation("a", evidence=[_item("rdr"), _item("code"), _item("code"), _item("code")]),
            # all inside the allowed set: no leak.
            RunObservation("b", evidence=[_item("rdr"), _item("code")]),
        ]
        m = compute_measures(qs, obs)["scope_leakage"]
        assert m.value == pytest.approx((3 / 4 + 0.0) / 2)
        assert m.detail["leaked_items"] == 3
        assert m.detail["total_items"] == 6

    def test_a_recorded_permitted_reason_is_not_a_leak(self) -> None:
        qs = [_q("a", allowed_corpora=["rdr"])]
        obs = [RunObservation("a", evidence=[
            _item("rdr"), _item("code", reason="rdr-implements-link"), _item("code"),
        ])]
        m = compute_measures(qs, obs)["scope_leakage"]
        assert m.value == pytest.approx(1 / 3)


class TestRepairYieldAndCost:
    def test_not_measurable_before_phase_3_without_a_repair_observation(self) -> None:
        qs = [_q("a")]
        m = compute_measures(qs, [RunObservation("a")])["repair_yield_and_cost"]
        assert isinstance(m.value, NotMeasurable) and m.value.phase == 3

    def test_yield_is_repaired_over_missing_and_cost_is_the_mean(self) -> None:
        qs = [_q("a"), _q("b")]
        obs = [
            RunObservation("a", repair=RepairOutcome(missing=3, repaired=2, cost_usd=0.10)),
            RunObservation("b", repair=RepairOutcome(missing=1, repaired=0, cost_usd=0.30)),
        ]
        m = compute_measures(qs, obs)["repair_yield_and_cost"]
        assert m.value == pytest.approx(2 / 4)
        assert m.detail["mean_cost_usd"] == pytest.approx(0.20)
        assert m.n == 2


class TestTerminalCompleteness:
    def test_not_measurable_before_phase_3_without_a_terminal_observation(self) -> None:
        m = compute_measures([_q("a")], [RunObservation("a")])["terminal_completeness"]
        assert isinstance(m.value, NotMeasurable) and m.value.phase == 3

    def test_addressed_or_disclosed_over_required(self) -> None:
        qs = [_q("a", required_facets=["f1", "f2", "f3", "f4"]), _q("b", required_facets=["g1", "g2"])]
        obs = [
            RunObservation("a", terminal=TerminalOutcome(addressed=["f1", "f2"], disclosed_missing=["f3"])),
            RunObservation("b", terminal=TerminalOutcome(addressed=["g1", "g2"], disclosed_missing=[])),
        ]
        m = compute_measures(qs, obs)["terminal_completeness"]
        assert m.value == pytest.approx((3 / 4 + 1.0) / 2)

    def test_a_facet_neither_addressed_nor_disclosed_counts_against(self) -> None:
        qs = [_q("a", required_facets=["f1", "f2"])]
        obs = [RunObservation("a", terminal=TerminalOutcome(addressed=["f1"], disclosed_missing=[]))]
        assert compute_measures(qs, obs)["terminal_completeness"].value == pytest.approx(0.5)


class TestComputeMeasures:
    def test_all_seven_measures_are_always_reported(self) -> None:
        out = compute_measures([_q("a")], [RunObservation("a")])
        assert tuple(out) == MEASURE_NAMES
        assert len(MEASURE_NAMES) == 7

    def test_an_observation_for_an_unknown_qid_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="nope"):
            compute_measures([_q("a")], [RunObservation("nope")])

    def test_an_unobserved_measure_is_not_a_zero(self) -> None:
        # No request was observed at all: every measure reports unmeasured, none reports 0.0.
        out = compute_measures([_q("a", required_sources=["R"])], [])
        for name, m in out.items():
            assert isinstance(m.value, NotMeasurable), name
        assert out["eligible_plan_recall"].n == 0

    def test_json_round_trip_and_rendering(self, tmp_path: Path) -> None:
        qs = [_q("a", required_sources=["RDR-084@ART"], required_facets=["arch"])]
        obs = [RunObservation(
            "a", selected_plan=LOOKUP, candidate_plans=[LOOKUP],
            evidence=[_item("code"), _item("rdr", source="RDR-084@ART", facets={"arch"})],
        )]
        path = tmp_path / "obs.json"
        path.write_text(json.dumps([o.to_dict() for o in obs]), encoding="utf-8")
        again = observations_from_json(path)
        assert again == obs
        rendered = json.loads(measures_to_json(compute_measures(qs, again)))
        assert rendered["wrong_shape_admission_rate"]["value"] == 1.0
        assert rendered["repair_yield_and_cost"]["value"] == "not measurable before Phase 3"
