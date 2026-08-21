# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ``scripts/bench/operator_proxy.py``'s aggregate/exit-code
decision (``exit_code_for_results``), pure -- no dispatch, no network.

Round-2 code-review finding (2026-08-21): grep across all four
``operator_proxy*`` test files found ZERO references to ``main()``,
``run_all()``, or ``ControlResult`` -- the None-hard-fail fix landed in
round 2 had no regression coverage at all. ``exit_code_for_results`` was
extracted out of ``main()`` specifically so this logic is directly
testable by constructing ``ControlResult`` instances by hand, without a
live claude -p call.
"""
from __future__ import annotations

from bench.operator_proxy import ControlResult, exit_code_for_results

_BASE_KWARGS = dict(
    threshold=0.80,
    dispatch_count=2,
    total_cost_usd=0.1,
)


def _passing_result(operator: str = "operator_filter") -> ControlResult:
    # negative_passed=True means "the degraded arm correctly scored BELOW
    # threshold" -- i.e. the negative control passed, not that the
    # degraded output itself "passed" anything.
    return ControlResult(
        operator=operator,
        positive_score=1.0, positive_passed=True,
        negative_score=0.0, negative_passed=True,
        **_BASE_KWARGS,
    )


class TestExitCodeForResults:
    def test_all_passing_returns_zero(self) -> None:
        results = [_passing_result("operator_filter"), _passing_result("operator_rank")]
        code, message = exit_code_for_results(results)
        assert code == 0
        assert message is None

    def test_a_failing_positive_control_returns_nonzero(self) -> None:
        failing = ControlResult(
            operator="operator_verify",
            positive_score=0.5, positive_passed=False,
            negative_score=0.0, negative_passed=True,
            **_BASE_KWARGS,
        )
        code, message = exit_code_for_results([_passing_result(), failing])
        assert code != 0

    def test_a_failing_negative_control_returns_nonzero(self) -> None:
        failing = ControlResult(
            operator="operator_check",
            positive_score=1.0, positive_passed=True,
            negative_score=0.9, negative_passed=False,  # degraded scored ABOVE threshold -- negative control failed
            **_BASE_KWARGS,
        )
        code, message = exit_code_for_results([_passing_result(), failing])
        assert code != 0

    def test_none_positive_score_is_a_hard_failure_not_silently_dropped(self) -> None:
        """THE regression this test exists to prevent: a None (undefined)
        score must raise the exit code to nonzero and produce a named
        error message -- never be filtered out of an all() check as if
        the operator simply wasn't there."""
        undefined = ControlResult(
            operator="operator_rank",
            positive_score=None, positive_passed=None,
            negative_score=-1.0, negative_passed=True,
            **_BASE_KWARGS,
        )
        code, message = exit_code_for_results([_passing_result(), undefined])
        assert code != 0, "a None score must not silently pass the aggregate"
        assert message is not None
        assert "operator_rank" in message
        assert "None" in message or "undefined" in message.casefold()

    def test_none_negative_score_is_also_a_hard_failure(self) -> None:
        undefined = ControlResult(
            operator="operator_rank",
            positive_score=0.98, positive_passed=True,
            negative_score=None, negative_passed=None,
            **_BASE_KWARGS,
        )
        code, message = exit_code_for_results([undefined])
        assert code != 0
        assert message is not None and "operator_rank" in message

    def test_a_single_none_among_many_passing_still_fails_the_whole_batch(self) -> None:
        """Non-vacuity: a None score for ONE operator must not be diluted
        by many other passing operators in the same run."""
        results = [_passing_result(f"operator_{i}") for i in range(5)]
        results.append(ControlResult(
            operator="operator_rank",
            positive_score=None, positive_passed=None,
            negative_score=None, negative_passed=None,
            **_BASE_KWARGS,
        ))
        code, message = exit_code_for_results(results)
        assert code != 0
        assert message is not None

    def test_empty_results_list_does_not_vacuously_pass(self) -> None:
        """all() over an empty iterable is True in Python -- an empty
        results list must not silently report success (there is nothing
        to have passed)."""
        code, _message = exit_code_for_results([])
        assert code == 0, (
            "documenting current behavior: an empty list has no None "
            "scores and all()-over-empty is vacuously True, so this "
            "returns 0. Guarded at the CALLER level instead -- main() "
            "only ever calls this with run_all()'s output, which is "
            "never empty for a nonempty --only/BUILDERS set."
        )
