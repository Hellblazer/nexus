# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 3 step 1 (nexus-jypmj.3.1): the coverage ledger and the state table.

RDR-232 section 4 asks for a ledger derived BEFORE terminal generation: for each required facet, which
retrieved items support it, contradict it, or none; for each named source and for the caller's scope, what
the deterministic checks of nexus-jypmj.1.8 found. This module holds the typed pieces and the pure logic.
The runner owns the boundary (``plan_run`` hands a :class:`BoundaryView` of the terminal reduction's
input to :meth:`CoverageBoundary.evaluate` just before that reduction is dispatched), so the ledger is
not hidden inside a bundled intermediate output and the plan prefix runs once.

What the boundary does, in order:

1. **Anchors.** The evidence the terminal reduction will be handed becomes :class:`EvidenceAnchor`
   records ``{item_id, chash, collection, tumbler | t2_record_id, content}`` (plus stream, source and
   kind). ``item_id`` (``e1``, ``e2``...) is a request-local join key, not a durable citation; the durable
   anchor is ``(tumbler, chash)`` and RDR-189 P1 owns normalising ``check``/``verify`` to it.
2. **Deterministic rows first.** ``build_evidence_diagnostics`` supplies the named-source and scope rows.
   A facet whose named source is missing is ``unsupported`` with reason ``source_missing`` BEFORE any
   model call, and the deterministic result outranks ``unassessed`` (RDR section 4, decision d of
   nexus-jypmj.3.1): it is never sent to the model and survives a failed assessment. A source that is ambiguous
   or whose lookup could not run is ``unresolved``, not missing: disclosed in the sources, blocking no facet
   (nexus-jypmj.3.17).
3. **Facet assessment.** One ``operator_check`` call per evidence stream (primary, t2, vector, graph,
   other), carrying at most :data:`MAX_FACETS_PER_ASSESSMENT_CALL` facets; more facets split into groups, and a
   stream of more than :data:`MAX_ANCHORS_PER_ASSESSMENT_CALL` anchors into balanced packs (nexus-jypmj.17).
   ``check.ok`` is never read as coverage. Coverage comes from the evidence records only: each record's
   ``item_id`` must be one of the call's input ids and its ``quote`` must open with the facet tag
   (``[f2] ...``, any case) of a facet in that call; anything else is rejected and counted
   (:attr:`AssessmentReport.rejected_records`). ``supports`` covers a facet, ``neutral`` does nothing, and
   ``contradicts`` marks a facet ``contradicted`` (retained for the reduction, never repaired into agreement)
   only when no item supports it: a facet with both is ``covered`` and carries the reason ``conflicted``, so
   the reduction and the notice can say the evidence conflicts.
   **An unusable reply is a failed assessment, not a finding.** A call that returns no evidence record at
   all, or only rejected ones, tells us nothing about the facets in it: they are ``unassessed`` (reason
   ``assessment_unusable``, outcome ``failed`` or ``partial_failure``, detail ``assessor_unusable``), never
   ``unsupported``, so a model that ignores the tag convention cannot make a withheld answer. "The items were
   read and nothing bears on the facet" is the assessor's neutral record (the instruction asks for one).
   A call that raises or comes back malformed costs only its own facets (nexus-jypmj.17): the other calls ran and
   count, and no facet is defaulted to covered.
   **The measurement counters** (nexus-jypmj.3.10): every attempted call is logged as an :class:`AssessmentCall` with
   a reply class (:data:`REPLY_CLASSES`: ``support``, ``neutral_or_contradict``, ``all_rejected``, ``no_records``,
   ``raised``), so the classes can be counted per call and per stream; ``failure_cause`` splits a failed outcome into an
   unusable reply and a raised call (or, when some calls ran and others were skipped unstarted, ``budget_skip`` /
   ``deadline_skip``, with the per-call ``skipped_budget_calls`` / ``skipped_deadline_calls`` beside it; a wholly
   skipped assessment stays ``none``); an unassessed facet carries the reason it is unassessed
   (``assessment_unusable``, ``assessment_skipped_deadline`` / ``_budget``, ``assessment_raised``, or
   ``assessment_incomplete`` for an assessment that ran whole but decided nothing (an item the cap left unread no longer
   makes a facet unassessed: nexus-jypmj.23); ``assessment_stopped_after_failure`` is kept in the
   closed list for older records but no pass emits it since nexus-jypmj.17, which does not stop at a failure), counted in
   :attr:`CoverageResult.unassessed_by_cause`;
   a conflicted facet is counted in ``facets_conflicted`` (a subset of ``facets.covered``); and
   :attr:`CoverageResult.repair_entered` says the run went into the repair step at all. None of this changes a
   state, a skip or an outcome.
