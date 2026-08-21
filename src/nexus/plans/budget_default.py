# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Derive the default ``budget_usd`` from recorded, POST-FLIP run history
(RDR-196 Phase 3 Step 0, nexus-nyry9.19).

Why this exists: ``nx_answer``'s shipped ``budget_usd=0.25`` predated any
measurement (RDR-196 § Risks; 196-R2 measured a single default-model
dispatch at 0.34-1.84 USD), so enforcing it would have refused most
multi-step plans. The default must instead be a NAMED percentile of
observed per-run cost, derived from real ``nx_answer_steps`` rows, with
the row count and tier configuration recorded so a later re-derivation
can tell whether the world moved.

R3 prevention (nx_plan_audit fold, 2026-08-20): pre-flip rows (recorded
while every operator dispatched at the strong tier) and post-flip rows
(.p2d, nexus-nyry9.17, extended by nexus-3mea3 2026-08-21: the
FLIPPED_OPERATORS set dispatches at the cheap tier by default) are DIFFERENT populations. Pooling them yields a median
that describes neither. The derivation therefore filters on the per-step
canonical ``model`` via :func:`is_post_flip_run` -- a model predicate,
not a timestamp cutoff, so it survives a later re-flip (it reads
``FLIPPED_OPERATORS`` live) and a canonical-id bump (it matches the tier
ALIAS family, ``haiku``, not a pinned id). ``nx answer-runs --steps``'
``by_operator`` aggregate keys on operator only and DOES pool the two
populations (.rg2 guardrail, T2 [23168]); never derive from it.

Why not ``cost_estimate.build_operator_price_table``: that table yields
per-OPERATOR dispatch medians (what one step costs); a budget caps a
whole RUN, so this module sums billable steps per run and takes
percentiles of run totals. Both key the population on the recorded
canonical model, never on operator alone.

Percentile prior (bead DO item 1): p50 refuses half of legitimate runs;
p95 at the floor of 30 runs is the second-most-expensive run observed
and protects nobody; p75 refuses a quarter; p90 cuts the expensive tail
at ~10% refusal and is the a-priori recommendation. The choice is made
against the printed refuse-fraction table once ``n >= MIN_DERIVATION_
RUNS``, because strong-tier cost varied 10-15x within one operator in
the .p2c A/B data and the post-flip distribution is likely long-tailed.

Enforcement stays OFF in this module. ``.p3c`` (nexus-nyry9.21) turns it
on, and only once :data:`DERIVED_BUDGET_USD` carries a value -- enforced
structurally by :func:`check_enforcement_invariant` at import.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Final

import structlog

from nexus.operators.model_tiers import FLIPPED_OPERATORS, resolve_model_for_tier
from nexus.plans.cost_estimate import _resolved

__all__ = [
    "BUDGET_ENFORCEMENT_ENABLED",
    "CANDIDATE_PERCENTILES",
    "DERIVED_BUDGET_USD",
    "MIN_DERIVATION_RUNS",
    "BudgetDerivation",
    "check_enforcement_invariant",
    "derive_budget_default",
    "describe_tier_config",
    "is_post_flip_run",
]

_log = structlog.get_logger(__name__)

# ── The derived default ──────────────────────────────────────────────────────
#
# PROVENANCE: UNDERIVED as of 2026-08-21. ``nx answer-runs --derive-budget``
# against the live store scanned 182 rows (143 executed-ok) and found n=0
# qualifying runs: per-step cost recording (cc61d4c31, .p1f) first shipped
# in conexus 7.14.0 / engine-service-v0.1.85 on 2026-08-21, and the newest
# recorded run (2026-08-19) came from a 7.13.0 client, so no row carries a
# ``steps`` list at all. Tier configuration in force for the eventual
# derivation: FLIPPED_OPERATORS = {filter, groupby, extract, rank, check,
# verify} at the cheap alias ``haiku`` (check/verify added by nexus-3mea3
# 2026-08-21; any run recorded between the 7.14.0 flip and that date with
# a strong-model check/verify step correctly classifies PRE-flip under
# is_post_flip_run's live read of the set); everything else HOLD.
#
# When a derivation run reports ``sufficient`` (n >= MIN_DERIVATION_RUNS),
# set this to the chosen percentile's value and REPLACE this comment with:
# the date, n, the percentile, the would-have-refused fraction, and the
# tier configuration string the run printed. A number with no n is not
# allowed here (bead VERIFICATION).
DERIVED_BUDGET_USD: Final[float | None] = None

