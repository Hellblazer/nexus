"""Termination semantics for nx_plan_audit (nexus-ll7zm).

The behaviour under test is a loop breaker, so every test here asks the
same question in a different state: can this audit hold a plan hostage?
"""

from __future__ import annotations

import pytest

from nexus.plans.audit_rounds import (
    BLOCKS_PLANNING,
    DISCOVER_AT_IMPLEMENTATION,
    MAX_BLOCKING_ROUNDS,
    NOT_READY,
    READY,
    RESIDUALS_ONLY,
    blocking_round_cap,
    partition_findings,
    render_audit_report,
    resolve_verdict,
    round_prompt,
)


def _finding(classification: str, title: str = "t", severity: str = "high") -> dict:
    return {"classification": classification, "title": title, "severity": severity}


class TestPartition:
    def test_splits_by_classification_preserving_order(self) -> None:
        findings = [
            _finding(BLOCKS_PLANNING, "a"),
            _finding(DISCOVER_AT_IMPLEMENTATION, "b"),
            _finding(BLOCKS_PLANNING, "c"),
        ]
        blocking, residual = partition_findings(findings)
        assert [f["title"] for f in blocking] == ["a", "c"]
        assert [f["title"] for f in residual] == ["b"]

    @pytest.mark.parametrize(
        "raw",
        ["blocks-planning", "BLOCKS_PLANNING", "  Blocks-Planning  "],
        ids=["lower", "underscore", "padded"],
    )
    def test_classification_is_normalised(self, raw: str) -> None:
        blocking, residual = partition_findings([{"classification": raw}])
        assert len(blocking) == 1 and not residual

    @pytest.mark.parametrize(
        "finding",
        [{}, {"classification": None}, {"classification": "whatever"}, "not a dict"],
        ids=["missing", "none", "unknown", "not-a-dict"],
    )
    def test_unclassified_defaults_to_blocking(self, finding: object) -> None:
        """The conservative default: nobody said, so nobody may downgrade it.

        Defaulting the other way would make this module's own failure
        mode "silently drop real blockers", which is worse than the loop
        it exists to break.
        """
        blocking, residual = partition_findings([finding])
        assert len(blocking) == 1 and not residual


class TestRoundCap:
    def test_unstated_budget_uses_the_default_cap(self) -> None:
        assert blocking_round_cap(0) == MAX_BLOCKING_ROUNDS

    def test_a_budget_may_tighten_the_cap(self) -> None:
        assert blocking_round_cap(1) == 1

    def test_a_budget_may_not_widen_the_cap(self) -> None:
        """Otherwise a caller buys extra rounds by declaring a big budget."""
        assert blocking_round_cap(99) == MAX_BLOCKING_ROUNDS


