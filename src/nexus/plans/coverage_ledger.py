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
   nexus-jypmj.3.1): it is never sent to the model and survives a failed assessment.
3. **Facet assessment.** One ``operator_check`` call per evidence stream (primary, t2, vector, graph,
   other), carrying at most :data:`MAX_FACETS_PER_ASSESSMENT_CALL` facets; more facets split into groups.
   ``check.ok`` is never read as coverage. Coverage comes from the evidence records only: each record's
   ``item_id`` must be one of the call's input ids and its ``quote`` must open with the facet tag
   (``[f2] ...``) of a facet in that call; anything else is rejected and counted
   (:attr:`AssessmentReport.rejected_records`). ``supports`` covers a facet, ``contradicts`` marks it
   ``contradicted`` (retained for the reduction, never repaired into agreement), ``neutral`` does nothing.
   The first failed call stops the assessment; no facet is then defaulted to covered.
4. **State.** :func:`state_for` computes ``coverage.state`` from the facet states (the section 4 table):
   ``complete``, ``partial``, ``unavailable`` or ``unassessed``. ``unavailable`` with reason ``uncovered``
   (every required facet known and none covered; contradicted is not covered) sets
   :attr:`CoverageResult.skip_generation`, which the runner honours by not dispatching the terminal
   reduction (Sam's ruling of 2026-10-25, nexus-jypmj.3.12). ``no_evidence`` stays reserved for zero
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

The assessment is a stage inside the answer's own deadline: a call is bounded by
:data:`ASSESSMENT_DEADLINE_SHARE` of what is left (so the terminal reduction is not starved by its own
pre-check) and by :data:`ASSESSMENT_CALL_TIMEOUT_S`, and it is not started when the allowance falls under
:data:`MIN_ASSESSMENT_CALL_S` or the USD allowance is spent (outcome ``skipped_deadline`` /
``skipped_budget``, state ``unassessed``). These three values are unmeasured starting points. Assessment
calls are ``StepRecord``s past the plan's own step indices, so the shared USD accumulator and the
``nx_answer_steps`` primary key see them without a new step source.

Re-entry (nexus-jypmj.3.2, the one repair): ``evaluate(view, prior=result)`` keeps the prior covered and
contradicted findings, reassesses only the facets still ``unsupported`` or ``unassessed`` against the
view's (merged) anchors, adds one round, and sums the assessment counts. Facets in
:attr:`CoverageResult.repair_facet_ids` are the ones a targeted retrieval can still help.

Redaction: the wire forms carry ids, states and counts; evidence text is never in them, and the source
labels (the caller's wording) are dropped when ``redact`` is set.

Pure apart from the injected ``check`` callable: stdlib, ``structlog`` and the RDR-232 value types.
"""
from __future__ import annotations

import hashlib
import json
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
from nexus.plans.request_coverage import Facet, RequestCoverage
from nexus.plans.reserved_stream import ReservedStream, active_reserved_stream, strip_source_note

_log = structlog.get_logger(__name__)

__all__ = [
    "ASSESSMENT_CALL_TIMEOUT_S",
    "ASSESSMENT_DEADLINE_SHARE",
    "ASSESSMENT_MAX_ITEM_CHARS",
    "ASSESSMENT_OPERATOR",
    "MAX_FACETS_PER_ASSESSMENT_CALL",
    "MIN_ASSESSMENT_CALL_S",
    "STREAM_ORDER",
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
    "state_for",
]

#: The operator the assessment goes through, by the runner's own dispatcher (never ``nx_answer``).
ASSESSMENT_OPERATOR = "operator_check"

#: Facets per assessment call. See the module docstring for the basis; unmeasured as accuracy.
MAX_FACETS_PER_ASSESSMENT_CALL: int = 6
#: Upper bound on one call; the share below applies first when a deadline is set.
ASSESSMENT_CALL_TIMEOUT_S: float = 60.0
#: A call may use at most this share of the deadline left when it starts, so generation keeps the rest.
ASSESSMENT_DEADLINE_SHARE: float = 0.4
#: No call is started with less than this allowance.
MIN_ASSESSMENT_CALL_S: float = 5.0
#: Characters of item text one call carries (the bundle prompt guard is 200,000 for the whole prompt).
#: Items past it are not assessed; a facet that no assessed item supports is then ``unassessed``.
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

_INSTRUCTION = (
    "Coverage assessment. For each facet listed below, find the items that bear on it. Add one evidence "
    "record per (item, facet) pair that matters: item_id is the item's id exactly as given; quote opens "
    "with the facet tag in square brackets (for example [f1]) followed by a short quote from the item; "
    "role is 'supports' when the item states what the facet asks for, 'contradicts' when it states the "
    "opposite, 'neutral' when it only mentions the topic. Use only the item ids and facet tags given, and "
    "add no others. Coverage is read from the records, not from ok.\nFacets:\n"
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
    """One required facet's state: ``covered`` (supporting evidence), ``contradicted`` (contradicting
    evidence, retained), ``unsupported`` (assessed, none bears on it) or ``unassessed`` (not known)."""

    facet_id: str
    state: FacetState
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {"facet_id": self.facet_id, "state": self.state, "evidence_ids": list(self.evidence_ids), "reason": self.reason}


@dataclass(frozen=True)
class SourceRow:
    """A named source's deterministic row: ``covered``, ``partial`` or ``missing``."""

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
class AssessmentReport:
    """What the facet assessment cost, for the run summary (nexus-jypmj.3.5): calls, facets per call.

    ``calls`` counts attempted calls (a failed call is one). ``rejected_records`` counts evidence records
    the validation dropped (an id outside the input, an unknown facet tag, an untagged quote or a bad role).
    ``skipped_items`` counts anchors a call could not carry. ``detail`` names why the assessment stopped.
    """

    outcome: AssessmentOutcome = "not_attempted"
    calls: int = 0
    facets_per_call: tuple[int, ...] = ()
    streams: tuple[str, ...] = ()
    rejected_records: int = 0
    skipped_items: int = 0
    latency_ms: int = 0
    detail: str = ""

    def merged_with(self, later: AssessmentReport) -> AssessmentReport:
        """The sum of two rounds; the later round's outcome and detail stand."""
        return AssessmentReport(
            outcome=later.outcome if later.outcome != "not_attempted" else self.outcome,
            calls=self.calls + later.calls, facets_per_call=self.facets_per_call + later.facets_per_call,
            streams=tuple(dict.fromkeys(self.streams + later.streams)),
            rejected_records=self.rejected_records + later.rejected_records,
            skipped_items=self.skipped_items + later.skipped_items,
            latency_ms=self.latency_ms + later.latency_ms, detail=later.detail or self.detail,
        )

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "calls": self.calls, "facets_per_call": list(self.facets_per_call),
            "streams": list(self.streams), "rejected_records": self.rejected_records,
            "skipped_items": self.skipped_items, "latency_ms": self.latency_ms, "detail": self.detail,
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

    @property
    def repair_facet_ids(self) -> tuple[str, ...]:
        return tuple(f.facet_id for f in self.ledger.findings if f.state == "unsupported" and f.reason == REPAIRABLE_REASON)

    @property
    def unresolved_facet_ids(self) -> tuple[str, ...]:
        """Facets the final notice names: unsupported or contradicted (an unassessed one is not known)."""
        return tuple(f.facet_id for f in self.ledger.findings if f.state in ("unsupported", "contradicted"))

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
        }

    def to_wire(self, *, redact: bool = False) -> dict[str, Any]:
        led = self.ledger
        return {
            **self.counts(),
            "findings": [f.to_wire() for f in led.findings],
            "anchors": [a.to_wire() for a in led.anchors],
            "sources": [{"state": s.state, "detail": "" if redact else s.detail, **({} if redact else {"source": s.source})} for s in led.sources],
            "scope": None if led.scope is None else {
                "state": led.scope.state, "detail": "" if redact else led.scope.detail, "conflict_steps": list(led.scope.conflict_steps)},
            "assessment": led.assessment.to_wire(),
        }