4. **State.** :func:`state_for` computes ``coverage.state`` from the facet states (the section 4 table):
   ``complete``, ``partial``, ``unavailable`` or ``unassessed``. ``unavailable`` with reason ``uncovered``
   (every required facet known and none covered; contradicted is not covered) sets
   :attr:`CoverageResult.skip_generation`, which the runner honours by not dispatching the terminal
   reduction (Sam's ruling of 2026-10-25, nexus-jypmj.3.12). What lets generation proceed is one predicate,
   :func:`counts_toward_a_usable_answer` (``covered`` or ``contradicted``, Sam 2026-10-27). ``no_evidence`` stays reserved for zero
   retrieval results, which the existing zero-evidence path already answers, so a boundary that finds no
   anchors never skips generation. The ``AnswerShape`` value, the notice and the counters are
   nexus-jypmj.3.3's.

**The facet cap is 6** (:data:`MAX_FACETS_PER_ASSESSMENT_CALL`). Basis: the labelled requests of the
golden set (24 in ``golden_v1.yaml``, 29 in ``golden_heldout_v1.yaml``) carry 1 to 6 required facets; the
largest is the motivating ART RDR-084 request with six; 52 of the 53 carry four or fewer. So six lets the
request that motivated the design cost one call per stream, and the first-pass extraction cap of 12 facets
(``request_coverage._MAX_FACETS``) costs at most two calls per stream. This is a calibration from the
facet-count distribution, NOT from a measured accuracy-versus-facets-per-call curve: Phase 3 step 4
(measurement) owns re-fitting it, and the value lives in one constant for that reason.

The assessment is a stage inside the answer's own deadline, and the stage as a whole has one allowance:
:data:`ASSESSMENT_STAGE_DEADLINE_SHARE` of the deadline left when the stage starts (all streams and facet
groups together, so the terminal reduction is not starved by its own pre-check, however many calls it makes).
Each call draws its timeout from what is left of that allowance, capped by :data:`ASSESSMENT_CALL_TIMEOUT_S`;
a call is not started when what is left falls under :data:`MIN_ASSESSMENT_CALL_S` or the USD allowance is
spent, and its facets are then ``unassessed`` (outcome ``skipped_deadline`` / ``skipped_budget`` when no call
was made, ``partial_failure`` after some were).

**The calls run concurrently** (nexus-jypmj.17), at most :data:`MAX_CONCURRENT_ASSESSMENT_CALLS` at a time (less when an API backend's configured gate is
lower; the ``claude -p`` backend's four concurrent subprocesses are unmeasured), each started in plan order (stream order, then facet group, then pack). Each call decides when it STARTS: the USD allowance
(the calls in flight and the starting call at the mean known cost, or at
:data:`ASSESSMENT_CALL_COST_PRIOR_USD` before any call reported one; one call always starts when none is in flight and
the allowance is above zero until a cost is measured, after which EVERY call is judged on it; a call that does not fit beside
the calls in flight is HELD, outside the gate, until a call ends, and is skipped for budget only when it cannot fit even
alone, nexus-jypmj.22) and the stage allowance left decide whether it runs
and what timeout it draws, and it is cut from outside at that timeout, so the stage cannot outlast its share
(beyond a cut call's bounded cleanup grace of about 1 s) whatever the check does with the timeout it is given. A call cut that way has its process tree killed
(:class:`nexus.operators.dispatch.CliBackend`) and its ``StepRecord`` is ``ok=False`` with an UNKNOWN cost: the CLI reports
spend only in its result event, which a killed call never writes, so what it spent before the cut is not knowable and is
not counted against the USD allowance. The replies are read after all calls end, in plan order, so
the ledger, ``call_log``, reply classes and unassessed causes do not depend on which call finished first. The 2026-10-28 canary
measured the serial design at +118 s with a 65-anchor call over the 60 s limit. These values are unmeasured starting
points. Assessment
calls are ``StepRecord``s past the plan's own step indices, so the shared USD accumulator and the
``nx_answer_steps`` primary key see them without a new step source.

The one repair (nexus-jypmj.3.2): :meth:`CoverageBoundary.run` is ``evaluate`` plus at most one repair, and
it is what the runner calls. When the first ledger has facets in :attr:`CoverageResult.repair_facet_ids`
(assessed, nothing held bears on them), the boundary retrieves for them (:mod:`nexus.plans.coverage_repair`:
primary source and explicit scope first, deterministic queries, bounded allowance and USD), asks the runner
to merge the new chunks into the reduction's input (:mod:`nexus.plans.repair_stream`) and to hand back the
input it now holds, and recomputes the ledger once with ``evaluate(view, prior=result)``. The skip decision
is made on that final ledger, so a ledger that covers nothing still gets its repair first.

Re-entry: ``evaluate(view, prior=result)`` keeps the prior covered and contradicted findings whose evidence
the new input still holds, reassesses a repairable gap against the NEW anchors only (the old ones were
already read for it), reassesses anything else not known against all of them, adds one round, and sums the
assessment counts. Evidence ids are stable across the two rounds (:func:`build_anchors` ``known``). A
reassessment that fails or is skipped leaves the first ledger in force for the facets it was known for: the
result is ``partial`` (or ``complete``), not ``unassessed`` (RDR section 4).

Redaction: the wire forms carry ids, states and counts; evidence text is never in them, and the source
labels (the caller's wording) are dropped when ``redact`` is set.

Pure apart from the injected ``check`` callable: stdlib, ``structlog`` and the RDR-232 value types.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import structlog

from nexus.plans.evidence import ScopeConstraint
from nexus.plans.evidence_diagnostics import (
    ReducerInputRecorder,
    _per_chunk_collections,
    _stream_of,
    active_recorder,
    build_evidence_diagnostics,
)
from nexus.plans.coverage_repair import (
    MAX_ANCHORS_PER_ASSESSMENT_CALL,
    MAX_REPAIR_ITEMS,
    MAX_REPAIR_QUERIES,
    MIN_REPAIR_QUERY_S,
    REPAIR_RESULTS_PER_QUERY,
    REPAIR_RESULTS_PER_QUERY_UNFILTERED,
    REPAIR_RETRIEVAL_SHARE,
    RepairHit,
    RepairOutcome,
    RepairQuery,
    RepairReport,
    RepairTarget,
    build_repair_queries,
    planned_reassessment_calls,
    repair_allowance,
    repair_reason,
    unresolved_note,
    usd_affordable,
)
from nexus.plans.repair_stream import RepairSupplement, SupplementItem, active_supplement
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.reserved_stream import ReservedStream, active_reserved_stream, strip_source_note

_log = structlog.get_logger(__name__)

__all__ = [
    "ASSESSMENT_CALL_COST_PRIOR_USD",
    "ASSESSMENT_CALL_TIMEOUT_S",
    "ASSESSMENT_MAX_ITEM_CHARS",
    "ASSESSMENT_OPERATOR",
    "ASSESSMENT_STAGE_DEADLINE_SHARE",
    "CONFLICTED_REASON",
    "INSTRUCTION_MARKER",
    "MAX_ANCHORS_PER_ASSESSMENT_CALL",
    "MAX_CONCURRENT_ASSESSMENT_CALLS",
    "MAX_FACETS_PER_ASSESSMENT_CALL",
    "MIN_ASSESSMENT_CALL_S",
    "RAISED_REASON",
    "REPLY_CLASSES",
    "STREAM_ORDER",
    "UNASSESSED_CAUSES",
    "UNUSABLE_REASON",
    "USABLE_ANSWER_FINDING_STATES",
    "AssessmentCall",
    "AssessmentReport",
    "BoundaryView",
    "CoverageBoundary",
    "CoverageLedger",
    "CoverageResult",
    "EvidenceAnchor",
    "FacetFinding",
    "ScopeRow",
    "SourceRow",
    "build_anchors",
    "counts_toward_a_usable_answer",
    "state_for",
]

#: The operator the assessment goes through, by the runner's own dispatcher (never ``nx_answer``).
ASSESSMENT_OPERATOR = "operator_check"

#: Facets per assessment call. See the module docstring for the basis; unmeasured as accuracy.
MAX_FACETS_PER_ASSESSMENT_CALL: int = 6
#: Upper bound on one call; the stage allowance below applies first when a deadline is set.
ASSESSMENT_CALL_TIMEOUT_S: float = 60.0
#: The WHOLE assessment stage (every stream, every facet group) may use at most this share of the deadline left
#: when the stage starts, so repair and generation keep the rest. Each call draws from what is left of it.
ASSESSMENT_STAGE_DEADLINE_SHARE: float = 0.4
#: No call is started with less than this allowance.
MIN_ASSESSMENT_CALL_S: float = 5.0
#: Assessment calls in flight at once (nexus-jypmj.17). The operator dispatch has no process-wide gate of its own for
#: a ``claude -p`` call; the direct-API backend gates HTTP attempts at ``NX_LLM_MAX_CONCURRENCY`` (default 4,
#: ``operators/llm_api.DEFAULT_MAX_CONCURRENCY``), and a call past that waits for a permit inside its own timeout. So
#: the stage never asks for more than the smaller of this constant and the configured gate on those backends
#: (:func:`dispatch_concurrency_cap`): more would only queue behind the gate and spend the call's timeout waiting.
#: The ``claude -p`` backend has no gate and only this constant bounds it: four concurrent ``claude`` subprocesses
#: have not been measured (rate limits, memory, host load). The canary's four streams fit in one wave.
MAX_CONCURRENT_ASSESSMENT_CALLS: int = 4
#: What one assessment call is assumed to cost until a call has reported a cost (nexus-jypmj.17). Without it the
#: first wave of up to ``MAX_CONCURRENT_ASSESSMENT_CALLS`` calls starts whenever the USD allowance is above zero. Source:
#: the live canary of 2026-10-28 (nexus-jypmj.3.10) ran 4 calls for 0.0166 USD (about 0.004 each), and the golden
#: run's assessment spent 0.331 USD over 40 calls in 18 assessed runs (about 0.008 a call, 0.018 a run), both on the
#: serial design; 0.02 is about 2.4 times the golden per-call cost, so the prior errs towards starting fewer calls. A call that reported its cost
#: replaces the prior with the mean of the known costs.
ASSESSMENT_CALL_COST_PRIOR_USD: float = 0.02
#: Slack on the USD fit test, so a cost that exactly uses up the allowance is not refused over float rounding.
_USD_EPS: float = 1e-9
#: Characters of item text one call carries (the bundle prompt guard is 200,000 for the whole prompt).
#: Items past it are not assessed (they are the report's ``unread_ids``); a facet that no assessed item supports
#: or contradicts is ``unsupported`` all the same, and the unread count is disclosed (nexus-jypmj.23).
ASSESSMENT_MAX_ITEM_CHARS: int = 120_000

#: The order streams are assessed in: the named source first, then its lifecycle records, then the plan's
#: own retrieval, then anything the run cannot attribute.
STREAM_ORDER: tuple[str, ...] = ("primary", "t2", "vector", "graph", "other")

FacetState = Literal["covered", "unsupported", "contradicted", "unassessed"]
CoverageState = Literal["complete", "partial", "unavailable", "unassessed"]
AssessmentOutcome = Literal[
    "not_attempted", "ok", "failed", "partial_failure", "skipped_deadline", "skipped_budget",
]
AnchorKind = Literal["chunk", "t2_record", "unattributed"]

#: Finding reasons that a targeted retrieval can still change: the facet was assessed against the evidence
#: held and nothing in it bears on the facet.
REPAIRABLE_REASON = "no_supporting_evidence"

_TAG = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*")
_ROLES = frozenset({"supports", "contradicts", "neutral"})

#: A finding's ``reason`` for a facet that is ``covered`` while other items contradict it.
CONFLICTED_REASON = "conflicted"
#: A finding's ``reason`` for a facet left ``unassessed`` because the assessor's reply for it was unusable.
UNUSABLE_REASON = "assessment_unusable"
#: A finding's ``reason`` for a facet left ``unassessed`` because the assessment call for it raised.
RAISED_REASON = "assessment_raised"
#: A finding's ``reason`` for a facet left ``unassessed`` because the pass stopped at an earlier failed call (a raised
#: call or a reply that is not the check shape) before this facet's group was ever attempted. It is not the failing
#: call's own cause: that call did not fail on this facet (nexus-jypmj.3.10, Phase 3 critique S4).
STOPPED_REASON = "assessment_stopped_after_failure"
#: The ``AssessmentReport.detail`` prefix of an unusable reply.
_UNUSABLE_DETAIL = "assessor_unusable"

#: What one assessment call came back as (nexus-jypmj.3.10, Phase 3 critique S-B): ``support`` (at least one
#: accepted record says an item supports a facet), ``neutral_or_contradict`` (accepted records, none supports),
#: ``all_rejected`` (records came back and every one failed validation), ``no_records`` (an empty reply, or one that
#: is not the check shape) and ``raised`` (the call itself raised). The first two are usable replies; the next two
#: are an unusable reply; the last is a failed call.
REPLY_CLASSES: tuple[str, ...] = ("support", "neutral_or_contradict", "all_rejected", "no_records", "raised")
#: Why a facet is ``unassessed`` in the final ledger, counted in ``CoverageResult.unassessed_by_cause``.
#: ``other`` is every reason not named here (an assessment never attempted, a reason this table does not know). An
#: item the size cap left unread is not one: it leaves the facet ``unsupported`` and shows in ``skipped_items``.
UNASSESSED_CAUSES: tuple[str, ...] = (
    "unusable_reply", "deadline_skip", "budget_skip", "raised_call", "stopped_after_failure", "other",
)
_CAUSE_OF_REASON: dict[str, str] = {
    UNUSABLE_REASON: "unusable_reply", "assessment_skipped_deadline": "deadline_skip",
    "assessment_skipped_budget": "budget_skip", RAISED_REASON: "raised_call", STOPPED_REASON: "stopped_after_failure",
}

#: THE ALL-CONTRADICTED RULE (undecided; Sam is deciding it, RDR-232 critique S1). The finding states that count
#: toward a usable answer: generation is skipped before it (``state_for``) and the prose is withheld after it
#: (``coverage_outcome.decide_outcome``) when NO facet is in one of these states and none is unassessed.
#: ``covered`` and ``contradicted`` (Sam, 2026-10-27, nexus-jypmj.3.13): contradicting evidence is evidence the
#: reduction must state, so a contradiction-only ledger is ``partial`` and the notice names the contradicted facets;
#: only a ledger of unsupported facets is ``uncovered``. Both sides read :func:`counts_toward_a_usable_answer`;
#: this set is the one place the rule lives.
USABLE_ANSWER_FINDING_STATES: frozenset[str] = frozenset({"covered", "contradicted"})


def counts_toward_a_usable_answer(state: str) -> bool:
    """Whether a facet in *state* lets generation proceed (see :data:`USABLE_ANSWER_FINDING_STATES`)."""
    return state in USABLE_ANSWER_FINDING_STATES


#: The opening words of the coverage-assessment instruction. A stable marker, exported for the unit-test guard
#: that refuses a live assessment (``tests/conftest.py``): retuning the rest of the prompt cannot unguard the suite.
INSTRUCTION_MARKER = "Coverage assessment. For each facet listed below"

_INSTRUCTION = INSTRUCTION_MARKER + (
    ", find the items that bear on it. Add one evidence "
    "record per (item, facet) pair that matters: item_id is the item's id exactly as given; quote opens "
    "with the facet tag in square brackets (for example [f1]) followed by a short quote from the item; "
    "role is 'supports' when the item states what the facet asks for, 'contradicts' when it states the "
    "opposite, 'neutral' when it only mentions the topic. Use only the item ids and facet tags given, and "
    "add no others. If no item bears on any listed facet, still add one neutral record for the first item "
    "and the first facet: a reply with no record is read as a failed assessment. "
    "Coverage is read from the records, not from ok.\nFacets:\n"
)


# ── value types ──────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvidenceAnchor:
    """One candidate the terminal reduction is handed, with where it came from.

    ``item_id`` is request-local (``e1``...). ``chash`` is the full sha256 of the chunk text without its
    ``[source: ...]`` line, or the reserved item's own chash. ``tumbler`` is the catalog document of a T3
    chunk (empty when the retrieval output carries none); ``t2_record_id`` is the reserved T2 record's token.
    """

    item_id: str
    stream: str
    kind: AnchorKind
    chash: str
    collection: str
    tumbler: str
    t2_record_id: str
    source: str
    content: str

    def to_wire(self) -> dict[str, Any]:
        """Identity and provenance, never the content."""
        return {
            "item_id": self.item_id, "stream": self.stream, "kind": self.kind, "chash": self.chash,
            "collection": self.collection, "tumbler": self.tumbler, "t2_record_id": self.t2_record_id,
        }


@dataclass(frozen=True)
class FacetFinding:
    """One required facet's state: ``covered`` (supporting evidence; ``reason`` is ``conflicted`` when other
    evidence contradicts it too), ``contradicted`` (contradicting evidence and no supporting one, retained),
    ``unsupported`` (assessed, none bears on it) or ``unassessed`` (not known)."""

    facet_id: str
    state: FacetState
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"facet_id": self.facet_id, "state": self.state, "evidence_ids": list(self.evidence_ids), "reason": self.reason}


@dataclass(frozen=True)
class SourceRow:
    """A named source's deterministic row: ``covered``, ``partial``, ``missing`` (no document carries it, or nothing of
    it was read) or ``unresolved`` (ambiguous or its lookup could not run: disclosed, and it blocks no facet)."""

    source: str
    state: str
    detail: str = ""


@dataclass(frozen=True)
class ScopeRow:
    """The caller scope's deterministic row (``satisfied``, ``widened``, ``conflict``, ``violated``,
    ``not_evaluated``, ``not_applicable``)."""

    state: str
    detail: str = ""
    conflict_steps: tuple[int, ...] = ()


@dataclass(frozen=True)
class AssessmentCall:
    """One attempted assessment call, for the run summary (nexus-jypmj.3.10): the evidence stream it read, how many
    facets it carried, what it came back as (one of :data:`REPLY_CLASSES`) and how many records the validation
    accepted and rejected. No text."""

    stream: str
    facets: int
    reply: str
    accepted: int = 0
    rejected: int = 0

    def to_wire(self) -> list[Any]:
        return [self.stream, self.facets, self.reply, self.accepted, self.rejected]


@dataclass(frozen=True)
class AssessmentReport:
    """What the facet assessment cost, for the run summary (nexus-jypmj.3.5): calls, facets per call.

    ``calls`` counts attempted calls (a failed call is one). ``rejected_records`` counts evidence records
    the validation dropped (an id outside the input, an unknown facet tag, an untagged quote or a bad role).
    ``skipped_items`` is the number of retrieved items the check never read because the per-call size cap left them out
    (nexus-jypmj.23): each such item is counted once per stream per round, never past ``anchors_total`` (the items the
    check was handed), and an item a later round read is no longer counted (:meth:`merged_with`). ``unread_ids`` names
    them and ``read_ids`` the items a completed call carried, which is how a repair finds the unread ones.
    ``detail`` names why the assessment stopped.

    The measurement counters (nexus-jypmj.3.10): ``call_log`` has one :class:`AssessmentCall` per attempted call, in
    order, from which the reply classes are counted per call and per stream; ``accepted_records`` counts the records
    the validation kept; ``unusable_calls`` counts calls that came back with no usable record (an empty reply, one
    that is not the check shape, or only rejected records); ``raised_calls`` counts calls that raised. Together they
    split ``outcome == "failed"`` into an unusable reply and a raised call (:attr:`failure_cause`).

    ``skipped_budget_calls`` and ``skipped_deadline_calls`` (shakedown 7.89.0 umbrella class c) count planned calls
    that never started because the USD allowance or the stage's share of the deadline was spent. They are per call,
    unlike ``unassessed_by_cause`` (per facet): one call that ran can cover every facet while three others were
    skipped, and only these counters then say the stage was cut short.
    """

    outcome: AssessmentOutcome = "not_attempted"
    calls: int = 0
    facets_per_call: tuple[int, ...] = ()
    streams: tuple[str, ...] = ()
    rejected_records: int = 0
    skipped_items: int = 0
    anchors_total: int = 0
    unread_ids: tuple[str, ...] = ()
    read_ids: tuple[str, ...] = ()
    latency_ms: int = 0
    detail: str = ""
    accepted_records: int = 0
    unusable_calls: int = 0
    raised_calls: int = 0
    call_log: tuple[AssessmentCall, ...] = ()
    skipped_budget_calls: int = 0
    skipped_deadline_calls: int = 0

    def merged_with(self, later: AssessmentReport) -> AssessmentReport:
        """The sum of two rounds; the later round's outcome and detail stand, except that a later round that
        failed or was skipped after an earlier round made calls is a ``partial_failure`` (the first ledger
        stands), not a failure of the whole assessment."""
        outcome: AssessmentOutcome = later.outcome if later.outcome != "not_attempted" else self.outcome
        if later.outcome in ("failed", "skipped_deadline", "skipped_budget") and self.calls > 0 and self.outcome in ("ok", "partial_failure"):
            outcome = "partial_failure"
        # An item is unread only if no round read it: a repair that reads an item the first round left out removes it
        # from the count, and an item both rounds left out is counted once, not twice.
        read = dict.fromkeys(self.read_ids + later.read_ids)
        unread = tuple(i for i in dict.fromkeys(self.unread_ids + later.unread_ids) if i not in read)
        return AssessmentReport(
            outcome=outcome,
            calls=self.calls + later.calls, facets_per_call=self.facets_per_call + later.facets_per_call,
            streams=tuple(dict.fromkeys(self.streams + later.streams)),
            rejected_records=self.rejected_records + later.rejected_records,
            skipped_items=len(unread), anchors_total=later.anchors_total or self.anchors_total,
            unread_ids=unread, read_ids=tuple(read),
            latency_ms=self.latency_ms + later.latency_ms, detail=later.detail or self.detail,
            accepted_records=self.accepted_records + later.accepted_records,
            unusable_calls=self.unusable_calls + later.unusable_calls,
            raised_calls=self.raised_calls + later.raised_calls,
            call_log=self.call_log + later.call_log,
            skipped_budget_calls=self.skipped_budget_calls + later.skipped_budget_calls,
            skipped_deadline_calls=self.skipped_deadline_calls + later.skipped_deadline_calls,
        )

    def reply_classes(self) -> dict[str, int]:
        """Attempted calls by reply class, every class present (zero included)."""
        out = {c: 0 for c in REPLY_CLASSES}
        for call in self.call_log:
            out[call.reply] = out.get(call.reply, 0) + 1
        return out

    def reply_by_stream(self) -> dict[str, dict[str, int]]:
        """Reply classes per evidence stream, for the streams that made a call, in order of first use."""
        out: dict[str, dict[str, int]] = {}
        for call in self.call_log:
            row = out.setdefault(call.stream, {c: 0 for c in REPLY_CLASSES})
            row[call.reply] = row.get(call.reply, 0) + 1
        return out

    @property
    def failure_cause(self) -> str:
        """What the failed calls were, over every round: ``raised_call``, ``unusable_reply``, ``mixed`` (both).
        Else, when at least one call ran and other planned calls never started, ``budget_skip`` (the USD allowance
        was spent or could not cover another call; it wins when the deadline cut calls too, the per-call counters carry both) or ``deadline_skip``;
        else ``none``. A failed call takes precedence over a skip. An assessment that was skipped whole (no call
        ran) is not a failure and reads ``none``, as it always did; its outcome and the skip counters say why."""
        if self.raised_calls and self.unusable_calls:
            return "mixed"
        if self.raised_calls:
            return "raised_call"
        if self.unusable_calls:
            return "unusable_reply"
        if self.calls:
            if self.skipped_budget_calls:
                return "budget_skip"
            if self.skipped_deadline_calls:
                return "deadline_skip"
        return "none"

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "calls": self.calls, "facets_per_call": list(self.facets_per_call),
            "streams": list(self.streams), "rejected_records": self.rejected_records,
            "skipped_items": self.skipped_items, "anchors_total": self.anchors_total, "latency_ms": self.latency_ms, "detail": self.detail,
            "accepted_records": self.accepted_records, "unusable_calls": self.unusable_calls,
            "raised_calls": self.raised_calls, "failure_cause": self.failure_cause,
            "reply_classes": self.reply_classes(), "reply_by_stream": self.reply_by_stream(),
            "call_log": [c.to_wire() for c in self.call_log],
            "skipped_budget_calls": self.skipped_budget_calls, "skipped_deadline_calls": self.skipped_deadline_calls,
        }


@dataclass(frozen=True)
class CoverageLedger:
    """The ledger of RDR-232 section 4: anchors, one finding per required facet, the deterministic
    source and scope rows, and the assessment report. ``rounds`` is 1 after the pre-generation assessment
    and 2 after the one repair reassessment."""

    anchors: tuple[EvidenceAnchor, ...] = ()
    findings: tuple[FacetFinding, ...] = ()
    sources: tuple[SourceRow, ...] = ()
    scope: ScopeRow | None = None
    assessment: AssessmentReport = field(default_factory=AssessmentReport)
    rounds: int = 1

    def finding(self, facet_id: str) -> FacetFinding | None:
        return next((f for f in self.findings if f.facet_id == facet_id), None)


@dataclass(frozen=True)
class CoverageResult:
    """What the boundary returns: the state, why, whether to skip generation, and the ledger.

    ``skip_generation`` is the only field the runner acts on. ``repair_facet_ids`` are the facets a
    targeted retrieval can still help (assessed, nothing in the held evidence bears on them); a missing
    source, a scope conflict, a contradiction and an unassessed facet are not repair targets.
    """

    state: CoverageState
    reason: str
    skip_generation: bool
    ledger: CoverageLedger
    repair: RepairReport = field(default_factory=RepairReport)

    @property
    def repair_facet_ids(self) -> tuple[str, ...]:
        return tuple(f.facet_id for f in self.ledger.findings if f.state == "unsupported" and f.reason == REPAIRABLE_REASON)

    @property
    def unread_tail_facet_ids(self) -> tuple[str, ...]:
        """Covered facets whose anchors were MOSTLY left unread (nexus-z6yvk, Sam 2026-10-31): strictly more than half of
        the ledger's anchors were never read, the per-call size cap having left them out. Every facet is assessed against
        every anchor, so the unread share is the ledger's own. A covered facet is repaired for these although it is
        covered: support in the unread tail is otherwise never read. A contradicted facet is not (a repair would be a
        search for agreement), and an unsupported one is already a repair target."""
        a = self.ledger.assessment
        if a.anchors_total <= 0 or 2 * len(a.unread_ids) <= a.anchors_total:
            return ()
        return tuple(f.facet_id for f in self.ledger.findings if f.state == "covered")

    @property
    def repair_target_ids(self) -> tuple[str, ...]:
        """What the one repair targets: the unsupported facets first, then the covered facets with most anchors unread."""
        return self.repair_facet_ids + tuple(i for i in self.unread_tail_facet_ids if i not in self.repair_facet_ids)

    @property
    def unresolved_facet_ids(self) -> tuple[str, ...]:
        """Facets the final notice names: unsupported or contradicted (an unassessed one is not known)."""
        return tuple(f.facet_id for f in self.ledger.findings if f.state in ("unsupported", "contradicted"))

    @property
    def conflicted_facet_ids(self) -> tuple[str, ...]:
        """Facets that are covered and also contradicted by other evidence: answered, but the reduction and the
        notice say the evidence conflicts."""
        return tuple(f.facet_id for f in self.ledger.findings if f.state == "covered" and f.reason == CONFLICTED_REASON)

    @property
    def unassessed_by_cause(self) -> dict[str, int]:
        """The unassessed facets of the final ledger by why they are unassessed (:data:`UNASSESSED_CAUSES`, every
        cause present). The cause is read from the finding's reason, which ``_assess`` sets from what happened."""
        out = {c: 0 for c in UNASSESSED_CAUSES}
        for f in self.ledger.findings:
            if f.state == "unassessed":
                out[_CAUSE_OF_REASON.get(f.reason, "other")] += 1
        return out

    @property
    def repair_entered(self) -> bool:
        """Whether the boundary went into the repair step at all (a repairable gap and repair switched on), whatever
        it then found: ``repair.outcome`` is ``not_attempted`` only when it did not."""
        return self.repair.outcome != "not_attempted"

    def counts(self) -> dict[str, Any]:
        """The numbers the run summary carries: no text, no source label."""
        by_state = {s: 0 for s in ("covered", "unsupported", "contradicted", "unassessed")}
        for f in self.ledger.findings:
            by_state[f.state] += 1
        a = self.ledger.assessment
        return {
            "state": self.state, "reason": self.reason, "skip_generation": self.skip_generation,
            "facets_required": len(self.ledger.findings), "facets": by_state, "anchors": len(self.ledger.anchors),
            "rounds": self.ledger.rounds, "assessment_outcome": a.outcome, "assessment_calls": a.calls,
            "facets_per_call": list(a.facets_per_call), "rejected_records": a.rejected_records,
            "repair_outcome": self.repair.outcome, "repair_facets": len(self.repair.facet_ids),
            "repair_queries": self.repair.queries, "repair_items_found": self.repair.items_found,
            "repair_items_merged": self.repair.items_merged, "repair_noted_facets": self.repair.noted_facets,
            # nexus-jypmj.3.10: additive. ``facets.covered`` still counts a conflicted facet; this is the subset.
            "facets_conflicted": len(self.conflicted_facet_ids), "unassessed_by_cause": self.unassessed_by_cause,
            "repair_entered": self.repair_entered,
        }

    @property
    def residual_gaps(self) -> tuple[FacetFinding, ...]:
        """Every required facet that is not covered, in ledger order (unassessed ones included: not known is a gap)."""
        return tuple(f for f in self.ledger.findings if f.state != "covered")

    @property
    def primary_source_state(self) -> dict[str, Any]:
        """The named sources' state in one word: ``not_named`` (none), ``missing`` (any source not found),
        ``unresolved`` (any ambiguous or not looked up), ``partial`` (any partly read), else ``covered``; with the
        number of named sources."""
        states = {s.state for s in self.ledger.sources}
        state = next((w for w in ("missing", "unresolved", "partial") if w in states), "not_named" if not states else "covered")
        return {"state": state, "sources": len(self.ledger.sources)}

    @property
    def corpus_counts(self) -> dict[str, Any]:
        """What the terminal reduction was handed, counted: anchors in all, by evidence stream and by collection
        (the collection name is a corpus identifier, never caller wording)."""
        by_stream: dict[str, int] = {}
        by_collection: dict[str, int] = {}
        for a in self.ledger.anchors:
            by_stream[a.stream] = by_stream.get(a.stream, 0) + 1
            if a.collection:
                by_collection[a.collection] = by_collection.get(a.collection, 0) + 1
        return {"anchors": len(self.ledger.anchors), "by_stream": by_stream, "by_collection": by_collection}

    def to_wire(self, *, redact: bool = False) -> dict[str, Any]:
        """The ``coverage`` object: the envelope's top-level key (RDR-232 section 4) and ``continuation.coverage``.

        Ids, states, counts and closed names only: never evidence text, never a facet's text. ``requested_facet_ids``
        and ``covered_facet_ids`` name the facets asked for and the ones settled; ``residual_gaps`` is every other
        facet with its state and reason; ``primary_source`` and ``corpus_counts`` say what was read. ``redact`` blanks
        the caller's source wording (source labels, row details).
        """
        led = self.ledger
        return {
            **self.counts(),
            "requested_facet_ids": [f.facet_id for f in led.findings],
            "covered_facet_ids": [f.facet_id for f in led.findings if f.state == "covered"],
            "residual_gaps": [{"facet_id": f.facet_id, "state": f.state, "reason": f.reason} for f in self.residual_gaps],
            "primary_source": self.primary_source_state,
            "corpus_counts": self.corpus_counts,
            "findings": [f.to_wire() for f in led.findings],
            "anchors": [a.to_wire() for a in led.anchors],
            "sources": [{"state": s.state, "detail": "" if redact else s.detail, **({} if redact else {"source": s.source})} for s in led.sources],
            "scope": None if led.scope is None else {
                "state": led.scope.state, "detail": "" if redact else led.scope.detail, "conflict_steps": list(led.scope.conflict_steps)},
            "assessment": led.assessment.to_wire(),
            "repair": self.repair.to_wire(),
        }


#: The async call the boundary makes for one assessment: ``(args, timeout=...) -> the operator's reply``.
CheckCall = Callable[..., Awaitable[Mapping[str, Any]]]
#: One repair retrieval call: ``(tool, args, timeout=...) -> the tool's structured output``.
RetrieveCall = Callable[..., Awaitable[Mapping[str, Any]]]
#: The repair's chunk fetch: ``(hits, timeout=...) -> the chunks' texts with provenance``.
FetchCall = Callable[..., Awaitable[Sequence[SupplementItem]]]


@dataclass
class BoundaryView:
    """What the runner hands the boundary at the start of the plan's trailing operator run.

    ``evidence_texts`` are the texts the run's evidence-consuming steps will be given, read before the first
    of them dispatches (the reserved stream merged, the cap applied; a step that reads another step of the run
    contributes nothing) and ``observed`` says whether that input could be read at all. ``step_outputs`` and
    ``tools`` are the executed prefix, for the deterministic checks.
    ``check`` performs one assessment call through the runner's dispatcher and accounts for it;
    ``remaining_seconds`` / ``remaining_usd`` return what is left of the answer's deadline and USD allowance
    (``None`` for no limit).

    The five optional fields are the repair's (nexus-jypmj.3.2); a view without them cannot repair
    (outcome ``unavailable``). ``retrieve(tool, args, timeout=...)`` and ``fetch(hits, timeout=...)`` run one
    retrieval call or the chunk fetch through the runner's dispatcher, as accounted ``sql`` steps.
    ``install_supplement(items)`` hands the new chunks to the runner's merge and ``rehydrate()`` re-reads the
    terminal reduction's input with them merged, returning ``(texts, observed)``. ``install_note(text)`` hands
    it the coverage note that names the unresolved facets to the generation (also read through ``rehydrate``).
    ``assessment_cost_estimate()`` is the mean known cost of the assessment calls so far, or ``None``.
    """

    evidence_texts: Sequence[str]
    observed: bool
    step_outputs: Sequence[Any]
    tools: Sequence[str]
    scope_diagnostics: Sequence[Mapping[str, Any]]
    resolved_args: Sequence[Mapping[str, Any]]
    check: CheckCall
    remaining_seconds: Callable[[], float | None]
    remaining_usd: Callable[[], float | None]
    #: The plan index of the first step of the trailing operator run: where the boundary sits.
    terminal_step: int | None = None
    retrieve: RetrieveCall | None = None
    fetch: FetchCall | None = None
    install_supplement: Callable[[Sequence[SupplementItem]], None] | None = None
    rehydrate: Callable[[], tuple[list[str], bool]] | None = None
    install_note: Callable[[str], None] | None = None
    assessment_cost_estimate: Callable[[], float | None] = lambda: None


# ── anchors ──────────────────────────────────────────────────────────────────────────────────────────────


def build_anchors(
    texts: Sequence[str], *, step_outputs: Sequence[Any], tools: Sequence[str], reserved: ReservedStream | None,
    known: Mapping[str, str] | None = None, supplement: RepairSupplement | None = None,
) -> tuple[EvidenceAnchor, ...]:
    """The typed anchors for *texts*, in order, deduplicated by text.

    Provenance follows ``evidence_diagnostics``: a reserved item carries its own stream, collection and
    document; a repair-supplement item carries the stream, collection and document it was retrieved from; any
    other text is attributed by the hash of its content (a retrieval step's ``ids`` and ``chunk_text_hash``
    are chunk hashes). A text no retrieval step produced is ``other``, kind ``unattributed``, and its chash
    is the hash of its text.

    *known* maps a text to the ``item_id`` an earlier round gave it, so the ids a first ledger cited stay
    valid in the second; any other text takes the next free id after the highest known one.
    """
    by_text = {i.text: i for i in reserved.items} if reserved is not None else {}
    known = known or {}
    next_n = 1 + max((int(v[1:]) for v in known.values() if v[1:].isdigit()), default=0)
    index: dict[str, tuple[str, str, str]] = {}
    for i, out in enumerate(step_outputs):
        if not isinstance(out, Mapping):
            continue
        tool = tools[i] if i < len(tools) else ""
        if isinstance(out.get("scope_conflict"), Mapping):
            continue
        stream = _stream_of(tool, out)
        ids = out.get("ids")
        if stream is None or not isinstance(ids, list):
            continue
        cols = _per_chunk_collections(out, len(ids))
        tumblers = out.get("tumblers")
        tums = [str(t) for t in tumblers] if isinstance(tumblers, list) and len(tumblers) == len(ids) else [""] * len(ids)
        for cid, col, tum in zip(ids, cols, tums, strict=True):
            index.setdefault(str(cid).lower(), (stream, col, tum))
        hashes = out.get("chunk_text_hash")
        if isinstance(hashes, list) and len(hashes) == len(ids):
            for h, col, tum in zip(hashes, cols, tums, strict=True):
                index.setdefault(str(h).lower(), (stream, col, tum))
    anchors: list[EvidenceAnchor] = []
    seen: set[str] = set()
    for text in texts:
        if not isinstance(text, str) or not text or text in seen:
            continue
        seen.add(text)
        item_id = known.get(text)
        if item_id is None:
            item_id = f"e{next_n}"
            next_n += 1
        held = by_text.get(text)
        if held is not None:
            is_t2 = held.stream == "t2"
            anchors.append(EvidenceAnchor(
                item_id=item_id, stream=held.stream, kind="t2_record" if is_t2 else "chunk", chash=held.chash,
                collection=held.collection, tumbler="" if is_t2 else held.doc_id,
                t2_record_id=held.token if is_t2 else "", source=held.source, content=text,
            ))
            continue
        repaired = supplement.provenance(text) if supplement is not None else None
        if repaired is not None:
            anchors.append(EvidenceAnchor(
                item_id, repaired.stream, "chunk", repaired.chash or hashlib.sha256(strip_source_note(text).encode("utf-8")).hexdigest(),
                repaired.collection, repaired.tumbler, "", "", text))
            continue
        digest = hashlib.sha256(strip_source_note(text).encode("utf-8")).hexdigest()
        found = index.get(digest)
        if found is None:
            anchors.append(EvidenceAnchor(item_id, "other", "unattributed", digest, "", "", "", "", text))
        else:
            anchors.append(EvidenceAnchor(item_id, found[0], "chunk", digest, found[1], found[2], "", "", text))
    return tuple(anchors)


# ── the state table ──────────────────────────────────────────────────────────────────────────────────────

_UNASSESSED_REASON: dict[str, str] = {
    "skipped_deadline": "assessment_skipped_deadline",
    "skipped_budget": "assessment_skipped_budget",
    "not_attempted": "assessment_not_attempted",
    # DEFENSIVE: no pass produces this today. ``_assess`` marks a facet unassessed only for a failed, unusable or
    # skipped call, and any of those makes the outcome something other than ``ok``; the cap-omitted anchors that once
    # produced it now leave a facet ``unsupported`` (nexus-jypmj.23). If a future pass leaves every facet unassessed
    # under ``ok``, the reason is not a failed assessment, and this keeps it from saying so.
    "ok": "assessment_incomplete",
}


def state_for(
    finding_states: Sequence[str], *, assessment_outcome: str, anchors: int, observed: bool, scope_conflict: bool = False,
) -> tuple[CoverageState, str, bool]:
    """``(coverage.state, reason, skip_generation)`` from the facet states (RDR-232 section 4).

    * no facets: ``unassessed`` (``no_facets``).
    * the terminal input could not be read and no deterministic check decided a facet: ``unassessed``.
    * observed but no anchors: ``unavailable`` (``no_evidence``, or ``scope_conflict`` when the caller's
      scope removed all of it); generation is NOT skipped, the existing zero-evidence path answers it.
    * every facet unassessed: ``unassessed``; the reason names why (failed, skipped for time or budget).
    * every facet known and none counting toward a usable answer (:func:`counts_toward_a_usable_answer`:
      ``covered`` or ``contradicted``, so every facet unsupported): ``unavailable`` / ``uncovered`` and generation
      is skipped. A usable answer needs at least one such facet, and the nexus-jypmj.3.12 ruling skips generation
      when the ledger has none. A contradicted facet is retained for the reduction (``partial``), alone or beside a
      covered one (nexus-jypmj.3.13). An unassessed facet is never known to be uncovered, so any unassessed facet
      keeps generation.
    * every facet covered: ``complete``. Otherwise ``partial``.

    The "usable answer" half of the table's ``partial`` and ``unavailable`` rows depends on the generated
    prose and is applied after generation (nexus-jypmj.3.3); this is the pre-generation state.
    """
    total = len(finding_states)
    if total == 0:
        return "unassessed", "no_facets", False
    known = any(s != "unassessed" for s in finding_states)
    if not observed and not known:
        return "unassessed", "evidence_not_observed", False
    if observed and anchors == 0:
        return "unavailable", "scope_conflict" if scope_conflict else "no_evidence", False
    n = {s: sum(1 for x in finding_states if x == s) for s in ("covered", "unsupported", "contradicted", "unassessed")}
    if n["unassessed"] == total:
        return "unassessed", _UNASSESSED_REASON.get(assessment_outcome, "assessment_failed"), False
    usable = sum(1 for s in finding_states if counts_toward_a_usable_answer(s))
    if usable == 0 and n["unassessed"] == 0:
        return "unavailable", "uncovered", True
    if n["covered"] == total:
        return "complete", "all_covered", False
    return "partial", "unresolved_facets", False


# ── the boundary ─────────────────────────────────────────────────────────────────────────────────────────


def _norm(ref: str) -> str:
    return re.sub(r"\s+", "", ref).upper()


def dispatch_concurrency_cap() -> int | None:
    """The process-wide gate on the LLM backend's concurrent calls, or ``None`` when there is none to read.

    The API backends (``anthropic``, ``openai_compat``) pass every HTTP attempt through ``NX_LLM_MAX_CONCURRENCY``
    (:func:`nexus.operators.llm_api.configured_concurrency`); a call past it waits for a permit inside its own timeout.
    The ``claude -p`` backend has no gate (``None``), and neither does a run where no backend is available or the
    setting cannot be read: the stage then falls back to :data:`MAX_CONCURRENT_ASSESSMENT_CALLS` alone."""
    try:
        from nexus.operators.llm_api import configured_concurrency  # noqa: PLC0415 - deferred: the ledger stays pure at import
        from nexus.operators.llm_backend import backend_status  # noqa: PLC0415 - deferred, as above

        if backend_status().name in ("cli", "none"):
            return None
        return configured_concurrency()
    except Exception:  # noqa: BLE001 - an unreadable setting or config must not stop the assessment; the constant bounds it
        return None


@dataclass(frozen=True)
class CoverageBoundary:
    """The configuration of one run's boundary, handed to ``plan_run``.

    ``request`` is the Phase 2 :class:`RequestCoverage`; ``None`` is a run that reached the boundary with
    none (state ``unassessed``). ``scope``, ``named_sources``, ``primary``, ``lifecycle``, ``owner_resolution``
    and ``dropped_scope`` are the inputs ``nx_answer`` already holds for ``build_evidence_diagnostics``.
    ``skip_generation_when_uncovered`` is the one switch on the destructive rule (decision b);
    ``max_repair_queries`` is the repair's (nexus-jypmj.3.2): ``0`` switches the repair off. The other
    fields are the tunables the module docstring names.
    """

    request: RequestCoverage | None
    scope: ScopeConstraint = field(default_factory=lambda: ScopeConstraint("none", ""))
    named_sources: Any = None
    primary: Any = None
    lifecycle: Any = None
    owner_resolution: Mapping[str, Any] | None = None
    dropped_scope: str = ""
    #: The document a one-source run is confined to (nexus-jypmj): the repair queries carry its chunk filter, and a
    #: repair hit is inside the scope exactly when its chunk hash is in the document's manifest set.
    confinement: Any = None
    skip_generation_when_uncovered: bool = True
    max_repair_queries: int = MAX_REPAIR_QUERIES
    max_repair_items: int = MAX_REPAIR_ITEMS
    max_facets_per_call: int = MAX_FACETS_PER_ASSESSMENT_CALL
    call_timeout_s: float = ASSESSMENT_CALL_TIMEOUT_S
    stage_share: float = ASSESSMENT_STAGE_DEADLINE_SHARE
    min_call_s: float = MIN_ASSESSMENT_CALL_S
    max_concurrent_calls: int = MAX_CONCURRENT_ASSESSMENT_CALLS
    call_cost_prior_usd: float = ASSESSMENT_CALL_COST_PRIOR_USD
    max_anchors_per_call: int = MAX_ANCHORS_PER_ASSESSMENT_CALL

    def stage_allowance(self, remaining: float | None) -> float | None:
        """Seconds the whole assessment stage may use out of *remaining* deadline seconds: ``stage_share`` of
        it, or ``None`` (no stage cap) with no deadline."""
        return None if remaining is None else max(remaining, 0.0) * self.stage_share

    def call_timeout(self, stage_left: float | None) -> float:
        """The timeout of one call drawn from *stage_left* seconds of the stage allowance: ``call_timeout_s``
        with no deadline, else that or what is left of the stage, whichever is smaller; ``0.0`` (do not start
        the call) when it falls under ``min_call_s``."""
        if stage_left is None:
            return self.call_timeout_s
        allowance = min(self.call_timeout_s, max(stage_left, 0.0))
        return allowance if allowance >= self.min_call_s else 0.0

    def _stage_reserve(self, view: BoundaryView) -> float | None:
        """Seconds of deadline the stage must leave untouched (repair and generation's), or ``None`` with no deadline."""
        remaining = view.remaining_seconds()
        allowance = self.stage_allowance(remaining)
        return None if remaining is None or allowance is None else max(remaining, 0.0) - allowance

    # ── deterministic rows ──

    def _deterministic(self, view: BoundaryView) -> tuple[tuple[SourceRow, ...], ScopeRow | None]:
        recorder = active_recorder()
        if recorder is None or recorder.items is None:
            recorder = ReducerInputRecorder()
            recorder.items = tuple(view.evidence_texts) if view.observed else None
            recorder.step = view.terminal_step
        try:
            diag = build_evidence_diagnostics(
                steps=list(view.step_outputs), tools=list(view.tools), scope=self.scope,
                scope_diagnostics=list(view.scope_diagnostics), resolved_args=list(view.resolved_args),
                recorder=recorder, reserved=active_reserved_stream(), primary=self.primary, lifecycle=self.lifecycle,
                named_sources=self.named_sources, reducer_expected=True, dropped_scope=self.dropped_scope,
                owner_resolution=self.owner_resolution,
            )
        except Exception as exc:  # noqa: BLE001 — a diagnostics failure never fails the answer; the rows are then absent
            _log.warning("nx_answer_coverage_deterministic_failed", error=type(exc).__name__)
            return (), None
        sources = tuple(SourceRow(str(r["source"]), str(r["state"]), str(r.get("detail") or "")) for r in diag["source_check"]["sources"])
        sc = diag["scope_check"]
        return sources, ScopeRow(str(sc["state"]), str(sc.get("detail") or ""), tuple(int(i) for i in sc.get("conflict_steps") or ()))

    @staticmethod
    def _blocked(facets: Sequence[Facet], sources: Sequence[SourceRow]) -> dict[str, str]:
        """Facet id -> ``source_missing`` for a facet a missing named source decides.

        Only ``missing`` blocks. An ``unresolved`` source (ambiguous, or its lookup could not run) is not known to be
        absent, so its facets are assessed on the evidence that was read and the ambiguity is disclosed (nexus-jypmj.3.17).

        A facet that names a missing source is decided by it; a facet that names none is decided when every
        named source is missing (nothing it could be grounded in was read). A facet that names a source that
        was read is not blocked.
        """
        missing = {_norm(s.source) for s in sources if s.state == "missing"}
        if not missing:
            return {}
        all_missing = len(missing) == len({_norm(s.source) for s in sources})
        out: dict[str, str] = {}
        for f in facets:
            hints = {_norm(h) for h in f.source_hints}
            if (hints & missing) or (not hints and all_missing):
                out[f.id] = "source_missing"
        return out

    # ── the evaluation ──

    async def evaluate(self, view: BoundaryView, *, prior: CoverageResult | None = None) -> CoverageResult:
        """Build the ledger for *view* and compute the state. Never raises for an assessment failure."""
        known = {a.content: a.item_id for a in prior.ledger.anchors} if prior is not None else None
        anchors = build_anchors(
            view.evidence_texts, step_outputs=view.step_outputs, tools=view.tools, reserved=active_reserved_stream(),
            known=known, supplement=active_supplement(),
        )
        sources, scope_row = self._deterministic(view)
        rounds = prior.ledger.rounds + 1 if prior is not None else 1
        base = CoverageLedger(anchors=anchors, sources=sources, scope=scope_row, rounds=rounds)
        carried = prior.repair if prior is not None else RepairReport()
        request = self.request
        if request is None or not request.facets:
            reason = "no_request_coverage" if request is None else "no_facets"
            result = CoverageResult("unassessed", reason, False, base, carried)
            self._log(result)
            return result

        conflict = scope_row is not None and scope_row.state == "conflict"
        blocked = self._blocked(request.facets, sources)
        prior_by_id = {f.facet_id: f for f in prior.ledger.findings} if prior is not None else {}
        report = prior.ledger.assessment if prior is not None else AssessmentReport()

        if view.observed and not anchors:
            reason = "scope_conflict" if conflict else "no_evidence"
            findings = tuple(FacetFinding(f.id, "unsupported", (), blocked.get(f.id, reason)) for f in request.facets)
            state, why, skip = state_for([f.state for f in findings], assessment_outcome=report.outcome, anchors=0, observed=True, scope_conflict=conflict)
            result = CoverageResult(state, why, False, replace(base, findings=findings, assessment=report), carried)
            self._log(result)
            return result

        anchor_ids = {a.item_id for a in anchors}
        # A covered or contradicted finding stands only while the evidence it cites is still in the reducer's input.
        stands = {
            fid for fid, f in prior_by_id.items()
            if f.state in ("covered", "contradicted") and set(f.evidence_ids) <= anchor_ids
        }
        # A covered facet with most of its anchors unread is reassessed against the unread ones (nexus-z6yvk); what it
        # already holds stands, the reassessment can only add to it.
        tail = stands & set(prior.unread_tail_facet_ids) if prior is not None else set()
        keep = stands - tail
        # A repairable gap was assessed against what the first round READ, so only the new anchors and the anchors
        # that round left unread (the per-call size cap, nexus-jypmj.23) can change it; every other facet that is not
        # decided is assessed against all of them.
        delta = [
            f for f in request.facets
            if f.id not in blocked and f.id not in keep and (p := prior_by_id.get(f.id)) is not None
            and (f.id in tail or (p.state == "unsupported" and p.reason == REPAIRABLE_REASON))
        ]
        delta_ids = {f.id for f in delta}
        full = [f for f in request.facets if f.id not in blocked and f.id not in keep and f.id not in delta_ids]
        assessed: dict[str, FacetFinding] = {}
        new_report = AssessmentReport()
        reserve = self._stage_reserve(view)  # one stage allowance for the full and the delta assessment together
        if full and anchors:
            assessed, new_report = await self._assess(full, anchors, view, reserve)
        prior_ids = {a.item_id for a in prior.ledger.anchors} if prior is not None else set()
        prior_unread = set(prior.ledger.assessment.unread_ids) if prior is not None else set()
        # The new anchors, and the first round's unread ones (they are still in the input, never assessed): the same
        # per-call size cap and the same stage allowance bound this pass as they bound the first.
        fresh = [a for a in anchors if a.item_id not in prior_ids or a.item_id in prior_unread]
        if delta and fresh:
            more, more_report = await self._assess(delta, fresh, view, reserve)
            assessed.update(more)
            new_report = more_report if new_report.outcome == "not_attempted" else new_report.merged_with(more_report)
        report = report.merged_with(new_report) if prior is not None else new_report
        # The unread count states what the check was handed: items still in the input that no round read, of all the
        # items the check was given (the same anchor never counts twice, nor past the number of anchors).
        unread = tuple(i for i in report.unread_ids if i in anchor_ids)
        report = replace(report, unread_ids=unread, skipped_items=len(unread), anchors_total=len(anchors))

        findings_list: list[FacetFinding] = []
        for f in request.facets:
            earlier = prior_by_id.get(f.id)
            if f.id in blocked:
                findings_list.append(FacetFinding(f.id, "unsupported", (), blocked[f.id]))
            elif f.id in keep:
                findings_list.append(prior_by_id[f.id])
            elif f.id in tail and earlier is not None:
                findings_list.append(_with_tail_evidence(earlier, assessed.get(f.id)))
            elif f.id in assessed and not (assessed[f.id].state == "unassessed" and earlier is not None and earlier.state == "unsupported"):
                findings_list.append(assessed[f.id])
            elif earlier is not None and earlier.state == "unsupported":
                findings_list.append(earlier)  # the reassessment failed, was skipped or had nothing new: the first ledger stands
            else:
                findings_list.append(FacetFinding(f.id, "unassessed", (), _UNASSESSED_REASON.get(report.outcome, "assessment_failed")))
        findings = tuple(findings_list)
        state, why, skip = state_for(
            [f.state for f in findings], assessment_outcome=report.outcome, anchors=len(anchors), observed=view.observed, scope_conflict=conflict,
        )
        result = CoverageResult(state, why, skip and self.skip_generation_when_uncovered, replace(base, findings=findings, assessment=report), carried)
        self._log(result)
        return result

    # ── the boundary the runner calls: the ledger, then at most one repair ──

    async def run(self, view: BoundaryView) -> CoverageResult:
        """:meth:`evaluate`, and one repair when the first ledger has a gap a targeted retrieval can still help.

        The result's ``skip_generation`` is decided on the FINAL ledger, so a ledger that covers nothing gets
        its one repair before generation is skipped. A repair that cannot run, fails or finds nothing new
        returns the first ledger with the reason in :attr:`CoverageResult.repair`.
        """
        first = await self.evaluate(view)
        targets = first.repair_target_ids
        result = first
        if targets and self.max_repair_queries > 0:
            try:
                result = await self._repair(view, first, targets)
            except Exception as exc:  # noqa: BLE001 — a repair failure never fails the answer; the first ledger stands
                _log.warning("nx_answer_coverage_repair_failed", error=type(exc).__name__)  # the type only: an exception's text can carry prompt text
                result = replace(first, repair=RepairReport(
                    "retrieval_failed", targets, detail=f"{type(exc).__name__}", **self._why(first)))
                self._withdraw(view)
        return self._note_unresolved(view, result)

    @staticmethod
    def _why(first: CoverageResult) -> dict[str, Any]:
        """The repair report's ``reason`` and ``unread_facet_ids`` for a repair of *first*'s targets (nexus-z6yvk)."""
        unsupported, tail = first.repair_facet_ids, tuple(i for i in first.unread_tail_facet_ids if i not in first.repair_facet_ids)
        return {"reason": repair_reason(unsupported=bool(unsupported), unread_tail=bool(tail)), "unread_facet_ids": tail}

    @staticmethod
    def _withdraw(view: BoundaryView) -> None:
        """Best effort after an unexpected failure: take the repair evidence back and read the input again, so
        what the reduction is handed matches the ledger that stands."""
        try:
            if view.install_supplement is not None:
                view.install_supplement(())
            if view.rehydrate is not None:
                view.rehydrate()
        except Exception as exc:  # noqa: BLE001 — the reduction hydrates as it would have
            _log.warning("nx_answer_coverage_repair_withdraw_failed", error=type(exc).__name__)

    def _note_unresolved(self, view: BoundaryView, result: CoverageResult) -> CoverageResult:
        """Hand the generation the explicit list of unresolved facets (RDR section 4, step 4), and the facets
        whose evidence conflicts (covered, and also contradicted).

        Only for a ``partial`` ledger (some evidence, some facet unresolved) or a ``complete`` one with a
        conflicted facet: a ledger that skips generation has no generation to tell, and a ``no_evidence``
        ledger must keep the existing zero-evidence path, which a note item would defeat. The note is installed
        on the runner's merge, the input is read once more, and the note counts as delivered only if the input
        now holds it.
        """
        ids = result.unresolved_facet_ids
        conflicted = result.conflicted_facet_ids
        if (
            result.state not in ("partial", "complete") or not (ids or conflicted) or result.skip_generation
            or view.install_note is None or view.rehydrate is None or self.request is None
        ):
            return result
        by_id = {f.id: f for f in self.request.facets}
        state_of = {f.facet_id: f.state for f in result.ledger.findings}
        listed = [(by_id[fid], state_of[fid]) for fid in ids if fid in by_id]
        listed += [(by_id[fid], CONFLICTED_REASON) for fid in conflicted if fid in by_id]
        if not listed:
            return result
        note = unresolved_note(listed)
        view.install_note(note)
        try:
            texts, _ = view.rehydrate()
        except Exception as exc:  # noqa: BLE001 — withdraw the note; the reduction hydrates as it would have
            view.install_note("")
            _log.warning("nx_answer_coverage_note_rehydrate_failed", error=type(exc).__name__)
            return result
        if note not in texts:
            view.install_note("")
            return result
        return replace(result, repair=replace(result.repair, noted_facets=len(listed)))

    def _repair_targets(self, view: BoundaryView) -> tuple[list[str], str, list[RepairTarget]]:
        """``(primary collections, their subtree, plan targets)``: where a repair query may look.

        The primary source's collections that hold something inside the caller's scope (a corpus scope admits the
        collection; a catalog-subtree scope admits a document under it, so a document address that names no
        primary document refuses the primary source), asked under the scope's subtree when it is a subtree a query
        can take. And the corpus, subtree and filters (where, content_type, author) each of the plan's own
        retrieval steps ran under, which the runner has already narrowed to the caller's scope. With no such step,
        a corpus scope is the target itself.
        """
        scope = self.scope
        primary: list[str] = []
        for item in getattr(self.primary, "items", ()) or ():
            col = getattr(item, "collection", "")
            if not col:
                continue
            if scope.kind == "subtree":
                admitted = scope.admits(col, tumbler=getattr(item, "doc_id", ""))
            elif scope.narrows_corpus:
                admitted = scope.admits(col)
            else:
                admitted = True
            if admitted:
                primary.append(col)
        subtree = scope.value if scope.kind == "subtree" and not scope.is_document_address else ""
        plan: list[RepairTarget] = []
        for r in view.resolved_args:
            if r.get("skipped") or r.get("tool") not in ("search", "query") or not r.get("corpus"):
                continue
            filters = r.get("filters")
            kept = tuple(sorted((str(k), str(v)) for k, v in filters.items() if v)) if isinstance(filters, Mapping) else ()
            target = RepairTarget(str(r["corpus"]), str(r.get("subtree") or subtree), kept)
            if target not in plan:
                plan.append(target)
        if not plan and scope.narrows_corpus:
            plan.append(RepairTarget(scope.value))
        return list(dict.fromkeys(primary)), subtree, plan

    def _hit_in_scope(self, hit: RepairHit) -> bool:
        """Whether a repair hit lies inside the caller's scope. A subtree filter ran server side where a query
        could take one, so an unattributed hit is kept there; a document address cannot be filtered server side,
        so a hit that does not name that document is not inside."""
        confinement = self.confinement
        if confinement is not None:
            # nexus-jypmj: a hit's catalog tumbler is the last writer of a chunk shared by several documents, so
            # it cannot say which document the chunk is in; the document's own manifest hashes can.
            key = hit.chash or hit.id
            return bool(key) and confinement.contains(key)
        scope = self.scope
        if scope.kind == "subtree":
            if hit.tumbler:
                return scope.admits(hit.collection, tumbler=hit.tumbler)
            return not scope.is_document_address
        if scope.narrows_corpus and hit.collection:
            return scope.admits(hit.collection)
        return True

    async def _repair(self, view: BoundaryView, first: CoverageResult, targets: tuple[str, ...]) -> CoverageResult:
        started = time.monotonic()

        def done(outcome: RepairOutcome, *, queries: int = 0, found: int = 0, merged: int = 0, detail: str = "", result: CoverageResult | None = None) -> CoverageResult:
            report = RepairReport(
                outcome, targets, queries, found, merged, int((time.monotonic() - started) * 1000), detail,
                **self._why(first))
            return replace(result or first, repair=report)

        if view.retrieve is None or view.fetch is None or view.install_supplement is None or view.rehydrate is None:
            return done("unavailable", detail="the runner gave the boundary no retrieval hooks")
        allowance = repair_allowance(view.remaining_seconds())
        if allowance <= 0:
            return done("skipped_deadline", detail="too little of the deadline was left")
        estimate = view.assessment_cost_estimate()
        # The first round's unread anchors are reassessed beside the repair evidence (nexus-jypmj.23): count their packs.
        unread_first = frozenset(first.ledger.assessment.unread_ids)
        stream_of = {a.item_id: a.stream for a in first.ledger.anchors}
        planned = planned_reassessment_calls(
            len(targets), self.max_facets_per_call,
            unread_per_stream=[sum(1 for i in unread_first if stream_of.get(i) == s) for s in STREAM_ORDER],
            full_facets=sum(1 for f in first.ledger.findings if f.state in ("unsupported", "unassessed") and f.facet_id not in targets),
            anchors_per_stream=[sum(1 for a in first.ledger.anchors if a.stream == s) for s in STREAM_ORDER],
            items_per_call=self.max_anchors_per_call,
        )
        if not usd_affordable(view.remaining_usd(), None if estimate is None else estimate * planned):
            return done("skipped_budget", detail="the USD allowance could not carry the reassessment")

        # The targets are in priority order, unsupported facets first (nexus-z6yvk): the query budget goes to them first.
        by_id = {f.id: f for f in (self.request.facets if self.request else ())}
        facets = [by_id[i] for i in targets if i in by_id]
        unsupported_ids = set(first.repair_facet_ids)
        primary, primary_subtree, plan = self._repair_targets(view)
        confinement = self.confinement
        # Without the engine's chunk filter a repair query reads the whole collection, so the siblings of the document
        # fill the page and the membership test drops them: ask for a wider page (nexus-jypmj).
        limit = (
            REPAIR_RESULTS_PER_QUERY_UNFILTERED
            if confinement is not None and not getattr(confinement, "sends_filter", False) else REPAIR_RESULTS_PER_QUERY
        )
        # Unsupported facets against every target first, then the covered facets with most anchors unread against what
        # budget is left: the covered facets never take a query an unsupported facet could have had.
        queries: list[RepairQuery] = []
        for group in ([f for f in facets if f.id in unsupported_ids], [f for f in facets if f.id not in unsupported_ids]):
            room = self.max_repair_queries - len(queries)
            if room > 0:
                queries += build_repair_queries(
                    group, primary_collections=primary, plan_targets=plan, max_queries=room,
                    limit=limit, primary_subtree=primary_subtree,
                )
        if confinement is not None and getattr(confinement, "sends_filter", False):
            for q in queries:
                q.args["chash_allow"] = confinement.allow_list()
        if not queries:
            return done("no_target", detail="no primary collection or plan retrieval scope to ask")

        # retrieval: bounded to its share of the allowance (the queries get three quarters of it, the chunk
        # fetch the rest), stopped by the first failure
        retrieval_until = started + allowance * REPAIR_RETRIEVAL_SHARE
        queries_until = started + allowance * REPAIR_RETRIEVAL_SHARE * 0.75
        # Every anchor of the first ledger is held, the unread ones too: a hit on an unread anchor would be fetched
        # only to be dropped by the text de-duplication below (its text is already in the input), at the cost of a
        # fetch slot and an inflated ``found``. The unread anchors are reassessed from the first ledger (``prior``),
        # not through retrieval.
        held_hashes = {a.chash.lower() for a in first.ledger.anchors if a.chash}
        hits: dict[str, RepairHit] = {}
        issued, failure = 0, ""
        # What went wrong in the retrieval, when the repair then goes on only to reassess the unread anchors:
        # ``(outcome, detail)``, so the report does not read as a plain ``completed`` (nexus-jypmj.23).
        retrieval_note: tuple[RepairOutcome, str] | None = None
        for q in queries:
            left = queries_until - time.monotonic()
            if left < MIN_REPAIR_QUERY_S:
                break
            issued += 1
            try:
                out = await view.retrieve(q.tool, q.args, timeout=left)
            except Exception as exc:  # noqa: BLE001 — a failed repair call leaves the first ledger in force
                failure = type(exc).__name__
                _log.warning("nx_answer_coverage_repair_query_failed", tool=q.tool, error=failure)
                break
            for hit in _hits(out):
                if (hit.chash or hit.id).lower() not in held_hashes and self._hit_in_scope(hit):
                    hits.setdefault(hit.id, hit)
        found = len(hits)
        # With unread anchors the repair has something to assess even when the retrieval brings nothing new: the
        # anchors the first round never read are reassessed below, so these early returns apply only without them.
        if failure and not hits and not unread_first:
            return done("retrieval_failed", queries=issued, detail=f"a repair query failed: {failure}")
        if not hits and not unread_first:
            return done("no_new_evidence", queries=issued, detail="the targeted queries found nothing not already held")

        texts, observed, merged = view.evidence_texts, view.observed, 0
        new: list[SupplementItem] = []
        if hits:
            try:
                fetched = await view.fetch(list(hits.values())[: self.max_repair_items * 2], timeout=max(MIN_REPAIR_QUERY_S, retrieval_until - time.monotonic()))
            except Exception as exc:  # noqa: BLE001 — as above
                _log.warning("nx_answer_coverage_repair_fetch_failed", error=type(exc).__name__)
                if not unread_first:
                    return done("retrieval_failed", queries=issued, found=found, detail=f"the chunk fetch failed: {type(exc).__name__}")
                retrieval_note = ("retrieval_failed", f"the chunk fetch failed: {type(exc).__name__}")
                fetched = []
            held_texts = {strip_source_note(t) for t in view.evidence_texts if isinstance(t, str)}
            for item in fetched:
                bare = strip_source_note(item.text)
                if item.text and bare not in held_texts:
                    held_texts.add(bare)
                    new.append(item)
                if len(new) >= self.max_repair_items:
                    break
        if not new and not unread_first:
            return done("no_new_evidence", queries=issued, found=found, detail="the chunks fetched were already held")
        if failure and not hits:
            retrieval_note = ("retrieval_failed", f"a repair query failed: {failure}")
        elif not hits:
            retrieval_note = ("no_new_evidence", "the targeted queries found nothing not already held")
        elif not new and retrieval_note is None:
            retrieval_note = ("no_new_evidence", "the chunks fetched were already held")

        if new:
            # merge: the runner puts the items into the reduction's input and returns that input
            view.install_supplement(new)
            try:
                texts, observed = view.rehydrate()
            except Exception as exc:  # noqa: BLE001 — withdraw the items; the reduction hydrates as it would have
                view.install_supplement(())
                _log.warning("nx_answer_coverage_repair_rehydrate_failed", error=type(exc).__name__)
                if not unread_first:
                    return done("not_merged", queries=issued, found=found, detail=f"re-reading the input failed: {type(exc).__name__}")
                retrieval_note = ("not_merged", f"re-reading the input failed: {type(exc).__name__}")
                texts, observed, new = view.evidence_texts, view.observed, []
            else:
                in_input = {strip_source_note(t) for t in texts if isinstance(t, str)}
                merged = sum(1 for i in new if strip_source_note(i.text) in in_input)
                if merged == 0:
                    view.install_supplement(())
                    if not unread_first:
                        return done("not_merged", queries=issued, found=found, detail="the reduction's input holds none of the repair evidence")
                    retrieval_note = ("not_merged", "the reduction's input holds none of the repair evidence")
                    texts, observed = view.evidence_texts, view.observed

        # recompute the ledger once, inside the repair allowance
        repair_until = started + allowance

        def remaining() -> float | None:
            left = max(0.0, repair_until - time.monotonic())
            outer = view.remaining_seconds()
            return left if outer is None else min(left, outer)

        reassess = replace(view, evidence_texts=texts, observed=observed, remaining_seconds=remaining)
        final = await replace(self, stage_share=1.0).evaluate(reassess, prior=first)
        if retrieval_note is not None:
            # The reassessment ran and is in ``final``; the outcome names why the retrieval brought nothing in.
            outcome, why = retrieval_note
            detail = f"{why}; the {len(unread_first)} anchors the first round left unread were reassessed"
            return done(outcome, queries=issued, found=found, merged=merged, detail=detail, result=final)
        detail = f"a repair query failed: {failure}; the queries after it were not issued" if failure else ""
        return done("completed", queries=issued, found=found, merged=merged, detail=detail, result=final)

    async def _assess(
        self, facets: Sequence[Facet], anchors: Sequence[EvidenceAnchor], view: BoundaryView, reserve: float | None = None,
    ) -> tuple[dict[str, FacetFinding], AssessmentReport]:
        """One concurrent assessment pass (nexus-jypmj.17). *reserve* is the seconds of deadline the stage must leave
        untouched (see :meth:`_stage_reserve`; ``None``: no deadline), so every call draws its timeout from what is
        left of the one stage allowance, however many calls the streams, facet groups and packs make.

        The planned calls are (stream, facet group, pack) in that nesting order, and they run at most
        ``max_concurrent_calls`` at a time, each started in plan order. A call decides at the moment it starts
        whether it may: the USD allowance (net of the calls in flight, at the mean cost of those finished) and the
        stage's remaining time (a call's timeout is what is left of the stage, capped by ``call_timeout_s``, and the
        call is cut at that timeout from outside, so no stage overruns its share however the check behaves). A call that
        does not fit beside the calls in flight (on the prior before any cost is measured, on the measured cost after)
        waits for a call to end and is judged again; it is skipped for budget only when the measured cost says it cannot
        fit even alone. If every call in flight ends without reporting a cost, a held call starts when the allowance
        is above zero, one at a time. One
        call that raises or replies unusably affects only its own facets; the others still count. The replies are
        read after all calls finish, in plan order, so the ledger, the call log and every count are the same
        whatever order the calls finish in."""
        cap = max(1, self.max_facets_per_call)
        groups = [tuple(facets[i:i + cap]) for i in range(0, len(facets), cap)]
        by_stream = {s: [a for a in anchors if a.stream == s] for s in STREAM_ORDER}
        planned: list[_PlannedCall] = []
        # The items the size cap leaves out of a stream, counted once per stream however many facet groups and
        # packs it has, and whether or not a call that carries the stream's other items ran (a skipped call does not
        # make its stream's omitted items vanish from the count).
        unread_ids: list[str] = []
        for s in STREAM_ORDER:
            if not by_stream[s]:
                continue
            _payload, included, _omitted = _pack(by_stream[s])
            unread_ids.extend(a.item_id for a in by_stream[s][len(included):])
            packs = _balanced_chunks(included, self.max_anchors_per_call)
            for g in groups:
                for pack in packs:
                    planned.append(_PlannedCall(
                        s, g, [{"id": a.item_id, "text": a.content} for a in pack], frozenset(a.item_id for a in pack),
                    ))
        tag_of = {f.id.lower(): f.id for f in facets}
        supports: dict[str, set[str]] = {f.id: set() for f in facets}
        contradicts: dict[str, set[str]] = {f.id: set() for f in facets}
        incomplete: dict[str, str] = {}  # facet id -> the finding reason for why it is not known
        started = time.monotonic()
        width = max(1, self.max_concurrent_calls)
        process_gate = dispatch_concurrency_cap()
        if process_gate is not None:
            width = min(width, max(1, process_gate))  # an API backend's calls queue for permits past its gate
        gate = asyncio.Semaphore(width)
        in_flight = 0
        waiters: list[asyncio.Future[None]] = []  # calls held on the prior alone, in plan order; woken when a call ends

        def wake_held() -> None:
            for w in waiters:
                if not w.done():
                    w.set_result(None)
            waiters.clear()

        async def run_one(call: _PlannedCall) -> _CallOutcome:
            nonlocal in_flight
            held = False
            while True:
                waiter: asyncio.Future[None] | None = None
                async with gate:
                    usd = view.remaining_usd()
                    remaining = view.remaining_seconds()
                    stage_left = None if reserve is None or remaining is None else remaining - reserve
                    timeout = self.call_timeout(stage_left)
                    if usd is not None:
                        # Once a call has reported a cost, EVERY call is judged on it: it runs when the allowance left
                        # covers it beside the calls in flight (a cost that exactly uses up what is left still runs),
                        # is held when it would fit once those finish, and is skipped when it cannot fit even alone.
                        # Before any cost is measured the prior stands in, but only for a call decided beside others
                        # in flight: a call with none in flight starts whenever the allowance is above zero, as the
                        # serial loop's did (the budget is soft: that one call past it is the accepted overshoot).
                        # A call held on the prior alone never starts on it while others run; it waits for a measured
                        # cost or for the wave to end.
                        if usd <= 0:
                            return _CallOutcome(skipped="skipped_budget", why="the USD allowance was spent")
                        estimate = view.assessment_cost_estimate()
                        if estimate is None:
                            hold = in_flight > 0 and (held or usd - self.call_cost_prior_usd * (in_flight + 1) < 0)
                        elif usd - estimate * (in_flight + 1) >= -_USD_EPS:
                            hold = False
                        elif in_flight > 0 and usd - estimate >= -_USD_EPS:
                            hold = True  # does not fit beside the calls in flight, fits once they finish
                        else:
                            return _CallOutcome(skipped="skipped_budget", why="the USD allowance cannot cover another call")
                        if hold:
                            # It waits outside the gate, so a call behind it is judged now and is not kept off a slot.
                            held = True
                            waiter = asyncio.get_running_loop().create_future()
                            waiters.append(waiter)
                    if waiter is None:
                        if timeout <= 0:
                            return _CallOutcome(skipped="skipped_deadline", why="too little of the assessment's share of the deadline was left")
                        args = {"items": json.dumps(call.payload), "check_instruction": _instruction(call.group)}
                        in_flight += 1
                        try:
                            reply = await asyncio.wait_for(view.check(args, timeout=timeout), timeout=timeout)
                            return _CallOutcome(records=_records(reply))
                        except Exception as exc:  # noqa: BLE001 — a failed assessment leaves the answer; its facets stay unassessed
                            return _CallOutcome(error=exc)
                        finally:
                            in_flight -= 1
                            wake_held()
                await waiter

        tasks = [asyncio.ensure_future(run_one(c)) for c in planned]
        try:
            outcomes = await asyncio.gather(*tasks)
        except BaseException:
            # A CancelledError that is not our own wait_for's (the caller was cancelled, or a check cancelled itself)
            # or any other exception that escapes a call: gather re-raises it and leaves the other calls running, so
            # cancel them and wait for them to unwind (each kills its own child) before leaving the stage.
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        def mark(group: Sequence[Facet], reason: str, *, override: bool = False) -> None:
            for f in group:
                if override:
                    incomplete[f.id] = reason
                else:
                    incomplete.setdefault(f.id, reason)

        calls, rejected = 0, 0
        read_ids: dict[str, None] = {}  # items a call with a usable reply carried
        accepted_total, unusable_n, raised_n = 0, 0, 0
        call_log: list[AssessmentCall] = []
        per_call: list[int] = []
        used_streams: list[str] = []
        completed = 0  # calls whose reply carried at least one accepted record
        detail = ""
        skip_outcome: AssessmentOutcome | None = None
        skip_detail = ""
        raise_detail = ""
        skipped_budget_n, skipped_deadline_n = 0, 0
        for call, res in zip(planned, outcomes, strict=True):
            stream, group = call.stream, call.group
            if res.skipped:
                if res.skipped == "skipped_budget":
                    skipped_budget_n += 1
                else:
                    skipped_deadline_n += 1
                if skip_outcome is None:
                    skip_outcome, skip_detail = res.skipped, res.why  # type: ignore[assignment]
                mark(group, _UNASSESSED_REASON[res.skipped])
                continue
            calls += 1
            per_call.append(len(group))
            if stream not in used_streams:
                used_streams.append(stream)
            if res.error is not None:
                exc = res.error
                _log.warning("nx_answer_coverage_assessment_failed", stream=stream, error=type(exc).__name__)
                raise_detail = f"call {calls} on the {stream} stream failed: {type(exc).__name__}"
                # A reply that is not the check shape is an unusable reply; any other failure is a call that raised.
                # Either way only this call's facets are unknown: the other calls ran and are read on their own.
                if isinstance(exc, _MalformedReply):
                    unusable_n += 1
                    call_log.append(AssessmentCall(stream, len(group), "no_records"))
                    mark(group, UNUSABLE_REASON, override=True)
                else:
                    raised_n += 1
                    call_log.append(AssessmentCall(stream, len(group), "raised"))
                    mark(group, RAISED_REASON)
                detail = raise_detail
                continue
            records = res.records or []
            accepted = 0
            rejected_here = 0
            call_supports = False
            # The call's own facets only: a tag for a facet this prompt never showed is rejected (N1).
            call_tags = {f.id.lower() for f in group}
            for rec in records:
                item_id, quote, role = rec.get("item_id"), rec.get("quote"), rec.get("role")
                m = _TAG.match(quote) if isinstance(quote, str) else None
                tag = m.group(1).lower() if m is not None else None
                facet_id = tag_of.get(tag) if tag in call_tags else None
                if item_id not in call.ids or role not in _ROLES or facet_id is None:
                    rejected += 1
                    rejected_here += 1
                    continue
                accepted += 1
                if role == "supports":
                    call_supports = True
                    supports[facet_id].add(str(item_id))
                elif role == "contradicts":
                    contradicts[facet_id].add(str(item_id))
            accepted_total += accepted
            call_log.append(AssessmentCall(
                stream, len(group),
                "no_records" if not records else "all_rejected" if accepted == 0
                else "support" if call_supports else "neutral_or_contradict",
                accepted, rejected_here,
            ))
            if accepted == 0:
                # Nothing usable came back (no record, or none that validates): that says nothing about these
                # facets, so they are not known. An unusable reply is a failed assessment, not "nothing supports".
                unusable_n += 1
                mark(group, UNUSABLE_REASON, override=True)
                detail = detail or (
                    f"{_UNUSABLE_DETAIL}: call {calls} on the {stream} stream returned {len(records)} record(s), none usable"
                )
                continue
            completed += 1
            read_ids.update(dict.fromkeys(call.ids))
            # Anchors the per-call cap left out (``unread_ids``) were never read.
            # A completed call decides its facets on what it read: a facet no READ item supports or contradicts is
            # unsupported (Sam, 2026-10-30, nexus-jypmj.23). Marking it unassessed meant a plan that hands the check
            # more anchors than its calls carry could never be withheld. The count of unread anchors is
            # AssessmentReport.skipped_items, which the envelope and both notices disclose.
        outcome: AssessmentOutcome = "ok"
        if skip_outcome is not None:
            outcome = skip_outcome if calls == 0 else "partial_failure"
            detail = skip_detail
        if raised_n:
            outcome = "failed" if completed == 0 else "partial_failure"
            detail = raise_detail
        elif unusable_n and outcome == "ok":
            outcome = "failed" if completed == 0 else "partial_failure"
        findings: dict[str, FacetFinding] = {}
        for f in facets:
            sup, con = supports[f.id], contradicts[f.id]
            if sup:
                # supporting evidence covers the facet; a contradiction beside it is marked, not allowed to cancel it
                findings[f.id] = FacetFinding(f.id, "covered", tuple(sorted(sup | con)), CONFLICTED_REASON if con else "")
            elif con:
                findings[f.id] = FacetFinding(f.id, "contradicted", tuple(sorted(con)), "contradicted")
            elif f.id in incomplete:
                findings[f.id] = FacetFinding(f.id, "unassessed", (), incomplete[f.id])
            else:
                findings[f.id] = FacetFinding(f.id, "unsupported", (), REPAIRABLE_REASON)
        report = AssessmentReport(
            outcome=outcome, calls=calls, facets_per_call=tuple(per_call), streams=tuple(used_streams),
            rejected_records=rejected, skipped_items=len(unread_ids), anchors_total=len(anchors),
            unread_ids=tuple(unread_ids), read_ids=tuple(read_ids),
            latency_ms=int((time.monotonic() - started) * 1000), detail=detail,
            accepted_records=accepted_total, unusable_calls=unusable_n, raised_calls=raised_n, call_log=tuple(call_log),
            skipped_budget_calls=skipped_budget_n, skipped_deadline_calls=skipped_deadline_n,
        )
        return findings, report

    def _log(self, result: CoverageResult) -> None:
        # Counts and closed names only: no question, no facet text, no source label, no evidence.
        _log.info("nx_answer_coverage_ledger", **result.counts())


_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class _PlannedCall:
    """One assessment call before it runs: the evidence stream, the facet group, the pack of items it reads (as the
    check's payload and the ids a record may cite). The items the character cap left out of a stream are counted once per
    stream in :meth:`CoverageBoundary._assess`, not on a call."""

    stream: str
    group: tuple[Facet, ...]
    payload: list[dict[str, str]]
    ids: frozenset[str]


@dataclass
class _CallOutcome:
    """What one planned call came to: skipped before it started (``skipped`` is ``skipped_budget`` or
    ``skipped_deadline``), raised (``error``), or replied (``records``)."""

    skipped: str = ""
    why: str = ""
    records: list[Mapping[str, Any]] | None = None
    error: Exception | None = None


def _with_tail_evidence(earlier: FacetFinding, new: FacetFinding | None) -> FacetFinding:
    """A covered facet after its unread anchors were read (nexus-z6yvk): it stays covered whatever they say. A supporting
    item adds to its evidence, a contradicting one marks it ``conflicted``, and a reassessment that found nothing, failed
    or was skipped leaves the finding as it was. Reading more never takes support away."""
    if new is None or new.state not in ("covered", "contradicted"):
        return earlier
    conflicted = earlier.reason == CONFLICTED_REASON or new.state == "contradicted" or new.reason == CONFLICTED_REASON
    return FacetFinding(
        earlier.facet_id, "covered", tuple(sorted(set(earlier.evidence_ids) | set(new.evidence_ids))),
        CONFLICTED_REASON if conflicted else "",
    )


def _balanced_chunks[T](items: Sequence[T], cap: int) -> list[list[T]]:
    """*items* in order as the fewest packs of at most *cap*, with sizes differing by at most one (65 at 20 is
    17, 16, 16, 16: no short tail pack and no pack past the cap)."""
    if not items:
        return []
    n = math.ceil(len(items) / max(1, cap))
    base, extra = divmod(len(items), n)
    out: list[list[T]] = []
    at = 0
    for i in range(n):
        size = base + (1 if i < extra else 0)
        out.append(list(items[at:at + size]))
        at += size
    return out


def _instruction(group: Sequence[Facet]) -> str:
    return _INSTRUCTION + "\n".join(f"[{f.id}] {_WS.sub(' ', f.text).strip()}" for f in group)


def _pack(anchors: Sequence[EvidenceAnchor]) -> tuple[list[dict[str, str]], list[EvidenceAnchor], int]:
    """The items one call carries, in order, up to :data:`ASSESSMENT_MAX_ITEM_CHARS`; the rest are counted."""
    payload: list[dict[str, str]] = []
    included: list[EvidenceAnchor] = []
    used = 0
    for a in anchors:
        if included and used + len(a.content) > ASSESSMENT_MAX_ITEM_CHARS:
            break
        payload.append({"id": a.item_id, "text": a.content})
        included.append(a)
        used += len(a.content)
    return payload, included, len(anchors) - len(included)


def _hits(out: Any) -> list[RepairHit]:
    """The chunks a repair retrieval returned: ``ids`` with their per-chunk collection, catalog document and
    content hash where the output carries them. A step output that is not the retrieval shape has none."""
    if not isinstance(out, Mapping) or not isinstance(out.get("ids"), list):
        return []
    ids = out["ids"]
    cols = _per_chunk_collections(out, len(ids))
    tumblers = out.get("tumblers")
    tums = [str(t) for t in tumblers] if isinstance(tumblers, list) and len(tumblers) == len(ids) else [""] * len(ids)
    hashes = out.get("chunk_text_hash")
    hs = [str(h) for h in hashes] if isinstance(hashes, list) and len(hashes) == len(ids) else [""] * len(ids)
    return [RepairHit(str(i), c, t, h) for i, c, t, h in zip(ids, cols, tums, hs, strict=True)]


class _MalformedReply(ValueError):
    """A check reply that is not the check shape: an unusable reply (that call's facets read ``assessment_unusable``), not a call that raised."""


def _records(reply: Any) -> list[Mapping[str, Any]]:
    """The evidence records of a check reply; a reply that is not the check shape is a failed call."""
    if not isinstance(reply, Mapping) or not isinstance(reply.get("evidence"), list):
        raise _MalformedReply("operator_check reply has no evidence list")
    return [r for r in reply["evidence"] if isinstance(r, Mapping)]
