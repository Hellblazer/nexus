# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 2 (nexus-jypmj.2.2): what the request asks for, derived before ``plan_match``.

:class:`RequestCoverage` is the request-side half of answer-shape eligibility (RDR-232 section 1):
the answer kind the question asks for, the facets that need evidence, the constraints that govern how
the answer treats it, the sources it names, the caller's explicit scope, and whether it asks about
current code. ``nx_answer`` builds it before plan matching, in a stage measured on its own, and
reports it in the envelope and in ``evidence_diagnostics``. Its ``answer_kind`` is the request kind the
answer-kind eligibility gate (``plans/eligibility.py``, nexus-jypmj.2.3) judges plan candidates by, and the gate
enforces by default (nexus-jypmj.2.8). The other plan-visible use is :attr:`RequestCoverage.wants_lifecycle_stream`,
which decides whether the T2 lifecycle stream (nexus-jypmj.1.7) is read.

**The measured numbers are in-sample.** The first-pass patterns were written with the 24 golden requests
(``bench/queries/golden_v1.yaml``) in view. The kind agreement on that set, its unknown rate and the lists of
golden requests that keep or lose the lifecycle stream are therefore in-sample: they show that the patterns
still fit the requests they were fitted to, and are not a rate on requests the author has not seen. On 30
fresh single-author questions the first version read 21 exactly, 6 as unknown and 3 as another kind (all
three as ``lookup``); the weak-lookup rule below was tightened for that. A held-out set is nexus-jypmj.2.8's
job, and nothing may read the golden numbers as the classifier's accuracy.

The first pass is deterministic and costs no model call. It follows the extraction table in the RDR:

* **Kind.** An explicit caller hint first; then conservative patterns, ordered by strength. Two different
  strong signals, or none, give ``unknown`` (never a guess); the verb the plan matcher infers from the
  question is deliberately not consulted, because that verb is a retrieval category and not an answer
  shape. A bare question word ("what ...", "which ...") is ``lookup`` only for a question with at most one
  facet; with more it is ``unknown``. The kinds are :class:`~nexus.plans.answer_kind.PlanAnswerKind`'s
  values and ``kind_decision`` uses ``answer_kind.ROUTE_SOURCES``' spellings (``model-assisted``), so the
  eligibility step (nexus-jypmj.2.3) and the grown-plan stamp read the same words.
* **Facets and constraints.** Sentences and comma clauses are separated; a clause that opens with a
  constraint verb (cite, ``distinguish ... from ...`` with a date, acceptance or record cue, do not infer, be
  brief) is a constraint, and every other clause of an enumerated request is a facet with a positional id
  (``f1``, ``f2``). Constraints get ``c1``, ``c2``. What the code guarantees about words: every word of the
  question is in a facet, a constraint or the subject except the joiners the structure splits on (``and``,
  ``also``, ``then``, ``plus``, ``but``, ``please`` at the head of a clause or between two noun items) and
  the listing verb at the head of a clause (``State the ...``). Where it cannot promise that (the question
  is longer than the first-pass cap, there are more constraints or facets than the caps, or the clause
  structure is not clear) ``facets_certain`` is ``False`` and the ``whole_question`` facet carries the full,
  uncut text; the part past the cap is still scanned for lifecycle topics and RDR ids.
* **Named sources.** The caller's ``source`` binding outranks anything read from the question text; an RDR
  id in the text is used only when no binding names a source.
* **Current code.** Only explicit wording sets it (source code, the code, the codebase, the current or
  actual implementation, "against its implementation"); a prohibition ("do not infer current code"), the
  word "source" in another sense ("the source RDR") and an RDR implementation-status facet never do.
* **Lifecycle stream.** :attr:`RequestCoverage.wants_lifecycle_stream` is true for a facet that touches
  gate, lifecycle or status vocabulary. The RDR states and events come from ``rdr-lifecycle.toml``. It also
  fails open: with uncertain facets and an RDR source the caller bound, the stream is read.

An optional second step (:func:`structured_extract`) makes at most one direct, schema-closed model call
when the first pass could not establish the kind or the clause structure. It is on by default (a kill switch
turns it off), has its own timeout, and its latency and cost are reported separately from plan-match latency
(:class:`ExtractionReport`). A failure leaves the first-pass result as it was. Model facets replace the
first pass's only when they cover every content word of the question; otherwise the ``whole_question``
facet stays beside them.

Redaction. Facet text, constraint text and source hints are the caller's wording. :func:`log_fields`
returns counts and closed-vocabulary names only; the texts are added only when the caller kept ``trace``
on. Ids are positional so they carry none of the question's words.