#: ``.p3c`` flips this; this bead leaves enforcement OFF by construction.
BUDGET_ENFORCEMENT_ENABLED: Final[bool] = False


def check_enforcement_invariant(enabled: bool, derived: float | None) -> None:
    """Enforcement may never be ON while the derived default is ``None``
    (RDR-196 § Risks: "enforcement ships off by default until the derived
    value exists"). Raised at import so a flip without a derivation
    cannot even load."""
    if enabled and derived is None:
        raise RuntimeError(
            "BUDGET_ENFORCEMENT_ENABLED is True but DERIVED_BUDGET_USD is None: "
            "derive the default first (nx answer-runs --derive-budget) and record "
            "its provenance (RDR-196 .p3a, nexus-nyry9.19)"
        )


check_enforcement_invariant(BUDGET_ENFORCEMENT_ENABLED, DERIVED_BUDGET_USD)

#: Percentiles every derivation run reports, so the choice of cap is made
#: against the full refuse-fraction table rather than one number.
CANDIDATE_PERCENTILES: Final[tuple[int, ...]] = (50, 75, 90, 95)

#: Non-vacuity floor: below this many qualifying post-flip runs the
#: derivation reports counts but names NO percentile values. n=5 would
#: make p95 the single most expensive run observed -- a sample, not a
#: distribution.
MIN_DERIVATION_RUNS: Final[int] = 30

#: Step sources whose ``cost_usd`` is attributable to this run's spend.
#: ``sql`` fast-path steps cost nothing and carry ``None`` legitimately.
_BILLABLE_SOURCES: Final[frozenset[str]] = frozenset({"llm", "bundle"})


@dataclass(frozen=True)
class BudgetDerivation:
    """Result of one derivation run. ``percentiles`` / ``would_refuse``
    are EMPTY (not zero) when ``sufficient`` is False."""

    n_rows_scanned: int
    n_executed_ok: int
    n_excluded_no_steps: int
    n_excluded_pre_flip: int
    n_excluded_unknown_cost: int
    n_runs: int
    tier_config: str
    costs: tuple[float, ...]
    percentiles: dict[int, float]
    would_refuse: dict[int, float]
    #: canonical model id -> count, over every flipped-operator LLM step in
    #: the executed-ok rows that carried steps. The visible signal for the
    #: alias/canonical coupling in :func:`is_post_flip_run`: if the cheap
    #: alias family ever stops being a substring of the canonical id a
    #: cheap dispatch records, every run lands in ``pre_flip`` and THIS
    #: line names the id that did it.
    flipped_step_models: dict[str, int]

    @property
    def sufficient(self) -> bool:
        return self.n_runs >= MIN_DERIVATION_RUNS

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_rows_scanned": self.n_rows_scanned,
            "n_executed_ok": self.n_executed_ok,
            "n_excluded_no_steps": self.n_excluded_no_steps,
            "n_excluded_pre_flip": self.n_excluded_pre_flip,
            "n_excluded_unknown_cost": self.n_excluded_unknown_cost,
            "n_runs": self.n_runs,
            "min_derivation_runs": MIN_DERIVATION_RUNS,
            "sufficient": self.sufficient,
            "tier_config": self.tier_config,
            "flipped_step_models": dict(self.flipped_step_models),
            "percentiles": {str(p): v for p, v in self.percentiles.items()},
            "would_refuse": {str(p): v for p, v in self.would_refuse.items()},
            "derived_budget_usd": DERIVED_BUDGET_USD,
            "enforcement_enabled": BUDGET_ENFORCEMENT_ENABLED,
        }


def describe_tier_config() -> str:
    """The tier configuration a derivation ran under, for provenance."""
    flipped = ",".join(sorted(op.removeprefix("operator_") for op in FLIPPED_OPERATORS))
    return f"flipped={{{flipped}}}@{resolve_model_for_tier('cheap')}; others=HOLD"


