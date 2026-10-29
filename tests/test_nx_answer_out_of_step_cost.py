# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.1.20 (found by the RDR-232 canary): a plan-miss run's cost includes the inline planner.
nexus-jypmj.5: the run's out-of-step share (planner plus request extraction) is stored beside it and makes
``nx answer-runs --steps`` check the run cost from both sides.

The planner dispatch's cost lands in an ambient usage sink that was read only for the budget check, so
the envelope's ``cost_usd`` and the persisted ``nx_answer_runs.cost_usd`` summed plan steps alone.
Tenant-free: the planner and the operator tools are stubbed, the T2 and telemetry layers are mocks.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

PLANNER_COST = 0.50
STEP_COST = 0.03


def _usage(cost: float):
    from nexus.operators.dispatch import DispatchUsage

    return DispatchUsage(
        model="claude-sonnet-5-20260101", cost_usd=cost, input_tokens=5, output_tokens=2,
        cache_creation_input_tokens=0, cache_read_input_tokens=0, duration_ms=100,
        duration_api_ms=90, num_turns=1,
    )


def _match(plan_id: int, confidence):
    from nexus.plans.match import Match

    plan = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
        {"tool": "summarize", "args": {"content": "condense the results"}},
    ]}
    return Match(
        plan_id=plan_id, name="t", description="t", confidence=confidence, dimensions={}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


async def _run(*, miss: bool):
    from nexus.mcp import core as mcp_core
    from nexus.mcp.core import nx_answer
    from nexus.operators.dispatch import _ambient_usage_sink

    async def planner(question, scope="", max_steps=6, **kwargs):  # noqa: ARG001
        sink = _ambient_usage_sink.get()
        assert sink is not None
        sink.append(_usage(PLANNER_COST))
        return _match(0, None)

    async def stub_search(**kwargs):  # noqa: ARG001
        return {"ids": [f"{1:064x}"], "tumblers": [], "distances": [0.1], "collections": ["knowledge__x"]}

    async def stub_summarize(**kwargs):  # noqa: ARG001
        sink = _ambient_usage_sink.get()
        if sink is not None:
            sink.append(_usage(STEP_COST))
        return {"summary": "a real answer"}

    db = MagicMock()
    db.plans.save_plan = MagicMock(return_value=1)
    db.plans.get_plan = MagicMock(return_value={"id": 1})
    matches = [] if miss else [_match(1, 0.9)]
    with patch("nexus.plans.matcher.plan_match", return_value=matches), \
         patch("nexus.mcp.core._nx_answer_plan_miss", side_effect=planner), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
         patch.object(mcp_core, "search", stub_search), \
         patch.object(mcp_core, "operator_summarize", stub_summarize):
        t2_ctx.return_value.__enter__.return_value = db
        result = await nx_answer(question="q", structured=True, trace=True)
    return result, db


def _persisted_cost(db) -> float | None:
    for name in ("record_nx_answer_run_complete", "record_nx_answer_run"):
        mock = getattr(db.telemetry, name)
        if mock.call_args is not None:
            return mock.call_args.kwargs["cost_usd"]
    raise AssertionError("no nx_answer_runs record was written")


@pytest.mark.asyncio
async def test_a_plan_miss_envelope_and_run_record_include_the_share() -> None:
    result, db = await _run(miss=True)
    assert db.plans.save_plan.called, "the inline planner ran: this is a plan miss"
    steps_cost = sum(s["cost_usd"] for s in result["steps"] if s["cost_usd"] is not None)
    assert steps_cost == pytest.approx(STEP_COST)
    assert result["cost_usd"] == pytest.approx(PLANNER_COST + STEP_COST)
    assert _persisted_cost(db) == pytest.approx(result["cost_usd"]), "the stored run and the envelope agree"


@pytest.mark.asyncio
async def test_a_plan_hit_cost_is_the_steps_alone() -> None:
    result, db = await _run(miss=False)
    assert not db.plans.save_plan.called, "a library plan matched: no planner dispatch"
    assert result["cost_usd"] == pytest.approx(STEP_COST)
    assert _persisted_cost(db) == pytest.approx(STEP_COST)


def test_every_run_record_call_in_nx_answer_passes_the_share() -> None:
    """The record arms are many; one that omitted the planner cost would store a lower bound for the
    runs it ends (a census, with a non-vacuity floor)."""
    import ast
    from pathlib import Path

    import nexus.mcp.core as core

    tree = ast.parse(Path(core.__file__).read_text())
    nx_answer = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "nx_answer")
    calls = [
        n for n in ast.walk(nx_answer)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        and n.func.id in ("_nx_answer_record_complete", "_nx_answer_record_run")
    ]
    assert len(calls) >= 13, f"the census examined {len(calls)} call sites; the arms were 13 when it was written"
    missing = [c.lineno for c in calls if not any(k.arg == "out_of_step_cost_usd" for k in c.keywords)]
    assert missing == [], f"record calls without out_of_step_cost_usd at lines {missing}"


# ── `nx answer-runs --steps` must not flag the out-of-step share as a cost inconsistency ───────────────────────


def _row(plan_id, run_cost, step_costs):
    return {
        "id": 1, "plan_id": plan_id, "cost_usd": run_cost,
        "steps": [{"operator": "summarize", "source": "llm", "cost_usd": c, "elapsed_ms": 10} for c in step_costs],
    }


