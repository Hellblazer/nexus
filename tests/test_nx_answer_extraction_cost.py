# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.9 (RDR-232 gate-flip critique, Observations): the structured request extraction's cost is part of the run.

The Haiku extraction call (about 0.002 USD, on about two thirds of real requests) was reported in the envelope's
``request_coverage.extraction`` only. ``cost_usd`` in the envelope, ``nx_answer_runs.cost_usd`` and the budget's
running spend all came from the steps and the inline planner, so ``nx answer-runs`` under-reported every run that
made the call and ``budget_usd`` ignored it.

Tenant-free: the extraction dispatch, the planner and the operator tools are stubbed, the T2 and telemetry layers
are mocks (the harness of ``tests/test_nx_answer_planner_cost.py``). The extraction dispatch is a fake that lands
a ``DispatchUsage`` in the sink it is handed, the way the real ``claude_dispatch`` does; nothing here is live.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import nexus.mcp.core as core
import nexus.plans.runner as runner
from nexus.commands.answer_runs import _step_breakdown
from nexus.mcp.core import nx_answer
from nexus.operators.dispatch import DispatchUsage, _ambient_usage_sink
from nexus.plans.match import Match

EXTRACT_COST = 0.004
PLANNER_COST = 0.50
STEP_COST = 0.03
QUESTION = "Tell me about plan matching."  # the first pass leaves the shape open: the extraction call is made


def _usage(cost: float):
    return DispatchUsage(
        model="claude-haiku-5-5", cost_usd=cost, input_tokens=5, output_tokens=2,
        cache_creation_input_tokens=0, cache_read_input_tokens=0, duration_ms=100,
        duration_api_ms=90, num_turns=1,
    )


def _match(plan_id: int, confidence):
    plan = {"steps": [
        {"tool": "search", "args": {"query": "$intent", "corpus": "knowledge"}},
        {"tool": "summarize", "args": {"content": "condense the results"}},
    ]}
    return Match(
        plan_id=plan_id, name="t", description="t", confidence=confidence, dimensions={}, tags="",
        plan_json=json.dumps(plan), required_bindings=[], optional_bindings=[],
        default_bindings={}, parent_dims=None,
    )


class _Extraction:
    """A fake ``claude_dispatch`` for the extraction: reports its cost in the sink, answers a valid result."""

    def __init__(self, cost: float | None = EXTRACT_COST) -> None:
        self.cost = cost
        self.calls = 0

    async def __call__(self, prompt, schema, *, timeout=300.0, usage_sink=None, **kw):  # noqa: ARG002
        self.calls += 1
        if self.cost is not None and usage_sink is not None:
            usage_sink.append(_usage(self.cost))
        return {"answer_kind": "lookup", "facets": ["plan matching"], "constraints": []}


async def _run(*, miss: bool = False, extraction: _Extraction | None = None, summary: bool = False, **kwargs):
    async def planner(question, scope="", max_steps=6, **kw):  # noqa: ARG001
        sink = _ambient_usage_sink.get()
        assert sink is not None
        sink.append(_usage(PLANNER_COST))
        return _match(0, None)

    async def stub_search(**kw):  # noqa: ARG001
        return {"ids": [f"{1:064x}"], "tumblers": [], "distances": [0.1], "collections": ["knowledge__x"]}

    async def stub_summarize(**kw):  # noqa: ARG001
        sink = _ambient_usage_sink.get()
        if sink is not None:
            sink.append(_usage(STEP_COST))
        return {"summary": "a real answer"}

    db = MagicMock()
    db.plans.save_plan = MagicMock(return_value=1)
    db.plans.get_plan = MagicMock(return_value={"id": 1})
    if summary:
        db.telemetry._supports_nx_answer_run_summary.return_value = True
    matches = [] if miss else [_match(1, 0.9)]
    fake = extraction if extraction is not None else _Extraction()
    with patch("nexus.plans.matcher.plan_match", return_value=matches), \
         patch("nexus.mcp.core._nx_answer_plan_miss", side_effect=planner), \
         patch("nexus.operators.dispatch.claude_dispatch", fake), \
         patch("nexus.mcp.core._t2_ctx") as t2_ctx, \
         patch("nexus.mcp.core._t2_index_write", lambda fn, **_kw: fn(db)), \
         patch("nexus.mcp.core.scratch", return_value="ok"), \
         patch("nexus.mcp_infra.get_t1_plan_cache", return_value=None), \
         patch.object(core, "search", stub_search), \
         patch.object(core, "operator_summarize", stub_summarize):
        t2_ctx.return_value.__enter__.return_value = db
        result = await nx_answer(question=QUESTION, structured=True, trace=True, **kwargs)
    return result, db, fake


