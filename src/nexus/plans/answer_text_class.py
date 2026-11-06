# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The degenerate class of an ``nx_answer`` run, from its final text (nexus-ix3fj).

ONE classifier, shared by the reader (``nx answer-runs``, ``nexus.commands.answer_runs``) and the writer
(``nexus.plans.run_summary``, which records the class of the FULL final text in the run summary). It lives
here, under ``plans``, so neither depends on the other's layer. :func:`heuristic_class` is the read-time
heuristic that ``nx answer-runs`` has always run over a row; :func:`classify_answer_text` is the same rule over
a bare text and step count.

:data:`ANSWER_CLASSES` is the closed vocabulary of a class a TEXT can have (the reader accepts a recorded class only
from it), and :data:`CLASS_VERSION` numbers the rule: bump it whenever a change to this module can change the class
of a text, so a recorded class from an older rule is told apart from a current one. The heuristic matches a refusal
phrase anywhere in the text it is given, so it can count a real answer as degenerate on a long text as well as a
short one, and on a text cut at 2000 characters it misses a raw payload that no longer parses; neither is a bound.
"""
from __future__ import annotations

import re

from nexus.plans.answer_shape import NON_ANSWER_SHAPES, AnswerShape, classify_answer_shape

__all__ = ["ANSWER_CLASSES", "CLASS_VERSION", "classify_answer_text", "heuristic_class", "is_redacted_row"]

#: The version of the classification rule in this module. Recorded in the run summary next to ``answer_class``
#: (``class_version``); a change that can move the class of a text bumps it.
CLASS_VERSION: int = 1

#: Every class :func:`classify_answer_text` can return for a text: the five code-templated shapes, ``other`` (an
#: answer, or a shape nothing names), and ``non_answer/<shape>`` for each non-answer shape the notice or the body can
#: name, plus the one a coverage verdict names without text. ``redacted`` is not here: it is a property of a stored
#: row, never of a text.
ANSWER_CLASSES: frozenset[str] = frozenset({
    "other", "planner_error", "error", "budget_warning_only", "query_listing_reroute", "empty_hydration",
    "non_answer/no_usable_answer",
    *(f"non_answer/{shape.value}" for shape in NON_ANSWER_SHAPES),
})

#: Code-templated header identifying nx_answer's single-step-guard
#: reroute to ``query()`` (nexus-x79ne / RDR-200 Phase 1b-1c gate:
#: q06/q08/q19, both arms, plan_id 471, ``step_count == 1`` — a REAL
#: ``query()`` call that ran to completion and returned a well-formed
#: document listing, never reduced). This shape carries ``step_count >
#: 0``, so without a dedicated check it lands in executed_ok/executed_
#: failed (no "Error:"/exhaustion prefix, no per-step ``ok=False``
#: signal) — reading as a normal success.
#:
#: ANCHORED to the start of ``final_text`` (code review, T2 [24198]): a
#: bare ``in`` substring match let a genuine synthesized answer that
#: happens to discuss "several documents (from the 2023 filing)"
#: misclassify. core.py's plain-corpus ``query()`` renderer (the ONLY
#: variant nx_answer's single-step guard can reach — it never passes
#: catalog-routing params) always emits this header as line 1, verbatim
#: (``"Found {N} documents (from {M} chunks across {K} collections)"``);
#: the catalog-routed variant is a DIFFERENT, unreachable-from-nx_answer
#: code path whose header is preceded by a routing-note line, so
#: anchoring to start-of-string costs nothing here. Checked together
#: with ``step_count == 1`` in :func:`heuristic_class` — every
#: gate-artifact instance of this shape carries it (the single-step
#: guard is the ONLY producer of this header, and it always records
#: exactly one step).
_QUERY_LISTING_REROUTE_HEADER_RE = re.compile(r"^Found \d+ documents \(from ")

#: Prefix of a budget WARNING line (RDR-196 .p3c) — distinct from
#: ``NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX``'s ``"[budget exhausted"``:
#: a warning lets execution proceed; an exhaustion halts it. Both are
#: emitted by ``nx_answer``'s single ``_result`` closure (core.py).
_BUDGET_WARNING_LINE_PREFIX = "[budget warning ("

#: Heuristic phrases an LLM operator step emits when it was fed entirely
#: empty/placeholder hydrated content (nexus-x79ne, RDR-200 Phase 1b q12
#: both arms: ``operator_compare`` received ten empty strings). NOT a
#: deterministic code-level marker — verified against
#: ``nexus.mcp.operator_requests``'s ``build_*_request`` builders: none
#: of them short-circuit on empty input, every one always dispatches to
#: ``claude -p``, so the model always produces free text. Heuristic, like
#: ``answer_runs._row_is_failed``'s own documented blind spot: a model phrasing
#: this differently is invisible to this list. Kept broad enough to
#: catch the gate-artifact class without over-matching a real
#: comparative answer that happens to discuss an absence of differences
#: (checked as a substring of the FULL final_text, not an exact match).
_EMPTY_HYDRATION_PHRASES: tuple[str, ...] = (
    "no comparison performed",
    "nothing to compare",
    "no items to compare",
    "no items were provided",
    "no content was provided",
    "nothing to summarize",
    "no content to summarize",
    "empty input",
    "no evidence was provided",
    "no evidence provided",
)

def _redacted_coverage_class(summary: object) -> str | None:
    """The coverage-decided non-answer class of a redacted row, read from its run summary; ``None`` unless the
    summary is an ``nx_answer`` run whose coverage state is ``unavailable``. ``fit_summary`` may cut the
    ``coverage`` block (and its reason), so a missing reason falls back to ``no_usable_answer``."""
    if not isinstance(summary, dict) or summary.get("kind") != "nx_answer":
        return None
    if summary.get("coverage_state") != "unavailable":
        return None
    block = summary.get("coverage")
    reason = block.get("reason") if isinstance(block, dict) else None
    if reason is None and summary.get("outcome") == "success":
        # The ``coverage`` block was cut and a recorded success cannot be a non-answer (every unavailable run that returned a
        # non-answer is recorded ``failure``): the one unavailable success is an RDR-147 option 1 answer, whose ``repair`` block
        # (and its ``resolution_generated_on_join``) may have been cut with it.
        return None
    if reason == "uncovered":
        repair = summary.get("repair")
        if isinstance(repair, dict) and repair.get("resolution_generated_on_join") is True:
            # RDR-147 option 1 (nexus-irndi): the ledger covers no facet and the call returned an ANSWER generated after an
            # entered exact-key join, so the unavailable state does not mean a non-answer. (Prose that was not an answer
            # moves the reason to ``no_usable_answer`` and is classed below.)
            return None
        return "non_answer/uncovered"
    if reason in ("no_evidence", "scope_conflict"):
        return "non_answer/no_evidence"
    return "non_answer/no_usable_answer"

_NON_ANSWER_NOTICE_RE = re.compile(r"^\[non-answer: ([a-z_]+)\]")


def _is_budget_warning_only_body(final_text: str) -> bool:
    """True when *final_text* is one or more ``[budget warning (...):
    ...]`` lines and NOTHING ELSE (nexus-x79ne: the run proceeded past
    the warning — every recorded over-cap line "ran anyway" — but
    produced no answer at all; RDR-200 Phase 1b q01 x2/q03/q13/q15,
    Phase 1c q01a1/q13a1/q22a1/q22a2 — code review, T2 [24198]:
    corrects the citation, which previously named q22 without q03).

    A warning followed by a real answer does NOT match: the remainder
    after stripping every leading warning line is non-empty, which is
    the documented common case (a warned-but-successful run) and must
    stay out of ``degenerate``.
    """
    remainder = final_text
    matched = False
    while remainder.startswith(_BUDGET_WARNING_LINE_PREFIX):
        matched = True
        newline_idx = remainder.find("\n")
        remainder = remainder[newline_idx + 1:] if newline_idx != -1 else ""
    return matched and remainder.strip() == ""


def is_redacted_row(row: dict) -> bool:
    return str(row.get("final_text") or "") == "[redacted]" or str(row.get("question") or "") == "[redacted]"


def classify_answer_text(final_text: str, *, step_count: int) -> str:
    """The class of a run's final text, by the read-time heuristic. THE classifier: the reader runs it over the
    stored text of a row with no recorded class, and the writer (``nexus.plans.run_summary``) runs it over the
    full text and records the result (nexus-ix3fj), so there is one rule and not two."""
    return heuristic_class({"final_text": final_text, "step_count": step_count})


def heuristic_class(row: dict) -> str:
    """Sub-classify a row from fields visible on the row itself.

    NOT the per-id source disposition (dead code / live-fixed / harness
    artifact) the nx-answer-degenerate-row-taxonomy census recorded (T2
    nexus/nx-answer-degenerate-row-taxonomy-2026-08-20) — that requires
    reading the CODE at the time of the run, not the telemetry row, and
    changes as code is fixed. This is a read-time heuristic over the row's
    own ``question``/``final_text``, stable across code changes.

    That census found 24 of 39 degenerate rows (62%) were the BENIGN
    ``trace=False`` redaction class, not errors — so "degenerate" must
    never be rendered as a synonym for "broken"; the ``redacted`` class
    exists specifically so this command doesn't imply that.

    nexus-x79ne (RDR-200 Phase 1b/1c gate): three well-formed-but-
    structurally-empty body shapes accounted for 9 of 24 gate questions
    while the pre-fix server-side field reported 1 (``planner_error``
    only) — reachable on ANY row, not only ``step_count == 0`` ones (see
    ``answer_runs._WELL_FORMED_DEGENERATE_CLASSES`` and ``answer_runs._split_four_way``,
    which is what actually widens the routing for the ``step_count > 0``
    case — this function alone cannot, since it is only ever called on a
    row already selected for classification).

    ORDERING (code review, T2 [24198] — Important, falsified live): the
    pre-existing ``"[redacted]"``/``"Planner error:"``/``"Error:"``
    checks run FIRST, before the three new heuristics below. A row like
    ``{"final_text": "Error: no evidence was provided by the upstream
    retrieval service (timeout)", "step_count": 0}`` genuinely contains
    the empty-hydration phrase "no evidence was provided" as a
    substring — checking the heuristics first misclassified it
    ``empty_hydration`` instead of ``error``, and (via the widened
    ``answer_runs._split_four_way``) diverted an ``step_count > 0`` row shaped like
    it straight into ``degenerate`` WITHOUT ever reaching
    ``answer_runs._row_is_failed``. The three new heuristics are all narrow
    enough (a code-templated header, a code-templated warning-line
    prefix, an explicit refusal-phrase list) that none of them can
    plausibly fire on real error/redaction text, so this reordering
    costs nothing in the other direction.
    """
    final_text = str(row.get("final_text") or "")
    question = str(row.get("question") or "")
    if final_text == "[redacted]" or question == "[redacted]":
        # A trace=False row's text cannot carry the coverage notice, so its run summary decides
        # (nexus-jypmj.3.15): unavailable coverage means the call returned a non-answer.
        return _redacted_coverage_class(row.get("run_summary")) or "redacted"
    if final_text.startswith("Planner error:"):
        return "planner_error"
    if final_text.startswith("Error:"):
        return "error"
    if _is_budget_warning_only_body(final_text):
        return "budget_warning_only"
    if (
        int(row.get("step_count") or 0) == 1
        and _QUERY_LISTING_REROUTE_HEADER_RE.match(final_text) is not None
    ):
        return "query_listing_reroute"
    # nexus-mm5tx: the runner's zero-evidence short-circuit never reaches a
    # model; it returns this fixed prefix, so the match is exact, not a
    # phrase heuristic. Prefix-anchored: a real answer quoting the phrase
    # mid-text is not a short-circuit.
    from nexus.plans.runner import _NO_EVIDENCE_RESULT_PREFIX  # noqa: PLC0415 - deferred: heavy import (the plan runner), needed only past the cheap checks above
    if final_text.startswith(_NO_EVIDENCE_RESULT_PREFIX):
        return "empty_hydration"
    if any(phrase in final_text.strip().lower() for phrase in _EMPTY_HYDRATION_PHRASES):
        return "empty_hydration"
    # nexus-90gyo / nexus-zy0kj: the write side's own verdict first (a
    # code-templated notice, exact prefix), then the shared text-only
    # classifier over the body for rows written before the notice
    # existed (run 706 / plan 488 class).
    m = _NON_ANSWER_NOTICE_RE.match(final_text)
    if m is not None:
        return f"non_answer/{m.group(1)}"
    shape = classify_answer_shape(final_text)
    if shape in NON_ANSWER_SHAPES and shape is not AnswerShape.LISTING:
        # LISTING is already named by the step_count==1 header rule above
        # (``query_listing_reroute``); a header at another step_count is
        # deliberately "other" (see that rule's own tests).
        return f"non_answer/{shape.value}"
    return "other"