def test_the_cost_consistency_check_allows_the_planner_share_on_an_ad_hoc_run_only() -> None:
    from nexus.commands.answer_runs import _step_breakdown

    def violations(*rows):
        return _step_breakdown(list(rows))["cost_consistency_violations"]

    assert violations(_row(0, PLANNER_COST + STEP_COST, [STEP_COST])) == [], "inline-planned run: planner + steps"
    assert violations(_row(0, PLANNER_COST, [None])) == [], "planner cost known, no step cost known"
    assert len(violations(_row(7, PLANNER_COST + STEP_COST, [STEP_COST]))) == 1, "a library plan has no planner share"
    assert len(violations(_row(0, 0.01, [STEP_COST]))) == 1, "a run below its own steps is still a drop"
    assert violations(_row(0, STEP_COST, [STEP_COST])) == [], "rows written before the planner share agree as before"
    assert violations(_row(None, None, [None])) == [], "both unknown still agrees"


# ── nexus-jypmj.5: the out-of-step share is stored on the run, so the check is two-sided ─────────────────────


def _persisted_kwargs(db) -> dict:
    for name in ("record_nx_answer_run_complete", "record_nx_answer_run"):
        mock = getattr(db.telemetry, name)
        if mock.call_args is not None:
            return dict(mock.call_args.kwargs)
    raise AssertionError("no nx_answer_runs record was written")


@pytest.mark.asyncio
async def test_a_plan_miss_run_is_stored_with_its_share_and_a_plan_hit_that_made_no_out_of_step_call_without_one() -> None:
    result, db = await _run(miss=True)
    kwargs = _persisted_kwargs(db)
    assert kwargs["out_of_step_cost_usd"] == pytest.approx(PLANNER_COST)
    steps_cost = sum(s["cost_usd"] for s in result["steps"] if s["cost_usd"] is not None)
    assert kwargs["cost_usd"] == pytest.approx(kwargs["out_of_step_cost_usd"] + steps_cost), "the stored parts add up to the stored whole"

    _hit, db_hit = await _run(miss=False)
    assert "out_of_step_cost_usd" not in _persisted_kwargs(db_hit), "a plan-match hit that made no planner or extraction call has no share: nothing is sent"


def _share_row(plan_id, run_cost, step_costs, share):
    row = _row(plan_id, run_cost, step_costs)
    if share is not _ABSENT:
        row["out_of_step_cost_usd"] = share
    return row


_ABSENT = object()


def _violations(*rows):
    from nexus.commands.answer_runs import _step_breakdown

    return _step_breakdown(list(rows))["cost_consistency_violations"]


def test_with_the_share_stored_the_check_is_two_sided() -> None:
    both = PLANNER_COST + STEP_COST
    assert _violations(_share_row(0, both, [STEP_COST], PLANNER_COST)) == [], "planner + steps is the run cost"
    assert len(_violations(_share_row(0, both + 0.25, [STEP_COST], PLANNER_COST))) == 1, "an over-count the one-sided check accepted"
    assert len(_violations(_share_row(0, both - 0.10, [STEP_COST], PLANNER_COST))) == 1, "a run below planner + steps is a drop"
    assert len(_violations(_share_row(0, STEP_COST, [STEP_COST], PLANNER_COST))) == 1, "the planner's share is missing from the run cost"


def test_with_the_share_stored_and_no_known_step_cost_the_run_cost_is_the_share() -> None:
    assert _violations(_share_row(0, PLANNER_COST, [None], PLANNER_COST)) == []
    assert len(_violations(_share_row(0, PLANNER_COST * 2, [None], PLANNER_COST))) == 1
    assert len(_violations(_share_row(0, None, [None], PLANNER_COST))) == 1, "a stored planner cost with no run cost disagrees"


def test_a_stored_zero_share_is_a_measured_free_planner_not_an_absent_one() -> None:
    assert _violations(_share_row(0, STEP_COST, [STEP_COST], 0.0)) == []
    assert len(_violations(_share_row(0, STEP_COST + 0.25, [STEP_COST], 0.0))) == 1, "zero pins the upper bound"


@pytest.mark.parametrize("planner", [_ABSENT, None], ids=["key-absent", "null"])
def test_a_run_with_no_stored_share_keeps_the_one_sided_check(planner) -> None:
    """Rows written before the field (or by an older client, or read from an older engine) have no planner cost."""
    both = PLANNER_COST + STEP_COST
    assert _violations(_share_row(0, both, [STEP_COST], planner)) == []
    assert _violations(_share_row(0, both + 0.25, [STEP_COST], planner)) == [], "no upper bound is sound without the field"
    assert len(_violations(_share_row(0, 0.01, [STEP_COST], planner))) == 1, "a run below its own steps is still a drop"


@pytest.mark.parametrize("plan_id", [0, 7, None], ids=["inline-planned", "library-plan", "planner-error"])
def test_a_stored_share_makes_the_check_two_sided_for_any_plan_id(plan_id) -> None:
    """A library-plan run has a share too (the request extraction runs for a plan-match hit), and when the row
    stores it the check is exact: run == steps + share, whatever the plan_id."""
    assert _violations(_share_row(plan_id, PLANNER_COST + STEP_COST, [STEP_COST], PLANNER_COST)) == [], "run == steps + share"
    assert len(_violations(_share_row(plan_id, PLANNER_COST + STEP_COST + 0.25, [STEP_COST], PLANNER_COST))) == 1, "over-count"
    assert len(_violations(_share_row(plan_id, STEP_COST, [STEP_COST], PLANNER_COST))) == 1, "the share is missing from the run cost"
    assert len(_violations(_share_row(plan_id, 0.01, [STEP_COST], PLANNER_COST))) == 1, "below its own steps is a drop"


def test_a_library_plan_run_without_a_stored_share_keeps_its_exact_step_check() -> None:
    assert len(_violations(_share_row(7, PLANNER_COST + STEP_COST, [STEP_COST], _ABSENT))) == 1, "no share, no allowance"
    assert _violations(_share_row(7, STEP_COST, [STEP_COST], _ABSENT)) == []
