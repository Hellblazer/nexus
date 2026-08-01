# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan promotion gates — RDR-079 P6 (nexus-rxk).

Closes RDR-078 Gap D (promotion lifecycle). Defines the purely-functional
gate evaluator used by ``nx plan promote``. The CLI is a thin wrapper
that surfaces the verdict to stdout and — when the gate passes and
``--dry-run`` is NOT set — writes a YAML template into the target tier.

Shipped defaults:
  * ``use_count >= 3`` — three actual runs.
  * ``success_count / (success_count + failure_count) >= 0.80`` —
    NOTE (nexus-yg49g, 2026-07-25): these counters used to be an EXCEPTION
    counter, not an outcome counter — ``nx_answer`` recorded success on any run
    that did not raise, including one whose retrieval steps returned zero
    evidence. A plan could therefore sit at 100% success and 0% usefulness, and
    this gate would happily promote it. Since that fix a zero-evidence run
    increments ``failure_count``, so this ratio means what it reads as. Counters
    recorded BEFORE that date still carry the old semantics — plan_etl copies
    them verbatim through migration — so a high rate on an old plan is not
    evidence of usefulness.
    eighty percent success rate.
  * description clarity — ``query`` is non-empty and ≥ 20 chars.

Gate thresholds are deliberately static here. Callers that need looser
thresholds for experimentation can override via function kwargs; the
CLI sticks to the shipped defaults so ``--dry-run`` verdicts are
reproducible without extra flags.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only (PEP 563 lazy): the runtime argument is whatever
    # the caller passes — production passes HttpPlanLibrary, the only
    # plan library left after nexus-i711w Stage 2 sub-stage A3 deleted
    # the SQLite PlanLibrary.
    from nexus.db.t2.http_plan_library import HttpPlanLibrary

__all__ = ["GateVerdict", "evaluate_gates", "DEFAULT_MIN_USE_COUNT",
           "DEFAULT_MIN_SUCCESS_RATE", "DEFAULT_MIN_DESCRIPTION_CHARS"]

DEFAULT_MIN_USE_COUNT = 3
DEFAULT_MIN_SUCCESS_RATE = 0.80
DEFAULT_MIN_DESCRIPTION_CHARS = 20


@dataclass(frozen=True)
class GateVerdict:
    """Result of evaluating promotion gates against a plan.

    ``plan`` is the raw row dict (from :meth:`HttpPlanLibrary.get_plan`)
    when the plan exists, ``None`` otherwise.
    """
    passed: bool
    reasons: list[str] = field(default_factory=list)
    plan: dict[str, Any] | None = None


def evaluate_gates(
    library: HttpPlanLibrary,
    plan_id: int,
    *,
    min_use_count: int = DEFAULT_MIN_USE_COUNT,
    min_success_rate: float = DEFAULT_MIN_SUCCESS_RATE,
    min_description_chars: int = DEFAULT_MIN_DESCRIPTION_CHARS,
) -> GateVerdict:
    """Evaluate gates against *plan_id* in *library*.

    Always collects every failing reason rather than short-circuiting —
    ``--dry-run`` consumers want the full failure list in one shot.
    """
    plan = library.get_plan(plan_id)
    if plan is None:
        return GateVerdict(
            passed=False,
            reasons=[f"plan {plan_id} not found"],
            plan=None,
        )

    reasons: list[str] = []

    use_count = int(plan.get("use_count") or 0)
    if use_count < min_use_count:
        reasons.append(
            f"use_count={use_count} below threshold {min_use_count}",
        )

    success_count = int(plan.get("success_count") or 0)
    failure_count = int(plan.get("failure_count") or 0)
    total_runs = success_count + failure_count
    if total_runs == 0:
        reasons.append(
            "success_rate undefined (no completed runs) — no evidence to promote",
        )
    else:
        rate = success_count / total_runs
        if rate < min_success_rate:
            reasons.append(
                f"success_rate={rate:.2f} below threshold {min_success_rate:.2f}",
            )

    query = str(plan.get("query") or "").strip()
    if len(query) < min_description_chars:
        reasons.append(
            f"description too short ({len(query)} chars, "
            f"need ≥ {min_description_chars})",
        )

    # Copy the row dict so mutations on the verdict don't reach back
    # into HttpPlanLibrary's returned row (``frozen=True`` protects the
    # reference, not the object it points to).
    return GateVerdict(passed=not reasons, reasons=reasons, plan=dict(plan))
