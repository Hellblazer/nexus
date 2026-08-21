# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator model-tier table and resolver (RDR-196 .p2b, nexus-nyry9.15).

Ships the ABILITY to route each operator to a cost tier.
``claude_dispatch`` (``nexus.operators.dispatch``) accepts a ``model``
keyword purely as a pass-through string; it never imports this module —
the 18 pre-.p2b ``claude_dispatch`` call sites are unaffected. This
module itself is consulted by exactly two production call sites
(``plans/runner.py::_default_dispatcher``, the isolated-operator-step
path; ``mcp/core.py::_nx_answer_plan_miss``, the inline planner) — see
``tests/test_operator_model_tiers.py::TestNotConsultedRepoWide``.

**RDR-196 .p2d landed (nexus-nyry9.17, 2026-08-21)**: the DEFAULT path
(``NX_OPERATOR_MODEL_TIERING`` unset, the common case) now routes the 4
:data:`FLIPPED_OPERATORS` (filter/groupby/extract/rank) to the cheap
tier automatically — no opt-in required. Every other operator (check/
verify/aggregate/summarize/compare/generate) still gets no ``model``
override by default (HOLD). ``NX_OPERATOR_MODEL_TIERING=1`` keeps its
.p2c meaning — a measurement override that consults the WHOLE tier
table (including "strong" entries) for A/B re-verification.
``NX_OPERATOR_MODEL_TIERING=0`` is the kill switch: forces every
operator back to strong (pre-.p2d behaviour), for rollback without a
code change. See ``docs/rdr/rdr-196-cost-aware-nx-answer.md``'s Phase 2
OUTCOME block for the full per-operator decision table.

