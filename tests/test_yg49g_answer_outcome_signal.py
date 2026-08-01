# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx_answer`` must record whether it ANSWERED, not whether it survived.

nexus-yg49g, observed on a live probe 2026-07-25: a run that returned

    "No matching evidence found ... The plan's retrieval steps returned zero
     results"

incremented the matched plan's ``success_count`` 1 -> 2.

MECHANISM. ``success=True`` was the fall-through after plan execution and
``success=False`` was reached only from an ``except``. So the counters were an
EXCEPTION counter wearing an outcome counter's name: "the DAG did not throw"
was recorded as "the plan answered the question". On the plan path the record
was emitted ~60 lines BEFORE ``final_text`` was extracted and before the
empty-retrieval guard that already knew the run had produced nothing — at that
point the outcome was not merely mis-decided, it was not yet computed.

WHY IT IS A BUG AND NOT A COSMETIC COUNTER:

  * ``plans/promote.py`` gates promotion on
    ``success/(success+failure) >= 0.80``. A plan that reliably retrieves
    nothing accrued a perfect record, so the gate preferentially promoted
    plans that return nothing quickly.
  * ``plans/matcher.py`` reads ``success_count`` when ranking, and the matcher
    is supposed to be able to decay always-failing plans (RDR-179 item 3). It
    could not: there were no failures to decay.
  * ``nx plan list`` / ``show`` present these to operators as quality signals.

THE FORCED CHOICE. The store is strictly binary — ``success_count`` /
``failure_count``, with a third "empty" column requiring a Liquibase changeset
and an engine tag. Recording NOTHING for a zero-evidence run was considered and
rejected: the promotion ratio would stay 1.0 and the gate would still promote.
So zero-evidence increments ``failure_count``. The lost distinction between
"errored" and "found nothing" is recoverable from telemetry, which stores the
final text, and from the ``nx_answer_empty_retrieval_guard`` event.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture()
def recorded() -> list[tuple[int, bool]]:
    """Capture every (plan_id, success) the code records."""
    return []


@pytest.fixture()
def capture_outcomes(recorded, monkeypatch: pytest.MonkeyPatch):
    from nexus.mcp import core

    def _rec(plan_id: int, *, success: bool) -> None:
        recorded.append((plan_id, success))

    monkeypatch.setattr(core, "_nx_answer_record_outcome", _rec)
    return recorded


# ── The predicate ───────────────────────────────────────────────────────────


class TestEmptyAnswerPredicate:
    @pytest.mark.parametrize("text", ["No results.", "no results", "  No Results.  ", ""])
    def test_recognises_the_synthesized_placeholders(self, text: str) -> None:
        from nexus.mcp.core import _nx_answer_text_is_empty

        assert _nx_answer_text_is_empty(text) is True

    @pytest.mark.parametrize("text", [
        "Found 3 results for 'x':\n  - a in c",
        # The over-matching trap: a REAL answer that discusses empty results.
        # A substring/startswith predicate would demote this to a failure,
        # swapping one wrong signal for another.
        "The query returned no results because the corpus was not indexed; "
        "reindex and retry.",
        "No matching papers, but three adjacent ones are listed below: ...",
    ])
    def test_does_not_over_match_real_answers(self, text: str) -> None:
        from nexus.mcp.core import _nx_answer_text_is_empty

        assert _nx_answer_text_is_empty(text) is False, (
            "a genuine answer that MENTIONS empty results must not be recorded "
            "as a failure"
        )


# ── Ordering: the record cannot precede the decision ────────────────────────


class TestOutcomeIsRecordedWhereItIsKnown:
    def test_success_is_not_recorded_before_the_empty_retrieval_guard(self) -> None:
        """Structural pin on the ordering bug.

        The original code recorded success at a point where ``final_text`` did
        not exist yet. Any future edit that moves the record back above the
        guard reintroduces the defect regardless of what the predicate says, so
        the ORDER is pinned directly.
        """
        import inspect

        from nexus.mcp import core

        src = inspect.getsource(core.nx_answer) if hasattr(core, "nx_answer") else ""
        if not src:
            import pathlib
            src = pathlib.Path(core.__file__).read_text(encoding="utf-8")

        guard = src.index("_nx_answer_is_empty_retrieval(result.steps)")
        after_guard = src[guard:]
        # The success record for the plan path must appear AFTER the guard.
        assert "_nx_answer_record_outcome(best.plan_id, success=True)" in after_guard, (
            "the plan path's success record is no longer downstream of the "
            "empty-retrieval guard — it cannot know the outcome up there "
            "(nexus-yg49g)"
        )

    def test_the_empty_retrieval_branch_records_a_failure(self) -> None:
        """The branch that detects zero evidence must record it as such."""
        import pathlib

        from nexus.mcp import core

        src = pathlib.Path(core.__file__).read_text(encoding="utf-8")
        guard = src.index("_nx_answer_is_empty_retrieval(result.steps)")
        # Look only at the guard's own body, not the whole rest of the file.
        body = src[guard:guard + 2000]
        assert "_nx_answer_record_outcome(best.plan_id, success=False)" in body, (
            "zero-evidence no longer records a failure — promote.py's "
            "success/(success+failure) gate would return to promoting plans "
            "that reliably return nothing (nexus-yg49g)"
        )


# ── The consumer that made the choice forced ────────────────────────────────


class TestPromotionGateSeesEmptyRuns:
    """The end-to-end reason zero-evidence had to count as a failure.

    evaluate_gates() reads the plan from a library, so these drive the real
    function through a stub library rather than guessing at a helper name — the
    first cut of this test looked for `is_eligible`/`eligible`/... and SKIPPED
    when it found none, which proved nothing while looking green.
    """

    @staticmethod
    def _lib(plan: dict):
        class _Lib:
            def get_plan(self, _pid):
                return plan
        return _Lib()

    def test_a_plan_that_always_returns_empty_is_refused(self) -> None:
        from nexus.plans.promote import evaluate_gates

        # Ten runs, every one zero-evidence. Under the OLD semantics these were
        # ten successes -> rate 1.0 -> promoted.
        verdict = evaluate_gates(
            self._lib({"use_count": 10, "success_count": 0, "failure_count": 10}),
            plan_id=1,
        )
        assert verdict.passed is False
        assert any("success_rate" in r or "rate" in r.lower() for r in verdict.reasons), (
            f"refused, but not for the rate — reasons: {verdict.reasons}"
        )

    def test_a_genuinely_useful_plan_still_passes_the_rate_gate(self) -> None:
        """Non-regression: the fix must not make promotion unreachable."""
        from nexus.plans.promote import evaluate_gates

        verdict = evaluate_gates(
            self._lib({"use_count": 10, "success_count": 9, "failure_count": 1}),
            plan_id=1,
        )
        rate_complaints = [r for r in verdict.reasons if "rate" in r.lower()]
        assert not rate_complaints, f"rate gate wrongly refused: {rate_complaints}"

    def test_no_completed_runs_is_refused_rather_than_treated_as_perfect(self) -> None:
        """Adjacent trap: 0/0 must not read as 100%."""
        from nexus.plans.promote import evaluate_gates

        verdict = evaluate_gates(
            self._lib({"use_count": 10, "success_count": 0, "failure_count": 0}),
            plan_id=1,
        )
        assert verdict.passed is False
        assert any("no completed runs" in r.lower() or "undefined" in r.lower()
                   for r in verdict.reasons), verdict.reasons
