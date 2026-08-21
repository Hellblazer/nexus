# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-196 .p2c A/B measurement helpers (nexus-nyry9.16).

Pure functions only — no I/O, no dispatch. The live harness
(``tests/integration/test_rdr_196_p2c_ab_measurement.py``) imports
:func:`total_dispatch_cost` to compute a per-record total; this module
exists separately so that computation is unit-testable without a live
engine or a real ``claude -p`` call.

## The bug this module fixes (review round-2, T2 [23144] Critical #3)

A record's ``ambient_usage`` list is populated by wrapping the WHOLE
``nx_answer()`` call in ``nexus.operators.dispatch.ambient_usage_sink``.
That sink is a single ``contextvars.ContextVar`` — nesting a SECOND
``ambient_usage_sink(...)`` context (which ``plans/runner.py`` does
around every ISOLATED plan step, but NOT around a fused ``dispatch_bundle``
call, which instead takes an explicit ``usage_sink=`` kwarg) SHADOWS the
outer sink for the duration of that inner call. The isolated-step
dispatch's usage lands in the runner's own local list and is correctly
folded into the returned envelope's per-step ``StepRecord`` — and
therefore into ``result["cost_usd"]`` — but it NEVER reaches the outer
harness's ``ambient_usage`` list.

Net effect: for an inline-planner run whose produced plan's downstream
work happens to fuse into a bundle, ``sum(ambient_usage costs)`` is
correct (both the planner's own dispatch and the bundle's dispatch reach
the outer sink). For a run whose downstream work is an ISOLATED step,
``sum(ambient_usage costs)`` silently UNDERCOUNTS by exactly the
downstream step's real cost — the outer sink only ever sees the
planner's own dispatch (always first, always unshadowed, since nothing
wraps the planner's own call in a nested sink) and never learns about a
downstream isolated step.

## The fix

``ambient_usage[0].cost_usd`` is ALWAYS the planner's own dispatch cost
(chronologically first, structurally never nested inside another sink).
``result["cost_usd"]`` ALWAYS correctly sums every downstream
``StepRecord``'s cost — bundle or isolated, that accounting happens
independently of the harness's outer sink at all, via
``plans/runner.py``'s own ``_record_step`` bookkeeping. So::

    total = ambient_usage[0].cost_usd + result["cost_usd"]

is correct in BOTH cases: for a bundle-downstream run this reproduces
the (already-correct) ``sum(ambient_usage)`` value; for an
isolated-downstream run it recovers the cost the naive sum silently
dropped.

For a NON-planner record (a plain operator-tiering run, no
``force_dynamic``), there is no separate "planner" dispatch — the
single StepRecord's own cost, read straight off ``result["cost_usd"]``,
is already the whole story, and ``ambient_usage`` (when present at all)
is redundant with it.
"""
from __future__ import annotations

from typing import Any


def total_dispatch_cost(record: dict[str, Any], *, is_planner_row: bool) -> float | None:
    """Compute the correct total real-money cost for one measurement
    *record* (one dict from the harness's ``results["operators"][op][arm]``
    or ``results["planner"][arm]`` list).

    Args:
        record: One run record, carrying ``cost_usd`` (the StepRecord-
            summed downstream cost) and ``ambient_usage`` (a list of
            ``DispatchUsage``-shaped dicts, chronological order).
        is_planner_row: True for an inline-planner (``force_dynamic``)
            row, where ``ambient_usage[0]`` is the planner's own,
            otherwise-uncaptured dispatch cost that must be added to
            ``result["cost_usd"]``. False for a plain operator-tiering
            row, where ``cost_usd`` alone is already the total (no
            separate untracked dispatch exists).

    Returns:
        The total cost in USD, or ``None`` when the inputs don't carry
        enough information to know (mirrors the project's "unknown is
        never fabricated as zero" convention).
    """
    step_cost = record.get("cost_usd")
    if not is_planner_row:
        return step_cost

    ambient = record.get("ambient_usage") or []
    if not ambient:
        # No planner dispatch was captured at all (e.g. force_dynamic
        # somehow bypassed claude_dispatch entirely, or a killed/failed
        # run) — whatever step_cost says is all we know.
        return step_cost
    planner_cost = ambient[0].get("cost_usd")
    if planner_cost is None and step_cost is None:
        return None
    return (planner_cost or 0.0) + (step_cost or 0.0)


def total_dispatch_count(record: dict[str, Any], *, is_planner_row: bool) -> int:
    """Number of real ``claude -p`` dispatches ``total_dispatch_cost``
    accounts for in *record* — the planner's own call (if
    ``is_planner_row``) plus one per executed ``StepRecord`` with a
    non-``None`` ``model`` (an SQL/no-op step contributes zero real
    dispatches)."""
    steps = record.get("steps") or []
    n = sum(1 for s in steps if s.get("model") is not None)
    if is_planner_row and (record.get("ambient_usage") or []):
        n += 1
    return n


def stitch_run_id(
    before_ids: set[Any],
    after_rows: list[dict[str, Any]],
    question: str,
) -> int | None:
    """Identify the single ``nx_answer_runs`` row a just-completed
    dispatch produced (round-2 review gap, T2 [23144] Significant #4:
    "no run_id stitched per record").

    The .p2c A/B harness reuses ONE question string across every
    arm/index of a given operator (and, for the inline-planner loop,
    across baseline vs candidate at the same index) — so ``question``
    text alone cannot disambiguate which persisted row belongs to which
    record. This disambiguates by PROCESS ORDER instead: *before_ids* is
    the set of ``row["id"]`` values that existed in the SAME ephemeral
    T2 tenant immediately BEFORE the dispatch was issued; *after_rows*
    is the ``rows`` list from a query taken immediately AFTER the
    dispatch returned. The correct row is the one with a matching
    question whose id was NOT in *before_ids*.

    Returns ``None`` — never guesses — when zero or more than one such
    candidate exists (a genuinely ambiguous or not-yet-visible case),
    rather than silently attributing cost/telemetry to the wrong run.
    """
    candidates = [
        row for row in after_rows
        if row.get("question") == question and row.get("id") not in before_ids
    ]
    if len(candidates) != 1:
        return None
    return candidates[0].get("id")