def _recorded(db):
    for name in ("record_nx_answer_run_complete", "record_nx_answer_run"):
        mock = getattr(db.telemetry, name)
        if mock.call_args is not None:
            return mock.call_args.kwargs
    raise AssertionError("no nx_answer_runs record was written")


@pytest.mark.asyncio
async def test_a_plan_hit_run_cost_includes_the_extraction_call() -> None:
    result, db, fake = await _run()
    assert fake.calls == 1 and not db.plans.save_plan.called, "a library plan matched, and the extraction ran"
    extraction = result["request_coverage"]["extraction"]
    assert extraction["outcome"] == "ok" and extraction["cost_usd"] == pytest.approx(EXTRACT_COST)
    assert result["cost_usd"] == pytest.approx(STEP_COST + EXTRACT_COST), "the envelope's run cost carries the call"
    assert _recorded(db)["cost_usd"] == pytest.approx(STEP_COST + EXTRACT_COST), "so does the stored run"


@pytest.mark.asyncio
async def test_a_plan_miss_run_cost_is_steps_planner_and_extraction() -> None:
    result, db, _ = await _run(miss=True)
    assert db.plans.save_plan.called, "the inline planner ran: this is a plan miss"
    assert result["cost_usd"] == pytest.approx(STEP_COST + PLANNER_COST + EXTRACT_COST)
    assert _recorded(db)["cost_usd"] == pytest.approx(result["cost_usd"])


@pytest.mark.asyncio
async def test_a_call_that_reports_no_cost_leaves_the_run_cost_as_the_steps_alone() -> None:
    """Unknown is not zero and not added: an extraction with no known cost changes nothing."""
    result, db, fake = await _run(extraction=_Extraction(cost=None))
    assert fake.calls == 1 and result["request_coverage"]["extraction"]["cost_usd"] is None
    assert result["cost_usd"] == pytest.approx(STEP_COST) and _recorded(db)["cost_usd"] == pytest.approx(STEP_COST)


@pytest.mark.asyncio
async def test_the_extraction_cost_is_spent_against_budget_usd_before_the_plan_runs() -> None:
    """The running spend that ``plan_run`` is handed as ``budget_usd_remaining`` is the cap minus what the
    planner AND the extraction already paid."""
    seen: dict = {}
    real = runner.plan_run

    async def spy(match, bindings, **kw):
        seen.update(kw)
        return await real(match, bindings, **kw)

    with patch.object(runner, "plan_run", spy):
        result, _, _ = await _run(budget_usd=5.0)
    assert result["request_coverage"]["extraction"]["cost_usd"] == pytest.approx(EXTRACT_COST)
    assert seen["budget_usd_remaining"] == pytest.approx(5.0 - EXTRACT_COST)


@pytest.mark.asyncio
async def test_an_extraction_that_alone_exhausts_the_budget_stops_the_run_before_any_step() -> None:
    result, db, _ = await _run(extraction=_Extraction(cost=2.0), budget_usd=1.0)
    assert result["step_count"] == 0 and result["steps"] == [], "no plan step ran: the cap was spent by the call"
    assert "budget exhausted (cost)" in result["final_text"]
    assert _recorded(db)["cost_usd"] == pytest.approx(2.0)


def test_every_record_arm_reads_its_cost_from_the_one_sink_the_extraction_reports_to() -> None:
    """The extraction's usage joins ``_planner_usage_sink``, the sink every record arm and the budget seed already
    read, rather than a second one each arm would have to remember."""
    tree = ast.parse(Path(core.__file__).read_text())
    nx_answer = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "nx_answer")
    coverage_calls = [
        n for n in ast.walk(nx_answer)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_nx_answer_request_coverage"
    ]
    assert len(coverage_calls) == 1, f"the census examined {len(coverage_calls)} coverage call sites; there is one"
    (call,) = coverage_calls
    sinks = [k.value for k in call.keywords if k.arg == "usage_sink"]
    assert len(sinks) == 1 and isinstance(sinks[0], ast.Name) and sinks[0].id == "_planner_usage_sink"