#: The async call the boundary makes for one assessment: ``(args, timeout=...) -> the operator's reply``.
CheckCall = Callable[..., Awaitable[Mapping[str, Any]]]


@dataclass
class BoundaryView:
    """What the runner hands the boundary at the terminal reduction.

    ``evidence_texts`` are the texts the first evidence-consuming step of the terminal segment will be
    given (the reserved stream merged, the cap applied) and ``observed`` says whether that input could be
    read at all. ``step_outputs`` and ``tools`` are the executed prefix, for the deterministic checks.
    ``check`` performs one assessment call through the runner's dispatcher and accounts for it;
    ``remaining_seconds`` / ``remaining_usd`` return what is left of the answer's deadline and USD allowance
    (``None`` for no limit).
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
    terminal_step: int | None = None


# ── anchors ──────────────────────────────────────────────────────────────────────────────────────────────


def build_anchors(
    texts: Sequence[str], *, step_outputs: Sequence[Any], tools: Sequence[str], reserved: ReservedStream | None,
) -> tuple[EvidenceAnchor, ...]:
    """The typed anchors for *texts*, in order, deduplicated by text.

    Provenance follows ``evidence_diagnostics``: a reserved item carries its own stream, collection and
    document; any other text is attributed by the hash of its content (a retrieval step's ``ids`` and
    ``chunk_text_hash`` are chunk hashes). A text no retrieval step produced is ``other``, kind
    ``unattributed``, and its chash is the hash of its text.
    """
    by_text = {i.text: i for i in reserved.items} if reserved is not None else {}
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
        item_id = f"e{len(anchors) + 1}"
        held = by_text.get(text)
        if held is not None:
            is_t2 = held.stream == "t2"
            anchors.append(EvidenceAnchor(
                item_id=item_id, stream=held.stream, kind="t2_record" if is_t2 else "chunk", chash=held.chash,
                collection=held.collection, tumbler="" if is_t2 else held.doc_id,
                t2_record_id=held.token if is_t2 else "", source=held.source, content=text,
            ))
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
    * every facet known and none covered (each unsupported or contradicted): ``unavailable`` /
      ``uncovered`` and generation is skipped. A usable answer needs at least one covered facet, and the
      nexus-jypmj.3.12 ruling skips generation when the ledger covers none; contradicted is not covered. A
      contradicted facet beside a covered one is retained for the reduction (``partial``).
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
    if n["covered"] == 0 and n["unassessed"] == 0:
        return "unavailable", "uncovered", True
    if n["covered"] == total:
        return "complete", "all_covered", False
    return "partial", "unresolved_facets", False


# ── the boundary ─────────────────────────────────────────────────────────────────────────────────────────


def _norm(ref: str) -> str:
    return re.sub(r"\s+", "", ref).upper()


@dataclass(frozen=True)
class CoverageBoundary:
    """The configuration of one run's boundary, handed to ``plan_run``.

    ``request`` is the Phase 2 :class:`RequestCoverage`; ``None`` is a run that reached the boundary with
    none (state ``unassessed``). ``scope``, ``named_sources``, ``primary``, ``lifecycle``, ``owner_resolution``
    and ``dropped_scope`` are the inputs ``nx_answer`` already holds for ``build_evidence_diagnostics``.
    ``skip_generation_when_uncovered`` is the one switch on the destructive rule (decision b); the other
    fields are the tunables the module docstring names.
    """

    request: RequestCoverage | None
    scope: ScopeConstraint = field(default_factory=lambda: ScopeConstraint("none", ""))
    named_sources: Any = None
    primary: Any = None
    lifecycle: Any = None
    owner_resolution: Mapping[str, Any] | None = None
    dropped_scope: str = ""
    skip_generation_when_uncovered: bool = True
    max_facets_per_call: int = MAX_FACETS_PER_ASSESSMENT_CALL
    call_timeout_s: float = ASSESSMENT_CALL_TIMEOUT_S
    deadline_share: float = ASSESSMENT_DEADLINE_SHARE
    min_call_s: float = MIN_ASSESSMENT_CALL_S

    def assessment_timeout(self, remaining: float | None) -> float:
        """The allowance for one call: ``call_timeout_s`` with no deadline, else that or the share of what
        is left, whichever is smaller; ``0.0`` when the allowance falls under ``min_call_s``."""
        if remaining is None:
            return self.call_timeout_s
        allowance = min(self.call_timeout_s, max(remaining, 0.0) * self.deadline_share)
        return allowance if allowance >= self.min_call_s else 0.0

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
        anchors = build_anchors(view.evidence_texts, step_outputs=view.step_outputs, tools=view.tools, reserved=active_reserved_stream())
        sources, scope_row = self._deterministic(view)
        rounds = prior.ledger.rounds + 1 if prior is not None else 1
        base = CoverageLedger(anchors=anchors, sources=sources, scope=scope_row, rounds=rounds)
        request = self.request
        if request is None or not request.facets:
            reason = "no_request_coverage" if request is None else "no_facets"
            result = CoverageResult("unassessed", reason, False, base)
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
            result = CoverageResult(state, why, False, replace(base, findings=findings, assessment=report))
            self._log(result)
            return result

        keep = {fid for fid, f in prior_by_id.items() if f.state in ("covered", "contradicted")}
        to_assess = [f for f in request.facets if f.id not in blocked and f.id not in keep]
        assessed: dict[str, FacetFinding] = {}
        new_report = AssessmentReport()
        if to_assess and anchors:
            assessed, new_report = await self._assess(to_assess, anchors, view)
        report = report.merged_with(new_report) if prior is not None else new_report

        findings_list: list[FacetFinding] = []
        for f in request.facets:
            if f.id in blocked:
                findings_list.append(FacetFinding(f.id, "unsupported", (), blocked[f.id]))
            elif f.id in keep:
                findings_list.append(prior_by_id[f.id])
            elif f.id in assessed:
                findings_list.append(assessed[f.id])
            else:
                findings_list.append(FacetFinding(f.id, "unassessed", (), _UNASSESSED_REASON.get(report.outcome, "assessment_failed")))
        findings = tuple(findings_list)
        state, why, skip = state_for(
            [f.state for f in findings], assessment_outcome=report.outcome, anchors=len(anchors), observed=view.observed, scope_conflict=conflict,
        )
        result = CoverageResult(state, why, skip and self.skip_generation_when_uncovered, replace(base, findings=findings, assessment=report))
        self._log(result)
        return result

    async def _assess(
        self, facets: Sequence[Facet], anchors: Sequence[EvidenceAnchor], view: BoundaryView,
    ) -> tuple[dict[str, FacetFinding], AssessmentReport]:
        cap = max(1, self.max_facets_per_call)
        groups = [list(facets[i:i + cap]) for i in range(0, len(facets), cap)]
        by_stream = {s: [a for a in anchors if a.stream == s] for s in STREAM_ORDER}
        planned = [(s, g) for s in STREAM_ORDER if by_stream[s] for g in groups]
        supports: dict[str, set[str]] = {f.id: set() for f in facets}
        contradicts: dict[str, set[str]] = {f.id: set() for f in facets}
        incomplete: set[str] = set()
        calls, rejected, skipped_items = 0, 0, 0
        per_call: list[int] = []
        used_streams: list[str] = []
        outcome: AssessmentOutcome = "ok"
        detail = ""
        started = time.monotonic()
        completed = 0
        for pos, (stream, group) in enumerate(planned):
            usd = view.remaining_usd()
            timeout = self.assessment_timeout(view.remaining_seconds())
            stop, why = "", ""
            if usd is not None and usd <= 0:
                stop, why = "skipped_budget", "the USD allowance was spent"
            elif timeout <= 0:
                stop, why = "skipped_deadline", "too little of the deadline was left"
            if stop:
                outcome = stop if completed == 0 else "partial_failure"  # type: ignore[assignment]
                detail = why
                for _s, g in planned[pos:]:
                    incomplete.update(f.id for f in g)
                break
            payload, included, omitted = _pack(by_stream[stream])
            skipped_items += omitted
            ids = {a.item_id for a in included}
            tags = {f.id for f in group}
            args = {"items": json.dumps(payload), "check_instruction": _instruction(group)}
            calls += 1
            per_call.append(len(group))
            if stream not in used_streams:
                used_streams.append(stream)
            try:
                reply = await view.check(args, timeout=timeout)
                records = _records(reply)
            except Exception as exc:  # noqa: BLE001 — a failed assessment leaves the answer; the facets stay unassessed
                _log.warning("nx_answer_coverage_assessment_failed", stream=stream, error=type(exc).__name__)
                outcome = "failed" if completed == 0 else "partial_failure"
                detail = f"call {calls} on the {stream} stream failed: {type(exc).__name__}"
                for _s, g in planned[pos:]:
                    incomplete.update(f.id for f in g)
                break
            completed += 1
            for rec in records:
                item_id, quote, role = rec.get("item_id"), rec.get("quote"), rec.get("role")
                m = _TAG.match(quote) if isinstance(quote, str) else None
                if item_id not in ids or role not in _ROLES or m is None or m.group(1) not in tags:
                    rejected += 1
                    continue
                if role == "supports":
                    supports[m.group(1)].add(str(item_id))
                elif role == "contradicts":
                    contradicts[m.group(1)].add(str(item_id))
            if omitted:
                incomplete.update(f.id for f in group)
        findings: dict[str, FacetFinding] = {}
        for f in facets:
            if contradicts[f.id]:
                findings[f.id] = FacetFinding(f.id, "contradicted", tuple(sorted(supports[f.id] | contradicts[f.id])), "contradicted")
            elif supports[f.id]:
                findings[f.id] = FacetFinding(f.id, "covered", tuple(sorted(supports[f.id])))
            elif f.id in incomplete:
                findings[f.id] = FacetFinding(f.id, "unassessed", (), _UNASSESSED_REASON.get(outcome, "assessment_incomplete"))
            else:
                findings[f.id] = FacetFinding(f.id, "unsupported", (), REPAIRABLE_REASON)
        report = AssessmentReport(
            outcome=outcome, calls=calls, facets_per_call=tuple(per_call), streams=tuple(used_streams),
            rejected_records=rejected, skipped_items=skipped_items,
            latency_ms=int((time.monotonic() - started) * 1000), detail=detail,
        )
        return findings, report

    def _log(self, result: CoverageResult) -> None:
        # Counts and closed names only: no question, no facet text, no source label, no evidence.
        _log.info("nx_answer_coverage_ledger", **result.counts())


_WS = re.compile(r"\s+")


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


def _records(reply: Any) -> list[Mapping[str, Any]]:
    """The evidence records of a check reply; a reply that is not the check shape is a failed call."""
    if not isinstance(reply, Mapping) or not isinstance(reply.get("evidence"), list):
        raise ValueError("operator_check reply has no evidence list")
    return [r for r in reply["evidence"] if isinstance(r, Mapping)]
