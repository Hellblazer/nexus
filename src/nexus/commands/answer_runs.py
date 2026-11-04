# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx answer-runs`` — read the nx_answer_runs telemetry table (nexus-eho3u).

Every ``nx_answer`` MCP call writes a row to ``nx_answer_runs`` via
``POST /v1/telemetry/nx_answer_runs/record`` — but until engine-service
carried the ``GET /v1/telemetry/nx_answer_runs/query`` route, nothing ever
read one back. This is that read surface, shaped after ``nx tier-status``
(``src/nexus/commands/tier_status.py``): same capability-honest degrade on a
pre-route engine (404 -> "deploy a newer engine", not a silent 0), same
--json / human-table split, same "no writes" vs "unreachable" distinction.

SERVICE-MODE ONLY. ``nx_answer_runs`` has no SQLite twin left to fall back
to (RDR-158 P3 retired the local stores); this verb is the sole reader, and
it is also the capture point the shakedown playbook's §4.5 telemetry
baseline snapshot names: total row count, latency-bucket histogram (fixed
edges — "SAME QUERIES, SAME BUCKETS, EVERY TIME"), and plan-match hit rate
versus inline-planner fallback.

RDR-196 .p1e (nexus-nyry9.11) extends this same surface — never a second
command — with a per-step breakdown (``--steps``) and a three-way
executed-ok / executed-failed / degenerate split of the listed rows. See
``answer_runs_cmd``'s own docstring (what ``--help`` actually renders) for
the default-population statement.