Tier -> model is an ALIAS mapping ("haiku"/"sonnet"), not a pinned model
id (RDR-196 R3). ``claude -p --model <alias>`` resolves the alias
server-side; the concrete model it actually resolves to is captured
separately, per dispatch, in ``DispatchUsage.model`` (RDR-196 .p1a,
sourced from the stream-json envelope's own ``canonicalModel`` field —
see ``nexus.operators.dispatch``). So a future alias re-point changes
what a tier costs without invalidating every measurement recorded
against this table: the alias is what you asked for, the canonical id
in telemetry is what you got.
"""
from __future__ import annotations

from typing import Final, Literal

Tier = Literal["cheap", "strong"]

#: Tier -> CLI ``--model`` alias. The ONE place a tier's target model
#: string lives — move a tier's target model here, nowhere else (see
#: ``tests/test_operator_model_tiers.py::TestSingleMappingLocation``).
_TIER_ALIASES: Final[dict[str, str]] = {
    "cheap": "haiku",
    "strong": "sonnet",
}

#: Operator name -> cost tier. RDR-196 §Approach's proposed split
#: (lines 239-240) named cheap for the mechanical/structural operators,
#: strong for the operators that synthesize or judge — but "cheap" here
#: means "flip-ELIGIBLE", not "flip-decided": a "cheap" entry is only
#: honest once a quality proxy exists to measure the tier delta against
#: (RDR-196 .p2a/.p2c). ``operator_aggregate`` and ``operator_summarize``
#: are pinned to "strong" despite the RDR's original proposal (review
#: fix, nexus-nyry9.16 round-2, T2 [23144] Significant #6) — no quality
#: proxy exists for them (196-R4; ``scripts/bench/operator_proxy_metrics
#: .THRESHOLDS`` has no entry for either), so a pre-set "cheap" here
#: would let a future WHOLE-TABLE activation (.p2d) silently flip two
#: unvalidated operators to a cheaper model with nothing checking
#: whether their output quality survived the switch. See
#: ``TestOperatorModelTierTable::test_every_cheap_entry_has_a_registered_proxy_metric``.
#: NOT consulted anywhere by default in this bead — exists so later
#: beads (.p2c measurement, .p2d default flip) have one place to read
#: from. Deliberately scoped to the 10 ``@mcp.tool``-registered
#: operators in ``nexus.mcp.core`` (``operator_extract`` ..
#: ``operator_aggregate``) — the RDR's mention of "the inline planner"
#: alongside this split names a distinct dispatch site
#: (``_nx_answer_plan_miss``), not one of these operators, and is out
#: of scope for this table.
OPERATOR_MODEL_TIER: Final[dict[str, Tier]] = {
    "operator_extract": "cheap",
    "operator_filter": "cheap",
    "operator_groupby": "cheap",
    "operator_aggregate": "strong",  # no proxy (196-R4); not flip-eligible until one exists
    "operator_rank": "cheap",
    "operator_summarize": "strong",  # no proxy (196-R4); not flip-eligible until one exists
    "operator_generate": "strong",
    "operator_check": "strong",
    "operator_verify": "strong",
    "operator_compare": "strong",
}


#: RDR-196 .p2d decision (nexus-nyry9.17, 2026-08-21): the 4 operators
#: whose default routing FLIPPED to "cheap" after clearing BOTH
#: pre-registered .p2c refutation criteria (T2 nexus_rdr/
#: 196-phase2-ab-measurement — cost 14-20x cheaper, mean/min agreement
#: at or above the .p2a frozen threshold on every one of 3 pairs, zero
#: plumbing failures). This is the ONLY set consulted on the DEFAULT
#: (no-env-override) path — see ``resolve_model_for_flipped_operator``.
#: Being "cheap" in ``OPERATOR_MODEL_TIER`` above is flip-ELIGIBLE, not
#: flip-DECIDED; this frozenset is the decided subset (today identical
#: to the table's "cheap" entries, but tracked separately on purpose —
#: a future eligibility change to the table, e.g. re-flipping aggregate/
#: summarize once a proxy exists for them, must not silently ALSO
#: become a default-flip without its own .p2d-shaped decision bead).
#: check/verify (HOLD — no tiering delta between arms, both strong in
#: every arm per this table) and aggregate/summarize/compare/generate
#: (HOLD by construction — no .p2a quality proxy) are absent by
#: construction, not by omission.
FLIPPED_OPERATORS: Final[frozenset[str]] = frozenset({
    "operator_filter",
    "operator_groupby",
    "operator_extract",
    "operator_rank",
})


class UnknownTierError(ValueError):
    """Raised when a tier or operator name isn't in the resolver's known
    set. Always names the unrecognized value."""


def resolve_model_for_tier(tier: str) -> str:
    """Resolve a cost tier to the ``--model`` alias to pass to
    ``claude_dispatch``. The single choke point for tier -> model
    resolution; nothing else in this codebase should hardcode a tier's
    target model string.

    Raises:
        UnknownTierError: *tier* is not a recognized tier name.
    """
    try:
        return _TIER_ALIASES[tier]
    except KeyError:
        raise UnknownTierError(
            f"unknown model tier {tier!r}; known tiers: {sorted(_TIER_ALIASES)}"
        ) from None


def resolve_model_for_operator(operator: str) -> str:
    """Resolve an operator name straight to its tier's ``--model`` alias,
    composing ``OPERATOR_MODEL_TIER`` + ``resolve_model_for_tier``.
    Convenience for callers (.p2c/.p2d) that key on operator name rather
    than tier directly.

    Raises:
        UnknownTierError: *operator* has no entry in
            ``OPERATOR_MODEL_TIER``.
    """
    try:
        tier = OPERATOR_MODEL_TIER[operator]
    except KeyError:
        raise UnknownTierError(
            f"no model tier configured for operator {operator!r}; "
            f"known operators: {sorted(OPERATOR_MODEL_TIER)}"
        ) from None
    return resolve_model_for_tier(tier)


def resolve_model_for_flipped_operator(operator: str) -> str | None:
    """Resolve *operator*'s DEFAULT (no-env-override) model — RDR-196
    .p2d (nexus-nyry9.17). Returns the cheap-tier alias when *operator*
    is in :data:`FLIPPED_OPERATORS`; ``None`` (no override — the caller
    passes no ``model`` kwarg, so the operator dispatches at whatever
    the untiered/pre-.p2d default has always been, i.e. HOLD) for every
    other name, known or unknown. This is the sole function the DEFAULT
    (env-unset) dispatch path may consult — the whole-table resolver
    (:func:`resolve_model_for_operator`) stays reserved for the
    ``NX_OPERATOR_MODEL_TIERING=1`` measurement override, which consults
    ``OPERATOR_MODEL_TIER`` in full (including its "strong" entries).
    Deliberately never raises: an operator absent from
    ``OPERATOR_MODEL_TIER`` entirely is not this function's problem to
    diagnose — it degrades to "no override", the safe HOLD side.
    """
    return resolve_model_for_tier("cheap") if operator in FLIPPED_OPERATORS else None