# ── `nx answer-runs --steps` must not flag the extraction's share as a cost inconsistency ───────────────────


def _row(plan_id, run_cost, step_costs, *, extraction_cost="absent"):
    row = {
        "id": 1, "plan_id": plan_id, "cost_usd": run_cost,
        "steps": [{"operator": "summarize", "source": "llm", "cost_usd": c, "elapsed_ms": 10} for c in step_costs],
    }
    if extraction_cost != "absent":
        row["run_summary"] = {"request": {"extraction": {"outcome": "ok", "calls": 1, "cost_usd": extraction_cost}}}
    return row


def _violations(*rows):
    return _step_breakdown(list(rows))["cost_consistency_violations"]


def test_a_library_plan_run_agrees_when_its_summary_records_the_extraction_share() -> None:
    assert _violations(_row(7, STEP_COST + EXTRACT_COST, [STEP_COST], extraction_cost=EXTRACT_COST)) == []
    assert _violations(_row(7, EXTRACT_COST, [None], extraction_cost=EXTRACT_COST)) == [], "no step cost known: the call alone"


def test_the_extraction_share_is_exact_for_a_library_plan_so_an_over_count_is_still_caught() -> None:
    """A library plan has no planner share, so run cost = steps + the recorded extraction, and nothing above it."""
    assert len(_violations(_row(7, STEP_COST + EXTRACT_COST + 0.5, [STEP_COST], extraction_cost=EXTRACT_COST))) == 1
    assert len(_violations(_row(7, STEP_COST + 2 * EXTRACT_COST, [STEP_COST], extraction_cost=EXTRACT_COST))) == 1, "counted twice"
    assert len(_violations(_row(7, 0.01, [STEP_COST], extraction_cost=EXTRACT_COST))) == 1, "below its own steps is a drop"


def test_a_row_written_before_the_call_was_counted_still_agrees_with_its_steps() -> None:
    """7.88.0 recorded the call's cost in the summary and left it out of the run cost; those rows are in the field,
    and the check does not turn them into violations. (It can no longer tell that omission from a real drop.)"""
    assert _violations(_row(7, STEP_COST, [STEP_COST], extraction_cost=EXTRACT_COST)) == []


def test_a_run_whose_summary_records_no_extraction_cost_is_held_to_the_steps_as_before() -> None:
    assert _violations(_row(7, STEP_COST, [STEP_COST], extraction_cost=None)) == []
    assert len(_violations(_row(7, STEP_COST + EXTRACT_COST, [STEP_COST], extraction_cost=None))) == 1
    assert len(_violations(_row(7, STEP_COST + EXTRACT_COST, [STEP_COST]))) == 1, "and so is a row with no summary at all"
    assert _violations(_row(7, STEP_COST, [STEP_COST])) == []


def test_an_inline_planned_run_still_allows_its_planner_share_beside_the_extraction() -> None:
    assert _violations(_row(0, PLANNER_COST + STEP_COST + EXTRACT_COST, [STEP_COST], extraction_cost=EXTRACT_COST)) == []
    assert len(_violations(_row(0, 0.01, [STEP_COST], extraction_cost=EXTRACT_COST))) == 1, "below its own steps is a drop"


@pytest.mark.asyncio
async def test_the_row_a_plan_hit_run_writes_passes_the_answer_runs_consistency_check() -> None:
    """End to end: what ``nx_answer`` hands the telemetry store (run cost, steps, the run summary) is a row that
    ``nx answer-runs --steps`` finds consistent, for a run that made the extraction call."""
    result, db, _ = await _run(summary=True)
    written = _recorded(db)
    assert written["run_summary"]["request"]["extraction"]["cost_usd"] == pytest.approx(EXTRACT_COST)
    assert written["run_summary"]["run"]["cost_usd"] == pytest.approx(result["cost_usd"]), "the summary's run cost is the run's"
    row = {
        "id": 1, "plan_id": 1, "cost_usd": written["cost_usd"], "steps": written["steps"],
        "run_summary": written["run_summary"],
    }
    assert _violations(row) == []