def is_post_flip_run(steps: list[dict[str, Any]]) -> bool:
    """True when every flipped-operator LLM step in *steps* recorded a
    cheap-tier canonical model.

    Steps of HOLD operators, ``sql`` steps, and ``bundle`` steps (bundles
    never consult tiers) are tier-invariant and do not vote. A flipped
    step whose model is unknown (``None``) is NOT proven post-flip and
    taints the run. An empty list is vacuously post-flip.
    """
    cheap_family = resolve_model_for_tier("cheap").lower()
    for step in steps:
        if step.get("source") != "llm":
            continue
        operator = step.get("operator")
        if not operator or _resolved(str(operator)) not in FLIPPED_OPERATORS:
            continue
        model = str(step.get("model") or "").lower()
        if cheap_family not in model:
            return False
    return True


def _nearest_rank(sorted_costs: list[float], p: int) -> float:
    idx = max(1, math.ceil(p / 100 * len(sorted_costs))) - 1
    return sorted_costs[idx]


def derive_budget_default(
    telemetry_store: Any, *, limit: int = 300, since: str | None = None,
) -> BudgetDerivation:
    """Scan executed-ok runs, keep the post-flip ones with fully-known
    cost, and report per-run cost percentiles plus the fraction of those
    runs each percentile would have refused.

    *since* bounds the scanned window (ISO 8601, server-stamped
    ``created_at``); *limit* is the page size the telemetry query returns
    and therefore the most rows this derivation can ever see.

    Never raises: a query failure or an engine without the steps route
    degrades to an all-zero derivation (``sufficient`` False), which the
    CLI prints as such -- never as a value.
    """
    from nexus.commands.answer_runs import _row_is_failed  # noqa: PLC0415 — deferred: layering (plans/ depending on commands/), same as cost_estimate

    tier_config = describe_tier_config()
    empty = BudgetDerivation(0, 0, 0, 0, 0, 0, tier_config, (), {}, {}, {})
    try:
        result = telemetry_store.query_nx_answer_runs(
            since=since, limit=limit, include_steps=True,
        )
    except Exception:  # noqa: BLE001 — boundary catch; the CLI reports n=0, never a value
        _log.warning("budget_default_query_failed", exc_info=True)
        return empty
    if not isinstance(result, dict) or result.get("steps_supported") is False:
        return empty

    rows = result.get("rows") or []
    n_executed_ok = n_no_steps = n_pre_flip = n_unknown = 0
    costs: list[float] = []
    flipped_models: dict[str, int] = {}
    for row in rows:
        if int(row.get("step_count") or 0) <= 0 or _row_is_failed(row):
            continue
        n_executed_ok += 1
        steps = row.get("steps") or []
        if not steps:
            n_no_steps += 1  # pre-.p1f client: no step records at all
            continue
        for step in steps:
            if step.get("source") == "llm" and _resolved(str(step.get("operator") or "")) in FLIPPED_OPERATORS:
                key = str(step.get("model") or "<unknown>")
                flipped_models[key] = flipped_models.get(key, 0) + 1
        if not is_post_flip_run(steps):
            n_pre_flip += 1
            continue
        billable = [s for s in steps if s.get("source") in _BILLABLE_SOURCES]
        if any(s.get("cost_usd") is None for s in billable):
            n_unknown += 1
            continue
        costs.append(float(sum(float(s["cost_usd"]) for s in billable)))

    percentiles: dict[int, float] = {}
    would_refuse: dict[int, float] = {}
    if len(costs) >= MIN_DERIVATION_RUNS:
        ordered = sorted(costs)
        for p in CANDIDATE_PERCENTILES:
            value = _nearest_rank(ordered, p)
            percentiles[p] = value
            would_refuse[p] = sum(1 for c in costs if c > value) / len(costs)

    return BudgetDerivation(
        n_rows_scanned=len(rows),
        n_executed_ok=n_executed_ok,
        n_excluded_no_steps=n_no_steps,
        n_excluded_pre_flip=n_pre_flip,
        n_excluded_unknown_cost=n_unknown,
        n_runs=len(costs),
        tier_config=tier_config,
        costs=tuple(costs),
        percentiles=percentiles,
        would_refuse=would_refuse,
        flipped_step_models=flipped_models,
    )