Pure: stdlib, ``structlog`` and the RDR-232 evidence value types. No catalog or T2 call is made here.
"""
from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import structlog

from nexus.plans.answer_kind import GOLDEN_LABEL_TO_KIND, PlanAnswerKind
from nexus.plans.evidence import EvidenceRequest, OwnerResolution, ScopeConstraint, SourceRange, SourceRef
from nexus.plans.switches import OFF_VALUES, warn_unrecognised

_log = structlog.get_logger(__name__)

__all__ = [
    "ACTIVE_PROJECT_TIER",
    "Ambiguity",
    "Constraint",
    "ExtractionReport",
    "FAILED_OUTCOMES",
    "Facet",
    "KIND_DECISIONS",
    "PERSISTENT_FAILURE_THRESHOLD",
    "REQUEST_KINDS",
    "ONE_SOURCE_NARROWED_KINDS",
    "RequestCoverage",
    "SourceHint",
    "SourceRange",
    "derive_request_coverage",
    "extraction_enabled",
    "log_fields",
    "normalize_kind",
    "note_extraction_outcome",
    "project_name_candidates",
    "reset_extraction_health",
    "structured_extract",
]

#: The request kinds whose secondary retrieval is narrowed to the one source they name (nexus-4xmpv, RDR-232
#: section 2a; Sam, 2026-10-30): a synthesis wants the parts of that document put together, and an
#: ``unknown`` request is what an extraction failure leaves a single-source synthesis as. Other kinds that name
#: one source (a lookup, a comparison) keep their cross-document retrieval.
ONE_SOURCE_NARROWED_KINDS: frozenset[str] = frozenset({"synthesis", "unknown"})

RequestKind = Literal["lookup", "synthesis", "discovery/listing", "comparison", "audit", "debug", "unknown"]

#: The request kinds are the plan side's :class:`~nexus.plans.answer_kind.PlanAnswerKind` values, in its order
#: (RDR-232 section 1), so a request kind and a plan's declared kind are compared as the same words.
REQUEST_KINDS: tuple[str, ...] = tuple(kind.value for kind in PlanAnswerKind)

#: How the kind was decided. The three route words are ``answer_kind.ROUTE_SOURCES`` exactly (hyphen and all):
#: a grown plan records the route that produced it, and a different spelling would be dropped to ``unknown``.
KIND_DECISIONS: tuple[str, ...] = ("explicit", "heuristic", "model-assisted", "unknown")
KindDecision = Literal["explicit", "heuristic", "model-assisted", "unknown"]
ConstraintKind = Literal["temporal_qualification", "citation", "no_current_code", "prohibition", "format"]
FacetOrigin = Literal["clause", "whole_question"]

#: The T2 lifecycle stream is read only for a request with a facet on one of these topics.
LIFECYCLE_TOPICS: tuple[str, ...] = ("gate", "lifecycle", "status")

#: The active-project tier of source resolution (nexus-jypmj.2.4) is wired: ``nx_answer`` feeds the RDR ids
#: this module reads from the question into named-source resolution and applies the tier there, after this
#: derivation (``nexus.plans.active_project``; the outcome is ``evidence_request.active_project`` in the
#: envelope). This module records that and never calls the tier.
ACTIVE_PROJECT_TIER = "wired"

#: Longest question the first pass reads for structure, and the most facets and constraints it returns. A
#: longer question is cut for the structural read only: the result is then uncertain (``facets_certain`` is
#: False) and its ``whole_question`` facet holds the full, uncut text, whose tail is still scanned for
#: lifecycle topics and RDR ids. The same holds when the constraints exceed the cap.
_MAX_QUESTION_CHARS = 8_000
_MAX_FACETS = 12
_MAX_CONSTRAINTS = 8
#: The most of a question the text scans read (RDR ids, ranges, project and title words, lifecycle topics in
#: the tail past the structural cap). The scans run on the event loop before ``plan_match``; past this the
#: rest is not read and the coverage says so (``scan_limit`` ambiguity), so a megabyte of text costs the
#: same as 64 KB. Cut at a space, so an id is never read half-written.
MAX_SCAN_CHARS = 64_000
#: At most this many named sources, source hints and per-source ambiguities are LISTED in the envelope; the
#: ``named_source_count`` and ``source_hint_count`` keys carry the totals. Resolution has its own, lower cap.
MAX_LISTED_SOURCES = 20


def _scan_prefix(text: str) -> str:
    """*text* cut to :data:`MAX_SCAN_CHARS` at a space (all of it when it is shorter)."""
    if len(text) <= MAX_SCAN_CHARS:
        return text
    cut = text.rfind(" ", 0, MAX_SCAN_CHARS)
    return text[: cut if cut > 0 else MAX_SCAN_CHARS]


def normalize_kind(raw: object) -> str | None:
    """*raw* as a member of :data:`REQUEST_KINDS`, or ``None``. A golden-set label (``discovery``) is read
    through ``answer_kind.GOLDEN_LABEL_TO_KIND``, the one table that maps those labels."""
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if text in REQUEST_KINDS:
        return text
    mapped = GOLDEN_LABEL_TO_KIND.get(text)
    return mapped.value if mapped is not None else None


# ── value types ──────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Facet:
    """A topic that requires evidence. ``id`` is positional (``f1``); ``topics`` is the closed set in
    :data:`LIFECYCLE_TOPICS` the text touches; ``source_hints`` are the source references the facet's own
    text names (``RDR-090``)."""

    id: str
    text: str
    origin: FacetOrigin = "clause"
    topics: tuple[str, ...] = ()
    source_hints: tuple[str, ...] = ()

    def to_wire(self, *, redact: bool = False) -> dict[str, Any]:
        wire: dict[str, Any] = {"id": self.id, "origin": self.origin, "topics": list(self.topics)}
        if not redact:
            wire["text"] = self.text
            wire["source_hints"] = list(self.source_hints[:MAX_LISTED_SOURCES])
        return wire


@dataclass(frozen=True)
class Constraint:
    """How the answer must treat the evidence. It does not trigger retrieval by itself."""

    id: str
    text: str
    kind: ConstraintKind

    def to_wire(self, *, redact: bool = False) -> dict[str, Any]:
        wire: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if not redact:
            wire["text"] = self.text
        return wire


@dataclass(frozen=True)
class SourceHint:
    """A title or project name the question text gives for a named source. A hint is a disclosure, never a
    resolution: it is not applied as an owner qualifier (only the ``owner``/``project`` bindings and a
    catalog-subtree scope are)."""

    source: str
    kind: Literal["title", "project"]
    text: str

    def to_wire(self, *, redact: bool = False) -> dict[str, Any]:
        wire: dict[str, Any] = {"source": self.source, "kind": self.kind}
        if not redact:
            wire["text"] = self.text
        return wire


@dataclass(frozen=True)
class Ambiguity:
    """A thing the request leaves open that the run must say, not settle."""

    code: Literal[
        "source_unqualified", "source_inferred", "owner_conflict", "owner_unresolved", "project_hint_conflict",
        "project_hint_unapplied", "kind_ambiguous", "scan_limit", "kind_hint_invalid",
    ]
    subject: str
    detail: str

    def to_wire(self, *, redact: bool = False) -> dict[str, Any]:
        wire: dict[str, Any] = {"code": self.code}
        if not redact:
            wire["subject"] = self.subject
            wire["detail"] = self.detail
        return wire


@dataclass(frozen=True)
class ExtractionReport:
    """The optional structured extraction step, reported apart from plan-match latency.

    ``outcome``: ``disabled`` (the setting is off), ``not_needed`` (the first pass established the shape),
    ``skipped_deadline`` (less than a second of the answer's budget was left, so no call was made), ``ok``
    (the result was used), ``timeout`` (the wait ran out or dispatch raised its own timeout), ``error`` (the
    call raised), ``invalid`` (the answer did not fit the closed schema) or ``unchanged`` (valid, but it
    added nothing the first pass lacked). ``calls`` is 0 or 1 and never more.
    """

    outcome: Literal[
        "disabled", "not_needed", "skipped_deadline", "ok", "timeout", "error", "invalid", "unchanged",
    ] = "disabled"
    calls: int = 0
    timeout_s: float | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "calls": self.calls, "timeout_s": self.timeout_s,
            "latency_ms": self.latency_ms, "cost_usd": self.cost_usd,
        }


@dataclass(frozen=True)
class RequestCoverage:
    """What the request asks for, derived before plan matching (see the module docstring)."""

    answer_kind: str = "unknown"
    facets: tuple[Facet, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    named_sources: tuple[SourceRef, ...] = ()
    explicit_scope: ScopeConstraint = field(default_factory=lambda: ScopeConstraint("none", ""))
    current_code_requested: bool = False
    kind_decision: KindDecision = "unknown"
    #: ``explicit`` (a ``source`` binding), ``question`` (read from the text) or ``none``.
    source_origin: Literal["explicit", "question", "none"] = "none"
    source_hints: tuple[SourceHint, ...] = ()
    #: Ranges of RDR numbers the question names by their ends (nexus-jypmj.2.4). Only the ends are sources;
    #: set for a question-origin source only (a ``source`` binding wins over the text).
    source_ranges: tuple[SourceRange, ...] = ()
    #: The topic sentence(s) the question opens with (``Synthesize RDR-084, <title>``), kept so that no clause
    #: of the question is dropped even where it is neither a facet nor a constraint.
    subject: tuple[str, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()
    extraction: ExtractionReport = field(default_factory=ExtractionReport)
    #: Whether the clause structure is clear enough for the structured step to leave alone.
    facets_certain: bool = True
    #: The first-pass stage's wall time, set by the caller that measured it.
    derive_ms: int | None = None
    #: What the active-project hook returned, recorded for disclosure only (nexus-jypmj.2.4 owns the tier).
    active_project: str | None = None

    @property
    def names_one_source(self) -> bool:
        """Whether the request names exactly ONE source: the one count of distinct named ``(kind, value)`` pairs
        (nexus-4xmpv). Every site that asks (the primary read's item cap, the secondary narrowing, the replay)
        reads this property, so none can disagree: a range whose two ends are the same id ("RDR-078 through
        RDR-078") names one source here, and a document named only by its title names none (it is a source hint,
        not a named source)."""
        return len({(s.kind, s.value) for s in self.named_sources}) == 1

    @property
    def narrows_to_one_source(self) -> bool:
        """Whether the run narrows every secondary retrieval step to the one source it names: a synthesis or
        ``unknown`` request (:data:`ONE_SOURCE_NARROWED_KINDS`) with :attr:`names_one_source`. The runner does the
        narrowing only if that source then resolves to one document (``nexus.plans.single_source``)."""
        return self.names_one_source and self.answer_kind in ONE_SOURCE_NARROWED_KINDS

    @property
    def wants_lifecycle_stream(self) -> bool:
        """Whether the T2 lifecycle stream is read (RDR-232 Phase 2 step 8): the request has a gate, lifecycle
        or status facet. It also FAILS OPEN: with uncertain facets (the clause structure could not be read,
        the question ran past a cap, or the derivation failed) and an RDR source (bound, or named in the question), a topic
        may sit where nothing looked, so the stream is read."""
        if any(t in LIFECYCLE_TOPICS for f in self.facets for t in f.topics):
            return True
        # A topic sentence ("Summarize RDR-210 status.") is the subject, not a facet, but its topic still counts.
        if any(t in LIFECYCLE_TOPICS for s in self.subject for t in _topics(_CUE_VERB.sub("", s, count=1))):
            return True
        # A source read from the question fails open like a bound one: whatever RDR it names is read for its
        # lifecycle only if it resolves (the stream is built from the RESOLVED sources), but whether to look
        # cannot depend on where the id was written.
        return (not self.facets_certain and self.source_origin in ("explicit", "question")
                and any(s.kind == "rdr" for s in self.named_sources))

    def without_unqualified_for(
        self, resolved: Iterable[str], inferred_over: Mapping[str, str] | None = None,
    ) -> RequestCoverage:
        """This coverage without the ``source_unqualified`` ambiguity of any source in *resolved* (the values
        the named-source resolver resolved to exactly one document: nothing is left to tell apart).

        *inferred_over* maps a resolved source to the detail of an inference that did not remove the doubt:
        the active project chose among documents that other projects also hold (nexus-jypmj.2.4). That
        source keeps a distinct ``source_inferred`` ambiguity instead of losing its ledger line.
        """
        done = set(resolved)
        inferred_over = inferred_over or {}
        kept: list[Ambiguity] = []
        for a in self.ambiguities:
            if a.code == "source_unqualified" and a.subject in done:
                if a.subject in inferred_over:
                    kept.append(Ambiguity("source_inferred", a.subject, inferred_over[a.subject]))
                continue
            kept.append(a)
        return self if tuple(kept) == self.ambiguities else replace(self, ambiguities=tuple(kept))

    def to_wire(self, *, redact: bool = False) -> dict[str, Any]:
        """The envelope form. *redact* drops every text and source hint (counts and closed names remain)."""
        return {
            "answer_kind": self.answer_kind,
            "kind_decision": self.kind_decision,
            "facets": [f.to_wire(redact=redact) for f in self.facets],
            "constraints": [c.to_wire(redact=redact) for c in self.constraints],
            "named_sources": [] if redact else [s.to_wire() for s in self.named_sources[:MAX_LISTED_SOURCES]],
            "named_source_count": len(self.named_sources),
            "source_origin": self.source_origin,
            "source_hints": [] if redact else [h.to_wire() for h in self.source_hints[:MAX_LISTED_SOURCES]],
            "source_hint_count": len(self.source_hints),
            "source_ranges": [r.to_wire() for r in self.source_ranges],
            "subject": [] if redact else list(self.subject),
            "explicit_scope": {"kind": self.explicit_scope.kind, "value": "" if redact else self.explicit_scope.value},
            "current_code_requested": self.current_code_requested,
            "ambiguities": [a.to_wire(redact=redact) for a in self.ambiguities],
            "wants_lifecycle_stream": self.wants_lifecycle_stream,
            "facets_certain": self.facets_certain,
            "active_project_tier": ACTIVE_PROJECT_TIER,
            "active_project": None if redact else self.active_project,
            "extraction": self.extraction.to_wire(),
            "derive_ms": self.derive_ms,
        }

    def diagnostics_summary(self) -> dict[str, Any]:
        """The counts and closed names ``evidence_diagnostics`` carries: no text, no source hint."""
        return {
            "answer_kind": self.answer_kind,
            "kind_decision": self.kind_decision,
            "facets": len(self.facets),
            "whole_question": any(f.origin == "whole_question" for f in self.facets),
            "constraints": len(self.constraints),
            "constraint_kinds": sorted({c.kind for c in self.constraints}),
            "named_sources": len(self.named_sources),
            "source_origin": self.source_origin,
            "ambiguities": [a.code for a in self.ambiguities],
            "current_code_requested": self.current_code_requested,
            "wants_lifecycle_stream": self.wants_lifecycle_stream,
            "extraction": self.extraction.outcome,
        }


def log_fields(coverage: RequestCoverage, *, trace: bool) -> dict[str, Any]:
    """The fields of the ``nx_answer_request_coverage`` log event.

    Counts and closed-vocabulary names always. The facet and constraint texts and the source hints are the
    caller's wording and are added only when *trace* is on (``trace=False`` redacts them, RDR-232 section 1).
    """
    fields: dict[str, Any] = dict(coverage.diagnostics_summary())
    fields["derive_ms"] = coverage.derive_ms
    fields["extraction_calls"] = coverage.extraction.calls
    fields["extraction_ms"] = coverage.extraction.latency_ms
    fields["extraction_cost_usd"] = coverage.extraction.cost_usd
    if trace:
        fields["facet_texts"] = [f.text for f in coverage.facets]
        fields["constraint_texts"] = [c.text for c in coverage.constraints]
        fields["source_hints"] = [h.text for h in coverage.source_hints]
    return fields


# ── text helpers ─────────────────────────────────────────────────────────────────────────────────────────

_WS = re.compile(r"\s+")
_ABBREV = ("e.g.", "i.e.", "vs.", "etc.", "cf.", "approx.", "no.", "fig.")
_SENT_END = re.compile(r"(?<=[.?!])\s+(?=[A-Z\"'(])")
_PROTECT_CANDIDATES = tuple(chr(c) for c in range(0xE000, 0xE010))


def _squash(text: str) -> str:
    return _WS.sub(" ", text).strip()


def _sentences(text: str) -> list[str]:
    # The abbreviation dots are hidden behind a private-use character the text does not itself contain, so a
    # literal control or private-use character in the question is never turned into a ".".
    mark = next((c for c in _PROTECT_CANDIDATES if c not in text), None)
    if mark is None:
        return [_squash(text)] if text.strip() else []
    protected = text
    for abbrev in _ABBREV:
        protected = re.sub(re.escape(abbrev), abbrev.replace(".", mark), protected, flags=re.IGNORECASE)
    parts = _SENT_END.split(protected)
    return [_squash(p.replace(mark, ".")) for p in parts if p.strip()]


def _split_top_level(text: str, seps: Sequence[str]) -> list[str]:
    """Split *text* at each of *seps* that is outside brackets and quotes."""
    out: list[str] = []
    depth = 0
    quote = ""
    buf: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(depth - 1, 0)
        elif depth == 0:
            hit = next((s for s in seps if text.startswith(s, i)), None)
            if hit is not None:
                out.append("".join(buf))
                buf = []
                i += len(hit)
                continue
        buf.append(ch)
        i += 1
    out.append("".join(buf))
    return out


_LEAD_FILLER = re.compile(r"^(?:and|also|then|plus|but|please)\s+", re.IGNORECASE)


def _strip_lead(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _LEAD_FILLER.sub("", text).strip()
    return text


def _strip_end(text: str) -> str:
    return text.strip().rstrip(".?!;:,").strip()


# ── constraints ──────────────────────────────────────────────────────────────────────────────────────────

_CITE = re.compile(
    r"^(?:cite|quote|reference|attribute|provide (?:citations|sources|references)|include (?:citations|sources|"
    r"references|links)|link (?:to )?(?:the )?sources?|show (?:your )?sources?|give (?:citations|sources|references))\b",
    re.IGNORECASE,
)
#: A temporal constraint is "distinguish/separate X from Y" with an explicit date, acceptance or record cue.
#: ``contrast`` and ``differentiate`` are requests for content (a facet), never constraints.
_TEMPORAL_LEAD = re.compile(r"^(?:distinguish|separate)\b[^.]*?\bfrom\b", re.IGNORECASE)
_TEMPORAL_BODY = re.compile(
    r"\b(?:accept\w*|dated?|as of|at the time of|records?|\d{4}-\d{2}-\d{2})\b", re.IGNORECASE,
)
_PROHIBIT = re.compile(r"^(?:do not|don't|never|avoid|must not|should not|without)\b", re.IGNORECASE)
_CODE_WORD = re.compile(r"\b(?:code|codebase|implementation|source)\b", re.IGNORECASE)
#: The gaps are bounded (``{0,80}``): an unbounded ``.*`` pair backtracks quadratically on "Answer in in in ...".
_FORMAT = re.compile(
    r"^(?:(?:be )?(?:brief|concise|terse)\b|(?:answer|respond|reply|write)\b[^.]{0,80}?\b(?:in|as|using)\b[^.]{0,80}?"
    r"\b(?:sentences?|words?|paragraphs?|bullets?|table|json|markdown|list)\b|limit\b[^.]{0,80}?\b(?:words|sentences)\b)",
    re.IGNORECASE,
)


def _constraint_kind(clause: str) -> ConstraintKind | None:
    """The kind of constraint *clause* is, when it opens like one; ``None`` for anything else."""
    text = _strip_lead(clause)
    if _CITE.match(text):
        return "citation"
    if _TEMPORAL_LEAD.match(text) and _TEMPORAL_BODY.search(text):
        return "temporal_qualification"
    if _PROHIBIT.match(text):
        return "no_current_code" if _CODE_WORD.search(text) else "prohibition"
    if _FORMAT.match(text):
        return "format"
    return None


# ── facets ───────────────────────────────────────────────────────────────────────────────────────────────

_LIST_VERB = re.compile(
    r"^(?:state|list|describe|explain|identify|summari[sz]e|cover|include|provide|name|outline|trace)\b\s*",
    re.IGNORECASE,
)
_CUE_VERB = re.compile(
    r"^(?:synthesi[sz]e|summari[sz]e|analy[sz]e|review|describe|explain|overview|audit|compare|trace|state|list|"
    r"give|tell me about|walk me through)\b",
    re.IGNORECASE,
)
_RDR_REF = re.compile(r"\bRDR[-_ ]?(\d{1,4})\b", re.IGNORECASE)
_ITEM_START = re.compile(
    r"^(?:the|a|an|its|their|each|every|any|all|both|some|what|how|which|where|when|why|who|whether|"
    r"whose|no|any)\b",
    re.IGNORECASE,
)
# A clause that opens with a verb (Compare how ART and Nexus handle X) is a request, never a noun pair.
_VERB_START = re.compile(
    r"^(?:compare|contrast|differentiate|distinguish|separate|trace|review|analy[sz]e|check|audit|synthesi[sz]e|"
    r"summari[sz]e|state|list|describe|explain|identify|cover|include|provide|name|outline|give|tell|walk|show|find)\b",
    re.IGNORECASE,
)
_WH_START = re.compile(r"^(?:what|how|which|where|when|why|who|whether|whose)\b", re.IGNORECASE)
_DETERMINER = re.compile(r"^(?:the|a|an|its|their|each|every|any|all|both|some)\s+", re.IGNORECASE)

# Lifecycle topics, read from a facet's own text (never from a constraint). The RDR states and events are the
# ones in src/nexus/tables/rdr-lifecycle.toml (the table `nx rdr set-status` checks), read once at import; the
# words around them (status, progress, phase, done, "where ... stand", supersedes, rejected) are fixed here.

#: Used only when the packaged table cannot be read. A test pins it to the table, so it cannot drift silently.
_FALLBACK_LIFECYCLE: tuple[tuple[str, ...], tuple[str, ...]] = (
    ("draft", "accepted", "deferred", "closed", "superseded", "abandoned"),
    ("accept", "close", "close-unaccepted", "supersede", "abandon", "defer", "resume"),
)

#: An event verb that is also an everyday word is matched only inflected ("closes", "resumed"), not bare.
_BARE_AMBIGUOUS = frozenset({"close", "resume", "defer"})


def _read_lifecycle_vocabulary() -> tuple[tuple[str, ...], tuple[str, ...]]:
    try:
        from nexus.tables.load import load_packaged_table  # noqa: PLC0415 — deferred; the table loader is not needed to import this module

        table = load_packaged_table("rdr-lifecycle.toml")
        states = tuple(table.dimensions["status"].domain)
        events = tuple(table.dimensions["event"].domain)
        if states and events:
            return states, events
    except Exception as exc:  # noqa: BLE001 — an unreadable table degrades to the pinned copy, never fails an import
        _log.warning("request_coverage_lifecycle_table_unreadable", error=type(exc).__name__)
    return _FALLBACK_LIFECYCLE


LIFECYCLE_STATES, LIFECYCLE_EVENTS = _read_lifecycle_vocabulary()


def _lifecycle_pattern(states: Sequence[str], events: Sequence[str]) -> re.Pattern[str]:
    """The regex over an RDR's lifecycle words: each state as written (a noun state also in the plural), each
    event verb inflected (``accepts``, ``deferred``, ``abandonment``, ``closure``) and, unless it is an
    everyday word, bare."""
    alternatives: list[str] = [re.escape(st) + ("" if st.endswith("ed") else "s?") for st in states]
    for event in events:
        if not event.isalpha():
            continue  # a compound event (close-unaccepted) is made of words already listed
        tail = "e" if event.endswith("e") else ""
        stem = event[: len(event) - len(tail)]
        endings = {"es" if tail else "s", "ed", "ing", "red", "ring", "ral", "ance", "ment", "ure"}
        if event not in _BARE_AMBIGUOUS:
            endings.add(tail)
        alternatives.append(re.escape(stem) + "(?:" + "|".join(sorted(endings)) + ")")
    return re.compile(r"\b(?:" + "|".join(alternatives) + r")\b", re.IGNORECASE)


_LIFECYCLE_WORDS = re.compile(r"\b(?:lifecycle|post-?mortem|reject\w*|supersed\w*|supersession)\b", re.IGNORECASE)
_STATE_NOUN = re.compile(
    r"\b(?:(?:the|its|their|current|what|which|in|of)\s+state|states|state\s+of)\b(?!\s+(?:machine|management|transition))",
    re.IGNORECASE,
)
_WHERE_STAND = re.compile(r"\bwhere\b[^.?!]{0,60}?\bstands?\b", re.IGNORECASE)
_REVIEW_WORDS = re.compile(r"\b(?:review\w*|critiqu\w*)\b", re.IGNORECASE)

_TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("gate", re.compile(r"\bgat(?:e|es|ed|ing)\b", re.IGNORECASE)),
    ("lifecycle", _lifecycle_pattern(LIFECYCLE_STATES, LIFECYCLE_EVENTS)),
    ("lifecycle", _LIFECYCLE_WORDS),
    ("status", re.compile(
        r"\b(?:status|statuses|progress|done|phases?|implementation (?:work|state|status)|implemented|unimplemented|"
        r"complete[d]?|pending|still open)\b", re.IGNORECASE)),
    ("status", _STATE_NOUN),
    ("status", _WHERE_STAND),
)

_ADJECTIVE_SUFFIX = re.compile(r"(?:al|ic|ive|ous|ful|ary|ent|ant|ible|able|ish|less)$", re.IGNORECASE)
_ADJECTIVES = frozenset({
    "new", "old", "cloud", "local", "remote", "high", "low", "big", "small", "same", "other", "main", "full",
    "short", "long", "first", "last", "next", "prior", "later", "early", "late", "open", "closed", "free",
})


def _topics(text: str) -> tuple[str, ...]:
    found = [name for name, rx in _TOPIC_PATTERNS if rx.search(text)]
    if _RDR_REF.search(text) and _REVIEW_WORDS.search(text):
        found.append("gate")  # a review or critique of an RDR is its gate record
    return tuple(name for name in dict.fromkeys(found))


def _content_words(text: str) -> list[str]:
    return [w for w in _DETERMINER.sub("", text.strip()).split() if w]


def _split_conjoined(item: str) -> list[str]:
    """Split ``the X and Y`` noun pairs into two items, only when both halves are clearly noun phrases.

    Conservative on purpose: a split that is not clearly right leaves the item whole (a coarser facet), and
    a wh-clause is never split.
    """
    if _WH_START.match(item) or _VERB_START.match(item) or len(item.split()) > 10:
        return [item]
    parts = _split_top_level(item, (" and ",))
    if len(parts) != 2:
        return [item]
    left, right = (p.strip() for p in parts)
    lw, rw = _content_words(left), _content_words(right)
    if not lw or not rw or len(lw) > 4 or len(rw) > 5:
        return [item]
    if len(lw) == 1:
        word = lw[0].lower()
        if _ADJECTIVE_SUFFIX.search(word) or word in _ADJECTIVES or len(rw) < 2:
            return [item]
    if len(rw) == 1:
        word = rw[0].lower()
        if _ADJECTIVE_SUFFIX.search(word) or word in _ADJECTIVES or len(lw) < 2:
            return [item]
    return [left, right]


def _segments(sentence: str) -> list[str]:
    """Comma clauses of *sentence*; a clause that does not open like an item or a constraint is glued to the
    one before it (``the T1, T2 and T3 tiers`` stays one item)."""
    raw = [s for s in (r.strip() for r in _split_top_level(sentence, (", and ", ", ", "; "))) if s]
    merged: list[str] = []
    for i, seg in enumerate(raw):
        lead = _strip_lead(seg)
        starts_item = bool(_ITEM_START.match(lead)) or _constraint_kind(lead) is not None
        if merged and not starts_item:
            merged[-1] = f"{merged[-1]}, {seg}"
        else:
            merged.append(seg)
    return merged


def _enumerative(segs: list[str], colon_split: bool, body: str) -> list[str]:
    """*segs* as they are when the sentence enumerates requested items (after a colon, with a listing verb or a
    question word opening the first clause, or a question word opening a later one); otherwise the
    non-constraint clauses are glued back into one (``across its docstring, the plan comments, and the RDRs``
    is one request, not three facets). Constraint clauses stay separate either way."""
    if len(segs) < 2 or colon_split:
        return segs
    first = _strip_lead(segs[0])
    later = [_strip_lead(s) for s in segs[1:]]
    if _LIST_VERB.match(first) or _WH_START.match(first) or any(_WH_START.match(s) for s in later):
        return segs
    if not any(_constraint_kind(_strip_lead(s)) is not None for s in segs):
        return [body]
    out: list[str] = []
    for seg in segs:
        if _constraint_kind(_strip_lead(seg)) is not None or not out or _constraint_kind(_strip_lead(out[-1])) is not None:
            out.append(seg)
        else:
            out[-1] = f"{out[-1]}, {seg}"
    return out


_TOPIC = re.compile(
    r"^(?:synthesi[sz]e|summari[sz]e|analy[sz]e|review|describe|explain|overview|audit|compare|trace|state|list|give|"
    r"tell me about|walk me through)\s+(?:the\s+)?(RDR[-_ ]?\d{1,4})\b(.*)$",
    re.IGNORECASE,
)


def _is_topic(head: str) -> bool:
    """A cue verb straight onto a named source and at most a title or a short qualifier, nothing asked of it:
    ``Synthesize RDR-084, LIST PARSE Canonical Consolidation``. ``State the migration path of RDR-155, and ...``
    is a request and not a topic."""
    m = _TOPIC.match(head)
    if not m or "?" in head:
        return False
    rest = m.group(2).lstrip(" ,:-\u2014").strip()
    if len(rest.split()) > 8 or ", and " in rest or ";" in rest:
        return False
    return not any(_constraint_kind(_strip_lead(p)) is not None for p in _split_top_level(rest, (", ",)))


@dataclass
class _Parsed:
    items: list[str] = field(default_factory=list)
    constraints: list[tuple[str, ConstraintKind]] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    listed: bool = False  # an enumerated structure (colon, listing verb or several clauses) was read


def _parse(question: str) -> _Parsed:
    out = _Parsed()
    for sentence in _sentences(question):
        body = _strip_end(sentence)
        if not body:
            continue
        # "Synthesize RDR-155: what ..., what ...": the colon ends a topic and starts the enumeration.
        head, colon, tail = body.partition(": ")
        if colon and _constraint_kind(head) is None and (
            _is_topic(head) or (_CUE_VERB.match(head) and len(head.split()) <= 6 and "?" not in head)
        ):
            out.topics.append(head)
            out.listed = True
            body = tail
            colon_split = True
        elif _is_topic(body):
            out.topics.append(body)
            continue
        else:
            colon_split = False
        segs = _enumerative(_segments(body), colon_split, body)
        if len(segs) > 1:
            out.listed = True
        for index, seg in enumerate(segs):
            seg = _strip_lead(seg)
            kind = _constraint_kind(seg)
            if kind is not None:
                out.constraints.append((seg, kind))
                continue
            if index == 0 and not colon_split:
                m = _LIST_VERB.match(seg)
                rest = seg[m.end():].strip() if m else ""
                if rest:
                    out.listed = True
                    out.items.extend(_split_conjoined(rest))
                    continue
            out.items.extend(_split_conjoined(seg))
    return out


# ── kind ─────────────────────────────────────────────────────────────────────────────────────────────────

_FAILURE_WORDS = re.compile(
    r"\b(?:error|errors|fail|fails|failed|failing|failure|failures|crash|crashes|exception|exceptions|bug|broken|"
    r"wrong|empty|no \w+(?: \w+)? (?:ids|results|rows)|returns? no|timeout|hang|hangs|stuck|regress\w*|"
    r"not working|doesn't|does not|cannot|can't|unexpected)\b",
    re.IGNORECASE,
)
#: "Why does nx_answer pick the lookup plan ...": a why-question about what a NAMED piece of code decided. The
#: subject must be a snake_case identifier and the verb a selection or return verb, so a design-rationale
#: question ("Why does the RDR choose tumblers?") and a bare "Why does X exist" stay unknown.
_BEHAVIOUR_VERB = (
    r"(?:pick|picks|picked|choose|chooses|chose|select|selects|selected|route|routes|routed|prefer|prefers|"
    r"skip|skips|skipped|drop|drops|dropped|ignore|ignores|ignored|reject|rejects|rejected|return|returns|returned)"
)
_CODE_SUBJECT = r"[`'\"]?[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?:\(\))?[`'\"]?"
_WHY_CODE_BEHAVIOUR = re.compile(
    rf"^why\s+(?:does|did|do)\s+(?:the\s+)?{_CODE_SUBJECT}\s+{_BEHAVIOUR_VERB}\b", re.IGNORECASE,
)
_DISCOVERY_VERB = re.compile(
    r"\b(?:discuss(?:es)?|mention(?:s)?|cover(?:s)?|describe(?:s)?|reference(?:s)?|cite(?:s)?|about|contain(?:s)?|"
    r"touch(?:es)?)\b",
    re.IGNORECASE,
)
#: A second question joined to the first ("... RDR-155 and what remains open?") asks for more than one thing.
_AND_WH = re.compile(r"\band\s+(?:what|how|which|who|whom|whose|when|where|why|whether)\b", re.IGNORECASE)
_WH_LEAD = re.compile(r"^(?:which|what)\s+((?:\S+\s*){1,3})", re.IGNORECASE)


_NOT_PLURAL = frozenset({
    "does", "has", "was", "this", "its", "always", "sometimes", "perhaps", "yes", "less", "across", "versus", "plus",
    "gets", "goes", "uses", "makes", "takes",
})


_NOT_PLURAL_END = re.compile(r"(?:ss|us|is|ous)$")


def _plural_head(lead: str) -> bool:
    """Whether one of the first three words after ``which``/``what`` reads as a plural noun (``Which RDRs``)."""
    m = _WH_LEAD.match(lead)
    if not m:
        return False
    for word in m.group(1).split():
        w = word.strip(".,;:?!()\"'").lower()
        if len(w) > 3 and w.endswith("s") and not _NOT_PLURAL_END.search(w) \
                and w not in _NOT_PLURAL and not _DISCOVERY_VERB.fullmatch(w):
            return True
    return False


def _strong_signals(text: str, facet_count: int, listed: bool) -> set[str]:
    """Kinds the (constraint-free) question states outright."""
    lead = text.strip()
    found: set[str] = set()
    if re.match(r"^(?:synthesi[sz]e|summari[sz]e|consolidate)\b", lead, re.IGNORECASE):
        found.add("synthesis")
    if re.match(r"^trace\b.*\b(?:evolution|history|development|lineage)\b", lead, re.IGNORECASE):
        found.add("synthesis")
    if re.match(r"^(?:explain|describe|outline|state)\b", lead, re.IGNORECASE) and listed and facet_count >= 2:
        found.add("synthesis")
    if re.search(r"\bcompare\b|\bcontrast\b|\bversus\b|\bvs\.?\b|\bdiffer(?:s|ence|ences)?\b|\bdifference between\b",
                 lead, re.IGNORECASE):
        found.add("comparison")
    if (re.match(r"^audit\b", lead, re.IGNORECASE)
            or re.match(r"^check (?:whether|that|if)\b", lead, re.IGNORECASE)
            or re.search(r"\bup[- ]to[- ]date with\b|\bconsistent(?:ly)? (?:across|with)\b|\bdiverge\w*\b", lead,
                         re.IGNORECASE)):
        found.add("audit")
    if (re.match(r"^why (?:does|do|is|are|did|was|were)\b", lead, re.IGNORECASE) and _FAILURE_WORDS.search(lead)):
        found.add("debug")
    if re.match(r"^what (?:causes|is causing|are causing|caused|'s causing)\b", lead, re.IGNORECASE) \
            and _FAILURE_WORDS.search(lead):
        found.add("debug")
    if _WHY_CODE_BEHAVIOUR.match(lead) and not _AND_WH.search(lead):
        found.add("debug")
    if re.match(r"^(?:which|what) (?:is|are|was|were) (?:\w+er|more \w+|less \w+|better|worse)\b[^?]*\bor\b", lead,
                re.IGNORECASE):
        found.add("comparison")
    if re.search(r"\broot cause\b|\bstack ?trace\b|\btraceback\b", lead, re.IGNORECASE):
        found.add("debug")
    if (re.match(r"^(?:list|enumerate)\b", lead, re.IGNORECASE)
            or re.match(r"^(?:show|give) me (?:all|every)\b", lead, re.IGNORECASE)
            or re.match(r"^find (?:all|every)\b", lead, re.IGNORECASE)):
        found.add("discovery/listing")
    if _plural_head(lead) and _DISCOVERY_VERB.search(lead):
        found.add("discovery/listing")
    return found


def _weak_lookup(text: str, facet_count: int, certain: bool) -> bool:
    """A bare question word opening, with nothing stronger: ``lookup`` only for a question that asks ONE thing
    (at most one facet, and a clause structure that was read). A wh-clause joined by ``and <wh-word>`` is a
    second question that the facet parse keeps inside the one item, so it counts as a second facet here.

    The boundary (RDR-232 section 1): a lookup includes several connected facts from one passage of one
    source; a synthesis gathers aspects across sections, sources or history. This rule cannot see which of
    the two a question with more than one facet is, since that depends on where the answer sits and the
    first pass reads only the wording. So it does not guess: more than one facet is ``unknown`` here, the
    request is sent to the structured extraction (``needs_structured_extract``), and the model, whose prompt
    states the same boundary, decides. A deterministic ``lookup`` for two facets would override that
    reading and lock in a wrong shape for every question that reads like a lookup and is not."""
    if facet_count > 1 or not certain or _AND_WH.search(text):
        return False
    lead = text.strip()
    if re.match(r"^(?:which|what|who|when|where)\b", lead, re.IGNORECASE) and not re.match(
        r"^what (?:causes|is causing|are causing|caused)\b", lead, re.IGNORECASE
    ):
        return not _plural_head(lead)
    return bool(re.match(r"^how (?:many|much|long|often)\b", lead, re.IGNORECASE))


def _infer_kind(text: str, facet_count: int, listed: bool, certain: bool = True) -> tuple[str, bool]:
    """``(kind, ambiguous)``: a single strong signal wins; two different ones are ambiguous; with none, a
    lone single-facet interrogative opening gives ``lookup``; anything else is ``unknown``."""
    strong = _strong_signals(text, facet_count, listed)
    if len(strong) == 1:
        return next(iter(strong)), False
    if len(strong) > 1:
        return "unknown", True
    if _weak_lookup(text, facet_count, certain):
        return "lookup", False
    return "unknown", False


# ── current code ─────────────────────────────────────────────────────────────────────────────────────────

_CODE_NOUN = r"(?:code|codebase|source code|implementation)"
#: Explicit wording only. "source" alone is not enough ("the source RDR", "the source document").
_CURRENT_CODE = re.compile(
    r"\bcurrent (?:code|codebase|implementation|source code)\b|\bactual (?:code|codebase|implementation|source code)\b|"
    rf"\bagainst (?:its|the|their) (?:current |actual )?{_CODE_NOUN}\b|"
    r"\b(?:in|from|read|inspect|check|search|against) the (?:code|codebase|source code)\b|\bsource code\b|"
    r"\bhow (?:is|are|does|do) [^.?!]{0,60}?\bimplemented\b|\bwhere is [^.?!]{0,40}?\bimplemented\b|"
    r"\b(?:up[- ]to[- ]date|consistent|in sync) with the (?:code|implementation)\b",
    re.IGNORECASE,
)


# ── named sources ────────────────────────────────────────────────────────────────────────────────────────

_PROJECT_STOP = frozenset({
    "the", "this", "that", "these", "those", "its", "their", "our", "each", "both", "same", "other", "an", "and",
    "or", "of", "in", "for", "from", "to", "rdr",
})
_NAME = r"[A-Za-z][A-Za-z0-9_-]{1,30}"
_PROJECT_AFTER = re.compile(rf"\bRDR[-_ ]?(\d{{1,4}})\s+(?:in|of|from|for)\s+(?:the\s+)?({_NAME})\b")
_PROJECT_POSSESSIVE = re.compile(rf"\b({_NAME})['’]s\s+RDR[-_ ]?(\d{{1,4}})\b")
_PROJECT_BEFORE = re.compile(r"\b([A-Z][A-Z0-9_-]{1,10})\s+RDR[-_ ]?(\d{1,4})\b")


def _rdr_sources(text: str) -> tuple[SourceRef, ...]:
    """The RDR ids *text* names, in order, once each. Reads at most :data:`MAX_SCAN_CHARS` of it."""
    text = _scan_prefix(text)
    found: dict[SourceRef, None] = {}
    for m in _RDR_REF.finditer(text):
        found.setdefault(SourceRef.parse(f"RDR-{int(m.group(1))}"))
    # A range's bare second end ("RDR-078 through 095") has no RDR prefix of its own, so the scan above misses it.
    for rng in _source_ranges(text):
        for end in (rng.start_value, rng.end_value):
            found.setdefault(SourceRef.parse(end))
    return tuple(found)


#: ``RDR-078 through RDR-095``, ``RDR-078 to RDR-095``, ``RDR-078 - RDR-095`` (any dash), ``RDR-078 until RDR-095``.
#: The second end may be a bare number after a range word or a dash (``RDR-078 through 095``) when it is written
#: at least as wide as the first (``078 through 095``, never ``078 through 10``); a bare number after plain
#: ``to`` or a hyphen is not a range (``RDR-078 to 10 phases``, ``RDR-084-2026``).
_RDR_RANGE = re.compile(
    r"\bRDR[-_ ]?(\d{1,4})\s*(?P<conn>through(?:\s+to)?|thru|until|till|to|-|\u2013|\u2014)\s*"
    r"(?:RDR[-_ ]?(\d{1,4})|(?P<bare>\d{1,4}))\b",
    re.IGNORECASE,
)
#: A comparison opens the sentence that holds ``RDR-078 to RDR-095``: the two are compared, not a span
#: (``Compare RDR-078 to RDR-095``, ``contrast RDR-078 to RDR-095``). Only the connector ``to`` is read this
#: way; ``through``, ``until`` and a dash still name a span after a comparison verb.
_COMPARISON_LEAD = re.compile(
    r"\b(?:compar(?:e|es|ed|ing|ison)|contrast(?:s|ed|ing)?|differences?|diff|distinguish(?:es|ed|ing)?|versus|vs)\b"
    r"[^.?!;\n]{0,60}$",
    re.IGNORECASE,
)


def _source_ranges(text: str) -> tuple[SourceRange, ...]:
    """The ranges of RDR numbers the text names by their ends. A range is never expanded; the caller keeps the
    ends as sources and discloses the rest (RDR-232: no requested clause is silently dropped)."""
    out: list[SourceRange] = []
    for m in _RDR_RANGE.finditer(text):
        conn = m.group("conn").lower()
        bare = m.group("bare")
        if bare is not None and (conn in ("to", "-") or len(bare) < len(m.group(1))):
            continue
        # the lead is a comparison word and at most 60 characters before the id: a bounded window, not the
        # whole prefix (a slice per match made the scan quadratic)
        if conn == "to" and _COMPARISON_LEAD.search(text[max(0, m.start() - 100): m.start()]):
            continue
        rng = SourceRange(int(m.group(1)), int(m.group(3) if bare is None else bare))
        if rng not in out:
            out.append(rng)
    return tuple(out)


#: A word right before an RDR id (``the conexus RDR-084``, ``Nexus RDR-084``): a CANDIDATE project name. Most
#: are verbs and articles ("Summarize RDR-084"), so a candidate counts only if it names a registered owner.
_NAME_BEFORE = re.compile(rf"\b({_NAME})\s+RDR[-_ ]?\d{{1,4}}\b")


def project_name_candidates(text: str) -> list[str]:
    """Every word the question puts next to an RDR id where a project name could stand, in order, without
    repeats: before the id, after ``in``/``of``/``from``/``for`` (``RDR-084 in detail``), or as a possessive.

    These are CANDIDATES. They carry no meaning until the caller checks them against the registered owner
    names (``nx_answer`` does, case-insensitively): ``in detail`` and ``Summarize`` are not projects, and a
    lower-case ``nexus`` is. Pure: no catalog, no stoplist beyond the articles and prepositions.
    """
    names: list[str] = []
    text = _scan_prefix(text)
    for pattern, group in ((_NAME_BEFORE, 1), (_PROJECT_AFTER, 2), (_PROJECT_POSSESSIVE, 1)):
        for m in pattern.finditer(text):
            name = m.group(group)
            if name.lower() in _PROJECT_STOP or name.lower().startswith("rdr") or name in names:
                continue
            names.append(name)
    return names


def _project_hints(text: str) -> list[SourceHint]:
    text = _scan_prefix(text)
    hints: list[SourceHint] = []

    def add(number: str, name: str) -> None:
        if name.lower() in _PROJECT_STOP or name.lower().startswith("rdr"):
            return
        hint = SourceHint(f"RDR-{int(number):03d}", "project", name)
        if hint not in hints:
            hints.append(hint)

    for m in _PROJECT_AFTER.finditer(text):
        add(m.group(1), m.group(2))
    for m in _PROJECT_POSSESSIVE.finditer(text):
        add(m.group(2), m.group(1))
    for m in _PROJECT_BEFORE.finditer(text):
        add(m.group(2), m.group(1))
    return hints


#: A title written right after an RDR id, ANYWHERE in the question, in parentheses or double quotes:
#: ``What did RDR-084 (LIST PARSE Canonical Consolidation) decide?``, ``RDR-084 titled "LIST PARSE ..."``.
#: The id may be followed by a comma or colon and by ``titled``/``called``/``named``. Single quotes are not
#: read (an apostrophe is not a quote).
_INLINE_TITLE = re.compile(
    r"\bRDR[-_ ]?(\d{1,4})\b[ ,:]*(?:(?:titled|entitled|called|named)\s+)?"
    r"(?:\((?P<paren>[^()]{3,200})\)|[\"\u201c](?P<quote>[^\"\u201d]{3,200})[\"\u201d])",
    re.IGNORECASE,
)
_TITLE_LEAD = re.compile(r"^(?:titled|entitled|called|named)\s+", re.IGNORECASE)
#: A parenthesis that opens like an aside is not a title (``RDR-084 (see RDR-090)``, ``(i.e. the gate)``).
_ASIDE_OPENER = re.compile(
    r"^(?:see|cf\.?|e\.g\.?|i\.e\.?|also|from|in|for|on|about|per|and|or|re|vs\.?|but|note|including)\b", re.IGNORECASE,
)


def _title_hints(topics: Sequence[str], text: str = "") -> list[SourceHint]:
    """The title a question gives for an RDR id, so the active-project tier can tell it does not fit.

    Two forms are read: the title after the id in a topic sentence (``Synthesize RDR-084, LIST PARSE
    Canonical Consolidation``), and a title written in parentheses or double quotes right after the id
    anywhere in *text* (``What did RDR-084 (LIST PARSE Canonical Consolidation) decide?``). A title is two
    or more words; a parenthesis that names another RDR or opens like an aside is not one. A topic-sentence
    title for an id wins over an inline one for the same id.
    """
    hints: list[SourceHint] = []
    for topic in topics:
        m = _RDR_REF.search(topic)
        if not m:
            continue
        rest = _TITLE_LEAD.sub("", topic[m.end():].lstrip(" ,:-—").strip(), count=1)
        for opener, closer in (("(", ")"), ('"', '"'), ("\u201c", "\u201d")):
            if len(rest) > 2 and rest[0] == opener and rest[-1] == closer and closer not in rest[1:-1]:
                rest = rest[1:-1].strip()
                break
        if len(rest.split()) >= 2:
            hints.append(SourceHint(f"RDR-{int(m.group(1)):03d}", "title", rest))
    have = {h.source for h in hints}
    for m in _INLINE_TITLE.finditer(_scan_prefix(text)):
        title = " ".join((m.group("paren") or m.group("quote") or "").split())
        source = f"RDR-{int(m.group(1)):03d}"
        if source in have or len(title.split()) < 2 or _RDR_REF.search(title) or _ASIDE_OPENER.match(title):
            continue
        hint = SourceHint(source, "title", title)
        if hint not in hints:
            hints.append(hint)
    return hints


# ── the first pass ───────────────────────────────────────────────────────────────────────────────────────


def _is_whole(items: Sequence[str], question_body: str) -> bool:
    def norm(text: str) -> str:
        return re.sub(r"\W+", "", text).lower()

    return len(items) == 1 and norm(items[0]) == norm(question_body)


def derive_request_coverage(
    question: str,
    *,
    scope: object = "",
    evidence_request: EvidenceRequest | None = None,
    explicit_kind: object = None,
    active_project: Callable[[], str | None] | None = None,
) -> RequestCoverage:
    """The deterministic first pass (no model call, no I/O).

    *evidence_request* carries the caller's ``source``/``owner``/``project`` bindings after
    :func:`~nexus.plans.evidence.build_evidence_request`; they outrank anything read from *question*.
    *explicit_kind* is a caller hint (``dimensions["answer_kind"]``); an invalid value is ignored and recorded
    as a ``kind_hint_invalid`` ambiguity.

    *active_project* is a disclosure seam, recorded and never used here: it returns a project name, not a
    tumbler or an owner, and runs too early to qualify a source. The tier itself is
    ``nexus.plans.active_project``, applied by ``nx_answer`` after this derivation as an ``inferred``
    ``OwnerResolution`` (nexus-jypmj.2.4), to the sources a binding or, failing that, the question names.

    Never raises: a failure degrades to one ``whole_question`` facet that is uncertain, keeps the caller's
    bound sources, and so fails open on the lifecycle stream.
    """
    try:
        return _derive(question, scope, evidence_request, explicit_kind, active_project)
    except Exception as exc:  # noqa: BLE001 — the derivation stage must never fail an answer; it degrades to the whole question
        _log.warning("request_coverage_derive_failed", error=type(exc).__name__)
        text = _squash(question or "")
        bound = tuple(evidence_request.sources) if evidence_request is not None and evidence_request.sources else ()
        return RequestCoverage(
            facets=(Facet("f1", text, "whole_question"),) if text else (),
            named_sources=bound,
            source_origin="explicit" if bound else "none",
            explicit_scope=ScopeConstraint.parse(scope),
            facets_certain=False,
        )


def _derive(
    question: str,
    scope: object,
    request: EvidenceRequest | None,
    explicit_kind: object,
    active_project: Callable[[], str | None] | None,
) -> RequestCoverage:
    full = _squash(question or "")
    # Past the cap the structure is read from the head only (cut at a word); the tail is not parsed.
    over_cap = len(full) > _MAX_QUESTION_CHARS
    cut = (full.rfind(" ", 0, _MAX_QUESTION_CHARS) if over_cap else len(full))
    cut = cut if cut > 0 else _MAX_QUESTION_CHARS
    text = full[:cut]
    scan = _scan_prefix(full)
    scan_cut = len(scan) < len(full)
    tail = scan[len(text):] if len(scan) > len(text) else ""
    parsed = _parse(text)

    constraints = tuple(
        Constraint(f"c{i}", ctext, ckind) for i, (ctext, ckind) in enumerate(parsed.constraints[:_MAX_CONSTRAINTS], 1)
    )
    constraint_overflow = len(parsed.constraints) > _MAX_CONSTRAINTS
    # The question without its constraint clauses: kind, topics and current-code wording are read from this, so
    # a prohibition ("do not infer current code") never counts as a request.
    stripped = text
    for ctext, _ckind in parsed.constraints:
        stripped = stripped.replace(ctext, " ")
    stripped = _squash(stripped)
    items = [i for i in (_strip_end(x) for x in parsed.items) if i]

    facets_certain = True
    if over_cap or constraint_overflow or not items or _is_whole(items, stripped) or len(items) > _MAX_FACETS:
        content_sentences = [s for s in _sentences(stripped) if _strip_end(s)]
        facets_certain = not (
            over_cap or constraint_overflow or len(content_sentences) >= 2 or stripped.count(",") >= 2
            or len(items) > _MAX_FACETS
        )
        # The one facet holds the full question, uncut. Its topics come from the parsed head without its
        # constraint clauses, plus the unparsed tail (a gate or status clause past the cap still counts).
        topics = tuple(dict.fromkeys(_topics(stripped) + (_topics(tail) if tail else ())))
        facets = (
            Facet("f1", full, "whole_question", topics, tuple(s.value for s in _rdr_sources(scan))),
        ) if full else ()
    else:
        facets = tuple(
            Facet(f"f{i}", item, "clause", _topics(item), tuple(s.value for s in _rdr_sources(item)))
            for i, item in enumerate(items, 1)
        )

    # kind: an explicit hint, else the patterns over the question without its constraint clauses
    hint = normalize_kind(explicit_kind)
    ambiguities: list[Ambiguity] = []
    if explicit_kind is not None and hint is None:
        # the caller named a kind and it is not one: ignored, and said so (never the value: it is input)
        ambiguities.append(Ambiguity(
            "kind_hint_invalid", "answer_kind",
            "dimensions['answer_kind'] is not one of " + ", ".join(REQUEST_KINDS) + "; it was ignored and the "
            "kind was derived from the question",
        ))
    if hint is not None and hint != "unknown":
        kind, decision = hint, "explicit"
    else:
        kind, ambiguous = _infer_kind(stripped, len(items), parsed.listed, facets_certain)
        decision = "heuristic" if kind != "unknown" else "unknown"
        if ambiguous:
            ambiguities.append(Ambiguity(
                "kind_ambiguous", "answer_kind",
                "the question states more than one answer shape; none was chosen",
            ))

    current_code = bool(_CURRENT_CODE.search(stripped))

    # named sources: bindings outrank the text
    from_text = _rdr_sources(scan)
    if request is not None and request.sources:
        sources, origin = tuple(request.sources), "explicit"
    elif from_text:
        sources, origin = from_text, "question"
    else:
        sources, origin = (), "none"

    hints = _title_hints(parsed.topics, scan) + _project_hints(scan)
    ambiguities.extend(_source_ambiguities(sources, hints, request))
    if scan_cut:
        ambiguities.append(Ambiguity(
            "scan_limit", "question",
            f"the question is longer than {MAX_SCAN_CHARS} characters; RDR ids, ranges and project names past that "
            "point were not read",
        ))
    qualifier = None
    if active_project is not None:
        try:
            qualifier = active_project()
        except Exception as exc:  # noqa: BLE001 — a failing seam is disclosed, not fatal
            _log.warning("request_coverage_active_project_failed", error=type(exc).__name__)

    return RequestCoverage(
        answer_kind=kind,
        facets=facets,
        constraints=constraints,
        named_sources=sources,
        explicit_scope=ScopeConstraint.parse(scope),
        current_code_requested=current_code,
        kind_decision=decision,  # type: ignore[arg-type]
        source_origin=origin,  # type: ignore[arg-type]
        source_hints=tuple(hints),
        source_ranges=_source_ranges(scan) if origin == "question" else (),
        subject=tuple(parsed.topics),
        ambiguities=tuple(ambiguities),
        facets_certain=facets_certain,
        active_project=qualifier if isinstance(qualifier, str) and qualifier else None,
    )


def _source_ambiguities(
    sources: Sequence[SourceRef], hints: Sequence[SourceHint], request: EvidenceRequest | None,
) -> list[Ambiguity]:
    out: list[Ambiguity] = []
    owner: OwnerResolution = request.owner if request is not None else OwnerResolution("none")
    rdr_sources = [s for s in sources if s.kind == "rdr"]
    if owner.status == "conflict" and sources:
        out.append(Ambiguity("owner_conflict", "owner", owner.detail))
    elif owner.status == "unresolved" and sources:
        out.append(Ambiguity("owner_unresolved", "owner", owner.detail))
    elif owner.status == "none":
        for s in rdr_sources[:MAX_LISTED_SOURCES]:
            named = sorted({h.text for h in hints if h.kind == "project" and h.source == s.value})
            extra = (
                f" The question names project {', '.join(repr(n) for n in named)} in its text, which is not used "
                "as a qualifier; pass it as the project binding."
                if named else ""
            )
            out.append(Ambiguity(
                "source_unqualified", s.value,
                f"{s.value} has no project qualifier: no owner or project binding and no catalog-subtree scope "
                "(scope 'rdr' is a corpus, not a project). The active-project tier runs after this derivation "
                "(evidence_request.active_project says what it did); without it a project that shares this number "
                f"cannot be told apart.{extra}",
            ))
    if owner.status == "none" and len(rdr_sources) > MAX_LISTED_SOURCES:
        out.append(Ambiguity(
            "scan_limit", "sources",
            f"{len(rdr_sources) - MAX_LISTED_SOURCES} more RDR ids are named than are listed "
            f"({MAX_LISTED_SOURCES}); none of them has a project qualifier either",
        ))
    by_source: dict[str, set[str]] = {}
    for h in hints:
        if h.kind == "project":
            by_source.setdefault(h.source, set()).add(h.text)
    for source, names in sorted(by_source.items())[:MAX_LISTED_SOURCES]:
        if len({n.lower() for n in names}) > 1:
            out.append(Ambiguity(
                "project_hint_conflict", source,
                f"the question text names more than one project for {source}: {', '.join(sorted(names))}",
            ))
        elif owner.status == "none":
            out.append(Ambiguity(
                "project_hint_unapplied", source,
                f"the question text names project {sorted(names)[0]!r} for {source}; text is not applied as a "
                "qualifier",
            ))
    return out


# ── the optional structured extraction ───────────────────────────────────────────────────────────────────

#: The kill switch, read from the environment by the caller. On unless this is ``0``, ``false``, ``off`` or
#: ``no`` (any case); unset, blank and every other value mean on (nexus-jypmj.2.8).
EXTRACT_ENV = "NX_ANSWER_STRUCTURED_EXTRACT"
#: Seconds the structured step may take, from the environment, bounded to [1, 60].
EXTRACT_TIMEOUT_ENV = "NX_ANSWER_STRUCTURED_EXTRACT_TIMEOUT"
DEFAULT_EXTRACT_TIMEOUT_S = 20.0
#: The model the structured call asks for: an alias (haiku, sonnet, opus) or a model id, passed to the
#: ``--model`` flag as given. Unset or blank means :data:`DEFAULT_EXTRACT_MODEL`.
EXTRACT_MODEL_ENV = "NX_ANSWER_STRUCTURED_EXTRACT_MODEL"
#: The default extraction model (nexus-jypmj.2.8): the cheap tier. Measured first on 56 calls with no timeout or
#: error, then re-taken on 2026-10-28 with the lookup/synthesis wording of nexus-jypmj.13: 98 calls (Haiku 67, Opus 31),
#: no timeout, error or invalid reply, and all five Phase 2 gate thresholds unchanged (T2
#: ``nexus/rdr232-jypmj13-remeasure-2026-10-28``). Always passed explicitly, so ``NX_OPERATOR_MODEL_TIERING`` (which
#: only picks the model of a dispatch that names none) does not change it; a set ``EXTRACT_MODEL_ENV`` wins.
DEFAULT_EXTRACT_MODEL = "haiku"

def extraction_enabled(environ: Mapping[str, str]) -> bool:
    """Whether the structured step is switched on in *environ* (on by default; ``0``/``false``/``off``/``no`` is off).

    A non-empty value outside both vocabularies reads as on and logs one warning per process (nexus-jypmj.2.8).
    """
    raw = str(environ.get(EXTRACT_ENV, "")).strip().lower()
    if raw in OFF_VALUES:
        return False
    warn_unrecognised(EXTRACT_ENV, raw)
    return True


def extraction_model(environ: Mapping[str, str]) -> str:
    """The model the structured call is asked to use: the trimmed setting, else :data:`DEFAULT_EXTRACT_MODEL`."""
    return str(environ.get(EXTRACT_MODEL_ENV, "")).strip() or DEFAULT_EXTRACT_MODEL


def extraction_timeout(environ: Mapping[str, str]) -> float:
    """Seconds the structured call may take: the setting clamped to [1, 60]; the default for a value that is not a finite number."""
    try:
        value = float(environ.get(EXTRACT_TIMEOUT_ENV, DEFAULT_EXTRACT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_EXTRACT_TIMEOUT_S
    if not math.isfinite(value):  # nan passes min/max untouched, and inf would be a 60 s wait nobody asked for
        return DEFAULT_EXTRACT_TIMEOUT_S
    return min(max(value, 1.0), 60.0)


#: The closed schema of the one extraction call.
EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer_kind", "facets", "constraints"],
    "properties": {
        "answer_kind": {"type": "string", "enum": [k for k in REQUEST_KINDS]},
        "facets": {
            "type": "array", "maxItems": _MAX_FACETS,
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "constraints": {
            "type": "array", "maxItems": _MAX_CONSTRAINTS,
            "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "kind"],
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 300},
                    "kind": {"type": "string", "enum": [
                        "temporal_qualification", "citation", "no_current_code", "prohibition", "format"]},
                },
            },
        },
    },
}

#: The two kind definitions the prompt shares with RDR-232 section 1's shape table (tests/test_rdr232_lookup_
#: boundary.py reads the table and fails if either phrase drifts). Sam decided on 2026-10-27 that the boundary
#: between them is the source and the passage, not the number of facts asked for.
_LOOKUP_DEFINITION = "a specific fact or small set of facts, including several connected facts from one passage of one source"
_SYNTHESIS_DEFINITION = "a unified account of multiple requested aspects across sections, sources or history"

_EXTRACT_PROMPT = (
    "Read the question below and return the shape of the answer it asks for. answer_kind is one of: lookup "
    f"({_LOOKUP_DEFINITION}), synthesis ({_SYNTHESIS_DEFINITION}), discovery/listing (an inventory), "
    "comparison, audit, debug, unknown. Asking for more than one fact does not make a question a synthesis: "
    "when one passage of one source answers all of them, it is a lookup; it is a synthesis only when the "
    "answer must be gathered from different sections, different sources or the history of a thing. facets "
    "lists, in the question's own words, each fact or topic that needs evidence (one entry per requested "
    "item; never omit a requested one; the number of facets does not decide the kind). constraints lists, in "
    "the question's own words, each instruction about how to treat the evidence (cite sources, distinguish "
    "dates, do not infer something, format limits); a constraint is never also a facet. Use answer_kind "
    "unknown when the question does not settle one.\n\nQuestion:\n"
)

#: ``(prompt, schema, *, timeout, usage_sink) -> dict`` : the shape of ``claude_dispatch``.
Dispatch = Callable[..., Awaitable[dict[str, Any]]]

#: Less than this much budget left and the call is not made (``skipped_deadline``): a model round trip does
#: not fit, and a call that cannot finish would only spend the answer's remaining time.
MIN_EXTRACT_SECONDS = 1.0

#: Seconds the outer wait allows past the timeout handed to dispatch: dispatch's own kill-and-reap grace
#: (``dispatch._TIMEOUT_CLEANUP_GRACE_SECONDS``, 5 s; a test pins this to it) plus one second. An outer wait
#: that fired at the timeout would cancel dispatch before its own kill path ran and orphan the claude -p tree.
DEFAULT_CLEANUP_GRACE_S = 6.0

# Words that carry no request (articles, prepositions, pronouns, cue verbs): a model reading is judged on the
# rest of the question's words.
_STOP = frozenset((
    "a an the of in on at to for from by with without as about across into onto over under between among "
    "and or but nor so also then than that this these those it its their there here is are was were be been "
    "being do does did has have had can could should would will may might must not no any all each both "
    "some what which who whom whose when where why how whether if i me my we us our you your tell give show "
    "explain describe state list name outline summarize summarise identify cover include provide trace "
    "walk please".split()
))


def _word_keys(text: str) -> set[str]:
    keys = set()
    for word in re.findall(r"[A-Za-z0-9][A-Za-z0-9_'’-]*", text.lower()):
        if word in _STOP:
            continue
        keys.add(word[:-1] if len(word) > 3 and word.endswith("s") else word)
    return keys


def uncovered_words(question: str, covering: Iterable[str]) -> list[str]:
    """The content words of *question* that no text in *covering* contains (case, a trailing plural ``s`` and
    function words ignored). Empty means the covering texts account for the question."""
    have: set[str] = set()
    for text in covering:
        have |= _word_keys(text)
    return sorted(_word_keys(question) - have)


def needs_structured_extract(coverage: RequestCoverage) -> bool:
    """Whether the first pass left the kind or the clause structure open."""
    return coverage.answer_kind == "unknown" or not coverage.facets_certain


def _validate_extract(raw: Any) -> tuple[str, list[str], list[tuple[str, str]]] | None:
    if not isinstance(raw, Mapping):
        return None
    kind = normalize_kind(raw.get("answer_kind"))
    facets_raw, cons_raw = raw.get("facets"), raw.get("constraints")
    if kind is None or not isinstance(facets_raw, list) or not isinstance(cons_raw, list):
        return None
    if len(facets_raw) > _MAX_FACETS or len(cons_raw) > _MAX_CONSTRAINTS:
        return None
    facets: list[str] = []
    for f in facets_raw:
        if not isinstance(f, str) or not f.strip() or len(f) > 300:
            return None
        facets.append(_squash(f))
    constraints: list[tuple[str, str]] = []
    valid_kinds = set(EXTRACT_SCHEMA["properties"]["constraints"]["items"]["properties"]["kind"]["enum"])
    for c in cons_raw:
        if (not isinstance(c, Mapping) or not isinstance(c.get("text"), str) or not c["text"].strip()
                or len(c["text"]) > 300 or c.get("kind") not in valid_kinds):
            return None
        constraints.append((_squash(c["text"]), str(c["kind"])))
    return kind, facets, constraints


async def structured_extract(
    coverage: RequestCoverage,
    question: str,
    *,
    dispatch: Dispatch,
    timeout: float,
    enabled: bool,
    usage_sink: list[Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    seconds_left: float | None = None,
    cleanup_grace: float = DEFAULT_CLEANUP_GRACE_S,
) -> RequestCoverage:
    """At most one direct, schema-closed extraction call, when the first pass left the shape open.

    Returns *coverage* unchanged, with an :class:`ExtractionReport`, when the step is disabled, not needed,
    skipped for want of budget (*seconds_left* under :data:`MIN_EXTRACT_SECONDS`), times out, raises or
    answers outside the schema. On success only what the first pass lacked is taken: the kind when it was
    ``unknown`` and the facets when the clause structure was uncertain. The model's facets replace the first
    pass's only when together with the constraints and subject they cover every content word of the question;
    otherwise the ``whole_question`` facet stays beside them and the structure stays uncertain. A question
    past the cap is only sent in part, so it always takes the second path. First-pass
    constraints and the caller's explicit bindings are never replaced. *usage_sink* is where the dispatch
    reports its cost; the report carries the sum. The call is never recursive ``nx_answer`` and never
    retried.

    The wait is bounded at *timeout* plus *cleanup_grace*, not at *timeout*: *dispatch* enforces *timeout*
    itself and kills its process tree on expiry, and an earlier outer cancel would pre-empt that.
    """
    if not enabled:
        return replace(coverage, extraction=ExtractionReport("disabled"))
    if not needs_structured_extract(coverage):
        return replace(coverage, extraction=ExtractionReport("not_needed", timeout_s=timeout))
    if seconds_left is not None and seconds_left < MIN_EXTRACT_SECONDS:
        return replace(coverage, extraction=ExtractionReport("skipped_deadline", calls=0, timeout_s=timeout))
    sink: list[Any] = usage_sink if usage_sink is not None else []
    started = clock()

    def report(outcome: str, calls: int = 1) -> ExtractionReport:
        cost = [u.cost_usd for u in sink if getattr(u, "cost_usd", None) is not None]
        return ExtractionReport(
            outcome,  # type: ignore[arg-type]
            calls=calls, timeout_s=timeout, latency_ms=int((clock() - started) * 1000),
            cost_usd=round(sum(cost), 6) if cost else None,
        )

    from nexus.operators.llm_types import OperatorTimeoutError  # noqa: PLC0415 — deferred; imports nothing from nexus

    try:
        raw = await asyncio.wait_for(
            dispatch(_EXTRACT_PROMPT + question[:_MAX_QUESTION_CHARS], EXTRACT_SCHEMA, timeout=timeout, usage_sink=sink),
            timeout=timeout + max(cleanup_grace, 0.0),
        )
    except (asyncio.TimeoutError, OperatorTimeoutError):
        return replace(coverage, extraction=report("timeout"))
    except Exception as exc:  # noqa: BLE001 — a failed extraction leaves the first-pass result, never a failed answer
        _log.warning("request_coverage_extract_failed", error=type(exc).__name__)
        return replace(coverage, extraction=report("error"))
    parsed = _validate_extract(raw)
    if parsed is None:
        return replace(coverage, extraction=report("invalid"))
    kind, facet_texts, _constraint_rows = parsed

    changed = False
    new_kind, decision = coverage.answer_kind, coverage.kind_decision
    if coverage.answer_kind == "unknown" and kind != "unknown" and coverage.kind_decision != "explicit":
        new_kind, decision, changed = kind, "model-assisted", True
    facets = coverage.facets
    certain = coverage.facets_certain
    if not coverage.facets_certain and facet_texts:
        model_facets = [
            Facet(f"f{i}", t, "clause", _topics(t), tuple(s.value for s in _rdr_sources(t)))
            for i, t in enumerate(facet_texts, 1)
        ]
        covering = (
            list(facet_texts) + [c.text for c in coverage.constraints] + list(coverage.subject)
        )
        # A question past the cap was only half sent: the model never saw its tail, so its reading can neither
        # replace the first pass's whole_question facet (which holds the tail) nor make the structure certain.
        full = _squash(question or "")
        if len(full) <= _MAX_QUESTION_CHARS and not uncovered_words(full, covering):
            facets, certain = tuple(model_facets), True
        else:
            # The model's reading missed part of the question: keep the whole question beside it.
            whole = next((f for f in coverage.facets if f.origin == "whole_question"), None)
            facets = tuple(model_facets) + (
                (replace(whole, id=f"f{len(model_facets) + 1}"),) if whole is not None else ()
            )
        changed = True
    return replace(
        coverage, answer_kind=new_kind, kind_decision=decision, facets=facets, facets_certain=certain,  # type: ignore[arg-type]
        extraction=report("ok" if changed else "unchanged"),
    )


# ── a persistently failing extraction ────────────────────────────────────────────────────────────────────────

#: Extraction outcomes that mean the call was made and gave nothing usable. ``ok`` and ``unchanged`` are the
#: working outcomes; ``disabled``, ``not_needed`` and ``skipped_deadline`` made no call and say nothing.
FAILED_OUTCOMES: frozenset[str] = frozenset({"error", "timeout", "invalid"})
_WORKING_OUTCOMES: frozenset[str] = frozenset({"ok", "unchanged"})

#: This many failed calls in a row, in one process, make the failure persistent (nexus-jypmj.16). One failure is a
#: per-request event and stays silent: the request keeps its first-pass kind and the gate fails closed.
PERSISTENT_FAILURE_THRESHOLD = 3

_health_lock = threading.Lock()
_consecutive_failures = 0
_failing_warned = False


def note_extraction_outcome(outcome: str, *, gate_enforcing: bool) -> bool:
    """Count one request's extraction *outcome*; warn once per process when the failures are persistent.

    A failed extraction leaves the request on its first-pass kind and the answer-kind gate keeps enforcing on it
    (per-request fail-closed is the design). First-pass kinds alone are the regime that failed the held-out
    lookup measurement, so a process in which the call fails every time (no ``claude`` CLI and no API key, an
    ``openai_compat`` endpoint that answers outside the schema, a timeout every time) enforces in it with no
    signal. After :data:`PERSISTENT_FAILURE_THRESHOLD` failures in a row this logs ``answer_kind_gate_extraction_failing``
    once per streak, the first time that happens while *gate_enforcing* (a gate in shadow loses nothing to it, and
    does not use the warning up). A working outcome ends the run and re-arms the warning, so a later streak after a
    recovery warns again (nexus-jypmj.21); an outcome with no call is ignored. Returns whether this call logged the
    warning. Never raises.
    """
    global _consecutive_failures, _failing_warned  # noqa: PLW0603 — process-wide, like the other once-per-process latches
    with _health_lock:
        if outcome in _WORKING_OUTCOMES:
            _consecutive_failures = 0
            _failing_warned = False  # recovered: a later streak warns again (nexus-jypmj.21)
            return False
        if outcome not in FAILED_OUTCOMES:
            return False
        _consecutive_failures += 1
        if _failing_warned or not gate_enforcing or _consecutive_failures < PERSISTENT_FAILURE_THRESHOLD:
            return False
        _failing_warned = True
        failures = _consecutive_failures
    _log.warning(
        "answer_kind_gate_extraction_failing",
        consecutive_failures=failures,
        last_outcome=outcome,
        consequence=(
            "the answer-kind gate is enforcing on first-pass request kinds alone, the regime that lost "
            "lookup recall in the held-out measurement; the latest extraction calls in this process all failed"
        ),
        remedy=(
            "run `nx doctor` (row 'Answer-kind extraction') to see why; make an LLM backend available "
            "(the claude CLI, or nx config set anthropic_api_key), or set NX_ANSWER_STRUCTURED_EXTRACT=0 "
            "to run the gate in shadow, or NX_ANSWER_KIND_ELIGIBILITY=0 to switch it off"
        ),
    )
    return True


def reset_extraction_health() -> None:
    """Forget the failure run and the spent warning (a test hook; the process-wide state is otherwise permanent)."""
    global _consecutive_failures, _failing_warned  # noqa: PLW0603
    with _health_lock:
        _consecutive_failures = 0
        _failing_warned = False
