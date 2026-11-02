# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shakedown 7.94.0, ANSWER class C (live row 985): a ``budget_usd=0.03`` run whose terminal extract+generate bundle
cost 0.506 USD still ran all five coverage-assessment calls, and its run summary said ``skipped_budget_calls 0``.

The report read that as the budget not gating the assessment. It does gate it, and the row was correct: the boundary
sits BEFORE the plan's trailing operator run (nexus-jypmj), so the 0.506 was spent after the assessment, which was
judged against what was left when it started (0.0133 of 0.03 once the inline planner's 0.0167 was taken). The five
calls cost 0.0118 in all and fit. These tests pin the live shape (a costly trailing reduction does not cut the
assessment that precedes it), the converse the report expected at the runner (an allowance a plan step has already
spent ends the run before the boundary, so no call is made), and the cut-short case the run summary exists for (an
allowance that carries one call and not the rest).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nexus.operators import llm_types
from nexus.operators.llm_types import DispatchUsage
from nexus.plans.coverage_ledger import CoverageBoundary
from nexus.plans.run_summary import RunSummaryInputs, build_run_summary
from tests.test_rdr232_coverage_concurrent import Canary, _reply
from tests.test_rdr232_coverage_ledger import PLAN, _cov, _request, _run


def _usage(cost: float) -> DispatchUsage:
    return DispatchUsage(
        model="haiku", cost_usd=cost, input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
        cache_read_input_tokens=0, duration_ms=1, duration_api_ms=1, num_turns=1,
    )


def _summary(cov: Any) -> dict[str, Any]:
    return build_run_summary(
        RunSummaryInputs(request=None), outcome="ok", plan_id=7, step_count=5, duration_ms=100, cost_usd=None,
        trace=True, coverage=cov, terminal_reached=True,
    )


def _model(cost: float):
    """An ``operator_check`` model that supports every facet and reports *cost* to the ambient usage sink."""
    async def model(args: dict) -> dict:
        sink = llm_types._ambient_usage_sink.get()
        assert sink is not None
        await asyncio.sleep(0.01)
        sink.append(_usage(cost))
        return _reply(args["items"], args["check_instruction"])
    return model


@pytest.mark.asyncio
async def test_a_costly_terminal_reduction_does_not_cut_the_assessment_that_runs_before_it() -> None:
    """Live row 985's shape: the allowance left at the boundary (0.0133) covers the five calls (0.0024 each, 0.012),
    and the terminal ``generate`` (here 0.5) is dispatched after them, past the allowance. Nothing was skipped, and
    the generate's cost is recorded after every assessment call."""
    class Spender(Canary):
        async def __call__(self, tool: str, args: dict) -> dict:
            if tool == "generate":
                sink = llm_types._ambient_usage_sink.get()
                assert sink is not None
                sink.append(_usage(0.5))
            return await super().__call__(tool, args)

    d = Spender(_model(0.0024), vector=3, graph=0, other=0)
    result = await _run(d, CoverageBoundary(request=_request(5), max_facets_per_call=1, call_timeout_s=5.0), budget_usd_remaining=0.0133)
    a = _cov(result).ledger.assessment
    assert a.calls == 5 and a.skipped_budget_calls == 0 and a.outcome == "ok", (a.calls, a.skipped_budget_calls, a.outcome)
    assert d.tools.count("generate") == 1 and d.tools.index("generate") > max(i for i, t in enumerate(d.tools) if t == "operator_check"), "dispatch order"
    assert sum(r.cost_usd or 0.0 for r in result.step_records) > 0.5, "non-vacuity: the run did overrun the allowance"


@pytest.mark.asyncio
async def test_an_allowance_spent_before_the_boundary_ends_the_run_before_any_assessment_call() -> None:
    """A plan step before the boundary spends 1.0 against 0.5: the runner's pre-segment check stops the run, the
    boundary is never reached, and no assessment call is made."""
    plan = {"steps": [
        *PLAN["steps"][:3],
        {"tool": "operator_check", "args": {"items": "$step3.contents", "check_instruction": "plan check"}},
        PLAN["steps"][2],
        PLAN["steps"][3],
    ]}

    async def model(args: dict) -> dict:
        sink = llm_types._ambient_usage_sink.get()
        assert sink is not None
        sink.append(_usage(1.0))
        return {"ok": True, "evidence": []}

    d = Canary(model, vector=3, graph=0, other=0)
    result = await _run(
        d, CoverageBoundary(request=_request(5), max_facets_per_call=1, call_timeout_s=5.0), plan=plan, budget_usd_remaining=0.5,
    )
    assert result.budget_exhausted_at_step is not None and result.coverage is None
    assert d.tools.count("operator_check") == 1, "only the plan's own check ran; no assessment call"
    assert "generate" not in d.tools


@pytest.mark.asyncio
async def test_an_allowance_that_carries_one_call_and_not_the_rest_is_a_budget_skip_in_the_run_summary() -> None:
    """0.0015 left at the boundary, 0.004 a call: the first call runs (the accepted overshoot), the others are held
    and then skipped. The stored run summary carries the per-call count and ``failure_cause`` ``budget_skip``."""
    d = Canary(_model(0.004), vector=3, graph=0, other=0)
    result = await _run(d, CoverageBoundary(request=_request(5), max_facets_per_call=1, call_timeout_s=5.0), budget_usd_remaining=0.0015)
    a = _cov(result).ledger.assessment
    assert a.calls == 1 and a.skipped_budget_calls == 4, (a.calls, a.skipped_budget_calls)
    stored = _summary(_cov(result))["coverage"]
    assert stored["skipped_budget_calls"] == 4 and stored["failure_cause"] == "budget_skip" and stored["assessment_outcome"] == "partial_failure"
    assert stored["unassessed_by_cause"]["budget_skip"] == 4
