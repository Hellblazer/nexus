# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Answer-kind eligibility — RDR-232 Phase 2 (nexus-jypmj.2.3).

A plan serves one kind of answer (``plans/answer_kind.py``); a request asks for one. This module says
whether a given plan may serve a given request, applies that to the candidates of every matcher
branch, and reports what it did.

The rule is the RDR's compatibility matrix (section 1), keyed by the request kind, over the plan's
declared kind and its provenance:

====================  =========================================
request kind          eligible declared plan kinds
====================  =========================================
``lookup``            lookup, synthesis, unknown
``synthesis``         synthesis, unknown
``discovery/listing`` discovery/listing, unknown
``comparison``        comparison, synthesis, unknown
``audit``             audit, synthesis, unknown
``debug``             debug, synthesis, unknown
``unknown``           curated synthesis and curated unknown only
====================  =========================================

``unknown`` plan kinds are limited by provenance, as the RDR says: a curated (reviewed builtin) unknown
plan is eligible as the table reads; an unknown plan that is not curated (grown with no recorded route,
or one that declares nothing) is eligible only for ``lookup`` and ``discovery/listing`` requests. An
``unknown`` request admits curated plans only, so it never reaches a grown plan, whatever kind that
plan recorded.

Provenance is decided from the STORED row, failing closed (the rule ``answer_kind.py`` states):

