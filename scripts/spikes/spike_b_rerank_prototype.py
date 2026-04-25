# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-088 Spike B — throwaway LLM-rerank prototype.

NOT production code. This prototype lives here strictly to run the
Phase 3 prerequisite spike (bead ``nexus-ac40.8``). If the spike clears
both pre-agreed thresholds (precision delta >= 0.05 AND recall delta
> -0.15), the prototype's shape informs the production landing in
``src/nexus/plans/matcher.py``. If the thresholds are missed, the
prototype is discarded along with Gap 4.

Given a ``(intent, top_match)`` pair where ``top_match.confidence``
lands in the caller's ambiguous band ``(effective_floor, 0.65]``,
the rerank dispatches a ``claude -p`` second-opinion call scoring the
match on a [0, 1] confidence scale. If the LLM confidence is below
0.90, the caller should drop the match to a ``NoMatch`` fall-through.

Dispatches via the existing ``claude_dispatch`` substrate — no new
infrastructure.
"""
from __future__ import annotations

from typing import Any

_RERANK_BAND_CEILING: float = 0.65
_RERANK_CONFIDENCE_GATE: float = 0.90


def _rerank_prompt(intent: str, plan_row: dict[str, Any]) -> str:
    return (
        "You are evaluating whether a retrieval plan is a good match for a "
        "user intent. Read both carefully and emit a confidence score on a "
        "[0, 1] scale.\n\n"
        f"USER INTENT:\n{intent}\n\n"
        "CANDIDATE PLAN:\n"
        f"  name: {plan_row.get('name', '')}\n"
        f"  verb: {plan_row.get('verb', '')}\n"
        f"  scope: {plan_row.get('scope', '')}\n"
        f"  match_text: {plan_row.get('match_text', '')}\n\n"
        "Return a JSON object with a single key `confidence` holding a "
        "float in [0, 1] where 1.0 means the plan is an excellent fit for "
        "the intent and 0.0 means the plan is a poor fit. Err on the side "
        "of lower confidence when the fit is partial, indirect, or relies "
        "on metaphor rather than an exact match to the plan's described "
        "capability."
    )


_RERANK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["confidence"],
    "properties": {
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
    },
}


async def rerank_confidence(
    intent: str, plan_row: dict[str, Any],
) -> float | None:
    """Return the LLM rerank confidence in [0, 1] or None on dispatch failure."""
    from nexus.operators.dispatch import claude_dispatch

    prompt = _rerank_prompt(intent, plan_row)
    try:
        result = await claude_dispatch(
            prompt, _RERANK_SCHEMA, timeout=120.0,
        )
    except Exception:  # noqa: BLE001
        return None
    conf = result.get("confidence") if isinstance(result, dict) else None
    if not isinstance(conf, (int, float)):
        return None
    return float(conf)


def in_rerank_band(confidence: float, effective_floor: float = 0.50) -> bool:
    """True when the cosine-match confidence is in the ambiguous band.

    Band is ``(effective_floor, _RERANK_BAND_CEILING]``. Above the
    ceiling the match is strong enough that an LLM second opinion is
    expected to add no value in expectation. Below the floor the match
    is filtered by the regular ``min_confidence`` gate.
    """
    return effective_floor < confidence <= _RERANK_BAND_CEILING


async def apply_rerank(
    intent: str,
    matches: list,
    *,
    library,
    effective_floor: float = 0.50,
) -> list:
    """Wrap ``plan_match`` output: fall-through empty when rerank fails gate.

    Returns the input list unchanged when:
      * No matches
      * Top match's confidence is outside the rerank band
      * Rerank confidence is None (dispatch failed; conservative: keep match)
      * Rerank confidence >= _RERANK_CONFIDENCE_GATE (0.90)

    Returns ``[]`` when rerank confidence falls below the gate — the
    caller interprets this as a match drop, falling through to dynamic
    generation / inline planning.
    """
    if not matches:
        return matches
    top = matches[0]
    if top.confidence is None:
        return matches  # FTS5 match — rerank does not apply
    if not in_rerank_band(float(top.confidence), effective_floor):
        return matches
    plan_row = library.get_plan(top.plan_id)
    if plan_row is None:
        return matches
    row_dict = dict(plan_row)  # sqlite3.Row supports dict conversion
    conf = await rerank_confidence(intent, row_dict)
    if conf is None:
        return matches  # conservative on dispatch failure
    if conf < _RERANK_CONFIDENCE_GATE:
        return []
    return matches
