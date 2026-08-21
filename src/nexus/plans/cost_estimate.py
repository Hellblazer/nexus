# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Predicted plan-cost estimation from step shape (RDR-196 Phase 3 Step 1,
nexus-nyry9.20).

Sam's 2026-08-21 decision, OPTION (a) (bd comment on nexus-nyry9.20,
superseding the RDR's original "rank by RECORDED per-plan median cost"
design): nexus-nyry9.3's .r3 census refuted a rankable recorded-history
population at authoring time (0 plans with >= 3 distinct successful runs
across the whole live+deleted library; cost_usd was 0.00 on 182/182 runs;
``plan_id`` is not even a stable key across a ``nx plan reseed``). This
module instead PREDICTS a plan's cost from its STEP SHAPE — operator
names, SQL-fast-path eligibility, and adjacent-operator bundling — priced
against Phase 1's per-operator telemetry when enough of it exists, falling
back to a small static per-tier table otherwise. Recorded per-plan medians
remain a later refinement (RDR-100 owns reseed-stable plan identity;
tracked on nexus-93cc6) — nothing here re-embeds a question or touches
plan-matching semantics; it only prices an already-matched candidate's
JSON.

RDR-100 FENCE: this module never calls ``plan_match``/``_plan_match``,
never computes or adjusts a ``Match.confidence`` value, and never changes
matcher.py. It is a pure function of already-materialized ``plan_json`` +
an already-built price table.

