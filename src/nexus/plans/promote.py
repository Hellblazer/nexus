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

from nexus.db.t2.plan_library import PlanLibrary

__all__ = ["GateVerdict", "evaluate_gates", "DEFAULT_MIN_USE_COUNT",
           "DEFAULT_MIN_SUCCESS_RATE", "DEFAULT_MIN_DESCRIPTION_CHARS"]

DEFAULT_MIN_USE_COUNT = 3
DEFAULT_MIN_SUCCESS_RATE = 0.80
DEFAULT_MIN_DESCRIPTION_CHARS = 20


@dataclass(frozen=True)
class GateVerdict:
    """Result of evaluating promotion gates against a plan.

    ``plan`` is the raw row dict (from :meth:`PlanLibrary.get_plan`)
    when the plan exists, ``None`` otherwise.
    """
    passed: bool
    reasons: list[str] = field(default_factory=list)
    plan: dict[str, Any] | None = None


def evaluate_gates(
    library: PlanLibrary,
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
    # into PlanLibrary's returned row (``frozen=True`` protects the
    # reference, not the object it points to).
    return GateVerdict(passed=not reasons, reasons=reasons, plan=dict(plan))
