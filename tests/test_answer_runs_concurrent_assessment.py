# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.17: ``answer-runs --steps`` does not double-count overlapping assessment calls.

The coverage assessment's calls run at the same time, each its own step record. ``by_plan.median_elapsed_ms`` is a run's
step time, and summing the assessment steps' ``elapsed_ms`` as if they ran one after the other reports far more than
the run took. A run that carries the summary's ``assessment_latency_ms`` is counted with the assessment stage once.
"""
from __future__ import annotations

from nexus.commands.answer_runs import _step_breakdown


def _step(index: int, operator: str, elapsed_ms: int, source: str = "llm") -> dict:
    return {"step_index": index, "operator": operator, "source": source, "cost_usd": 0.0, "elapsed_ms": elapsed_ms, "ok": True}


def _row(*, summary: dict | None, plan_id: int = 7) -> dict:
    """A 2-step plan (search 100 ms, generate 1000 ms) plus four overlapping assessment calls of 25 s each that
    took 30 s of wall clock together."""
    steps = [_step(0, "search", 100, "sql"), _step(1, "generate", 1_000)]
    steps += [_step(2 + i, "operator_check", 25_000) for i in range(4)]
    row: dict = {"id": 1, "plan_id": plan_id, "step_count": 2, "cost_usd": 0.0, "steps": steps}
    if summary is not None:
        row["run_summary"] = summary
    return row


def test_overlapping_assessment_steps_count_as_the_stage_once() -> None:
    row = _row(summary={"kind": "nx_answer", "coverage": {"assessment_latency_ms": 30_000}})
    out = _step_breakdown([row])
    assert out["by_plan"]["7"]["median_elapsed_ms"] == 100 + 1_000 + 30_000, "not 101_100, the sum of calls that overlapped"
    # per-operator rows stay what they say: each call's own time
    assert out["by_operator"]["operator_check"]["total_elapsed_ms"] == 100_000
    assert out["by_operator"]["operator_check"]["count"] == 4


def test_serial_assessment_steps_are_not_reduced_below_their_sum() -> None:
    """A run whose calls did not overlap (stage wall time above the sum, repair included) keeps the sum."""
    row = _row(summary={"kind": "nx_answer", "coverage": {"assessment_latency_ms": 500_000}})
    assert _step_breakdown([row])["by_plan"]["7"]["median_elapsed_ms"] == 100 + 1_000 + 100_000


def test_a_run_without_the_summary_latency_keeps_the_plain_sum() -> None:
    """Nothing says how long the stage took, so the sum (an upper bound) is reported unchanged."""
    for summary in (None, {"kind": "nx_answer"}, {"coverage": {"assessment_latency_ms": None}}):
        assert _step_breakdown([_row(summary=summary)])["by_plan"]["7"]["median_elapsed_ms"] == 101_100


def test_a_plan_step_that_is_itself_an_operator_check_is_not_taken_for_an_assessment_call() -> None:
    """Only ``operator_check`` steps numbered past the plan's own steps are assessment calls."""
    row = _row(summary={"kind": "nx_answer", "coverage": {"assessment_latency_ms": 30_000}})
    row["steps"][1] = _step(1, "operator_check", 2_000)
    assert _step_breakdown([row])["by_plan"]["7"]["median_elapsed_ms"] == 100 + 2_000 + 30_000