NOT COUPLED TO PHASE 2 MODEL TIERING: this module deliberately does NOT
import ``nexus.operators.model_tiers`` / ``OPERATOR_MODEL_TIER``. Model-
tier routing is OFF by default (gated behind ``NX_OPERATOR_MODEL_TIERING``,
.p2d's still-pending default-tier flip) -- today every LLM operator
dispatch actually runs under the SAME (untiered) default model regardless
of what a tier table would say, so pricing by an inert tier split would
be misleading, not merely premature. The bead's own acceptance criteria
name exactly ONE static-fallback source -- T2 ``nexus_rdr/
196-phase2-quality-proxy``'s measured STRONG-tier costs -- applied
uniformly to every non-sql-fast-path LLM operator; per-operator
differentiation resumes once real per-operator HISTORY exists (this
module's primary price source already handles that, keyed off the
CANONICAL model recorded on each dispatch, never a tier alias).

NOT MODELLED: ``MAX_BUNDLE_PROMPT_CHARS`` runtime bundle fallback
(``nexus.plans.bundle``, 200_000 chars). :func:`estimate_plan_cost` prices
a contiguous operator run as ONE bundled dispatch purely from STEP SHAPE
-- it never resolves step args or composes the real prompt, so it cannot
know whether that bundle's actual runtime prompt would exceed the cap.
``plans/runner.py``'s ``plan_run`` DOES resolve args, and when the real
composed prompt is oversized it logs ``bundle_oversized_fallback_to_per_
step`` and falls back to N isolated dispatches for that segment instead
of the 1 this module assumed. This is a documented, one-directional risk:
a plan whose bundle would trigger that runtime fallback has its true cost
UNDER-counted here (every ``bundle-dominant(...)`` ``basis`` string
carries no signal of this either way -- the caller cannot distinguish "a
bundle that will really run as one dispatch" from "one that will split at
runtime" from the estimate alone).
"""
from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from nexus.plans.bundle import OperatorBundleSlice, is_operator_tool, segment_steps

if TYPE_CHECKING:
    from nexus.plans.match import Match

__all__ = [
    "MIN_HISTORY_SAMPLES",
    "PLAN_CHOICE_CONFIDENCE_BAND",
    "STATIC_FALLBACK_COST_USD",
    "STATIC_FALLBACK_MS",
    "OperatorPriceTable",
    "PlanCostEstimate",
    "build_operator_price_table",
    "choose_within_band",
    "estimate_plan_cost",
    "get_cached_price_table",
    "invalidate_price_table_cache",
]

_log = structlog.get_logger()

# ── Confidence band (selection policy, RDR-196 Phase 3 Step 1) ─────────────
#
# A NAMED constant, not a user-facing knob (RDR §Technical Design). Width
# justified against REAL confidence values already on record in this
# codebase, not intuition: matcher.py's own ``_SCOPE_FIT_WEIGHT`` rationale
# (nexus-svcg, 5 real fixture pairs) documents a genuine competing-match
# case at raw cosine 0.79 vs 0.82 -- a 0.03 gap the matcher itself treats
# as close enough that scope-fit, not raw confidence, should decide the
# winner. 0.05 comfortably covers that measured gap (so cost-ranking can
# engage on the same class of "genuinely close" candidates the matcher's
# own design already recognizes) while staying an order of magnitude
# narrower than the distance from a typical hit to either confidence floor
# (_PLAN_MATCH_MIN_CONFIDENCE=0.40 in mcp/core.py, _GROWN_PLAN_MIN_CONFIDENCE
# =0.55 in plans/matcher.py) -- so a candidate that is genuinely a better
# semantic match (a gap bigger than the noise band matcher.py's own
# reasoning already treats as close) is never overridden by a cheaper but
# worse-matching plan.
PLAN_CHOICE_CONFIDENCE_BAND: Final[float] = 0.05

# ── Static fallback price ────────────────────────────────────────────────────
#
# Used for any LLM operator whose recorded-history sample count is below
# MIN_HISTORY_SAMPLES. Sourced from T2 (dated 2026-08-21), the ONE source
# the bead names ("a documented STATIC fallback table (from T2 nexus_rdr/
# 196-phase2-quality-proxy's measured strong-tier costs, dated)"):
#
#   nexus_rdr/196-phase2-quality-proxy -- "Bead total: 29 live strong-tier
#   dispatches, ~$6.6 measured/estimated" (.p2a's quality-proxy exercise;
#   every baseline-arm dispatch in that measurement ran at the strong
#   tier, and -- since model-tier routing is OFF by default today, see
#   the module docstring -- the strong tier is also the realistic stand-in
#   for "whatever the untiered default model actually costs") ->
#   6.6 / 29 ~= $0.2276/dispatch, rounded down slightly to $0.23.
#
# A small-sample average, not a distribution; refine once .p1f (the
# paired engine+client release that lands real cost_usd end to end)
# accumulates real per-operator history -- at that point most operators
# resolve via the history path above and this constant only backstops the
# rarely/never-run ones. The 11s figure is the RDR's own cited "single
# round trip has an ~11s floor independent of workload" (nx_answer
# docstring, mcp/core.py) -- the substrate's session-bootstrap cost.
#
# FORWARD-LINK (code review round 1, T2 nyry9.20-code-review-2026-08-21,
# item #3): this single flat figure stands in for EVERY LLM operator
# because model-tier routing is off by default (see the module
# docstring's "NOT COUPLED TO PHASE 2 MODEL TIERING" section). Once
# .p2d (nexus-nyry9.17) flips the default and cheap-tier operators
# (extract/filter/groupby/aggregate/rank/summarize) actually route to a
# cheaper model in production, THIS constant must be re-priced (most
# likely split back into a cheap/strong pair, sourced from .p2c's real
# A/B measurement rather than the .p2a baseline-only figure above) --
# otherwise every cheap-tier operator's estimate silently over-counts
# relative to its new real cost. Tracked on nexus-nyry9.17 (bd comment).
STATIC_FALLBACK_COST_USD: Final[float] = 0.23
STATIC_FALLBACK_MS: Final[int] = 11_000

#: Minimum executed-ok, "llm"-source samples an operator needs in recorded
#: telemetry before its median is trusted over the static fallback. 3
#: mirrors nexus-nyry9.3's own "N=3, the natural floor for a median"
#: framing (an even N has no unique middle value).
MIN_HISTORY_SAMPLES: Final[int] = 3

#: operator_filter / operator_groupby / operator_aggregate try a SQL path
#: first (RDR-089 follow-up: ``source="auto"`` on those three MCP tools,
#: their own default) and only fall to an LLM dispatch when the aspect
#: data can't answer the request. Phase 3 Step 1 prices a step naming one
#: of these OPTIMISTICALLY at the real-SQL cost ($0) -- the same treatment
#: StepRecord gives an ACTUAL sql-fast-path hit (``source="sql"``,
#: ``cost_usd=0.0``, ``model=None``) -- UNLESS the step's own ``args``
#: set ``source="llm"`` explicitly (a plan author forcing the LLM path,
#: skipping SQL entirely), in which case it is priced like any other LLM
#: operator (see :func:`_step_source_arg`). ``source="aspects"`` (forces
#: SQL, never dispatches) and the unset/``"auto"`` default both keep the
#: optimistic-zero treatment -- for ``"auto"`` this remains a documented
#: modeling simplification, not a hidden one: a plan whose filter/
#: groupby/aggregate step falls through to the LLM path AT RUNTIME despite
#: not requesting it explicitly will have its true cost under-estimated by
#: this table. Both bare and ``operator_``-prefixed spellings are included
#: (plan YAMLs use either -- same convention as ``BUNDLEABLE_OPERATORS``).
_SQL_FAST_PATH_ELIGIBLE: Final[frozenset[str]] = frozenset({
    "filter", "groupby", "aggregate",
    "operator_filter", "operator_groupby", "operator_aggregate",
})

#: Retrieval/traversal/hydration tools a plan step may name that are NOT
#: LLM operators -- a REAL zero cost (no ``claude -p`` call happens at
#: all, same as StepRecord's "sql" source for these on this substrate).
#: Deliberately an EXPLICIT allowlist, not "anything that isn't an
#: operator": a genuinely unknown/misspelled tool name must be
#: UNPRICEABLE (``basis`` says so, the whole plan's estimate goes to
#: ``None``), never silently priced at zero just because it also isn't a
#: recognized operator. Sourced from the tool names observed in the
#: nexus-nyry9.3 .r3 census step shapes (T2
#: nexus_rdr/196-residual-3-grown-plan-generality) plus the other
#: catalog/retrieval MCP tools a plan step can legitimately name.
_KNOWN_RETRIEVAL_TOOLS: Final[frozenset[str]] = frozenset({
    "search", "query", "traverse",
    "store_get", "store_get_many", "store_list",
    "search_metadata_scoped", "search_graph_hop", "search_topic_scoped",
    "plan_search",
})


@dataclass(frozen=True)
class PlanCostEstimate:
    """A predicted cost for one plan, from its step shape.

    ``usd``/``ms`` are ``None`` -- never ``0`` -- when any step in the
    plan could not be priced (an operator absent from both recorded
    history and :data:`OPERATOR_MODEL_TIER`). ``basis`` always explains
    the number: per-segment source tags joined together (e.g.
    ``"search=retrieval(zero-cost); operator_rank=history(n=5,
    model=claude-haiku-4-5); operator_generate=static-fallback(strong,
    thin-history)"``), so a caller/log line can show exactly which parts
    of the estimate are measured vs assumed.
    """

    usd: float | None
    ms: int | None
    basis: str


@dataclass(frozen=True)
class _OperatorPrice:
    median_cost_usd: float | None
    median_ms: int | None
    n: int
    model: str | None


class OperatorPriceTable:
    """operator name -> best-known per-dispatch price.

    Built by :func:`build_operator_price_table` (recorded telemetry) with
    a static per-tier fallback baked into :meth:`price_for` itself, so a
    caller never has to special-case "no history" separately. Immutable
    once built; callers must not mutate ``_prices``.
    """

    __slots__ = ("_prices",)

    def __init__(self, prices: dict[str, _OperatorPrice]):
        self._prices = prices

    def price_for(self, operator: str) -> tuple[float | None, int | None, str]:
        """Return ``(usd, ms, basis)`` for one dispatch of *operator*.

        Prefers recorded history once it has >= :data:`MIN_HISTORY_SAMPLES`
        executed-ok, "llm"-source samples; falls back to
        :data:`STATIC_FALLBACK_COST_USD` for any RECOGNIZED operator
        (:func:`nexus.plans.bundle.is_operator_tool`) otherwise. Returns
        ``(None, None, ...)`` only when *operator* is not a recognized
        operator at all -- this table has never heard of it, it is not
        priced at zero.
        """
        rec = self._prices.get(operator)
        if (
            rec is not None
            and rec.n >= MIN_HISTORY_SAMPLES
            and rec.median_cost_usd is not None
        ):
            model_note = rec.model or "unknown-model"
            return (
                rec.median_cost_usd, rec.median_ms,
                f"history(n={rec.n}, model={model_note})",
            )
        if is_operator_tool(operator):
            return (
                STATIC_FALLBACK_COST_USD, STATIC_FALLBACK_MS,
                "static-fallback(thin-history)",
            )
        return None, None, "unpriceable(no-history, not-a-known-operator)"


def build_operator_price_table(telemetry_store: Any, *, limit: int = 300) -> OperatorPriceTable:
    """Build a per-operator price table from recorded Phase 1 telemetry.

    Calls ``telemetry_store.query_nx_answer_runs(include_steps=True,
    limit=limit)`` and aggregates over the EXECUTED-OK population at the
    ROW level -- ``int(row["step_count"] or 0) > 0`` AND NOT
    ``nexus.commands.answer_runs._row_is_failed(row)``, the SAME predicate
    ``nx answer-runs``' own ``--steps`` breakdown uses (imported, never a
    second definition, so the two surfaces can't silently drift apart on
    what "executed-ok" means: a row whose LAST step failed, or whose
    ``final_text`` carries the budget-exhausted marker, is excluded here
    exactly as it is excluded there, even if some of its earlier
    individual steps look fine in isolation). Within a qualifying row,
    steps are further filtered to ``source == "llm"`` (isolated dispatches
    only -- a ``"bundle"`` step's cost is fused across >= 2 operators and
    is not attributable to any single operator name, so bundle rows are
    excluded here; :func:`estimate_plan_cost` prices a bundled SHAPE
    separately, it does not lean on this table having bundle-derived
    numbers), a per-step ``ok`` true, and a non-``None`` ``cost_usd``.

    Grouped by ``(operator, model)`` per RDR-196 R3 -- ``model`` is always
    the recorded CANONICAL model id (``StepRecord.model``, sourced from
    ``DispatchUsage.model``/``canonicalModel``), never a requested alias.
    The operator KEY is canonicalized via :func:`_resolved` (the same
    normalization :func:`OperatorPriceTable.price_for`'s callers apply to
    their lookup key) before bucketing -- ``plans/runner.py`` records
    ``StepRecord.operator`` VERBATIM from whatever spelling the plan JSON's
    ``tool`` field used (bare ``"rank"`` or resolved ``"operator_rank"``,
    plan authors use either), so bucketing by the raw string would split
    one operator's history across two keys and silently starve BOTH of
    :data:`MIN_HISTORY_SAMPLES`. Within one operator, the ``(model, n)``
    group with the LARGEST sample count becomes that operator's
    representative price -- the model most often actually resolved for it
    in practice, since a future estimate cannot know in advance which
    canonical model a not-yet-run dispatch will resolve to.

    Never raises: a query failure or an engine that doesn't support
    ``include_steps`` (``steps_supported`` false) both degrade to an empty
    table, which makes every :meth:`OperatorPriceTable.price_for` call
    fall through to the static fallback -- exactly today's reality per
    nexus-nyry9.3's .r3 census (0/182 runs carried non-zero ``cost_usd``
    at authoring time).
    """
    from nexus.commands.answer_runs import _row_is_failed  # noqa: PLC0415 — deferred: layering (plans/ depending on commands/), documented rather than duplicated per review directive (T2 nyry9.20-code-review round 2)

    try:
        result = telemetry_store.query_nx_answer_runs(limit=limit, include_steps=True)
    except Exception:  # noqa: BLE001 — boundary catch; empty table is the correct degrade, must not crash the caller
        _log.warning("cost_estimate_price_table_query_failed", exc_info=True)
        return OperatorPriceTable({})

    if not isinstance(result, dict):
        return OperatorPriceTable({})
    if result.get("steps_supported") is False:
        _log.info("cost_estimate_price_table_steps_unsupported")
        return OperatorPriceTable({})

    # operator -> model -> [(cost_usd, elapsed_ms_or_None), ...]
    buckets: dict[str, dict[str | None, list[tuple[float, int | None]]]] = {}
    for row in result.get("rows") or []:
        if int(row.get("step_count") or 0) <= 0:
            continue  # degenerate row: no executed steps to aggregate
        if _row_is_failed(row):
            continue  # row-level executed-ok predicate, same as `nx answer-runs`
        for step in row.get("steps") or []:
            if not step.get("ok"):
                continue
            if step.get("source") != "llm":
                continue
            cost = step.get("cost_usd")
            if cost is None:
                continue
            operator = step.get("operator")
            if not operator:
                continue
            canonical = _resolved(str(operator))
            model = step.get("model")
            elapsed = step.get("elapsed_ms")
            buckets.setdefault(canonical, {}).setdefault(model, []).append(
                (float(cost), elapsed if isinstance(elapsed, int) else None)
            )

    prices: dict[str, _OperatorPrice] = {}
    for operator, by_model in buckets.items():
        model, samples = max(by_model.items(), key=lambda kv: len(kv[1]))
        costs = [c for c, _ in samples]
        elapsed_values = [e for _, e in samples if e is not None]
        prices[operator] = _OperatorPrice(
            median_cost_usd=statistics.median(costs),
            median_ms=int(statistics.median(elapsed_values)) if elapsed_values else None,
            n=len(samples),
            model=model,
        )
    return OperatorPriceTable(prices)


# ── Process-local cache ─────────────────────────────────────────────────────
#
# A time-boxed cache, not an unbounded one: a long-lived MCP process must
# not serve an increasingly stale median forever. ``invalidate_price_table_
# cache`` is the explicit hook a future caller (e.g. the run-recorder, once
# .p3c widens the wire -- out of this bead's fenced core.py region) can use
# to force a rebuild the moment new history lands, without waiting out the
# TTL.
_CACHE_TTL_S: Final[float] = 300.0
_cached_table: OperatorPriceTable | None = None
_cached_table_built_at: float = 0.0


def get_cached_price_table(telemetry_store: Any, *, force_refresh: bool = False) -> OperatorPriceTable:
    """Process-local cached :func:`build_operator_price_table`.

    Rebuilds when the cache is empty, older than :data:`_CACHE_TTL_S`
    seconds, or *force_refresh* is set. See :func:`invalidate_price_table_
    cache` for an explicit invalidation hook.
    """
    global _cached_table, _cached_table_built_at
    now = time.monotonic()
    if (
        force_refresh
        or _cached_table is None
        or (now - _cached_table_built_at) >= _CACHE_TTL_S
    ):
        _cached_table = build_operator_price_table(telemetry_store)
        _cached_table_built_at = now
    return _cached_table


def invalidate_price_table_cache() -> None:
    """Force the next :func:`get_cached_price_table` call to rebuild.

    Exists so a caller that just recorded a new run can prevent the
    process cache from serving a stale median against that plan's own
    freshly-accumulated history for up to :data:`_CACHE_TTL_S` seconds.
    Not wired into the run-recorder by this bead (that call site is
    outside this bead's fenced ``core.py`` region -- Step 4 / the
    telemetry-write path); the TTL bounds staleness in the meantime.
    """
    global _cached_table, _cached_table_built_at
    _cached_table = None
    _cached_table_built_at = 0.0


# ── Step-shape cost estimation ──────────────────────────────────────────────


def _extract_tool(step: dict[str, Any]) -> str:
    """Normalize a plan step's tool name. Local copy of
    ``bundle._extract_tool_name`` (private there) -- same three-key
    fallback + ``mcp__...__`` prefix strip, kept in sync by inspection
    since importing a leading-underscore symbol across modules is worse
    than one small duplicated helper.
    """
    tool = step.get("tool") or step.get("op") or step.get("operation") or ""
    if not isinstance(tool, str):
        return ""
    if tool.startswith("mcp__"):
        tool = tool.rsplit("__", 1)[-1]
    return tool


def _resolved(tool: str) -> str:
    return tool if tool.startswith("operator_") else f"operator_{tool}"


def _step_source_arg(step: dict[str, Any]) -> str:
    """The effective ``source`` for a filter/groupby/aggregate step.

    Explicit ``args["source"]`` when it is a non-empty string; otherwise
    ``"auto"`` -- the MCP tools' own default (``operator_filter`` /
    ``operator_groupby`` / ``operator_aggregate`` all declare
    ``source: str = "auto"``). Only ``"llm"`` changes pricing (forces a
    real LLM-operator price instead of the optimistic zero) -- ``"auto"``
    and ``"aspects"`` both keep the zero treatment, see
    :data:`_SQL_FAST_PATH_ELIGIBLE`'s docstring for why.
    """
    args = step.get("args")
    if not isinstance(args, dict):
        return "auto"
    source = args.get("source")
    return source if isinstance(source, str) and source else "auto"


def _price_step(
    step: dict[str, Any], price_table: OperatorPriceTable,
) -> tuple[float | None, int | None, str, bool]:
    """Price one non-bundled step. Returns ``(usd, ms, basis, unpriceable)``."""
    tool = _extract_tool(step)
    if not tool:
        return None, None, "empty-tool", True
    if not is_operator_tool(tool):
        if tool in _KNOWN_RETRIEVAL_TOOLS:
            # Real zero, same as StepRecord's "sql" source for these on
            # this substrate -- no claude -p call happens at all.
            return 0.0, 0, "retrieval(zero-cost)", False
        # A tool this table has never heard of: unpriceable, NOT a
        # silent zero, so a malformed/unknown step can't make an entire
        # plan look free.
        return None, None, f"unpriceable(unknown-tool:{tool})", True
    if tool in _SQL_FAST_PATH_ELIGIBLE:
        source = _step_source_arg(step)
        if source != "llm":
            return 0.0, 0, f"sql-fast-path-eligible(optimistic-zero, source={source})", False
        # Explicit source="llm": the SQL path is never even attempted,
        # so this is a real LLM operator for pricing purposes.
        usd, ms, basis = price_table.price_for(_resolved(tool))
        if usd is None:
            return None, None, basis, True
        return usd, ms, f"sql-fast-path-eligible-forced-llm({basis})", False
    usd, ms, basis = price_table.price_for(_resolved(tool))
    if usd is None:
        return None, None, basis, True
    return usd, ms, basis, False


def _price_bundle(
    steps: list[dict[str, Any]], price_table: OperatorPriceTable,
) -> tuple[float | None, int | None, str, bool]:
    """Price a contiguous bundle of >= 2 operator steps as ONE dispatch
    (nexus-h33x8.6 a3/.6 finding: a bundled dispatch costs ~ one
    dispatch, not N -- subject to the MAX_BUNDLE_PROMPT_CHARS runtime
    caveat in the module docstring). Priced at the MOST EXPENSIVE member
    that actually needs pricing -- a single fused prompt must satisfy
    whichever bundled step needs the strongest model, so pricing at the
    cheapest member would silently under-count a bundle containing even
    one strong-tier operator. A member that is sql-fast-path-eligible AND
    not forced to ``source="llm"`` (see :func:`_step_source_arg`)
    contributes nothing (same optimistic-zero rule as an isolated one);
    if EVERY member is such a step, the whole bundle prices at zero.
    """
    needs_pricing = [
        s for s in steps
        if not (
            _extract_tool(s) in _SQL_FAST_PATH_ELIGIBLE
            and _step_source_arg(s) != "llm"
        )
    ]
    if not needs_pricing:
        return 0.0, 0, "bundle-all-sql-fast-path-eligible(optimistic-zero)", False
    priced = [price_table.price_for(_resolved(_extract_tool(s))) for s in needs_pricing]
    if any(usd is None for usd, _, _ in priced):
        return None, None, "bundle-contains-unpriceable-operator", True
    usd, ms, basis = max(priced, key=lambda p: p[0])  # type: ignore[arg-type,return-value]
    return usd, ms, f"bundle-dominant({basis})", False


def estimate_plan_cost(
    plan_json: str | dict[str, Any], price_table: OperatorPriceTable,
) -> PlanCostEstimate:
    """Predict a plan's cost/latency from its STEP SHAPE.

    Never re-executes anything, never re-embeds the question, never
    touches confidence -- a pure function of already-materialized plan
    JSON plus an already-built :class:`OperatorPriceTable`.

    Bundling contract (RDR-196 §Approach, the h33x8.6 finding): adjacent
    steps the runner would fuse into one dispatch (per
    :func:`nexus.plans.bundle.segment_steps`, the SAME segmentation logic
    the runner itself uses -- reused here read-only, not reimplemented)
    are priced as ONE dispatch, not N.

    A plan with ANY unpriceable step (an operator absent from both
    recorded history and :data:`nexus.operators.model_tiers.
    OPERATOR_MODEL_TIER`) returns ``usd=None, ms=None`` for the WHOLE
    plan -- the bead's own rule: a ``None`` estimate is ranked LAST among
    tied candidates, never treated as ``0``.
    """
    if isinstance(plan_json, str):
        try:
            plan = json.loads(plan_json)
        except (json.JSONDecodeError, TypeError):
            return PlanCostEstimate(usd=None, ms=None, basis="malformed-plan-json")
    elif isinstance(plan_json, dict):
        plan = plan_json
    else:
        return PlanCostEstimate(usd=None, ms=None, basis="malformed-plan-json")

    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not steps:
        return PlanCostEstimate(usd=None, ms=None, basis="no-steps")

    total_usd: float | None = 0.0
    total_ms: int | None = 0
    bases: list[str] = []
    unpriceable = False

    for segment in segment_steps(steps):
        if isinstance(segment, OperatorBundleSlice):
            bundle_steps = [steps[i] for i in segment.plan_indices]
            tools = [_extract_tool(s) for s in bundle_steps]
            usd, ms, basis, seg_unpriceable = _price_bundle(bundle_steps, price_table)
            bases.append(f"bundle[{','.join(tools)}]={basis}")
        else:
            usd, ms, basis, seg_unpriceable = _price_step(segment.step, price_table)
            tool = _extract_tool(segment.step)
            bases.append(f"{tool or '<empty>'}={basis}")

        if seg_unpriceable:
            unpriceable = True
        total_usd = None if (total_usd is None or usd is None) else total_usd + usd
        total_ms = None if (total_ms is None or ms is None) else total_ms + ms

    basis_str = "; ".join(bases)
    if unpriceable:
        return PlanCostEstimate(usd=None, ms=None, basis=f"unpriceable: {basis_str}")
    return PlanCostEstimate(usd=total_usd, ms=total_ms, basis=basis_str)


# ── Selection ────────────────────────────────────────────────────────────────


def _candidate_log(match: "Match", estimate: PlanCostEstimate, *, in_band: bool) -> dict[str, Any]:
    return {
        "plan_id": match.plan_id,
        "confidence": match.confidence,
        "predicted_cost_usd": estimate.usd,
        "predicted_ms": estimate.ms,
        "basis": estimate.basis,
        "in_band": in_band,
    }


def choose_within_band(
    matches: "list[Match]",
    price_table: OperatorPriceTable,
    *,
    band: float = PLAN_CHOICE_CONFIDENCE_BAND,
) -> "tuple[Match, list[dict[str, Any]]]":
    """Pick which above-floor, matcher-ordered candidate to run.

    Selection rule (RDR-196 Phase 3 Step 1, Sam's 2026-08-21 OPTION (a);
    tightened by code-review round 1, T2 nyry9.20-code-review-2026-08-21
    BLOCKER, and made direction-symmetric by round 2, T2
    review-nexus-nyry9.20-round2 / nyry9.20-code-review-round2-2026-08-21
    CRITICAL): the eligible set is the CONTIGUOUS PREFIX of *matches*, in
    the matcher's own order, whose raw ``confidence`` is within *band* of
    ``matches[0]``'s raw confidence IN EITHER DIRECTION -- walking from
    index 0, the prefix ENDS at the first candidate whose raw confidence
    differs from ``matches[0]``'s by more than *band*, above OR below,
    even if a later candidate would individually satisfy the band check.
    Among that prefix, pick the LOWEST predicted-cost estimate; ties
    broken by EARLIER matcher position (never by confidence -- see why
    below). Outside the prefix, the matcher's own top choice wins
    outright.

    Why the per-candidate test is ``abs(best_conf - m.confidence) <=
    band``, not the one-sided ``best_conf - m.confidence <= band`` that
    shipped through round 2: matcher.py's RDR-091 scope-fit re-ranking
    reorders ``matches`` by an ADJUSTED score (``confidence * (1 + weight
    * scope_fit)``) while ``Match.confidence`` stays raw cosine -- so
    ``matches`` is NOT sorted by raw confidence, and the candidate right
    after ``matches[0]`` can carry a raw confidence WELL ABOVE
    ``matches[0]``'s. That is not noise; it is a candidate the matcher
    deliberately DEMOTED below ``matches[0]`` via scope-fit despite its
    higher raw score. The one-sided test treated "raw confidence above
    best_conf" as automatically in-band (the subtraction goes negative,
    trivially <= band), which let cost-ranking reach past exactly the
    relevance decision the matcher just made. The symmetric test closes
    that: a gap exceeding *band* in EITHER direction ends the prefix, so
    cost-ranking only rearranges WITHIN a run of candidates the matcher
    itself judged comparably relevant -- never past a scope-fit demotion
    in either direction.

    Why a prefix, not a per-candidate filter over the whole list: the
    same scope-fit reordering means a later, lower-position candidate
    that happens to be individually within-band of ``matches[0]`` may be
    reachable only by skipping over an earlier, out-of-band candidate --
    admitting it anyway would let cost-ranking see past that earlier
    candidate's demotion too. Stopping permanently at the first
    out-of-band candidate (matcher order, checked in both directions)
    guarantees cost-ranking never overrides that ordering -- it only
    rearranges WITHIN a run of candidates the matcher itself judged
    comparably relevant. Tie-breaking by matcher position (not
    confidence) follows for the same reason: re-introducing a confidence
    comparison at the tie-break would reopen the exact ordering question
    the prefix rule exists to close.

    A candidate whose estimate is ``usd=None`` NEVER wins a cost
    comparison -- it sorts strictly after every priced candidate in the
    prefix, matching the bead's "never treated as cost 0" requirement. If
    every prefix candidate is unpriceable, ``matches[0]`` is returned --
    the tie resolves to earliest matcher position, i.e. the top match.

    ``matches[0].confidence is None`` (the FTS5 fallback sentinel) means
    there is no numeric band to compute -- returns ``matches[0]``
    unchanged; every candidate is still logged with ``in_band`` set only
    for the chosen one, for observability.

    RDR-100 fence: does not call ``plan_match``, does not read or write
    any ``Match.confidence`` value, does not reorder *matches* -- only
    picks one element of the list the matcher already returned in its
    own order.
    """
    if not matches:
        raise ValueError("choose_within_band: matches must be non-empty")

    best_conf = matches[0].confidence
    decision_log: list[dict[str, Any]] = []

    if best_conf is None:
        for m in matches:
            estimate = estimate_plan_cost(m.plan_json, price_table)
            decision_log.append(_candidate_log(m, estimate, in_band=(m is matches[0])))
        return matches[0], decision_log

    # Contiguous prefix: walk matches in MATCHER ORDER, stop permanently
    # at the first candidate whose raw confidence differs from best_conf
    # by more than `band` in EITHER direction (abs, not one-sided -- a
    # later candidate with raw confidence far ABOVE best_conf is one the
    # matcher's own RDR-091 scope-fit re-ranking deliberately demoted, and
    # cost-ranking must not reach past that demotion any more than it may
    # reach past a low-confidence exclusion). A candidate past that point
    # is never re-admitted even if its own raw confidence would
    # individually pass the check.
    prefix: list[tuple["Match", PlanCostEstimate]] = []
    still_in_prefix = True
    for m in matches:
        estimate = estimate_plan_cost(m.plan_json, price_table)
        in_band_now = (
            still_in_prefix
            and m.confidence is not None
            and abs(best_conf - m.confidence) <= band
        )
        if in_band_now:
            prefix.append((m, estimate))
        else:
            still_in_prefix = False
        decision_log.append(_candidate_log(m, estimate, in_band=in_band_now))

    if len(prefix) <= 1:
        return matches[0], decision_log

    def _sort_key(indexed: "tuple[int, tuple[Match, PlanCostEstimate]]") -> tuple[int, float, int]:
        position, (_m, est) = indexed
        cost_rank = 1 if est.usd is None else 0  # None sorts LAST, never as 0
        cost_value = est.usd if est.usd is not None else float("inf")
        return (cost_rank, cost_value, position)  # tie -> earlier matcher position

    _pos, (chosen, _est) = min(enumerate(prefix), key=_sort_key)
    return chosen, decision_log