RDR-232 Phase 3 (nexus-jypmj.3.3) adds a fourth population beside those three:
**insufficient-evidence**, a partial answer returned behind the
``[insufficient-evidence: <n> unresolved]`` notice. It is read from the run
summary the row carries (``coverage_partial``), which survives ``trace=False``
redaction, and from the notice itself when a row has no summary and its text is
visible. See ``_row_is_insufficient_evidence`` for the rule and
``docs/cli-reference.md`` § nx answer-runs for the reader-facing text.
"""
from __future__ import annotations

import json as _json
import re as _re
import statistics as _statistics
from datetime import UTC, datetime
from typing import Any

import click
import structlog

from nexus.plans.answer_text_class import ANSWER_CLASSES, heuristic_class, is_redacted_row

_log = structlog.get_logger(__name__)

#: Fixed latency bucket order for human-table rendering — matches the
#: TelemetryRepository.queryNxAnswerRuns bucket keys exactly (never
#: re-derived; see that method's docstring for the edges).
#: nexus-spbay: the first FULL day of real cost recording. The client half
#: (cc61d4c31) landed 2026-08-21 04:08 UTC and its paired engine
#: (engine-service-v0.1.85 / v7.14.0) deployed 2026-08-21 11:15 UTC —
#: cost_usd was a hardcoded 0.0 before that (critic finding: an earlier
#: 08-20 value under-warned for the 08-20..08-21T11:15 window; 08-22 is
#: the conservative first clean day, over-warning slightly for windows
#: touching 08-21 rather than ever under-warning). Rows older than this
#: dilute any cost average with structurally-fake zeros; the human report
#: labels windows that reach past it. Lexicographic compare against the
#: row's ISO created_at is sound (both are ISO-8601, zero-padded).
COST_INSTRUMENTED_SINCE = "2026-08-22"

#: RDR-200 consumer caveat (critic-F2, T2 [23952], population sweep
#: item 6), extending the SAME "always-labeled" pattern
#: COST_INSTRUMENTED_SINCE's own caveat established just above: this
#: module's WHOLE-SET block (``hit_count``/``fallback_count``/``total``/
#: ``avg_duration_ms``/``latency_buckets`` — the engine's raw
#: aggregates, printed BEFORE this file's own four-way split and
#: unrelated to it) has the SAME zero-visibility problem
#: telemetry_cmd.py's figure 1 has, for the SAME reason (RDR-200 F6,
#: zero engine change): every ``nx_answer_report`` completion row
#: (``plan_id=None``) is misclassified server-side as an inline-planner
#: fallback, and a paired continuation call contributes 2 physical rows
#: for 1 logical invocation. Unconditional — this whole-set block has
#: no per-row visibility to gate the caveat on presence/absence.
NX_ANSWER_RUNS_WHOLE_SET_CONTINUATION_CAVEAT = (
    "hit/fallback/total/avg_* above (the WHOLE --since-filtered set) "
    "have ZERO visibility into RDR-200 continuation rows: every "
    "nx_answer_report completion row is misclassified server-side as "
    "an inline-planner fallback, and a paired continuation call "
    "contributes 2 physical rows for 1 logical invocation — the "
    "four-way split above (handed-off count, unreported rate) is the "
    "marker-aware read"
)

_BUCKET_ORDER = ("under_5s", "5s_to_30s", "30s_to_2min", "2min_to_5min", "over_5min")
_BUCKET_LABEL = {
    "under_5s":    "<5s",
    "5s_to_30s":   "5s-30s",
    "30s_to_2min": "30s-2min",
    "2min_to_5min": "2min-5min",
    "over_5min":   ">5min",
}
#: Upper edge (exclusive), in ms, for each bucket except the last — mirrors
#: TelemetryRepository's SQL CASE edges (5s/30s/2min/5min) exactly, so a
#: client-side re-bucketing (RDR-196 .p1e review-fix S2 — see
#: ``_bucketize_durations``) produces the identical histogram shape the
#: engine would for the same population.
_BUCKET_EDGES_MS = (
    ("under_5s", 5_000),
    ("5s_to_30s", 30_000),
    ("30s_to_2min", 120_000),
    ("2min_to_5min", 300_000),
)

#: RDR-196 .p1c critique (T2 [23073], lme1s dev-notes): a literal
#: to-the-cent tolerance is too loose for typical $0.0001-$0.05 per-call
#: costs. Use a RELATIVE epsilon with a tiny absolute floor for the
#: near-zero case instead.
_COST_EPSILON_REL = 0.005
_COST_EPSILON_ABS_FLOOR = 1e-6


def _within_epsilon(a: float, b: float) -> bool:
    epsilon = max(_COST_EPSILON_ABS_FLOOR, _COST_EPSILON_REL * max(abs(a), abs(b)))
    return abs(a - b) <= epsilon


def _costs_agree(
    run_cost: "float | None", step_sum: "float | None", *, planner_share_allowed: bool = False,
    extraction_cost: "float | None" = None,
    out_of_step_cost: "float | None" = None,
) -> bool:
    """Whether a row's own ``cost_usd`` and the sum of its step costs
    agree within the relative/sub-cent epsilon. Both ``None`` (unknown vs
    unknown) trivially agree; exactly one ``None`` never agrees.

    *out_of_step_cost* (nexus-jypmj.5) is the run's out-of-step dispatch share as the run stored it
    (``nx_answer_runs.out_of_step_cost_usd``: the inline planner call plus the request-extraction call, neither
    a plan step's). When the row has it the check is TWO-SIDED for ANY ``plan_id``: the run cost must equal the
    step sum plus the share (a step sum of nothing known counts as zero, the way the writer adds only what it
    knows), so an over-count is caught for an inline-planned run, a library-plan run and a planner-error run
    alike, and *planner_share_allowed* and *extraction_cost* are not consulted. The rest of this docstring is
    the rule for a row WITHOUT the field (written before it, by an older client, or against an older engine).

    *extraction_cost* (nexus-jypmj.9) is the structured request extraction's cost as the row's run summary
    records it. The call is no step's, and since nexus-jypmj.9 the run's ``cost_usd`` carries it, so a run with
    a recorded extraction cost agrees at ``step_sum + extraction_cost`` (an exact share, not a one-sided
    allowance: a library plan has no planner share, so an over-count is still caught) or, for a row written
    by a release that left the call out of the run cost (7.88.0 recorded it in the summary only), at
    ``step_sum``. Without a recorded extraction cost the rule is unchanged.

    *planner_share_allowed* (an inline-planned run, ``plan_id`` 0; nexus-jypmj.1.20): the run's
    ``cost_usd`` also carries the planner dispatch's own cost, which is no step's, so a run cost
    at or above the step sum agrees, and a known run cost agrees with no known step cost. A run
    cost below its steps is still a drop. The allowance is one-sided on purpose: without the stored share,
    ``run_cost - step_sum`` cannot be told from an over-count and no upper bound on it is sound.
    """
    if out_of_step_cost is not None:
        if run_cost is None:
            return False
        return _within_epsilon(run_cost, (step_sum or 0.0) + out_of_step_cost)
    if run_cost is None and step_sum is None:
        return True
    if planner_share_allowed and run_cost is not None:
        if step_sum is None:
            return True
        if run_cost >= step_sum:
            return True
    if extraction_cost is not None and extraction_cost > 0 and not planner_share_allowed and run_cost is not None:
        base = step_sum or 0.0
        return _within_epsilon(run_cost, base) or _within_epsilon(run_cost, base + extraction_cost)
    if run_cost is None or step_sum is None:
        return False
    return _within_epsilon(run_cost, step_sum)


def _row_extraction_cost(row: dict) -> "float | None":
    """The structured request extraction's cost a row's run summary records (``request.extraction.cost_usd``),
    or ``None`` when the row has no summary, the summary lacks the block (cut for size) or the call reported no
    cost."""
    summary = row.get("run_summary")
    request = summary.get("request") if isinstance(summary, dict) else None
    extraction = request.get("extraction") if isinstance(request, dict) else None
    cost = extraction.get("cost_usd") if isinstance(extraction, dict) else None
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost != cost:
        return None
    return float(cost)


def _row_step_cost_sum(row: dict) -> "float | None":
    """Sum of a row's known (non-``None``) step costs, or ``None`` when no
    step reports a known cost — never a fabricated zero (mirrors
    ``_nx_answer_record_run``'s own blanket rule, core.py)."""
    steps = row.get("steps") or []
    known = [s.get("cost_usd") for s in steps if s.get("cost_usd") is not None]
    return sum(known) if known else None


def _bucketize_durations(durations_ms: "list[int]") -> dict[str, int]:
    """Client-side re-bucketing of a duration list into the SAME
    fixed-edge histogram shape ``TelemetryRepository`` computes
    server-side (see ``_BUCKET_EDGES_MS``). RDR-196 .p1e review-fix S2:
    lets the page-scoped executed-ok population get its own, honestly
    page-scoped, latency-bucket view rather than borrowing the engine's
    whole-set one."""
    buckets = {k: 0 for k in _BUCKET_ORDER}
    for d in durations_ms:
        dur = int(d or 0)
        for key, edge in _BUCKET_EDGES_MS:
            if dur < edge:
                buckets[key] += 1
                break
        else:
            buckets["over_5min"] += 1
    return buckets


#: The subset of :func:`_classify_degenerate_row`'s return values that
#: are reachable regardless of ``step_count`` (nexus-x79ne). Unlike the
#: pre-existing ``redacted``/``planner_error``/``error``/``other``
#: classes — which stay scoped to ``step_count == 0`` rows exactly as
#: before this bead — a row matching one of these three lands in
#: ``degenerate`` even when it executed real steps, because the shape
#: itself (a well-formed listing / a warning-only body / an empty-
#: hydration refusal) IS the degenerate signal, not the step count.
_WELL_FORMED_DEGENERATE_CLASSES: frozenset[str] = frozenset({
    "query_listing_reroute", "empty_hydration", "budget_warning_only",
    # nexus-90gyo / nexus-zy0kj: the answer-shape non-answers. Rows
    # written by a post-fix nx_answer carry the ``[non-answer: <shape>]``
    # notice (classified below by its prefix); rows written BEFORE the
    # fix (run 706 / plan 488 class) still carry the raw payload, which
    # the shared classifier names directly from the body.
    "non_answer/hydration_dump", "non_answer/extractions_only",
    "non_answer/ranking_only", "non_answer/operator_payload",
    "non_answer/retrieval_only",
    "non_answer/listing", "non_answer/empty",
    # RDR-232 Phase 3 (nexus-jypmj.3.3): the coverage-decided non-answers carry the notice too. Without these
    # a row with executed steps would fall through to executed-ok.
    "non_answer/uncovered", "non_answer/no_evidence",
    # A redacted row whose summary says unavailable because the generated prose was no usable answer: the
    # text that named the shape was not recorded (nexus-jypmj.3.15).
    "non_answer/no_usable_answer",
})


def _recorded_answer_class(row: dict) -> str | None:
    """The class the writer recorded in the row's run summary (``answer_class``), or ``None`` when the row has none
    or it is not in the classifier's vocabulary (nexus-ix3fj). The writer computed it over the FULL final text with
    :func:`nexus.plans.answer_text_class.classify_answer_text`; the row's own ``final_text`` is cut at 2000
    characters. Only a name in :data:`nexus.plans.answer_text_class.ANSWER_CLASSES` is taken, so a summary from a
    future or foreign writer cannot put an arbitrary string into the degenerate breakdown."""
    summary = row.get("run_summary")
    if not isinstance(summary, dict):
        return None
    recorded = summary.get("answer_class")
    if isinstance(recorded, str) and recorded in ANSWER_CLASSES:
        return recorded
    return None


def _classify_with_basis(row: dict) -> tuple[str, str]:
    """``(class, basis)`` of one row. The basis says what decided the class:

    * ``recorded``: the class the writer recorded over the full final text. It is taken for every row that has one,
      a redacted (``trace=False``) row included: the writer classified the real text before the row was redacted,
      so a redacted raw payload is degenerate here. One exception: a recorded ``other`` on a redacted row still
      yields to a coverage verdict in its run summary (``unavailable`` coverage is a non-answer whatever prose
      was generated; nexus-jypmj.3.15).
    * ``redacted``: a redacted row with no recorded class; it stays ``redacted`` (or the coverage verdict), the benign
      class that says nothing was recorded to judge it by.
    * ``heuristic``: a row with a visible text and no recorded class (written before this change, or by an engine
      without the run summary); :func:`nexus.plans.answer_text_class.heuristic_class` over the stored text, which is
      cut at 2000 characters.
    """
    recorded = _recorded_answer_class(row)
    redacted = is_redacted_row(row)
    if recorded is not None:
        if redacted and recorded == "other":
            verdict = heuristic_class(row)
            if verdict != "redacted":
                return verdict, "recorded"
        return recorded, "recorded"
    return heuristic_class(row), "redacted" if redacted else "heuristic"


def _classify_degenerate_row(row: dict) -> str:
    """The class of one row: the class the writer recorded over the full final text when the row's run summary
    carries one, else :func:`nexus.plans.answer_text_class.heuristic_class` over the stored (cut) text
    (nexus-ix3fj). See :func:`_classify_with_basis` for which one decided and the redacted-row rules."""
    return _classify_with_basis(row)[0]


def _classification_basis(row: dict) -> str:
    """``recorded``, ``redacted`` or ``heuristic``: what decided *row*'s class (see :func:`_classify_with_basis`)."""
    return _classify_with_basis(row)[1]


def _classification_basis_counts(rows: "list[dict]") -> dict[str, int]:
    """How many of *rows* were classified from a recorded class, how many were redacted with none recorded, and how
    many by the read-time heuristic over the stored text. Only the heuristic rows are text-limited: they see the
    text cut at 2000 characters, so a raw payload past the cut reads as an answer there, and the phrase match can
    count a real answer as degenerate (on a recorded row too, where it saw the whole text). The degenerate counts
    are exact on no basis; they are limited by truncation while ``heuristic`` is non-zero."""
    counts = {"recorded": 0, "heuristic": 0, "redacted": 0}
    for r in rows:
        counts[_classification_basis(r)] += 1
    return counts


def _row_is_failed(row: dict) -> bool:
    """Whether an EXECUTED row (``step_count > 0``) is a failed run, not a
    success. RDR-196 .p1e review-fix (Important, code-review-expert T2
    [23110]): before this fix, a run that completed >=1 real steps and
    then failed landed in the same "executed" bucket as a genuine
    success — reproducing exactly the population-mislabeling class this
    bead exists to prevent (the 45x-wrong docstring counted degenerate
    rows as successes; this would have counted FAILED-but-partially-
    executed rows as successes instead, a different flavor of the same
    mistake).

    THREE independently-sufficient signals, checked in this order:

    1. ``final_text`` starts with ``"Error:"`` (core.py's plan-execution
       exception-path prefix, the same string
       :func:`_classify_degenerate_row` already keys on for the
       step_count==0 case).
    2. ``final_text`` starts with the budget-exhausted partial-answer
       marker's prefix (RDR-196 .p1e review-fix round 2, code-review-
       expert T2 [23115]): a budget-exhausted run is a NAMED failure
       class (nexus-yg49g doctrine explicitly records it as
       ``success=False``) whose ``step_count`` can be > 0 and whose
       ``final_text`` is NEVER "Error:"-prefixed — before this signal,
       such a row misclassified as executed-ok in the tool's DEFAULT
       (no ``--steps``) invocation, silently polluting
       ``executed_ok_avg_duration_ms``/``executed_ok_latency_buckets``
       with exactly the near-deadline durations those fields exist to
       exclude. Imports ``NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX``
       from ``nexus.mcp.core`` (the single emitter of this text) rather
       than retyping the literal, so the two can never drift apart.
    3. Only checkable when ``--steps`` fetched the row's ``steps``: the
       LAST recorded step's ``ok`` is ``False`` (a plan that ran to
       completion but whose terminal dispatch itself failed).

    RULE when NONE of the three signals is observable (no known
    text-marker prefix matched, AND ``--steps`` was not requested so
    there is no per-step ``ok`` to check): the row is classified
    executed-OK by default. This is a stated, accepted blind spot, not
    a silent one — a failure whose ONLY signal is a per-step
    ``ok=False`` is invisible to the default (no ``--steps``) view;
    pass ``--steps`` to see it. Every failure class this arc has
    produced so far (plan-execution exception, budget exhaustion) DOES
    carry a ``final_text`` marker and is caught by signals 1-2 without
    ``--steps``; only a hypothetical FUTURE failure class with neither
    a text marker nor an "Error:"/budget prefix would fall into this
    blind spot.
    """
    final_text = str(row.get("final_text") or "")
    if final_text.startswith("Error:"):
        return True
    # RDR-196 .p1e review-fix round 2 (code-review-expert T2 [23115]):
    # deferred import (heavy nexus.mcp.core module; matches this
    # module's existing PLC0415 convention, and doc.py's own precedent
    # of deferred-importing a single name from nexus.mcp.core) --
    # imports the SAME constant core.py's single emitter uses, never a
    # retyped literal.
    from nexus.mcp.core import NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

    if final_text.startswith(NX_ANSWER_BUDGET_EXHAUSTED_MARKER_PREFIX):
        return True
    steps = row.get("steps")
    if isinstance(steps, list) and steps:
        last = steps[-1]
        if isinstance(last, dict) and last.get("ok") is False:
            return True
    return False


#: Extracts the ``continuation_id`` embedded in either RDR-200 marker's
#: ``final_text`` (``NX_ANSWER_CONTINUATION_MARKER_PREFIX`` /
#: ``NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX``, both
#: ``core.py``-defined — see ``_row_is_handed_off``/``_row_is_report_
#: event`` for the deferred import of the actual prefix strings).
#:
#: Marker-collision hardening (critic-F3, T2 [23952], falsified live: a
#: crafted row whose synthesized ``final_text`` merely STARTS WITH the
#: marker prefix — organic or adversarial corpus content landing
#: verbatim in a generate/summarize step's output, the same threat
#: class R7's fence-escape hardening treats as live — was silently
#: misclassified as a genuine handoff/report by a bare prefix match,
#: vanishing from executed_ok/failed and permanently inflating
#: unreported_count with an unpairable fabricated id). The id pattern
#: is now anchored to the EXACT shape ``continuation_envelope.py``'s
#: ``continuation_id = str(uuid.uuid4())`` always produces (8-4-4-4-12
#: hex, case-insensitive) — reduces, but per the critique's own ruling
#: does not eliminate, the false-positive surface: a deliberate,
#: byte-exact reproduction of marker+valid-uuid is not distinguishable
#: from a real one by this check alone.
_UUID4_SHAPE = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_CONTINUATION_ID_RE = _re.compile(rf"continuation_id=({_UUID4_SHAPE})")


def _continuation_id_from_text(final_text: str) -> "str | None":
    """The ``continuation_id`` embedded in a handoff or report row's
    ``final_text``, or ``None`` when the text carries neither marker,
    no id, or an id that isn't uuid4-shaped."""
    m = _CONTINUATION_ID_RE.search(final_text)
    return m.group(1) if m else None


def _marker_carries_parseable_id(text: str, prefix: str) -> bool:
    """Whether *text* both starts with *prefix* AND carries a PARSEABLE
    (uuid4-shaped) ``continuation_id=`` immediately after it — the
    tightened classification gate (critic-F3). Anchored to the exact
    position right after ``"{prefix} continuation_id="`` so a
    coincidental uuid elsewhere later in caller-controlled excerpt text
    can never satisfy this; the id must be delimited by whitespace,
    ``]``, or end-of-string right after it — the handoff marker's
    ``...id]`` and the report marker's ``...id ok=True]`` shapes both
    match, nothing else does.
    """
    if not text.startswith(prefix):
        return False
    pattern = (
        rf"^{_re.escape(prefix)} continuation_id="
        rf"(?:{_UUID4_SHAPE})(?:[\s\]]|$)"
    )
    return _re.match(pattern, text) is not None


def _row_is_handed_off(row: dict) -> bool:
    """Whether *row* is an RDR-200 continuation HANDOFF row (nexus-4e75w.5).

    Imports ``NX_ANSWER_CONTINUATION_MARKER_PREFIX`` from ``nexus.mcp.
    core`` (the single emitter of this text) rather than retyping the
    literal — same convention as ``_row_is_failed``'s own budget-marker
    import, for the same reason: a hand-typed second copy silently
    drifts the moment either side's wording changes. Requires a
    PARSEABLE id (:func:`_marker_carries_parseable_id`), not a bare
    prefix match — critic-F3 marker-collision hardening.
    """
    from nexus.mcp.core import NX_ANSWER_CONTINUATION_MARKER_PREFIX  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

    return _marker_carries_parseable_id(
        str(row.get("final_text") or ""), NX_ANSWER_CONTINUATION_MARKER_PREFIX,
    )


def _row_is_continuation_report(row: dict) -> bool:
    """Whether *row* is an RDR-200 continuation REPORT EVENT (written by
    ``nx_answer_report``), never a run. Same deferred-import and
    parseable-id convention as :func:`_row_is_handed_off`."""
    from nexus.mcp.core import NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

    return _marker_carries_parseable_id(
        str(row.get("final_text") or ""), NX_ANSWER_CONTINUATION_REPORT_MARKER_PREFIX,
    )


def _row_is_insufficient_evidence(row: dict) -> bool:
    """Whether *row* is a partial answer returned with the insufficient-evidence notice (RDR-232 section 4,
    nexus-jypmj.3.3; gate critique S1).

    The notice opens the recorded text, but a ``trace=False`` row records ``[redacted]``, so the text cannot
    classify it. The rule, in order:

    1. A row that carries a run summary with the coverage counts is bucketed from the summary, for both
       ``trace`` values: a successful ``nx_answer`` run whose ``coverage_partial`` is 1. The summary is the
       record of the state the call RETURNED (a partial ledger whose prose was no usable answer is recorded
       ``unavailable``), so text that merely opens like the notice cannot override it, and a partial ledger on
       a run that did not succeed (budget exhausted, error, hand-off) is not an insufficient-evidence result.
    2. Without a summary (an engine that predates the column, or a row written before it), a row whose text is
       visible (``trace=True``) is bucketed by the notice's prefix at the start of the text.
    3. A ``trace=False`` row without a summary has nothing left to read and stays redacted, as documented in
       the command's help text.
    """
    summary = row.get("run_summary")
    if isinstance(summary, dict) and "coverage_partial" in summary:
        return (
            summary.get("kind") == "nx_answer"
            and summary.get("outcome") == "success"
            and summary.get("coverage_partial") == 1
        )
    from nexus.plans.answer_shape import INSUFFICIENT_EVIDENCE_NOTICE_PREFIX  # noqa: PLC0415 - deferred: keep CLI startup fast

    return str(row.get("final_text") or "").startswith(INSUFFICIENT_EVIDENCE_NOTICE_PREFIX)


def _split_insufficient_evidence(rows: "list[dict]") -> "tuple[list[dict], list[dict]]":
    """``(insufficient_evidence_rows, the_rest)``: the bucket is taken off BEFORE the four-way split, so these
    rows are in none of executed-ok, executed-failed, handed-off or degenerate."""
    bucket: list[dict] = []
    rest: list[dict] = []
    for r in rows:
        (bucket if _row_is_insufficient_evidence(r) else rest).append(r)
    return bucket, rest


def _split_four_way(
    rows: "list[dict]",
) -> "tuple[list[dict], list[dict], list[dict], dict[str, list[dict]], list[dict]]":
    """Split *rows* (a fetched PAGE, not the whole filtered set) into
    executed-ok, executed-failed, handed-off, and degenerate — RDR-200
    §Telemetry's four-way run split (nexus-4e75w.5), widened by
    nexus-x79ne. A row carrying the continuation REPORT marker is a
    report EVENT, not a run: it is returned separately (*report_events*,
    for :func:`_continuation_pairing_stats` to pair against the
    handed-off rows) and never lands in any of the four run buckets — it
    can never double-count a run or misclassify as ``degenerate`` on its
    own ``step_count == 0``.

    Marker checks run BEFORE the ``step_count`` branch precisely because
    a handoff can legitimately carry ``step_count == 0`` (a cut at the
    very first plan step — an all-operator-suffix plan) — without
    checking the marker first, such a row would misclassify as
    degenerate instead of handed-off.

    nexus-x79ne: :func:`_classify_degenerate_row` now runs on EVERY row
    reaching this point, not only ``step_count == 0`` ones — the RDR-200
    Phase 1b/1c gate found three well-formed-but-structurally-empty body
    shapes (a query()-listing reroute, a budget-warning-only body, an
    empty-hydration refusal) that carry ``step_count > 0`` and, gated on
    the OLD ``step_count == 0``-only call, never reached classification
    at all — the server-side ``degenerate_count`` read 1 where the gate
    artifacts showed 9. Only the THREE classes in
    :data:`_WELL_FORMED_DEGENERATE_CLASSES` divert a row here; a
    ``step_count > 0`` row classified ``redacted``/``planner_error``/
    ``error``/``other`` (impossible for the first three by construction,
    but ``redacted`` can legitimately co-occur with real executed steps)
    proceeds through the UNCHANGED executed/failed split.

    Returns ``(executed_ok, executed_failed, handed_off,
    degenerate_by_class, report_events)``.
    """
    executed_ok: list[dict] = []
    executed_failed: list[dict] = []
    handed_off: list[dict] = []
    degenerate: dict[str, list[dict]] = {}
    report_events: list[dict] = []
    for r in rows:
        if _row_is_continuation_report(r):
            report_events.append(r)
            continue
        if _row_is_handed_off(r):
            handed_off.append(r)
            continue
        cls = _classify_degenerate_row(r)
        if cls in _WELL_FORMED_DEGENERATE_CLASSES:
            degenerate.setdefault(cls, []).append(r)
        elif int(r.get("step_count") or 0) > 0:
            (executed_failed if _row_is_failed(r) else executed_ok).append(r)
        else:
            degenerate.setdefault(cls, []).append(r)
    return executed_ok, executed_failed, handed_off, degenerate, report_events


def _continuation_pairing_stats(
    handed_off: "list[dict]", report_events: "list[dict]",
) -> dict[str, Any]:
    """Pair handoff rows to report events by ``continuation_id`` and
    compute the **unreported rate** — RDR-200 §Telemetry's honest
    relabelling of what this metric measures: "a handoff with no paired
    completion is counted as unreported, not abandoned" — the mechanism
    cannot distinguish a reduction that never happened from one that
    happened and was never reported (the report line is a convention
    the calling model must remember to follow).

    A handoff row whose ``final_text`` carries no parseable
    ``continuation_id`` (malformed, should not happen in practice) is
    excluded from the denominator rather than silently counted either
    way — ``unparseable_handoff_count`` surfaces it instead.
    """
    reported_ids = {
        cid for r in report_events
        if (cid := _continuation_id_from_text(str(r.get("final_text") or "")))
    }
    handoff_ids = [
        _continuation_id_from_text(str(r.get("final_text") or ""))
        for r in handed_off
    ]
    unparseable = sum(1 for cid in handoff_ids if cid is None)
    parseable_ids = [cid for cid in handoff_ids if cid is not None]
    total = len(parseable_ids)
    unreported = sum(1 for cid in parseable_ids if cid not in reported_ids)
    return {
        "handoff_count": total,
        "reported_count": total - unreported,
        "unreported_count": unreported,
        "unreported_rate": (unreported / total) if total else None,
        "unparseable_handoff_count": unparseable,
        "report_event_count": len(report_events),
    }


def _run_step_elapsed_ms(row: dict, steps: "list[dict]") -> int:
    """A run's step time: the sum of its steps' ``elapsed_ms``, with the coverage assessment counted once.

    The assessment's calls run concurrently (nexus-jypmj.17), each its own ``operator_check`` step numbered past the
    plan's own ``step_count`` steps, so their ``elapsed_ms`` overlap and a plain sum reports more than the run took.
    When the row's run summary carries ``coverage.assessment_latency_ms`` (the stage's wall time), the assessment
    steps contribute ``min(their sum, that latency)``: the latency when the calls overlapped, their sum when they did
    not (the stage's own time also covers repair and queueing, so it can exceed a serial sum). A row without the
    summary value keeps the plain sum, which is then an upper bound.
    """
    total = sum(int(s.get("elapsed_ms") or 0) for s in steps)
    summary = row.get("run_summary")
    coverage = summary.get("coverage") if isinstance(summary, dict) else None
    latency = coverage.get("assessment_latency_ms") if isinstance(coverage, dict) else None
    plan_steps = row.get("step_count")
    if isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency != latency or isinstance(plan_steps, bool) or not isinstance(plan_steps, int):
        return total
    assessment = sum(
        int(s.get("elapsed_ms") or 0) for s in steps
        if s.get("operator") == "operator_check" and isinstance(s.get("step_index"), int) and s["step_index"] >= plan_steps
    )
    return total - assessment + min(assessment, int(latency))


def _step_breakdown(rows: "list[dict]") -> dict[str, Any]:
    """Per-operator / per-source cost+elapsed aggregates (count, sum,
    median), per-plan cost/elapsed medians, and run-vs-sum(steps)
    cost-consistency violations — computed ONLY from *rows* carrying a
    ``steps`` list (i.e. ``--steps`` was requested and the engine
    supports it; a row with no steps, or an empty ``steps`` list,
    contributes nothing here).

    *rows* is the caller-chosen population: executed-ok only by default,
    or executed-ok + executed-failed when ``--include-failed`` is passed
    (RDR-196 .p1e review-fix, Important) — this function itself is
    population-agnostic, it only ever sees what the caller hands it.

    Unknown (``None``) per-step costs are excluded from sums/medians —
    their count is still surfaced (``unknown_cost_count``), never folded
    in as a silent zero.
    """
    # RDR-196 .p1e review-fix (S1, substantive-critic T2 [23111]):
    # by_operator/by_source now compute median_cost_usd/median_elapsed_ms
    # too, matching by_plan's existing shape — accumulate raw lists here,
    # finalize (sum + median) once at the end via _finalize_agg below.
    by_operator_raw: dict[str, dict[str, Any]] = {}
    by_source_raw: dict[str, dict[str, Any]] = {}
    by_plan_costs: dict[str, list[float]] = {}
    by_plan_elapsed: dict[str, list[int]] = {}
    violations: list[dict[str, Any]] = []

    def _bump(bucket: dict[str, dict[str, Any]], key: str, *, cost: "float | None", elapsed_ms: int) -> None:
        entry = bucket.setdefault(key, {
            "count": 0, "known_costs": [], "unknown_cost_count": 0, "elapsed_list": [],
        })
        entry["count"] += 1
        entry["elapsed_list"].append(elapsed_ms)
        if cost is not None:
            entry["known_costs"].append(cost)
        else:
            entry["unknown_cost_count"] += 1

    def _finalize_agg(raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for key, entry in raw.items():
            costs = entry["known_costs"]
            elapsed = entry["elapsed_list"]
            out[key] = {
                "count": entry["count"],
                "known_cost_count": len(costs),
                "unknown_cost_count": entry["unknown_cost_count"],
                "total_cost_usd": sum(costs) if costs else 0.0,
                "median_cost_usd": _statistics.median(costs) if costs else None,
                "total_elapsed_ms": sum(elapsed),
                "median_elapsed_ms": _statistics.median(elapsed) if elapsed else None,
            }
        return out

    for row in rows:
        steps = row.get("steps") or []
        if not steps:
            continue
        plan_id = row.get("plan_id")
        # RDR-196 .p1e review-fix (S3, substantive-critic T2 [23111]):
        # `if plan_id else "fallback"` used Python truthiness, so
        # plan_id=0 -- the documented ad-hoc inline-planner sentinel
        # (core.py::_nx_answer_plan_miss's `Match(plan_id=0, ...)`;
        # plans.id is BIGSERIAL, 0 can never be a real plan) -- collapsed
        # into the SAME bucket as plan_id=None (a genuine planner-error
        # miss, no plan at all). Two different populations conflated;
        # `is not None` distinguishes "a real plan_id, possibly 0" from
        # "no plan_id".
        plan_key = str(plan_id) if plan_id is not None else "fallback"
        run_cost = row.get("cost_usd")
        step_sum = _row_step_cost_sum(row)
        out_of_step_cost = row.get("out_of_step_cost_usd")  # nexus-jypmj.5: None for a row that stored none
        if isinstance(out_of_step_cost, bool) or not isinstance(out_of_step_cost, (int, float)) or out_of_step_cost != out_of_step_cost:
            out_of_step_cost = None
        if not _costs_agree(
            run_cost, step_sum, planner_share_allowed=plan_id == 0, extraction_cost=_row_extraction_cost(row),
            out_of_step_cost=out_of_step_cost,
        ):
            violations.append({
                "id": row.get("id"), "run_cost_usd": run_cost,
                "step_cost_sum_usd": step_sum,
            })
        for s in steps:
            op = str(s.get("operator") or "")
            src = str(s.get("source") or "")
            cost = s.get("cost_usd")
            elapsed = int(s.get("elapsed_ms") or 0)
            _bump(by_operator_raw, op, cost=cost, elapsed_ms=elapsed)
            _bump(by_source_raw, src, cost=cost, elapsed_ms=elapsed)
        if step_sum is not None:
            by_plan_costs.setdefault(plan_key, []).append(step_sum)
        by_plan_elapsed.setdefault(plan_key, []).append(_run_step_elapsed_ms(row, steps))

    by_plan: dict[str, dict[str, Any]] = {}
    for plan_key in set(by_plan_costs) | set(by_plan_elapsed):
        costs = by_plan_costs.get(plan_key) or []
        elapsed = by_plan_elapsed.get(plan_key) or []
        by_plan[plan_key] = {
            "run_count": len(elapsed),
            "median_cost_usd": _statistics.median(costs) if costs else None,
            "median_elapsed_ms": _statistics.median(elapsed) if elapsed else None,
        }

    return {
        "by_operator": _finalize_agg(by_operator_raw),
        "by_source": _finalize_agg(by_source_raw),
        "by_plan": by_plan,
        "cost_consistency_violations": violations,
    }


#: Short connect/read timeout for the BEST-EFFORT plan-library lookups
#: below -- distinct from HttpPlanLibrary's own 30s default. This column
#: is a nice-to-have enrichment of an already-successful report, never a
#: gate; a slow/unreachable plan-library service should degrade this ONE
#: column to None quickly rather than making the whole `--steps` report
#: wait up to 30s per plan_id.
_PREDICTED_COST_LOOKUP_TIMEOUT_S: float = 3.0


def _add_predicted_costs(by_plan: dict[str, dict[str, Any]], telemetry_store: Any) -> None:
    """Read-time estimate-vs-actual (RDR-196 Phase 3 Step 1, nexus-nyry9.20,
    code-review round 1 dormancy item #2): for each real ``plan_id`` key in
    *by_plan* (``"fallback"`` -- the ad-hoc inline-planner sentinel, no
    plan JSON exists for it -- is skipped), fetch the plan's stored
    ``plan_json`` from the plan library and compute a PREDICTED cost via
    :func:`nexus.plans.cost_estimate.estimate_plan_cost` against a price
    table built from *telemetry_store* -- the SAME mechanism ``nx_answer``'s
    own Step 1 selection uses, so a caller comparing this column against
    the row's recorded ``median_cost_usd`` sees the estimate the live
    system would actually have produced, not an independently-reimplemented
    approximation.

    Client-only and computed FRESH on every call -- nothing here is
    persisted; ``predicted_cost_usd`` is not yet a column on
    ``nx_answer_runs``/``nx_answer_steps`` (see docs/cli-reference.md's
    "Predicted vs actual cost" section for what is and is not yet wired).

    Mutates *by_plan* in place, adding ``predicted_cost_usd`` (``float |
    None``) and ``predicted_basis`` (``str``) to every entry. Fails soft
    at every boundary -- a price-table build failure, an unreachable plan
    library, a deleted plan, or a malformed ``plan_json`` all degrade that
    plan's (or every plan's) predicted fields to ``None`` with a named
    ``predicted_basis`` reason, never crash the report and never a silent
    ``0``.
    """
    from nexus.plans.cost_estimate import (  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast
        build_operator_price_table,
        estimate_plan_cost,
    )

    price_table = None
    try:
        price_table = build_operator_price_table(telemetry_store)
    except Exception:  # noqa: BLE001 — boundary catch; predicted-cost column degrades to None, must not crash the report
        _log.debug("answer_runs_price_table_build_failed", exc_info=True)

    library = None
    if price_table is not None:
        try:
            from nexus.db.t2.http_plan_library import HttpPlanLibrary  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

            library = HttpPlanLibrary(timeout=_PREDICTED_COST_LOOKUP_TIMEOUT_S)
        except Exception:  # noqa: BLE001 — boundary catch; predicted-cost column degrades to None, must not crash the report
            _log.debug("answer_runs_plan_library_init_failed", exc_info=True)

    for plan_key, entry in by_plan.items():
        entry["predicted_cost_usd"] = None
        entry["predicted_basis"] = "unavailable"
        if plan_key == "fallback" or price_table is None or library is None:
            if plan_key == "fallback":
                entry["predicted_basis"] = "ad-hoc-no-plan-json"
            continue
        try:
            plan_id = int(plan_key)
        except ValueError:
            continue
        try:
            row = library.get_plan(plan_id)
        except Exception:  # noqa: BLE001 — boundary catch; this plan's predicted cost stays None, others unaffected
            _log.debug("answer_runs_plan_lookup_failed", plan_id=plan_id, exc_info=True)
            continue
        if row is None:
            entry["predicted_basis"] = "plan-not-found"
            continue
        plan_json = row.get("plan_json")
        if not plan_json:
            entry["predicted_basis"] = "plan-json-missing"
            continue
        estimate = estimate_plan_cost(plan_json, price_table)
        entry["predicted_cost_usd"] = estimate.usd
        entry["predicted_basis"] = estimate.basis


@click.command("answer-runs")
@click.option(
    "--since", "since", default=None,
    help=(
        "ISO 8601 timestamp; only count/list runs at or after this moment. "
        "created_at is SERVER-stamped (the engine's clock, not this "
        "machine's) — a run written moments ago may not appear for a "
        "sub-second-precision --since value if the two clocks have drifted "
        "by even a few hundred ms; prefer a cutoff with real separation "
        "(minutes+) from any write you expect to see."
    ),
)
@click.option(
    "--limit", "limit", type=int, default=20,
    help="Max rows in the listed page (does not affect the whole-set aggregates).",
)
@click.option(
    "--before-id", "before_id", type=click.IntRange(min=1), default=None,
    help=(
        "List only runs with id below N, highest id first (nexus-1ss4n). The engine caps a page at 300 "
        "rows, so --limit alone cannot reach older runs: take the smallest id of the page you have and "
        "pass it here for the next one. Ids do not move while runs are recorded, so pages neither skip nor "
        "repeat. The totals and averages stay those of the whole --since-filtered set, not of this "
        "window. Needs an engine that advertises nx_answer_runs_before_id_supported; an older engine "
        "would ignore the bound and return the newest rows again, so the command refuses (exit 1) instead."
    ),
)
@click.option(
    "--all", "all_rows", is_flag=True, default=False,
    help=(
        "List EVERY run in the --since window, newest id first, paging the engine 300 rows at a time "
        "(nexus-1ss4n). Replaces --limit (not combinable with it, --before-id or --derive-budget). "
        "The totals and averages are the whole --since-filtered set, as without --all. Same engine "
        "requirement and refusal as --before-id."
    ),
)
@click.option(
    "--steps", "want_steps", is_flag=True, default=False,
    help=(
        "Also fetch and render the per-step breakdown (by operator, by "
        "source llm|sql|bundle — count/sum/median cost+elapsed each — plus "
        "per-plan cost/elapsed medians) for the listed rows, plus a "
        "run-vs-sum(steps) cost-consistency check. Requires an engine "
        "carrying the nx_answer_steps read route (nexus-lme1s / RDR-196 "
        ".p1c-b) — an older engine silently ignores the request "
        "server-side, so this command probes the same capability signal "
        "the write side uses and says so explicitly rather than rendering "
        "an empty breakdown indistinguishable from 'no steps were ever "
        "recorded'."
    ),
)
@click.option(
    "--include-failed", "include_failed", is_flag=True, default=False,
    help=(
        "Fold executed-but-FAILED rows into the --steps breakdown "
        "population alongside executed-ok rows (default: executed-ok "
        "only). A failed run still ran real, billable steps before it "
        "failed; --include-failed answers 'what did failures cost', "
        "the default answers 'what does a working run cost'."
    ),
)
@click.option(
    "--derive-budget", "derive_budget", is_flag=True, default=False,
    help=(
        "RDR-196 Phase 3 Step 0 (nexus-nyry9.19): derive the default "
        "budget_usd from recorded POST-FLIP run history. Scans executed-ok "
        "runs (implies --steps), keeps only runs whose flipped-operator "
        "steps recorded a cheap-tier canonical model (pre-flip strong-tier "
        "rows are a different population and are counted, not pooled), "
        "and prints per-run cost percentiles with the fraction of runs "
        "each would have refused. Names NO value below the non-vacuity "
        "floor. Replaces the rest of the report. Honors --since; --limit "
        "is raised to at least 300 (the telemetry page cap) because the "
        "derivation needs every row it can see, not a display page; "
        "--steps is implied and --include-failed is ignored (failed runs "
        "never enter a budget derivation)."
    ),
)
@click.option(
    "--json", "json_out", is_flag=True, default=False,
    help="Emit structured JSON instead of the human table.",
)
@click.pass_context
def answer_runs_cmd(
    ctx: click.Context,
    since: str | None,
    limit: int,
    before_id: int | None,
    all_rows: bool,
    want_steps: bool,
    include_failed: bool,
    derive_budget: bool,
    json_out: bool,
) -> None:
    """Read nx_answer_runs: recent runs plus exact aggregates (nexus-eho3u).

    Reports total run count, plan-match hit rate vs. inline-planner
    fallback, average duration/cost, and a fixed-edge latency histogram —
    the §4.5 shakedown-playbook baseline block, computed over the WHOLE
    ``--since``-filtered set (including degenerate rows: a degenerate run
    still spent real wall-clock time, and its now-nullable ``cost_usd`` is
    correctly excluded by the engine's own AVG-ignores-NULL semantics) —
    plus the last N rows.

    RDR-196 .p1e (nexus-nyry9.11) adds a page-scoped (of the ``--limit``
    rows actually listed, NOT the whole-set block above — the two are
    DIFFERENT populations, printed and labeled separately so neither is
    mistaken for the other) three-way split: executed-ok, executed-failed
    (completed >=1 step but the run itself failed), and degenerate
    (``step_count == 0``, itself named by class: redacted / planner_error
    / error / other). **Default population for every ``--steps``
    aggregate (by-operator/by-source/by-plan) and for the page-scoped
    executed-ok latency block: executed-ok rows only** — the failure
    population that produced this arc's original 45x-wrong latency figure
    must never silently re-enter an aggregate meant to answer "what does a
    normal run cost/take". Pass ``--include-failed`` to fold
    executed-failed rows into the ``--steps`` breakdown too.

    RDR-232 Phase 3 (nexus-jypmj.3.3): a partial answer, returned with the
    ``[insufficient-evidence: <n> unresolved]`` notice ahead of the prose, is
    its own **insufficient-evidence** bucket, in none of the others (and
    so outside the executed-ok latency and ``--steps`` aggregates). A row that
    carries a run summary is bucketed from it, for ``trace=True`` and
    ``trace=False`` rows alike: a successful ``nx_answer`` run whose
    ``coverage_partial`` count is 1. A row without a summary (an engine that
    predates the run summary column, or a row written before it) is bucketed
    from the notice itself when its text is visible (``trace=True``); a
    ``trace=False`` row without a summary has its text redacted, so it stays
    redacted: it is counted where it was before and is not recognizable as
    insufficient-evidence. ``--json`` carries the bucket as
    ``insufficient_evidence_count``. The two coverage non-answers,
    ``uncovered`` (evidence found, none of it covering a requested part) and
    ``no_evidence`` (nothing usable reached the reduction), are not in this
    bucket: they carry the ``[non-answer: <shape>]`` notice and are counted as
    degenerate classes ``non_answer/uncovered`` and ``non_answer/no_evidence``.
    A ``trace=False`` row cannot carry the notice; when its run summary says
    coverage ``unavailable`` it is counted in the same classes, or as
    ``non_answer/no_usable_answer`` when the prose was no usable answer.
    """
    # nexus-1ss4n: option conflicts are usage errors, raised before any read.
    if all_rows and before_id is not None:
        raise click.UsageError("--all starts from the newest run and cannot be combined with --before-id")
    if all_rows and ctx.get_parameter_source("limit") == click.core.ParameterSource.COMMANDLINE:
        raise click.UsageError("--all lists every run; it cannot be combined with --limit")
    if before_id is not None:
        from nexus.db.limits import MAX_QUERY_RESULTS  # noqa: PLC0415 - deferred: keep CLI startup fast

        if limit > MAX_QUERY_RESULTS:
            raise click.UsageError(
                f"--limit {limit} is above the engine's {MAX_QUERY_RESULTS}-row page; "
                f"with --before-id use --limit {MAX_QUERY_RESULTS} or less, or use --all"
            )
    if derive_budget and (all_rows or before_id is not None):
        raise click.UsageError("--derive-budget reads its own window and cannot be combined with --all or --before-id")
    # nexus-spbay: validate/normalize --since BEFORE the service try-block —
    # a bad value must fail as a usage error naming the value, never fall
    # into the except arm below and masquerade as a service-read failure
    # (and never reach an old engine, whose parser silently turned any
    # unparseable since into `since now()` = a guaranteed "(no runs)").
    if since:
        from nexus.db.t2.http_telemetry_store import normalize_since_filter  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        try:
            since = normalize_since_filter(since)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--since") from exc
    try:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast

        store = HttpTelemetryStore()
        if derive_budget:
            from nexus.plans.budget_default import derive_budget_default  # noqa: PLC0415 - deferred: only on this flag

            _emit_budget_derivation(
                derive_budget_default(store, limit=max(limit, 300), since=since),
                json_out=json_out,
            )
            return
        if all_rows:
            result = store.query_nx_answer_runs_all(
                since=since, include_steps=want_steps,
                # stderr, never stdout: --json output must stay one clean document.
                progress=lambda rows, cursor: click.echo(
                    f"answer-runs: {rows} rows read, next before_id={cursor}", err=True,
                ),
            )
            limit = len(result.get("rows") or [])
        elif before_id is not None:
            result = store.query_nx_answer_runs(
                since=since, limit=limit, include_steps=want_steps, before_id=before_id,
            )
        else:
            result = store.query_nx_answer_runs(since=since, limit=limit, include_steps=want_steps)
    except Exception as exc:  # noqa: BLE001 — degrade to the honest failure-shaped message, never a silent 0
        _log.debug("answer_runs_service_read_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import (  # noqa: PLC0415 - deferred: heavy import, keep CLI startup fast
            AnswerRunsPagingUnsupportedError,
            nx_answer_runs_read_failure_message,
        )

        if isinstance(exc, AnswerRunsPagingUnsupportedError):
            # nexus-1ss4n: asked to page, engine cannot. Never the zero-exit "(no runs)" degrade: a paging
            # caller scripting on the exit status must see the refusal.
            raise click.ClickException(str(exc)) from exc
        if all_rows or before_id is not None:
            # Any other failure on a paging read (a transport error or 5xx on a page, a malformed reply) is
            # an exit 1 too: a half-read window reported as "service unreachable" with status 0 reads as a
            # complete one to a script. Unbounded reads keep the degrade below.
            raise click.ClickException(nx_answer_runs_read_failure_message(exc)) from exc

        msg = nx_answer_runs_read_failure_message(exc)
        if json_out:
            click.echo(_json.dumps({"service_backed": True, "message": msg}, indent=2))
        else:
            click.echo(msg)
        return
    _emit_report(
        result, since=since, limit=limit, json_out=json_out,
        want_steps=want_steps, include_failed=include_failed,
        telemetry_store=store, before_id=before_id, all_rows=all_rows,
    )


def _emit_budget_derivation(derivation: Any, *, json_out: bool) -> None:
    """Render a :class:`nexus.plans.budget_default.BudgetDerivation`."""
    if json_out:
        click.echo(_json.dumps(derivation.as_dict(), indent=2))
        return
    from nexus.plans.budget_default import MIN_DERIVATION_RUNS  # noqa: PLC0415 - deferred: only on this flag

    d = derivation
    click.echo("budget_usd derivation (RDR-196 .p3a, post-flip executed-ok runs):")
    click.echo(f"  tier config: {d.tier_config}")
    click.echo(f"  rows scanned: {d.n_rows_scanned}   executed-ok: {d.n_executed_ok}")
    click.echo(
        f"  excluded: no step records {d.n_excluded_no_steps}, "
        f"pre-flip {d.n_excluded_pre_flip}, unknown cost {d.n_excluded_unknown_cost}, "
        f"continuation handoff/report {d.n_excluded_continuation}"
    )
    if d.flipped_step_models:
        seen = ", ".join(f"{m} ({n})" for m, n in sorted(d.flipped_step_models.items()))
        click.echo(f"  flipped-operator steps recorded model: {seen}")
    click.echo(f"  qualifying post-flip runs: n={d.n_runs} (floor {MIN_DERIVATION_RUNS})")
    if not d.sufficient:
        click.echo("  INSUFFICIENT: no value named (n below the non-vacuity floor)")
    else:
        for p, value in d.percentiles.items():
            click.echo(
                f"  p{p}: {value:.4f} USD   would refuse {d.would_refuse[p]:.1%} of these runs"
            )
    click.echo(
        f"  current DERIVED_BUDGET_USD: {d.as_dict()['derived_budget_usd']}   "
        f"enforcement: {'ON' if d.as_dict()['enforcement_enabled'] else 'OFF'}"
    )


def _emit_report(
    result: dict,
    *,
    since: str | None,
    limit: int,
    json_out: bool,
    want_steps: bool = False,
    include_failed: bool = False,
    telemetry_store: Any = None,
    before_id: int | None = None,
    all_rows: bool = False,
) -> None:
    rows = result.get("rows") or []
    # RDR-232 Phase 3 (nexus-jypmj.3.3): partial answers are their own bucket, taken off before the four-way split.
    insufficient_evidence_rows, split_rows = _split_insufficient_evidence(rows)
    (
        executed_ok_rows, executed_failed_rows, handed_off_rows,
        degenerate_by_class, report_event_rows,
    ) = _split_four_way(split_rows)
    degenerate_count = sum(len(v) for v in degenerate_by_class.values())
    degenerate_breakdown = {k: len(v) for k, v in degenerate_by_class.items()}
    # nexus-ix3fj: which rows were classified from the class the writer recorded over the full text, and which by
    # the read-time heuristic over the stored text (cut at 2000 characters). The rows that went through the
    # classifier: every run row, not the insufficient-evidence bucket (decided by its summary) and not the
    # continuation handoff and report rows (decided by their markers).
    classification_basis = _classification_basis_counts(
        executed_ok_rows + executed_failed_rows + [r for v in degenerate_by_class.values() for r in v]
    )
    # True while a row with a visible text and no recorded class is in the counts: it was classified from text cut at
    # 2000 characters, so a raw payload past the cut is not counted degenerate. Redacted rows without a recorded
    # class are named apart (``redacted``) and do not set it: nothing was recorded to judge them by.
    truncation_limited = classification_basis["heuristic"] > 0
    # RDR-200 §Telemetry (nexus-4e75w.5): continuation runs are NEVER
    # folded into the headless cost population — handed_off_rows is a
    # SEPARATE bucket from executed_ok/executed_failed by construction
    # (_split_four_way classifies the marker BEFORE the step_count
    # branch), so breakdown_rows below already excludes them without
    # any extra filtering here.
    continuation_stats = _continuation_pairing_stats(handed_off_rows, report_event_rows)

    # RDR-196 .p1e review-fix (Important): executed-ok only by default;
    # --include-failed folds executed-failed rows in too. Never
    # degenerate rows -- those are a structurally different population
    # (zero steps ran at all) with no meaningful per-step cost/elapsed
    # to aggregate.
    breakdown_rows = (
        executed_ok_rows + executed_failed_rows if include_failed else executed_ok_rows
    )

    step_data: dict[str, Any] | None = None
    if want_steps:
        steps_supported = result.get("steps_supported")
        if steps_supported is False:
            step_data = {"steps_supported": False}
        else:
            step_data = {
                "steps_supported": steps_supported,
                **_step_breakdown(breakdown_rows),
            }
            # RDR-196 Phase 3 Step 1 (nexus-nyry9.20, code-review round 1
            # dormancy item #2): read-time estimate-vs-actual, computed
            # fresh on every call -- never persisted (see docs/
            # cli-reference.md's "Predicted vs actual cost" section for
            # what nx_answer's own Step 1 logs but does not yet write to
            # this table).
            if telemetry_store is not None:
                _add_predicted_costs(step_data["by_plan"], telemetry_store)

    # RDR-196 .p1e review-fix (S2, substantive-critic T2 [23111]): a
    # page-scoped latency view of the SAME executed-ok population the
    # --steps breakdown uses, computed client-side from duration_ms —
    # distinct from (and explicitly labeled apart from) the engine's
    # whole-set `avg_duration_ms`/`latency_buckets` below, which still
    # blends degenerate + executed + failed rows by design (that block
    # answers a different question: "the whole recorded population").
    executed_ok_durations = [r.get("duration_ms", 0) for r in executed_ok_rows]
    executed_ok_avg_duration_ms = (
        sum(executed_ok_durations) / len(executed_ok_durations)
        if executed_ok_durations else None
    )
    executed_ok_latency_buckets = _bucketize_durations(executed_ok_durations)

    if json_out:
        # nexus-eho3u review fix (critic S1): echo the query envelope
        # (since/limit/captured_at) alongside the store result, matching
        # `nx tier-status --json` (tier_status.py's `_emit_report` wraps
        # scope/session_id/last_n/since around the store rows the same
        # way) — a caller diffing successive §4.5 baseline snapshots needs
        # to know what window and page size produced each one, not just
        # the numbers. `captured_at` is THIS process's wall clock, stamped
        # at render time — display metadata for a human reading the
        # snapshot later, not a value compared against any server
        # timestamp (see the clock-skew note in --since's help text).
        payload: dict[str, Any] = {
            "since": since,
            "limit": limit,
            "captured_at": datetime.now(UTC).isoformat(),
            # nexus-1ss4n: present only when the keyset options were used, so the default envelope is unchanged.
            **({"before_id": before_id} if before_id is not None else {}),
            **({"all": True} if all_rows else {}),
            **result,
            # RDR-196 .p1e: PAGE-scoped (of the listed rows, not the
            # whole --since-filtered set the `latency_buckets`/
            # `avg_duration_ms` keys above cover — see answer_runs_cmd's
            # own docstring for the population distinction).
            "executed_ok_count": len(executed_ok_rows),
            "executed_failed_count": len(executed_failed_rows),
            "degenerate_count": degenerate_count,
            "degenerate_breakdown": degenerate_breakdown,
            # nexus-ix3fj: how the rows above were classified; ``heuristic`` rows undercount a raw payload over 2000 chars.
            "classification_basis": classification_basis,
            "degenerate_count_truncation_limited": truncation_limited,
            # RDR-232 Phase 3 (nexus-jypmj.3.3): partial answers returned with the insufficient-evidence notice.
            "insufficient_evidence_count": len(insufficient_evidence_rows),
            "executed_ok_avg_duration_ms": executed_ok_avg_duration_ms,
            "executed_ok_latency_buckets": executed_ok_latency_buckets,
            # RDR-200 §Telemetry (nexus-4e75w.5): the fourth split bucket
            # plus the handoff/report pairing stats. handed_off_count is
            # the same page-scoped population `_continuation_pairing_
            # stats`'s handoff_count re-derives (kept as its own key so a
            # caller doesn't have to reach into `continuation` for the
            # plain count) — report_event rows are counted ONLY inside
            # `continuation` (they are not a run bucket).
            "handed_off_count": len(handed_off_rows),
            "continuation": continuation_stats,
            # critic-F2 (T2 [23952]): caveats the RAW `hit_count`/
            # `fallback_count`/`total`/`avg_*` keys `**result` just
            # spread above — those are unconditionally present whenever
            # the store returned any rows, so this caveat is too.
            "whole_set_continuation_caveat": NX_ANSWER_RUNS_WHOLE_SET_CONTINUATION_CAVEAT,
        }
        if step_data is not None:
            payload["step_breakdown"] = step_data
        click.echo(_json.dumps(payload, indent=2))
        return

    total = result.get("total", 0)
    scope_label = f"since {since}" if since else "all time"
    if before_id is not None:
        scope_label += f", listing ids below {before_id}"
    click.echo(f"nx_answer_runs ({scope_label}):")
    if total == 0:
        click.echo("  (no runs)")
        return

    hit = result.get("hit_count", 0)
    fallback = result.get("fallback_count", 0)
    avg_dur = result.get("avg_duration_ms")
    avg_cost = result.get("avg_cost_usd")
    oldest = result.get("oldest_created_at") or "<unknown>"

    click.echo(f"  total: {total}")
    if before_id is not None:
        click.echo(
            "  (the totals and averages below cover the whole --since-filtered set, "
            f"not only the ids below {before_id})"
        )
    click.echo(f"  oldest: {oldest}")
    click.echo(
        f"  plan-match hit: {hit}  inline-planner fallback: {fallback}"
    )
    click.echo(f"    CAVEAT: {NX_ANSWER_RUNS_WHOLE_SET_CONTINUATION_CAVEAT}")
    # RDR-196 .p1e review-fix (S2): explicitly labeled — this is the
    # engine's WHOLE `--since`-filtered set, degenerate + executed-ok +
    # executed-failed all blended, unchanged from pre-.p1e behavior. An
    # unlabeled reading of this block is exactly the 45x-wrong-docstring
    # mistake this arc exists to prevent.
    if avg_dur is not None:
        click.echo(
            f"  avg duration_ms: {avg_dur:.0f}  "
            "(engine aggregate: ALL rows incl. degenerate + failed, whole "
            "--since-filtered set)"
        )
    if avg_cost is not None:
        click.echo(f"  avg cost_usd: {avg_cost:.6f}")
        # nexus-spbay folded finding (disuse analysis, T2 [23879]):
        # cost_usd was hardcoded 0.0 until RDR-196 Phase 0/1 landed
        # (~2026-08-20), so any window reaching back past that dilutes
        # the average with structurally-fake zeros — the all-time figure
        # read ~6.5x low ($0.098 vs a real ~$0.64-1.05/call). Label it
        # whenever the filtered set can contain pre-instrumentation rows.
        if str(oldest) < COST_INSTRUMENTED_SINCE:
            click.echo(
                f"    (CAUTION: window includes rows before "
                f"{COST_INSTRUMENTED_SINCE}, when cost_usd was recorded as "
                f"a hardcoded 0.0 — the average is diluted; pass "
                f"--since {COST_INSTRUMENTED_SINCE} for instrumented-era cost)"
            )

    buckets = result.get("latency_buckets") or {}
    if buckets:
        click.echo(
            "  latency buckets (engine aggregate: ALL rows incl. "
            "degenerate + failed, whole --since-filtered set):"
        )
        for key in _BUCKET_ORDER:
            n = buckets.get(key, 0)
            if n:
                click.echo(f"    {_BUCKET_LABEL[key]:<10} {n}")

    # RDR-196 .p1e / RDR-200 §Telemetry: page-scoped FOUR-way split —
    # explicitly labeled by its own scope (of the N rows LISTED below),
    # never mixed into the whole-set aggregates printed above.
    # report_event rows are a fifth, non-run population (excluded from
    # this split entirely — RDR-200's own contract) and are surfaced
    # separately just below, never folded into "row(s) shown".
    click.echo(
        f"  of the {len(rows)} row(s) shown: executed-ok {len(executed_ok_rows)}, "
        f"executed-failed {len(executed_failed_rows)}, "
        f"handed-off {len(handed_off_rows)}, "
        f"insufficient-evidence {len(insufficient_evidence_rows)}, degenerate {degenerate_count}"
    )
    for cls in sorted(degenerate_breakdown):
        click.echo(f"    degenerate/{cls}: {degenerate_breakdown[cls]}")
    click.echo(
        f"  classified: {classification_basis['recorded']} by recorded class (full final text), "
        f"{classification_basis['heuristic']} by read-time heuristic (stored text, cut at 2000 characters), "
        f"{classification_basis['redacted']} redacted with no recorded class"
    )
    if truncation_limited:
        click.echo(
            "    the degenerate counts above are not exact while heuristic rows remain: a raw payload cut at "
            "2000 characters reads as an answer there, and the empty-hydration phrase match can count a "
            "real answer as degenerate on any row"
        )

    if handed_off_rows or report_event_rows:
        click.echo(
            f"  continuation (RDR-200): {continuation_stats['handoff_count']} "
            f"handoff(s), {continuation_stats['reported_count']} reported, "
            f"{continuation_stats['unreported_count']} unreported"
        )
        rate = continuation_stats["unreported_rate"]
        if rate is not None:
            click.echo(f"    unreported rate: {rate:.1%}")
        if continuation_stats["unparseable_handoff_count"]:
            click.echo(
                f"    unparseable continuation_id: "
                f"{continuation_stats['unparseable_handoff_count']}"
            )

    if executed_ok_rows:
        click.echo(
            "  executed-ok latency (page-scoped, of the executed-ok rows "
            "shown only):"
        )
        if executed_ok_avg_duration_ms is not None:
            click.echo(f"    avg duration_ms: {executed_ok_avg_duration_ms:.0f}")
        for key in _BUCKET_ORDER:
            n = executed_ok_latency_buckets.get(key, 0)
            if n:
                click.echo(f"    {_BUCKET_LABEL[key]:<10} {n}")

    if step_data is not None:
        if step_data.get("steps_supported") is False:
            click.echo(
                "  --steps requested but this engine does not support "
                "include_steps (steps_supported=false) — run/aggregate "
                "data only; deploy an engine carrying nexus-lme1s to see "
                "the per-step breakdown."
            )
        else:
            pop_label = "executed-ok+failed" if include_failed else "executed-ok"
            click.echo(f"  by operator ({pop_label}):")
            for op, agg in sorted(step_data["by_operator"].items()):
                mc = agg["median_cost_usd"]
                me = agg["median_elapsed_ms"]
                mc_s = f"{mc:.6f}" if mc is not None else "unknown"
                me_s = f"{me:.0f}ms" if me is not None else "unknown"
                click.echo(
                    f"    {op:<20} n={agg['count']:<4} "
                    f"cost_usd={agg['total_cost_usd']:.6f} "
                    f"(unknown={agg['unknown_cost_count']}) "
                    f"median_cost_usd={mc_s} "
                    f"elapsed_ms={agg['total_elapsed_ms']} median_elapsed_ms={me_s}"
                )
            click.echo(f"  by source ({pop_label}):")
            for src, agg in sorted(step_data["by_source"].items()):
                mc = agg["median_cost_usd"]
                me = agg["median_elapsed_ms"]
                mc_s = f"{mc:.6f}" if mc is not None else "unknown"
                me_s = f"{me:.0f}ms" if me is not None else "unknown"
                click.echo(
                    f"    {src:<20} n={agg['count']:<4} "
                    f"cost_usd={agg['total_cost_usd']:.6f} "
                    f"(unknown={agg['unknown_cost_count']}) "
                    f"median_cost_usd={mc_s} "
                    f"elapsed_ms={agg['total_elapsed_ms']} median_elapsed_ms={me_s}"
                )
            by_plan = step_data["by_plan"]
            if by_plan:
                click.echo(
                    f"  by plan ({pop_label}, median cost / median elapsed / "
                    f"predicted cost, RDR-196 Phase 3 Step 1 -- read-time, "
                    f"not persisted):"
                )
                for plan_key, agg in sorted(by_plan.items()):
                    mc = agg["median_cost_usd"]
                    me = agg["median_elapsed_ms"]
                    mc_s = f"{mc:.6f}" if mc is not None else "unknown"
                    me_s = f"{me:.0f}ms" if me is not None else "unknown"
                    pc = agg.get("predicted_cost_usd")
                    pc_s = f"{pc:.6f}" if pc is not None else agg.get("predicted_basis", "unavailable")
                    click.echo(
                        f"    plan={plan_key:<8} runs={agg['run_count']:<4} "
                        f"median_cost_usd={mc_s} median_elapsed_ms={me_s} "
                        f"predicted_cost_usd={pc_s}"
                    )
            violations = step_data["cost_consistency_violations"]
            if violations:
                click.echo(
                    f"  cost-consistency violations "
                    f"(run.cost_usd vs sum(steps.cost_usd), epsilon "
                    f"{_COST_EPSILON_REL:.1%} relative / "
                    f"{_COST_EPSILON_ABS_FLOOR} absolute floor):"
                )
                for v in violations:
                    click.echo(
                        f"    id={v['id']} run={v['run_cost_usd']!r} "
                        f"sum(steps)={v['step_cost_sum_usd']!r}"
                    )

    rows_page = result.get("rows") or []
    if before_id is not None and not rows_page:
        click.echo()
        click.echo(f"  (no runs with id below {before_id}: the start of the window)")
    if rows_page:
        click.echo()
        click.echo(f"  {'all' if all_rows else 'last'} {len(rows_page)} run(s):")
        for r in rows_page:
            plan_id = r.get("plan_id")
            # plan_id 0 is the ad-hoc Match sentinel every successful
            # inline-planner run carries (core.py::_nx_answer_plan_miss) —
            # NOT a real plan (plans.id is BIGSERIAL, so 0 can never be
            # one). `not plan_id` is True for both None and 0, so a
            # fallback row renders "fallback", never the misleading
            # "plan=0" (nexus-eho3u review fix). This per-row display
            # convention is UNCHANGED by the .p1e review-fix (S3) above —
            # that fix is scoped to _step_breakdown's by_plan GROUPING key
            # only, where conflating plan_id=0 with plan_id=None hid a
            # real population (see _step_breakdown's own comment).
            is_fallback = not plan_id
            tag = "fallback" if is_fallback else f"plan={plan_id}"
            question = str(r.get("question", ""))[:60]
            click.echo(
                f"    {r.get('created_at', ''):<21} {tag:<10} "
                f"{r.get('duration_ms', 0):>7}ms  {question}"
            )
