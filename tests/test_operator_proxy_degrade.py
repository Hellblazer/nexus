# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the RDR-196 .p2a harness's synthetic degradation +
scoring pipeline (nexus-nyry9.14) against FIXED, hand-authored structured
outputs — no dispatch, no network. Fixture-level stand-in for what a real
strong-tier run's output shape looks like for each in-scope operator.

Proves the harness's own plumbing (``score`` + each ``degrade_*``) before
any money is spent on a live control: self-agreement (same output fed
twice) scores >= threshold, and a degraded copy scores < threshold, for
every one of the 6 in-scope operators.
"""
from __future__ import annotations

import pytest

from bench.operator_proxy import (
    DEGRADERS,
    FIXTURE_ITEMS,
    GRADED_DEGRADERS,
    THRESHOLDS,
    WRONG_GROUNDING_DEGRADERS,
    DegenerateGroundingError,
    degrade_check,
    degrade_check_wrong_grounding,
    degrade_extract,
    degrade_filter,
    degrade_groupby,
    degrade_rank,
    degrade_verify,
    degrade_verify_wrong_grounding,
    score,
)

_UNIVERSE = [it["id"] for it in FIXTURE_ITEMS]


def _self_and_degraded_scores(operator_name: str, output: dict) -> tuple[float, float]:
    positive = score(operator_name, output, output)
    degraded = DEGRADERS[operator_name](output)
    negative = score(operator_name, output, degraded)
    return positive, negative


class TestFilterDegrade:
    _OUTPUT = {
        "items": [{"id": "rdr-095"}, {"id": "rdr-089"}],
        "rationale": [
            {"id": i, "reason": "r"} for i in _UNIVERSE
        ],
    }

    def test_self_agreement_meets_threshold(self) -> None:
        pos, _ = _self_and_degraded_scores("operator_filter", self._OUTPUT)
        assert pos >= THRESHOLDS["operator_filter"]

    def test_degraded_fails_threshold(self) -> None:
        _, neg = _self_and_degraded_scores("operator_filter", self._OUTPUT)
        assert neg < THRESHOLDS["operator_filter"]

    def test_degrade_is_the_true_complement(self) -> None:
        degraded = degrade_filter(self._OUTPUT)
        degraded_ids = {it["id"] for it in degraded["items"]}
        kept_ids = {it["id"] for it in self._OUTPUT["items"]}
        assert degraded_ids == set(_UNIVERSE) - kept_ids
        assert degraded_ids.isdisjoint(kept_ids)


class TestGroupbyDegrade:
    _OUTPUT = {
        "groups": [
            {"key_value": "catalog", "items": [{"id": "rdr-049"}, {"id": "rdr-053"}]},
            {"key_value": "storage", "items": [{"id": "rdr-086"}, {"id": "rdr-061"}]},
            {"key_value": "taxonomy", "items": [{"id": "rdr-070"}]},
            {"key_value": "observability", "items": [{"id": "rdr-067"}]},
            {"key_value": "hooks", "items": [{"id": "rdr-095"}, {"id": "rdr-089"}]},
        ],
    }

    def test_self_agreement_meets_threshold(self) -> None:
        pos, _ = _self_and_degraded_scores("operator_groupby", self._OUTPUT)
        assert pos >= THRESHOLDS["operator_groupby"]

    def test_degraded_fails_threshold(self) -> None:
        _, neg = _self_and_degraded_scores("operator_groupby", self._OUTPUT)
        assert neg < THRESHOLDS["operator_groupby"]

    def test_degrade_merges_to_one_group(self) -> None:
        degraded = degrade_groupby(self._OUTPUT)
        assert len(degraded["groups"]) == 1
        merged_ids = {it["id"] for it in degraded["groups"][0]["items"]}
        assert merged_ids == set(_UNIVERSE)


class TestGroupbyDegradeSingleGroupEdgeCase:
    """Code-review finding (2026-08-21): merging into one group is a NO-OP
    when the real output already IS one group -- the negative control
    would silently pass. Covers the split-to-singletons fallback branch."""

    _SINGLE_GROUP_OUTPUT = {
        "groups": [
            {"key_value": "everything", "items": [{"id": i} for i in _UNIVERSE]},
        ],
    }

    def test_degraded_still_fails_threshold_when_real_output_is_one_group(self) -> None:
        _, neg = _self_and_degraded_scores("operator_groupby", self._SINGLE_GROUP_OUTPUT)
        assert neg < THRESHOLDS["operator_groupby"], (
            f"negative control scored {neg} on a single-group real output -- "
            f"the merge-to-one-group degradation would have been a no-op here"
        )

    def test_degrade_splits_to_singletons_not_merge(self) -> None:
        degraded = degrade_groupby(self._SINGLE_GROUP_OUTPUT)
        assert len(degraded["groups"]) == len(_UNIVERSE)
        for group in degraded["groups"]:
            assert len(group["items"]) == 1

    def test_self_agreement_still_meets_threshold(self) -> None:
        pos, _ = _self_and_degraded_scores("operator_groupby", self._SINGLE_GROUP_OUTPUT)
        assert pos >= THRESHOLDS["operator_groupby"]


class TestRankDegrade:
    _OUTPUT = {"ranked": [f"[{i}] title" for i in _UNIVERSE]}

    def test_self_agreement_scores_1(self) -> None:
        pos, _ = _self_and_degraded_scores("operator_rank", self._OUTPUT)
        assert pos == 1.0
        assert pos >= THRESHOLDS["operator_rank"]

    def test_reversed_scores_exactly_minus_1_and_fails_threshold(self) -> None:
        _, neg = _self_and_degraded_scores("operator_rank", self._OUTPUT)
        assert abs(neg - (-1.0)) < 1e-9
        assert neg < THRESHOLDS["operator_rank"]

    def test_degrade_is_exact_reversal(self) -> None:
        degraded = degrade_rank(self._OUTPUT)
        assert degraded["ranked"] == list(reversed(self._OUTPUT["ranked"]))


class TestExtractDegrade:
    _OUTPUT = {
        "extractions": [
            {"id": i, "category": c, "year": 2026}
            for i, c in zip(_UNIVERSE, [
                "catalog", "catalog", "storage", "storage",
                "taxonomy", "observability", "hooks", "hooks",
            ])
        ],
    }

    def test_self_agreement_meets_threshold(self) -> None:
        pos, _ = _self_and_degraded_scores("operator_extract", self._OUTPUT)
        assert pos == 1.0
        assert pos >= THRESHOLDS["operator_extract"]

    def test_degraded_fails_threshold(self) -> None:
        _, neg = _self_and_degraded_scores("operator_extract", self._OUTPUT)
        assert neg < THRESHOLDS["operator_extract"]

    def test_degrade_keeps_only_one_field_per_record(self) -> None:
        degraded = degrade_extract(self._OUTPUT)
        for rec in degraded["extractions"]:
            assert len(rec) == 1


class TestCheckDegrade:
    _OUTPUT = {
        "ok": False,
        "evidence": [
            {"item_id": "rdr-049", "quote": "q", "role": "contradicts"},
            {"item_id": "rdr-086", "quote": "q", "role": "supports"},
        ],
    }

    def test_self_agreement_meets_threshold(self) -> None:
        pos, _ = _self_and_degraded_scores("operator_check", self._OUTPUT)
        assert pos == 1.0
        assert pos >= THRESHOLDS["operator_check"]

    def test_degraded_fails_threshold(self) -> None:
        _, neg = _self_and_degraded_scores("operator_check", self._OUTPUT)
        assert neg < THRESHOLDS["operator_check"]

    def test_degrade_inverts_ok_and_flips_roles(self) -> None:
        degraded = degrade_check(self._OUTPUT)
        assert degraded["ok"] is True
        roles = {e["role"] for e in degraded["evidence"]}
        assert roles == {"supports", "contradicts"}
        # every role actually flipped from the original
        original_by_id = {e["item_id"]: e["role"] for e in self._OUTPUT["evidence"]}
        for e in degraded["evidence"]:
            assert e["role"] != original_by_id[e["item_id"]]


class TestVerifyDegrade:
    _OUTPUT = {
        "verified": True,
        "reason": "directly supported",
        "citations": ["rdr-086 chash section"],
    }

    def test_self_agreement_meets_threshold(self) -> None:
        pos, _ = _self_and_degraded_scores("operator_verify", self._OUTPUT)
        assert pos == 1.0
        assert pos >= THRESHOLDS["operator_verify"]

    def test_degraded_fails_threshold(self) -> None:
        _, neg = _self_and_degraded_scores("operator_verify", self._OUTPUT)
        assert neg < THRESHOLDS["operator_verify"]

    def test_degrade_inverts_verified_and_clears_citations(self) -> None:
        degraded = degrade_verify(self._OUTPUT)
        assert degraded["verified"] is False
        assert degraded["citations"] == []


def test_all_six_in_scope_operators_have_a_degrader() -> None:
    assert set(DEGRADERS) == set(THRESHOLDS)


class TestWrongGroundingDegrade:
    """substantive-critic CRITICAL finding (2026-08-21): degrade_check /
    degrade_verify always flip the boolean verdict TOGETHER with the
    grounding, so bool_plus_jaccard's 0.5 cap on a bool mismatch means
    the Jaccard/grounding term alone has never been shown to fail below
    threshold. These hold the verdict FIXED and empty ONLY the grounding,
    isolating that term. Pure math, no live dispatch needed: for any
    nonempty real grounding, bool_plus_jaccard(v, v, S, {}) =
    0.5*1 + 0.5*0 = 0.5, which is < 0.70 for both operators -- if it were
    NOT below threshold, the composite or threshold would be wrong (per
    the bead's own non-vacuity doctrine)."""

    _CHECK_OUTPUT = {
        "ok": False,
        "evidence": [
            {"item_id": "rdr-049", "quote": "q", "role": "contradicts"},
            {"item_id": "rdr-086", "quote": "q", "role": "supports"},
        ],
    }
    _VERIFY_OUTPUT = {
        "verified": True,
        "reason": "directly supported",
        # Two citations (not one) so graded_degrade_verify's "drop 1 of N"
        # is a genuine partial loss, distinct from the wrong-grounding
        # case's full wipe -- a single-citation fixture would make the
        # two degradations identical and the graded test degenerate.
        "citations": ["rdr-086 chash section", "chash index paragraph"],
    }

    def test_check_wrong_grounding_keeps_verdict(self) -> None:
        degraded = degrade_check_wrong_grounding(self._CHECK_OUTPUT)
        assert degraded["ok"] == self._CHECK_OUTPUT["ok"]
        assert degraded["evidence"] == []

    def test_check_wrong_grounding_scores_below_threshold(self) -> None:
        score_val = score("operator_check", self._CHECK_OUTPUT, degrade_check_wrong_grounding(self._CHECK_OUTPUT))
        assert score_val is not None
        assert score_val == 0.5  # bool agrees (1.0*0.5) + empty-vs-nonempty jaccard (0.0*0.5)
        assert score_val < THRESHOLDS["operator_check"], (
            "the evidence/grounding term alone was never shown to fail "
            "below threshold -- composite or threshold is wrong"
        )

    def test_verify_wrong_grounding_keeps_verdict(self) -> None:
        degraded = degrade_verify_wrong_grounding(self._VERIFY_OUTPUT)
        assert degraded["verified"] == self._VERIFY_OUTPUT["verified"]
        assert degraded["citations"] == []

    def test_verify_wrong_grounding_scores_below_threshold(self) -> None:
        score_val = score("operator_verify", self._VERIFY_OUTPUT, degrade_verify_wrong_grounding(self._VERIFY_OUTPUT))
        assert score_val is not None
        assert score_val == 0.5
        assert score_val < THRESHOLDS["operator_verify"], (
            "the citation/grounding term alone was never shown to fail "
            "below threshold -- composite or threshold is wrong"
        )

    def test_wrong_grounding_covers_exactly_check_and_verify(self) -> None:
        assert set(WRONG_GROUNDING_DEGRADERS) == {"operator_check", "operator_verify"}

    def test_check_wrong_grounding_raises_on_already_empty_evidence(self) -> None:
        """Round-2 substantive-critic finding: an already-empty real
        grounding must NOT silently produce an empty-vs-empty vacuous
        pass -- it must raise an explicit, named marker instead."""
        already_empty = {"ok": True, "evidence": []}
        with pytest.raises(DegenerateGroundingError, match="operator_check"):
            degrade_check_wrong_grounding(already_empty)

    def test_verify_wrong_grounding_raises_on_already_empty_citations(self) -> None:
        already_empty = {"verified": True, "reason": "x", "citations": []}
        with pytest.raises(DegenerateGroundingError, match="operator_verify"):
            degrade_verify_wrong_grounding(already_empty)

    def test_degenerate_error_is_never_a_silent_1_0_score(self) -> None:
        """Prove the failure mode this guard prevents: WITHOUT the guard,
        scoring an empty-vs-empty grounding pair would silently return
        1.0 (a vacuous pass) via exact_membership_jaccard's "both empty
        -> agree" convention. The guard raises before that comparison
        can happen at all."""
        already_empty_check = {"ok": True, "evidence": []}
        # Confirm the underlying convention really would look like a
        # pass if nothing guarded it (documents WHY the guard exists).
        vacuous_score = score("operator_check", already_empty_check, already_empty_check)
        assert vacuous_score == 1.0
        # And confirm the guard actually intervenes before that happens
        # for the wrong-grounding degrader specifically.
        with pytest.raises(DegenerateGroundingError):
            degrade_check_wrong_grounding(already_empty_check)


class TestGradedDegrade:
    """substantive-critic SIGNIFICANT finding (2026-08-21): every
    DEGRADERS entry is caricature-level (full inversion/merge/reversal/
    field-wipe) -- proven to reject obvious garbage, never shown to
    detect a realistic small quality loss. These perturb ONE element and
    report the resulting score (not necessarily below threshold -- the
    point is to STATE the smallest loss this gate can see, for .p2c to
    judge against a real cheap-vs-strong quality gap)."""

    _FILTER_OUTPUT = {
        "items": [{"id": "rdr-095"}, {"id": "rdr-089"}],
        "rationale": [{"id": i, "reason": "r"} for i in _UNIVERSE],
    }
    _GROUPBY_OUTPUT = {
        "groups": [
            {"key_value": "catalog", "items": [{"id": "rdr-049"}, {"id": "rdr-053"}]},
            {"key_value": "storage", "items": [{"id": "rdr-086"}, {"id": "rdr-061"}]},
            {"key_value": "taxonomy", "items": [{"id": "rdr-070"}]},
            {"key_value": "observability", "items": [{"id": "rdr-067"}]},
            {"key_value": "hooks", "items": [{"id": "rdr-095"}, {"id": "rdr-089"}]},
        ],
    }
    _RANK_OUTPUT = {"ranked": [f"[{i}] title" for i in _UNIVERSE]}
    _EXTRACT_OUTPUT = {
        "extractions": [
            {"id": i, "category": c, "year": 2026}
            for i, c in zip(_UNIVERSE, [
                "catalog", "catalog", "storage", "storage",
                "taxonomy", "observability", "hooks", "hooks",
            ])
        ],
    }
    _CHECK_OUTPUT = TestWrongGroundingDegrade._CHECK_OUTPUT
    _VERIFY_OUTPUT = TestWrongGroundingDegrade._VERIFY_OUTPUT

    _CASES = [
        ("operator_filter", _FILTER_OUTPUT),
        ("operator_groupby", _GROUPBY_OUTPUT),
        ("operator_rank", _RANK_OUTPUT),
        ("operator_extract", _EXTRACT_OUTPUT),
        ("operator_check", _CHECK_OUTPUT),
        ("operator_verify", _VERIFY_OUTPUT),
    ]

    def test_graded_score_is_strictly_between_full_degrade_and_perfect(self) -> None:
        """A near-miss must score worse than self-agreement (1.0 case) but
        is NOT required to fail threshold -- that's the whole point: it
        measures the gate's SENSITIVITY, not another pass/fail gate."""
        report: dict[str, float] = {}
        for operator_name, fixed_output in self._CASES:
            graded = GRADED_DEGRADERS[operator_name](fixed_output)
            graded_score = score(operator_name, fixed_output, graded)
            full_degraded = DEGRADERS[operator_name](fixed_output)
            full_score = score(operator_name, fixed_output, full_degraded)
            assert graded_score is not None and full_score is not None
            report[operator_name] = graded_score
            # graded (near-miss) must not score WORSE than the full
            # caricature degradation -- it's a smaller perturbation.
            assert graded_score >= full_score, (
                f"{operator_name}: graded near-miss ({graded_score}) scored "
                f"worse than the full degradation ({full_score}) -- a "
                f"smaller perturbation should never look more wrong"
            )
            # and it must not score a perfect 1.0 -- the perturbation must
            # be visible to the metric at all.
            assert graded_score < 1.0, (
                f"{operator_name}: a one-element perturbation was completely "
                f"invisible to the metric (scored 1.0) -- smallest "
                f"detectable loss for this gate is larger than one element"
            )
        # Not asserted further -- this IS the "score band" report per
        # operator; see T2 nexus_rdr/196-phase2-quality-proxy for the
        # recorded numbers from this exact table.
        assert set(report) == set(THRESHOLDS)

    def test_graded_degraders_cover_all_six_in_scope_operators(self) -> None:
        assert set(GRADED_DEGRADERS) == set(THRESHOLDS)
