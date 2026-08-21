# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator model-tier table and resolver (RDR-196 .p2b, nexus-nyry9.15).

Ships the ABILITY to route each operator to a cost tier. **Nothing in this
module is consulted by any production call site as of this bead** —
``claude_dispatch`` (``nexus.operators.dispatch``) accepts a ``model``
keyword purely as a pass-through string; it never imports this module.
The 18 existing ``claude_dispatch`` call sites are unaffected. The
default-tier flip is a later bead (RDR-196 .p2d), gated on .p2c's
measured A/B evidence — see ``docs/rdr/rdr-196-cost-aware-nx-answer.md``
§Approach.

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
