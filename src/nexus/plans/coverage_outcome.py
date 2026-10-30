# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 3 step 3 (nexus-jypmj.3.3): the post-generation half of the coverage-state table.

The runner's boundary (:mod:`nexus.plans.coverage_ledger`) decides the state BEFORE terminal generation. Two rows
of RDR section 4's table depend on what generation returned, so they are applied here, after it:

* a **usable answer** is prose that ``classify_answer_shape`` reads as ``answered`` AND at least one required facet
  that counts toward one (``counts_toward_a_usable_answer``: ``covered`` or ``contradicted``, Sam 2026-10-27) or one
  not yet assessed. ``partial`` with a usable answer is ``insufficient_evidence``; ``partial`` or ``complete``
  without one is ``unavailable``.
* prose with every facet known and each unsupported is ``uncovered`` (Sam's ruling, nexus-jypmj.3.12); with an
  unassessed facet the prose is kept as a partial answer; ``no_evidence`` stays reserved for evidence that never
  reached the reduction.

:func:`decide_outcome` is the one place ``nx_answer`` takes ``answer_shape``, the counter outcome, the no-grow flag
and the FINAL coverage state from. The run summary is told the final state (it was built before generation), so a
downgrade here shows in the summary's counts. ``unassessed`` (and no boundary at all) changes nothing: today's
classification, today's counters, today's growth.

Pure: no model, no store.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from nexus.plans.answer_shape import (
    INSUFFICIENT_EVIDENCE_NOTICE_PREFIX,
    NON_ANSWER_SHAPES,
    AnswerShape,
)
from nexus.plans.coverage_ledger import CONFLICTED_REASON, CoverageResult, counts_toward_a_usable_answer

__all__ = [
    "NO_USABLE_ANSWER_REASON",
    "OutcomeVerdict",
    "decide_outcome",
    "render_insufficient_evidence",
]

#: ``CoverageResult.reason`` after generation when the prose is not a usable answer (a payload, empty).
NO_USABLE_ANSWER_REASON = "no_usable_answer"
#: Characters of a facet's text the notice shows.
_MAX_FACET_CHARS = 200

_STATE_LABEL = {
    "unsupported": "no supporting evidence found",
    "contradicted": "the evidence contradicts it",
    "unassessed": "not assessed",
}


def _clip(text: str, limit: int = _MAX_FACET_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass(frozen=True)
class OutcomeVerdict:
    """What ``nx_answer`` does with a finished run.

    ``shape`` is the result's ``answer_shape``; ``success`` is the plan counter outcome (``success_count`` when
    true, ``failure_count`` when false); ``no_grow`` blocks the plan-grow step even for an answer; ``coverage``
    is the coverage result with its FINAL state and reason (the run summary's), ``None`` when no boundary ran.
    """

    shape: AnswerShape
    success: bool
    no_grow: bool
    coverage: CoverageResult | None


def _usable(coverage: CoverageResult) -> int:
    """Facets that count toward a usable answer (the all-contradicted rule lives in ``counts_toward_a_usable_answer``)."""
    return sum(1 for f in coverage.ledger.findings if counts_toward_a_usable_answer(f.state))


def _unassessed(coverage: CoverageResult) -> int:
    return sum(1 for f in coverage.ledger.findings if f.state == "unassessed")


def _unavailable(coverage: CoverageResult, reason: str) -> CoverageResult:
    return coverage if (coverage.state, coverage.reason) == ("unavailable", reason) else replace(
        coverage, state="unavailable", reason=reason,
    )


def decide_outcome(coverage: CoverageResult | None, classified: AnswerShape) -> OutcomeVerdict:
    """The verdict for a run whose terminal output classified as *classified* under *coverage*."""
    if coverage is None or coverage.state == "unassessed":
        return OutcomeVerdict(classified, classified not in NON_ANSWER_SHAPES, False, coverage)

    if coverage.state == "unavailable":
        # Nothing reached the reduction: no_evidence (a scope conflict that removed it all included). Evidence
        # that covered no facet: uncovered. Whatever generation returned is not an answer.
        shape = AnswerShape.UNCOVERED if coverage.reason == "uncovered" else AnswerShape.NO_EVIDENCE
        return OutcomeVerdict(shape, False, True, coverage)

    # complete or partial: a usable answer is answered prose with at least one covered facet.
    if classified is not AnswerShape.ANSWERED:
        return OutcomeVerdict(classified, False, True, _unavailable(coverage, NO_USABLE_ANSWER_REASON))
    # No facet counts toward an answer: uncovered, but only when every facet is KNOWN. An unassessed facet is not
    # known to be uncovered (the pre-generation skip already requires none unassessed), so the prose that
    # generation paid for stays, behind the partial notice that names the unassessed facets as not assessed.
    if _usable(coverage) == 0 and _unassessed(coverage) == 0:
        return OutcomeVerdict(AnswerShape.UNCOVERED, False, True, _unavailable(coverage, "uncovered"))
    if coverage.state == "partial":
        return OutcomeVerdict(AnswerShape.INSUFFICIENT_EVIDENCE, True, True, coverage)
    return OutcomeVerdict(AnswerShape.ANSWERED, True, False, coverage)


def render_insufficient_evidence(coverage: CoverageResult, facet_texts: Mapping[str, str], prose: str) -> str:
    """The partial answer: the marker line, the unresolved facets, a blank line, then *prose*.

    ``[insufficient-evidence: <n> unresolved]`` opens the text (a count and no question text, so it survives
    ``trace=False`` wherever the text is kept). Every facet that is not covered is named with why: unsupported,
    contradicted, not assessed, or its named source was not found.
    """
    pending = [f for f in coverage.ledger.findings if f.state != "covered"]
    conflicted = [f for f in coverage.ledger.findings if f.state == "covered" and f.reason == CONFLICTED_REASON]

    def clip(facet_id: str) -> str:
        return _clip(str(facet_texts.get(facet_id) or facet_id))

    lines = [
        f"{INSUFFICIENT_EVIDENCE_NOTICE_PREFIX} {len(pending)} unresolved]",
        "The evidence did not settle these parts of the request; the answer below does not cover them:",
    ]
    for f in pending:
        label = "the named source was not found" if f.reason == "source_missing" else _STATE_LABEL.get(f.state, f.state)
        lines.append(f"  - {f.facet_id} ({label}): {clip(f.facet_id)}")
    unresolved = [r for r in coverage.ledger.sources if r.state == "unresolved"]
    if unresolved:
        # nexus-jypmj.3.17: an ambiguous or unlookable source blocked nothing; say that it was not read, and why.
        lines.append("A named source could not be resolved, so nothing of it was read; the parts above rest on other evidence:")
        lines.extend(f"  - {r.source}: {_clip(r.detail or 'unresolved')}" for r in unresolved)
    unread = coverage.ledger.assessment.skipped_items
    if unread > 0:
        # nexus-jypmj.23: the check decided on what it read; say how much it did not.
        lines.append(
            f"{unread} retrieved item(s) were not read by the coverage check (its per-call size cap), so the evidence "
            "they hold was not counted for the parts above."
        )
    if conflicted:
        lines.append("The evidence conflicts on these parts; the answer below follows the supporting evidence:")
        lines.extend(f"  - {f.facet_id} (the evidence conflicts): {clip(f.facet_id)}" for f in conflicted)
    return "\n".join(lines) + "\n\n" + prose