* ``curated``: the ``builtin-template`` tag AND a declaration that says ``curated``;
* ``grown``: the ``grown`` tag OR a declaration that says ``grown``, whichever is the less trusted
  reading. A ``curated`` declaration on a row without the builtin tag is not trusted: it reads as grown
  with kind ``unknown`` (``untrusted_curated_claim``). A grown plan's recorded kind counts only when its
  route is in :data:`TRUSTED_ROUTES`, which ships empty. Every recorded stamp (``explicit``,
  ``heuristic``, ``model-assisted``) is the request's label for the kind, not a property of the plan's steps
  (an ``explicit`` hint is the caller's, and the planner builds its steps without it), and the RDR relaxes
  the unknown-kind restriction for recorded provenance only "after review": until a reviewed decision
  (nexus-jypmj.2.8) adds a route, a grown plan reads as grown with kind ``unknown`` (``unreviewed_route``).
  The route is still recorded at the grow site; only eligibility discounts it;
* ``unattested``: nothing valid was declared. It is neither curated nor grown, its kind is ``unknown``,
  and it is treated like any other untrusted unknown plan. A builtin seeded before nexus-jypmj.2.1 is
  this until ``nx plan reseed`` (or the upgrade ladder) writes its declaration; the exclusion reason is
  then ``undeclared_builtin``.

The session cache's ``answer_provenance`` metadata is not read here: it is stale until a refresh.

The whole filter sits behind :data:`ANSWER_KIND_ELIGIBILITY_ENABLED`, which ships ``False``
(nexus-jypmj.2.8 turns it on once thresholds are set from the baseline measurement). With it off an
:class:`EligibilityFilter` still judges and counts every candidate, so the run envelope can say what the
gate WOULD exclude, but :meth:`EligibilityFilter.admit` keeps every candidate and the matcher's output,
metrics and over-fetch are identical to a call that passed no filter.

The matcher calls :meth:`EligibilityFilter.admit` last on each branch, after every other drop (unmet
binding, scope conflict, always-failing, FTS literal drop), so a branch's ``excluded`` count is
"candidates that would have been returned (or pooled) but for eligibility", not "candidates something
else would have dropped anyway". A candidate that is both unrunnable and the wrong shape is counted
under the binding drop and never reaches ``examined``.

:meth:`EligibilityFilter.admit` never raises. If judging a row fails, the candidate counts under the
``judge_error`` reason; with the filter not enforcing it is kept (the shadow filter must never fail
``plan_match``), and when enforcing it is dropped (fail closed).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from nexus.plans.answer_kind import PlanAnswerKind, decode_declaration

_log = structlog.get_logger(__name__)

__all__ = [
    "ANSWER_KIND_ELIGIBILITY_ENABLED",
    "BRANCHES",
    "BUILTIN_TAG",
    "GROWN_TAG",
    "REASONS",
    "TRUSTED_ROUTES",
    "EligibilityFilter",
    "PlanStanding",
    "Verdict",
    "classify_plan",
    "coerce_kind",
    "eligibility",
    "eligible_matches",
    "fallback_reason",
]

#: The one switch. Off: the filter reports what it would exclude and excludes nothing, the planner's
#: few-shots are as before. On: ineligible candidates are removed from every matcher branch and from the
#: planner's few-shots. Read at call time through the module, so a test can flip it.
ANSWER_KIND_ELIGIBILITY_ENABLED: bool = False

#: The row tags provenance reads. ``BUILTIN_TAG`` is ``matcher._CATEGORY_PLAN_TAG``; a test pins that.
BUILTIN_TAG = "builtin-template"
GROWN_TAG = "grown"

#: The matcher branches that produce candidates.
BRANCHES: tuple[str, ...] = ("cosine", "category_route", "fts")

#: Why a plan was refused. ``undeclared_builtin`` and ``untrusted_curated_claim`` name the stored row's
#: defect and replace the matrix reason when the row has one.
REASONS: frozenset[str] = frozenset({
    "kind_mismatch",
    "unknown_request_untrusted_plan",
    "unknown_request_narrow_plan",
    "untrusted_unknown_plan",
    "undeclared_builtin",
    "untrusted_curated_claim",
    "unreviewed_route",
    "judge_error",
})

#: The grown-plan routes whose recorded kind eligibility honours. EMPTY on purpose: even ``explicit`` is the
#: request's label (the caller's ``dimensions["answer_kind"]`` hint), not a check of the plan's steps, and
#: the inline planner builds its steps before any stamp. Until a reviewed decision (nexus-jypmj.2.8)
#: names a route here, every grown plan reads as ``unknown`` kind whatever its recorded route.
TRUSTED_ROUTES: frozenset[str] = frozenset()

#: At most this many refused plan ids are listed in the envelope.
_MAX_LISTED_IDS = 20

_L, _S, _D = PlanAnswerKind.LOOKUP, PlanAnswerKind.SYNTHESIS, PlanAnswerKind.DISCOVERY_LISTING
_C, _A, _B, _U = (
    PlanAnswerKind.COMPARISON, PlanAnswerKind.AUDIT, PlanAnswerKind.DEBUG, PlanAnswerKind.UNKNOWN,
)

#: Request kind -> the KNOWN plan kinds it accepts. ``unknown`` plan kinds and the ``unknown`` request are
#: rules of their own (see :func:`eligibility`), not rows of this table.
_COMPATIBLE: dict[PlanAnswerKind, frozenset[PlanAnswerKind]] = {
    _L: frozenset({_L, _S}),
    _S: frozenset({_S}),
    _D: frozenset({_D}),
    _C: frozenset({_C, _S}),
    _A: frozenset({_A, _S}),
    _B: frozenset({_B, _S}),
}

#: Request kinds an untrusted unknown-kind plan may still serve.
_UNKNOWN_PLAN_REQUESTS = frozenset({_L, _D})


def coerce_kind(raw: object) -> PlanAnswerKind:
    """The :class:`PlanAnswerKind` *raw* names; anything outside the vocabulary is ``UNKNOWN``."""
    if isinstance(raw, PlanAnswerKind):
        return raw
    try:
        return PlanAnswerKind(raw)
    except (ValueError, TypeError):
        return _U


@dataclass(frozen=True)
class PlanStanding:
    """What the stored row says a plan is: its kind, how far to trust it, and a defect if it has one.

    ``route`` is the recorded route of a grown plan's declared kind (``explicit``, ``heuristic``,
    ``model-assisted``), whether or not it was honoured; ``None`` when the row records none. ``kind`` is the
    EFFECTIVE kind: ``unknown`` when the declared one was discounted.
    """

    kind: PlanAnswerKind
    provenance: str  # "curated" | "grown" | "unattested"
    flag: str | None = None
    route: str | None = None


@dataclass(frozen=True)
class Verdict:
    """Whether a plan may serve a request, and the matrix reason when it may not."""

    eligible: bool
    reason: str | None = None


_OK = Verdict(True)


def classify_plan(tags: str | None, plan_json: Any) -> PlanStanding:
    """Decide a row's kind and provenance from its tags and ``plan_json``, failing closed.

    Takes only what the stored row holds; the session cache's metadata is deliberately not an input.
    """
    text = tags if isinstance(tags, str) else ""
    tokens = {t.strip() for t in text.split(",") if t.strip()}
    declared = decode_declaration(plan_json)
    if GROWN_TAG in tokens or declared.provenance == "grown":
        # A grown declaration that validated carries its route, so its kind is a recorded one. A grown
        # TAG with a curated or absent declaration has no route: its kind is unknown.
        if declared.provenance != "grown":
            return PlanStanding(_U, "grown")
        if declared.kind is _U:
            return PlanStanding(_U, "grown")
        if declared.route in TRUSTED_ROUTES:
            return PlanStanding(declared.kind, "grown", route=declared.route)
        # the request's guessed kind is not the plan's shape: read it as unknown until reviewed
        return PlanStanding(_U, "grown", "unreviewed_route", route=declared.route)
    if declared.provenance == "curated":
        if BUILTIN_TAG in tokens:
            return PlanStanding(declared.kind, "curated")
        return PlanStanding(_U, "grown", "untrusted_curated_claim")
    return PlanStanding(_U, "unattested", "undeclared_builtin" if BUILTIN_TAG in tokens else None)


def eligibility(request_kind: PlanAnswerKind, plan_kind: PlanAnswerKind, provenance: str) -> Verdict:
    """May a plan of *plan_kind* and *provenance* serve a request of *request_kind*? (the matrix)

    *provenance* is ``curated``, ``grown`` or ``unattested``. An ``unattested`` plan declared nothing, so
    whatever kind is passed with it is read as ``unknown``.
    """
    if provenance == "unattested":
        plan_kind = _U
    if request_kind is _U:
        if provenance != "curated":
            return Verdict(False, "unknown_request_untrusted_plan")
        if plan_kind not in (_S, _U):
            return Verdict(False, "unknown_request_narrow_plan")
        return _OK
    if plan_kind is not _U:
        return _OK if plan_kind in _COMPATIBLE[request_kind] else Verdict(False, "kind_mismatch")
    if provenance == "curated" or request_kind in _UNKNOWN_PLAN_REQUESTS:
        return _OK
    return Verdict(False, "untrusted_unknown_plan")


def _judge(request_kind: PlanAnswerKind, tags: str | None, plan_json: Any) -> tuple[PlanStanding, Verdict]:
    standing = classify_plan(tags, plan_json)
    verdict = eligibility(request_kind, standing.kind, standing.provenance)
    if not verdict.eligible and standing.flag is not None:
        verdict = Verdict(False, standing.flag)
    return standing, verdict


def _safe_judge(request_kind: PlanAnswerKind, tags: Any, plan_json: Any) -> tuple[PlanStanding, Verdict]:
    """:func:`_judge` that never raises: a fault is the ``judge_error`` verdict on an unattested standing.

    Callers on the production path (the matcher, ``nx_answer``) use this one, so a judging fault with the
    gate off cannot fail ``plan_match``; the fault is logged and counted under ``judge_error``.
    """
    try:
        return _judge(request_kind, tags, plan_json)
    except Exception:  # noqa: BLE001 — a judging fault must never fail the caller; logged and counted
        _log.warning("eligibility_judge_failed", exc_info=True)
        return PlanStanding(_U, "unattested", "judge_error"), Verdict(False, "judge_error")


def eligible_matches(request_kind: PlanAnswerKind, matches: Iterable[Any] | None) -> list[Any]:
    """*matches* without the plans a request of *request_kind* may not use (quiet: no counting)."""
    kept = []
    for m in matches or ():
        _, verdict = _safe_judge(request_kind, getattr(m, "tags", None), getattr(m, "plan_json", None))
        if verdict.eligible:
            kept.append(m)
    return kept


class EligibilityFilter:
    """Judges matcher candidates for one request and keeps the account the envelope reports.

    Mutable on purpose: the matcher fills the branch counts, ``nx_answer`` adds the returned summary and
    the fallback reason. With ``enforce`` False it judges and counts and admits everything.
    """

    def __init__(self, request_kind: PlanAnswerKind | str, decision: str = "unknown", enforce: bool = False) -> None:
        self.request_kind = coerce_kind(request_kind)
        self.decision = decision
        self.enforce = enforce
        self._branches: dict[str, dict[str, Any]] = {
            b: {"examined": 0, "excluded": 0, "reasons": {}} for b in BRANCHES
        }
        self._judged: dict[int, tuple[PlanStanding, Verdict]] = {}
        self._excluded_ids: list[int] = []
        self._returned: dict[str, int] | None = None
        self._selected: dict[str, Any] | None = None
        self._fallback: str | None = None

    def admit(self, row: Mapping[str, Any], branch: str) -> bool:
        """Judge the candidate *row* found by *branch*; False means the matcher must drop it."""
        counts = self._branches[branch]
        counts["examined"] += 1
        plan_id = row.get("id") if isinstance(row, Mapping) else None
        standing, verdict = _safe_judge(
            self.request_kind, row.get("tags") if isinstance(row, Mapping) else None,
            row.get("plan_json") if isinstance(row, Mapping) else None,
        )
        if isinstance(plan_id, int):
            self._judged[plan_id] = (standing, verdict)
        if verdict.eligible:
            return True
        counts["excluded"] += 1
        counts["reasons"][verdict.reason] = counts["reasons"].get(verdict.reason, 0) + 1
        if isinstance(plan_id, int) and plan_id not in self._excluded_ids:
            self._excluded_ids.append(plan_id)
        return not self.enforce

    @property
    def excluded_total(self) -> int:
        """Candidate exclusions summed over the branches (one plan found by two branches counts twice)."""
        return sum(b["excluded"] for b in self._branches.values())

    @property
    def excluded_plan_ids(self) -> list[int]:
        return list(self._excluded_ids)

    def record_returned(self, matches: Sequence[Any], selected: Any) -> None:
        """Note what the matcher returned and which plan ``nx_answer`` selected (id 0 is the inline plan)."""

        def judged(m: Any) -> tuple[PlanStanding, Verdict]:
            plan_id = getattr(m, "plan_id", 0)
            if plan_id in self._judged:
                return self._judged[plan_id]
            return _safe_judge(self.request_kind, getattr(m, "tags", None), getattr(m, "plan_json", None))

        self._returned = {
            "total": len(matches), "eligible": sum(1 for m in matches if judged(m)[1].eligible),
        }
        plan_id = getattr(selected, "plan_id", 0)
        if not plan_id:
            self._selected = None
            return
        standing, verdict = judged(selected)
        self._selected = {
            "plan_id": plan_id, "eligible": verdict.eligible, "kind": standing.kind.value,
            "provenance": standing.provenance, "route": standing.route, "reason": verdict.reason,
        }

    def set_fallback(self, reason: str | None) -> None:
        self._fallback = reason

    def to_wire(self) -> dict[str, Any]:
        return {
            "request_kind": self.request_kind.value,
            "kind_decision": self.decision,
            "enforced": self.enforce,
            "branches": {b: {**c, "reasons": dict(c["reasons"])} for b, c in self._branches.items()},
            "excluded_total": self.excluded_total,
            "excluded_plan_ids": self._excluded_ids[:_MAX_LISTED_IDS],
            "returned": dict(self._returned) if self._returned is not None else None,
            "selected": dict(self._selected) if self._selected is not None else None,
            "fallback_reason": self._fallback,
        }


def fallback_reason(
    *, force_dynamic: bool, matches: Sequence[Any], hit: bool, filt: EligibilityFilter | None,
) -> str | None:
    """Why ``nx_answer`` went to the inline planner; ``None`` when a library plan was selected.

    ``all_candidates_ineligible`` only when the gate is enforcing and refused something: in shadow mode
    nothing was refused, so exclusion is not why nothing matched.
    """
    if force_dynamic:
        return "force_dynamic"
    if hit:
        return None
    if not matches:
        if filt is not None and filt.enforce and filt.excluded_total:
            return "all_candidates_ineligible"
        return "no_plan_candidates"
    return "below_confidence_floor"