class TestVerdict:
    def test_blockers_within_the_cap_block(self) -> None:
        verdict, blocking, residual, reason = resolve_verdict(
            [_finding(BLOCKS_PLANNING)], round_number=1
        )
        assert verdict == NOT_READY
        assert len(blocking) == 1 and not residual and not reason

    def test_residuals_alone_never_block(self) -> None:
        verdict, blocking, residual, _ = resolve_verdict(
            [_finding(DISCOVER_AT_IMPLEMENTATION)] * 3, round_number=1
        )
        assert verdict == READY
        assert not blocking and len(residual) == 3

    def test_no_findings_is_ready(self) -> None:
        verdict, blocking, residual, _ = resolve_verdict([], round_number=1)
        assert verdict == READY and not blocking and not residual

    def test_second_round_may_still_block(self) -> None:
        verdict, _, _, _ = resolve_verdict(
            [_finding(BLOCKS_PLANNING)], round_number=MAX_BLOCKING_ROUNDS
        )
        assert verdict == NOT_READY

    def test_past_the_cap_nothing_blocks(self) -> None:
        """The loop breaker. Round 3 cannot return NOT READY."""
        verdict, blocking, residual, reason = resolve_verdict(
            [_finding(BLOCKS_PLANNING, "still blocking, allegedly")],
            round_number=MAX_BLOCKING_ROUNDS + 1,
        )
        assert verdict == RESIDUALS_ONLY
        assert not blocking
        assert len(residual) == 1
        assert reason and str(MAX_BLOCKING_ROUNDS) in reason

    def test_past_the_cap_the_verdict_is_not_ready_either(self) -> None:
        """RESIDUALS-ONLY, never READY: the findings were real.

        A READY verdict here would erase them, which is the opposite
        failure from the loop and just as bad.
        """
        verdict, _, _, _ = resolve_verdict(
            [_finding(BLOCKS_PLANNING)], round_number=9
        )
        assert verdict == RESIDUALS_ONLY
        assert verdict != READY

    def test_past_the_cap_with_no_findings_is_ready(self) -> None:
        """A clean re-audit past the cap is clean, not RESIDUALS-ONLY.

        RESIDUALS-ONLY with nothing to record would tell the reader to
        record something that does not exist.
        """
        verdict, blocking, residual, reason = resolve_verdict(
            [], round_number=MAX_BLOCKING_ROUNDS + 1
        )
        assert verdict == READY
        assert not blocking and not residual and not reason

    @pytest.mark.parametrize("bad_round", [0, -3])
    def test_a_non_positive_round_gets_round_one_semantics(
        self, bad_round: int
    ) -> None:
        """The clamp direction matters: a caller bug must land within the
        cap (may block), never silently past it (cannot block)."""
        verdict, _, _, _ = resolve_verdict(
            [_finding(BLOCKS_PLANNING)], round_number=bad_round
        )
        assert verdict == NOT_READY

    def test_a_tight_budget_caps_earlier(self) -> None:
        verdict, _, residual, reason = resolve_verdict(
            [_finding(BLOCKS_PLANNING)], round_number=2, budget_rounds=1
        )
        assert verdict == RESIDUALS_ONLY
        assert len(residual) == 1
        assert "budget" in reason

    def test_the_models_own_verdict_is_not_an_input(self) -> None:
        """resolve_verdict takes findings and a round, never a proposed verdict.

        Pinning the signature: a defect-finder must not be the thing that
        decides when to stop finding defects.
        """
        import inspect

        params = set(inspect.signature(resolve_verdict).parameters)
        assert params == {"findings", "round_number", "budget_rounds"}


class TestReport:
    def test_residuals_carry_the_recording_instruction(self) -> None:
        report = render_audit_report(
            READY, [], [_finding(DISCOVER_AT_IMPLEMENTATION, "fetch missing")]
        )
        assert "fetch missing" in report
        assert "Record these residuals" in report
        assert "do NOT re-plan" in report

    def test_blocking_and_residual_are_shown_separately(self) -> None:
        report = render_audit_report(
            NOT_READY,
            [_finding(BLOCKS_PLANNING, "wrong sequencing")],
            [_finding(DISCOVER_AT_IMPLEMENTATION, "missing flag")],
        )
        assert BLOCKS_PLANNING in report and DISCOVER_AT_IMPLEMENTATION in report
        assert report.index("wrong sequencing") < report.index("missing flag")

    def test_the_cap_reason_is_shown_so_a_reader_is_not_misled(self) -> None:
        _, blocking, residual, reason = resolve_verdict(
            [_finding(BLOCKS_PLANNING)], round_number=3
        )
        report = render_audit_report(
            RESIDUALS_ONLY, blocking, residual, reason=reason, round_number=3
        )
        assert "round 3" in report
        assert "cap" in report

    def test_report_states_the_round(self) -> None:
        assert "(round 2)" in render_audit_report(READY, [], [], round_number=2)

    def test_empty_report_says_so(self) -> None:
        assert "No findings." in render_audit_report(READY, [], [])


class TestPrompts:
    def test_within_the_cap_the_prompt_names_the_budget(self) -> None:
        text = round_prompt(1)
        assert "round 1" in text and str(MAX_BLOCKING_ROUNDS) in text

    def test_past_the_cap_the_prompt_forbids_arguing_for_another_round(self) -> None:
        text = round_prompt(MAX_BLOCKING_ROUNDS + 1)
        assert "residuals" in text
        assert "do not argue for another round" in text
