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
``lookup``            lookup, unknown
``synthesis``         synthesis, unknown
``discovery/listing`` discovery/listing, unknown
``comparison``        comparison, synthesis, unknown
``audit``             audit, synthesis, unknown
``debug``             debug, synthesis, unknown
``unknown``           curated synthesis and curated unknown only
====================  =========================================

That table is for curated plans and, with the limits below, grown ones. A **saved** plan (``plan_save`` with a
declared kind, or a template in a project, RDR or repo tier; nexus-0vh80, Sam 2026-11-03) is a column of its own:

====================  =========================================
request kind          eligible saved plans
====================  =========================================
a known kind          saved plans of that SAME kind only
``unknown``           none
====================  =========================================

A saved plan's kind is its saver's claim, not reviewed, so it is honoured at once but narrowly. It serves only the
row of its own kind, with none of the compatibility cells a curated plan has (a saved synthesis plan serves no
comparison, audit or debug request) and none of the unknown-plan allowances (a saved plan of unknown kind serves
nothing, not even a lookup). It serves only the project that saved it: its row's ``project`` must equal the
request's (:attr:`EligibilityFilter.request_projects`: the session's directory name, the one a grown plan is filed
under, and the primary checkout's name, so a session in a linked worktree matches a plan saved under the repository's
name; compared stripped), and a blank one on either side serves nothing. And a shape veto applies: a plan whose last step is not an operator (it hands back
what it retrieved) cannot claim lookup, synthesis, comparison, audit or debug (``saved_retrieval_only``;
``answer_kind.SHAPE_VETOED_KINDS``). Reasons: ``kind_mismatch``, ``saved_unknown_plan``, ``saved_other_project``,
``saved_retrieval_only``; the ``unknown`` request keeps ``unknown_request_untrusted_plan``.

The ``unknown`` kind in the ``lookup`` and ``discovery/listing`` rows means a curated or a grown plan of unknown
kind; a plan with no answer-kind declaration (``unattested``) is eligible for neither of those two rows, nor for
any other (Sam, 2026-11-03, nexus-rflvp; see the provenance rules below).

A ``lookup`` request admits no synthesis plan (Sam, 2026-10-30, nexus-jypmj.11): a synthesis plan outscored the
lookup plan and cost 8.9 times as much on H09. The rows of the other kinds are unchanged.

A ``synthesis`` request, or an ``unknown`` one, that names exactly ONE source does not admit a citation-traversal
plan (Sam, 2026-10-30, nexus-4xmpv; restored by nexus-jypmj after the round-3 removal): the request wants the parts of
one document read, and the plan walks the reference graph around it (plan 467 on G06 and on two of the six near-miss
requests); under the document confinement its summarize terminal produced both facet mismatches of re-measure 3.
The abstract-themes plan stays allowed (Sam, 2026-10-30): the deny-list holds citation-traversal only. The
``unknown`` request is included because an extraction failure leaves single-source requests unknown, and the unknown
row admits curated synthesis plans. The plan is named by what it declares, the ``strategy`` dimension of a curated
(builtin) row, listed in :data:`SINGLE_SOURCE_EXCLUDED_STRATEGIES`, so a re-seeded library with new plan ids is
still covered. The count of named sources is :attr:`RequestCoverage.names_one_source` (``nx_answer`` passes 1 or 0).
This rule is the one exclusion that keeps excluding while the request's extraction failed or was skipped
(Sam, 2026-10-31, nexus-x3eoc): the answer-kind rows of the matrix then run in shadow and the filter reports it
(``enforced_reasons``, below), but a request left ``unknown`` by a failed call is the case this rule exists for.
Nothing else changes: a request naming two sources or none still admits it, and the matrix rows above are
untouched. The request is not left without a plan: the matcher offers its other eligible synthesis plans and
otherwise ``nx_answer`` plans inline.

``unknown`` plan kinds are limited by provenance, as the RDR says: a curated (reviewed builtin) unknown
plan is eligible as the table reads; a grown unknown plan (grown with no recorded route, or whose recorded
route is not trusted) is eligible only for ``lookup`` and ``discovery/listing`` requests; an unattested plan,
one that declares nothing, is eligible for neither (Sam, 2026-11-03, nexus-rflvp: user-saved plans, which
declare nothing, won 31 of 77 lookups since the gate was enabled, one of them another project's plan through
the cosine branch, and answered with raw ids; the curated lookup plan answered the same questions; reason
``unattested_plan``). No request kind admits an unattested plan. An
``unknown`` request admits curated plans only, so it never reaches a grown plan, whatever kind that
plan recorded.

Provenance is decided from the STORED row, failing closed (the rule ``answer_kind.py`` states):

* ``curated``: the ``builtin-template`` tag AND a declaration that says ``curated``. Only the builtin tier writes
  one: the seed loader declares a project, RDR or repo template ``saved`` whatever tags its author wrote
  (nexus-0vh80; a row seeded before that, which still says ``curated``, reads curated until ``nx plan reseed``
  rewrites it);
* ``saved``: a declaration that validates and says ``saved``, with any tags except ``grown`` (the grown tag makes it
  unattested, as for any declaration but a grown one);
* ``grown``: a declaration that validates and says ``grown``. The ``grown`` TAG alone is not attestation
  (nexus-3ihve, Sam's nexus-rflvp rule: no declaration means unattested): a row with the tag and no valid
  grown declaration, a curated one included, reads ``unattested`` (plan 517, tagged before declarations
  existed, is such a row). The tag still makes a row protected from a reseed (:data:`PROTECTED_TAGS`). A
  ``curated`` declaration on a row without the builtin tag is not trusted: it reads as grown
  with kind ``unknown`` (``untrusted_curated_claim``). A grown plan's recorded kind counts only when its
  route is in :data:`TRUSTED_ROUTES`, which ships empty. Every recorded stamp (``explicit``,
  ``heuristic``, ``model-assisted``) is the request's label for the kind, not a property of the plan's steps
  (an ``explicit`` hint is the caller's, and the planner builds its steps without it), and the RDR relaxes
  the unknown-kind restriction for recorded provenance only "after review": until a reviewed decision
  (nexus-jypmj.2.8) adds a route, a grown plan reads as grown with kind ``unknown`` (``unreviewed_route``).
  The route is still recorded at the grow site; only eligibility discounts it. A discounted recorded kind
  never widens eligibility, but it does narrow it for a ``lookup`` request (nexus-indyo,
  ``grown_route_not_lookup``): a grown plan that recorded any kind other than lookup was grown by a request
  that was not a lookup, so its steps were planned for something else, the H09 cost shape the lookup row
  exists to keep out (nexus-jypmj.11). This is a guard, not a measured effect: replaying the 77
  lookup runs since the gate was enabled against the live library (2026-11-03) changed no selection only
  because none of its 5 grown plans matched a lookup; the one live case (run 775 took grown plan 584, a
  non-answer) cannot be replayed, the row being deleted. Two more guards (nexus-a4g2a, halcyon 2026-11-11):
  a grown plan that recorded no kind serves no lookup or listing (``grown_route_unrecorded``, or the row's
  own defect flag), and a ``discovery/listing`` request takes only a grown plan that recorded discovery
  (``grown_route_not_discovery``). Neither was measured (5 grown plans, none accruing); both only narrow, so
  the worst case is an inline replan, and a good hit can be lost (grown plan 585 served discovery run 794
  well). A grown plan that recorded lookup still serves lookups, mislabelled or not (open in nexus-a4g2a);
* ``unattested``: nothing valid was declared. It is neither curated nor grown, its kind is ``unknown``, and
  every request kind refuses it (nexus-rflvp: ``unattested_plan`` for ``lookup`` and ``discovery/listing``,
  the reasons it always had for the others). A builtin seeded before nexus-jypmj.2.1 is this until
  ``nx plan reseed`` (or the upgrade ladder) writes its declaration; the exclusion reason is then
  ``undeclared_builtin``, and the library precondition below keeps the whole gate in shadow while any
  reseedable builtin is undeclared, so such a builtin keeps its lookups until the reseed (an orphan builtin no
  reseed can fix is not counted by that check, and loses them). Plan save writes a ``saved`` declaration now
  (nexus-0vh80), so a plan saved before that stays unattested until it is saved again with a kind.

The session cache's ``answer_provenance`` metadata is not read here: it is stale until a refresh.

The whole filter sits behind a setting read at call time, :func:`eligibility_enforced`: the environment
variable ``NX_ANSWER_KIND_ELIGIBILITY`` (``0``, ``false``, ``off`` or ``no`` is the kill switch; unset,
blank or anything else enforces), over the default :data:`ANSWER_KIND_ELIGIBILITY_ENABLED`, which ships
``True`` (nexus-jypmj.2.8 turned it on after thresholds were set from the measurement). In shadow mode an
:class:`EligibilityFilter` still judges and counts every candidate, so the run envelope can say what the
gate WOULD exclude, but :meth:`EligibilityFilter.admit` keeps every candidate and the matcher's output,
metrics and over-fetch are identical to a call that passed no filter.

The gate is not enforced in four cases that are not the setting (RDR-232 section 1: such a builtin reads as
unattested until a reseed, and the gate is not enabled before every builtin carries its declaration; the
measured failing regime is the gate with deterministic kinds only). ``nx_answer`` runs the request's filter
in shadow (built so, or degraded with :meth:`EligibilityFilter.degrade_to_shadow`) when (1) the structured
request extraction is switched off (``extraction_disabled``), (2) this request's extraction call failed
(``extraction_failed``: a timeout, an error or an answer outside the schema; nexus-x3eoc, Sam 2026-10-31),
(3) the call was needed but skipped for lack of time budget (``extraction_skipped``, Sam 2026-10-31), or (4)
the plan library holds a builtin template, a row a reseed can fix, with no declaration
(``library_not_reseeded``). In (2) and (3) the request keeps its first-pass kind, the regime that failed the
held-out measurement, so the answer-kind rows report what they would exclude and exclude nothing; the next
request is unaffected, and ``request_coverage.extraction.outcome`` in the envelope names which. The one-source
rule above is NOT one of those rows: it keeps excluding under (2) and (3) (Sam 2026-10-31), because its reason
is exactly that a failed extraction leaves a single-source request unknown. The envelope says so:
``enforced`` is false, ``shadow_reason`` names the case, and ``enforced_reasons`` lists the exclusion reasons
that still exclude (``["single_source_synthesis"]``; empty in every other case). The library check counts only
builtin-tagged rows of the global tier whose
dimensions match a shipped template (or a retired one, which a reseed deletes), so an orphan the seed loader
leaves in place cannot hold the library in shadow for good, and it reports how many such rows it examined
(``builtins_checked``): a library with none enforces, and ``builtins_checked`` of 0 says the check had nothing
to examine. The upgrade ladder's plan-library axis reconciles builtins (``seed_plan_templates(reconcile=True)``)
on upgrade, which writes the declarations, so an upgraded install enforces. Precedence when several apply:
``disabled_by_setting``, then ``extraction_disabled``, then ``extraction_failed``, then ``extraction_skipped``
(only one of the two applies to a request), then ``library_not_reseeded`` (a filter already in shadow
is not checked against the library).

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

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from nexus.plans.answer_kind import (
    SHAPE_VETOED_KINDS,
    PlanAnswerKind,
    decode_declaration,
    terminal_is_retrieval_only,
)
from nexus.plans.switches import OFF_VALUES, warn_unrecognised

_log = structlog.get_logger(__name__)

__all__ = [
    "ANSWER_KIND_ELIGIBILITY_ENABLED",
    "ANSWER_KIND_ELIGIBILITY_ENV",
    "BRANCHES",
    "BUILTIN_TAG",
    "GROWN_TAG",
    "LIBRARY_CHECKS",
    "LIBRARY_CHECK_TTL_S",
    "LIBRARY_REMEDY",
    "PROTECTED_TAGS",
    "REASONS",
    "RESERVED_PROVENANCE_TAGS",
    "SHADOW_REASONS",
    "SINGLE_SOURCE_EXCLUDED_STRATEGIES",
    "TRUSTED_ROUTES",
    "BuiltinCheck",
    "EligibilityFilter",
    "PlanStanding",
    "Verdict",
    "classify_plan",
    "coerce_kind",
    "check_builtins",
    "declared_strategy",
    "project_names",
    "eligibility",
    "eligibility_enforced",
    "eligible_matches",
    "fallback_reason",
    "memoized_library_check",
    "reseedable_dimensions",
    "strip_reserved_tags",
    "reset_library_memo",
    "warn_library_not_reseeded",
]

#: The default of the gate (nexus-jypmj.2.8: ``True``). Off: the filter reports what it would exclude and
#: excludes nothing, the planner's few-shots are as before. On: ineligible candidates are removed from every
#: matcher branch and from the planner's few-shots. Read at call time through the module (by
#: :func:`eligibility_enforced`), so a test can flip it; the environment variable overrides it.
ANSWER_KIND_ELIGIBILITY_ENABLED: bool = True

#: The kill switch. ``0``, ``false``, ``off`` or ``no`` (any case) means shadow; unset, blank or any other
#: value defers to :data:`ANSWER_KIND_ELIGIBILITY_ENABLED` (enforce).
ANSWER_KIND_ELIGIBILITY_ENV = "NX_ANSWER_KIND_ELIGIBILITY"

#: ``shadow_reason`` values the envelope reports (and the only ones :meth:`EligibilityFilter.degrade_to_shadow`
#: accepts): the setting turned the gate off, the structured request extraction is switched off, this request's
#: extraction call failed (nexus-x3eoc) or was skipped for lack of time (Sam 2026-10-31), or the library still
#: holds builtins without their declaration.
SHADOW_REASONS: frozenset[str] = frozenset(
    {"disabled_by_setting", "extraction_disabled", "extraction_failed", "extraction_skipped", "library_not_reseeded"}
)

#: The exclusion reasons that stay ENFORCED while the filter is in shadow, by ``shadow_reason`` (Sam 2026-10-31,
#: nexus-x3eoc ruling A): the one-source rule keeps excluding citation-traversal when the extraction failed or was
#: skipped, because a request whose extraction did not run is one the rule exists for (it leaves a single-source
#: request ``unknown``). Every other exclusion in those shadows is reported and not enforced.
SHADOW_ENFORCED_REASONS: dict[str, frozenset[str]] = {
    "extraction_failed": frozenset({"single_source_synthesis"}),
    "extraction_skipped": frozenset({"single_source_synthesis"}),
}

#: ``library_check`` values the envelope reports: ``checked`` (the library was read and counted) or
#: ``unreadable`` (it was not: no count in the cache and the listing failed). ``null`` means no check ran
#: (the filter was already in shadow, or no plan match ran).
LIBRARY_CHECKS: frozenset[str] = frozenset({"checked", "unreadable"})

#: At most this many undeclared rows are named in the envelope and in the warning.
_MAX_LISTED_NAMES = 8


def eligibility_enforced(environ: Mapping[str, str]) -> bool:
    """Whether the answer-kind gate enforces for a request, decided now from *environ*.

    Unset or blank: :data:`ANSWER_KIND_ELIGIBILITY_ENABLED` (``True``). An off value (``0``, ``false``,
    ``off``, ``no``): shadow. Anything else: enforce, and a value outside both vocabularies logs one warning
    per process (nexus-jypmj.2.8).
    """
    raw = str(environ.get(ANSWER_KIND_ELIGIBILITY_ENV, "")).strip().lower()
    if not raw:
        return ANSWER_KIND_ELIGIBILITY_ENABLED
    if raw in OFF_VALUES:
        return False
    warn_unrecognised(ANSWER_KIND_ELIGIBILITY_ENV, raw)
    return True


#: What clears ``library_not_reseeded`` (nexus-jypmj.16). ``nx plan reseed`` rewrites every builtin it can, and
#: that is the whole remedy for a library seeded before the declarations. Three things leave a row undeclared
#: through a reseed, all reported by the reseed itself (``seed_plan_templates`` echoes them): a template the
#: installed conexus rejects (a ``seed load error``; the row is never written, so only a build whose template
#: validates clears it), a template whose description another row already holds (``not reconciled``; the seed
#: loader refuses to overwrite that row, so it clears when that row is deleted, after a look at it, since it can
#: be a grown plan) and a ``not reconciled`` whose reason is the page cap (the seed loader cannot prove a
#: description unused when the project's plan listing is full, so it refuses every write there until the listing
#: fits). The words in quotes are the reseed's own; tests/test_rdr232_library_remedy.py runs the seed loader
#: through each case.
LIBRARY_REMEDY = (
    "nx plan reseed. If the gate stays in shadow, read the reseed's warnings: a 'seed load error' for a template "
    "means the installed conexus rejects it, and only an upgraded conexus clears it; a 'not reconciled' warning "
    "(description already belongs to library row id=N) clears when row N is deleted and nx plan reseed is run "
    "again, but inspect row N first with nx plan show N, because it can be a plan someone grew and a delete cannot "
    "be undone, and run nx plan delete N only when it is not; a 'not reconciled' warning that says the plan listing "
    "hit its page cap is not about one row: the project holds more plan rows than one listing returns, so find the "
    "ones you no longer need with nx plan list, delete them with nx plan delete, and run nx plan reseed again."
)

_warned_libraries: set[tuple[int, tuple[str, ...]]] = set()


def warn_library_not_reseeded(check: "BuiltinCheck") -> None:
    """Log the not-reseeded warning once per process for each distinct (count, names) (it recurs on every request)."""
    key = (check.undeclared, check.undeclared_names)
    if key in _warned_libraries:
        return
    _warned_libraries.add(key)
    _log.warning(
        "answer_kind_gate_shadowed_library_not_reseeded",
        undeclared_builtins=check.undeclared,
        builtins_checked=check.checked,
        undeclared_builtin_names=list(check.undeclared_names),
        remedy=LIBRARY_REMEDY,
    )


#: The row tags provenance reads. ``BUILTIN_TAG`` is ``matcher._CATEGORY_PLAN_TAG``; a test pins that.
BUILTIN_TAG = "builtin-template"
GROWN_TAG = "grown"

#: Tags only a writer that attests may set: the grow site stamps ``grown`` with a grown declaration, the seed
#: loader stamps ``builtin-template`` with a curated one. ``plan_save`` removes both from a caller's tags
#: (nexus-3ihve); the writers call the library directly and are not affected. ``ad-hoc`` is not here: it
#: reads as nothing in :func:`classify_plan`.
RESERVED_PROVENANCE_TAGS: frozenset[str] = frozenset({BUILTIN_TAG, GROWN_TAG})


def strip_reserved_tags(tags: str | None) -> tuple[str, tuple[str, ...]]:
    """*tags* without :data:`RESERVED_PROVENANCE_TAGS`, and the reserved tags removed (in order, once each).

    Other tags keep their order; the result is comma-joined with each token stripped, so a tag string with
    no reserved tag in it comes back as its tokens, normalised, never reworded.
    """
    kept: list[str] = []
    removed: list[str] = []
    for token in (t.strip() for t in (tags or "").split(",")):
        if not token:
            continue
        if token in RESERVED_PROVENANCE_TAGS:
            if token not in removed:
                removed.append(token)
        else:
            kept.append(token)
    return ",".join(kept), tuple(removed)


#: The matcher branches that produce candidates.
BRANCHES: tuple[str, ...] = ("cosine", "category_route", "fts")

#: Why a plan was refused. ``undeclared_builtin`` and ``untrusted_curated_claim`` name the stored row's
#: defect and replace the matrix reason when the row has one. ``unattested_plan`` (nexus-rflvp) is the matrix
#: reason for a plan that declares nothing, asked to serve a lookup or a listing. ``saved_*`` (nexus-0vh80) are the
#: saved column's: an unknown kind, another project's (or no) saving project, a retrieval-only plan claiming an
#: answering kind.
REASONS: frozenset[str] = frozenset({
    "kind_mismatch",
    "unknown_request_untrusted_plan",
    "unknown_request_narrow_plan",
    "untrusted_unknown_plan",
    "undeclared_builtin",
    "untrusted_curated_claim",
    "unreviewed_route",
    "single_source_synthesis",
    "grown_route_not_lookup",
    "grown_route_unrecorded",
    "grown_route_not_discovery",
    "unattested_plan",
    "saved_unknown_plan",
    "saved_other_project",
    "saved_retrieval_only",
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
    _L: frozenset({_L}),  # lookup plans only: Sam, 2026-10-30 (nexus-jypmj.11), RDR-232 section 1
    _S: frozenset({_S}),
    _D: frozenset({_D}),
    _C: frozenset({_C, _S}),
    _A: frozenset({_A, _S}),
    _B: frozenset({_B, _S}),
}

#: The ``strategy`` dimensions of curated plans that a synthesis or unknown request naming exactly one source does
#: not admit (nexus-4xmpv; Sam, 2026-10-30). ``citation-traversal`` (``conexus/plans/builtin/citation-traversal.yml``,
#: dimensions ``verb: research``, ``strategy: citation-traversal``) traces the references AROUND a document; a
#: request that names one source and asks for its parts to be put together needs that document read. Matched on
#: the declared dimension, never on a plan id. ``abstract-themes`` is NOT listed: Sam kept it allowed on 2026-10-30.
SINGLE_SOURCE_EXCLUDED_STRATEGIES: frozenset[str] = frozenset({"citation-traversal"})

#: Request kinds the single-source rule applies to: synthesis, and unknown (an extraction failure leaves a
#: single-source synthesis request unknown, and the unknown row admits curated synthesis plans).
_SINGLE_SOURCE_REQUESTS = frozenset({_S, _U})

#: Request kinds a GROWN unknown-kind plan may still serve. An unattested plan serves none (nexus-rflvp).
_UNKNOWN_PLAN_REQUESTS = frozenset({_L, _D})

#: The refusals a grown plan's recorded kind caused, counted per recorded kind (``refused_recorded_kinds``):
#: the lookup narrowing (nexus-indyo, nexus-q7dpt) and the listing narrowing (nexus-a4g2a).
_RECORDED_KIND_REASONS = frozenset({"grown_route_not_lookup", "grown_route_not_discovery"})


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
    EFFECTIVE kind: ``unknown`` when the declared one was discounted. ``recorded_kind`` is the kind a grown plan
    recorded when that kind was discounted (``unreviewed_route``); it never widens eligibility, it only narrows
    it (the lookup rule, nexus-indyo). ``None`` everywhere else.
    """

    kind: PlanAnswerKind
    provenance: str  # "curated" | "grown" | "saved" | "unattested"
    flag: str | None = None
    route: str | None = None
    recorded_kind: PlanAnswerKind | None = None


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
        # TAG with a curated or absent declaration attests nothing (nexus-3ihve): the tag is claimable text,
        # the declaration is what a writer stamps. It reads unattested, so it serves no request kind.
        if declared.provenance != "grown":
            return PlanStanding(_U, "unattested")
        if declared.kind is _U:
            return PlanStanding(_U, "grown")
        if declared.route in TRUSTED_ROUTES:
            return PlanStanding(declared.kind, "grown", route=declared.route)
        # the request's guessed kind is not the plan's shape: read it as unknown until reviewed
        return PlanStanding(_U, "grown", "unreviewed_route", route=declared.route, recorded_kind=declared.kind)
    if declared.provenance == "saved":
        # nexus-0vh80: the saver's own claim, whatever else the tags say (a project-tier template may carry the
        # builtin tag; the declaration is what the seed loader stamped). Eligibility narrows it.
        return PlanStanding(declared.kind, "saved")
    if declared.provenance == "curated":
        if BUILTIN_TAG in tokens:
            return PlanStanding(declared.kind, "curated")
        return PlanStanding(_U, "grown", "untrusted_curated_claim")
    return PlanStanding(_U, "unattested", "undeclared_builtin" if BUILTIN_TAG in tokens else None)


@dataclass(frozen=True)
class BuiltinCheck:
    """What the library precondition found: builtin rows a reseed can fix, and how many lack their declaration.

    ``checked`` is the number of such rows examined (declared or not), so a count of zero with ``checked`` of 0
    reads as "nothing to examine", not "examined and clean". ``undeclared_names`` are at most
    :data:`_MAX_LISTED_NAMES` labels of the undeclared ones, each ``<name> (id <n>)`` or, for a row with no name,
    ``id <n>`` (several builtin rows share a name, so the name alone would repeat).
    """

    checked: int
    undeclared: int
    undeclared_names: tuple[str, ...] = ()


#: Tags that make a row user-grown: the seed loader's reconcile refuses to rewrite one
#: (``seed_loader._GROWN_TAGS``; a test pins that these are the same).
PROTECTED_TAGS: frozenset[str] = frozenset({GROWN_TAG, "ad-hoc"})

_reseedable: frozenset[str] | None = None


def reseedable_dimensions() -> frozenset[str] | None:
    """The canonical dimensions a reseed writes or deletes at the global tier; ``None`` if they cannot be read.

    The shipped builtin templates (read from the directory ``nx plan reseed`` itself loads, the same way
    ``nx doctor --check-plan-library`` reads it, scope forced to ``global``) plus the retired templates the
    reconcile deletes (``seed_loader.RETIRED_TEMPLATE_DIMENSIONS``). A builtin-tagged row at any other
    dimensions is an orphan: the seed loader leaves it in place, so a reseed cannot give it a declaration.
    Read once per process; a failure is not remembered.
    """
    global _reseedable
    if _reseedable is not None:
        return _reseedable
    try:
        import yaml  # noqa: PLC0415 — deferred; only the library check needs the templates

        from nexus.commands.catalog import _resolve_plugin_root  # noqa: PLC0415 — deferred; same resolver the reseed uses
        from nexus.indexer_utils import find_repo_root  # noqa: PLC0415 — deferred, as above
        from nexus.plans.schema import canonical_dimensions_json  # noqa: PLC0415 — deferred, as above
        from nexus.plans.seed_loader import RETIRED_TEMPLATE_DIMENSIONS  # noqa: PLC0415 — deferred, as above

        repo_root = find_repo_root(Path.cwd()) or Path.cwd()
        directory = _resolve_plugin_root(repo_root) / "plans" / "builtin"
        dims: set[str] = set()
        for path in sorted(directory.iterdir()):
            if not (path.is_file() and path.suffix in (".yml", ".yaml")):
                continue
            template = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            own = dict(template.get("dimensions") or {})
            own["scope"] = "global"  # the global tier's scope override
            dims.add(canonical_dimensions_json(own))
    except Exception as exc:  # noqa: BLE001 — the library check degrades to "unreadable", it never fails a request
        _log.warning("answer_kind_gate_shipped_templates_unreadable", error=f"{type(exc).__name__}: {exc}"[:200])
        return None
    if not dims:
        _log.warning("answer_kind_gate_shipped_templates_unreadable", error="no templates found")
        return None
    _reseedable = frozenset(dims) | RETIRED_TEMPLATE_DIMENSIONS
    return _reseedable


def _row_label(row: Mapping[str, Any]) -> str:
    """``<name> (id <n>)``, or ``id <n>`` for a row with no name (builtin rows can share a name)."""
    name = row.get("name")
    return f"{name} (id {row.get('id')})" if name else f"id {row.get('id')}"


def check_builtins(rows: Iterable[Mapping[str, Any]]) -> BuiltinCheck | None:
    """Examine the builtin rows of *rows* that a reseed can fix, and count those without a valid declaration.

    A row is examined when it carries the builtin tag, no grown tag, sits at the global tier (empty project) and
    its dimensions are a shipped or retired template's (:func:`reseedable_dimensions`). It is undeclared when
    :func:`classify_plan` flags it ``undeclared_builtin``, the judgement the filter itself makes. A row that
    cannot be read is not examined. ``None`` when the shipped templates cannot be read (the check is unknown).
    """
    fixable = reseedable_dimensions()
    if fixable is None:
        return None
    checked = 0
    undeclared = 0
    names: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        tags = row.get("tags")
        tokens = {t.strip() for t in tags.split(",") if t.strip()} if isinstance(tags, str) else set()
        if BUILTIN_TAG not in tokens or tokens & PROTECTED_TAGS:
            continue
        # str(): a dimensions value that is not a string (a dict or list is unhashable) is simply not a shipped
        # template's canonical JSON, so the row is not examined; it must not raise.
        if (row.get("project") or "") != "" or str(row.get("dimensions")) not in fixable:
            continue
        try:
            standing = classify_plan(tags, row.get("plan_json"))
        except Exception:  # noqa: BLE001 — an unreadable row is examined nowhere; the filter reports it as judge_error
            continue
        checked += 1
        if standing.flag == "undeclared_builtin":
            undeclared += 1
            if len(names) < _MAX_LISTED_NAMES:
                names.append(_row_label(row))
    return BuiltinCheck(checked, undeclared, tuple(names))


#: How long a library check made without the plan cache is reused. The cache refreshes on a 90 s bound; a
#: process without one lists the library at most this often instead of on every request.
LIBRARY_CHECK_TTL_S = 90.0

_memo: tuple[float, BuiltinCheck | None] | None = None


def memoized_library_check(
    read: Callable[[], BuiltinCheck | None], *, now: Callable[[], float] = time.monotonic,
) -> BuiltinCheck | None:
    """``read()``, reused for :data:`LIBRARY_CHECK_TTL_S` seconds; an unreadable result (``None``) is reused too.

    Two requests that both find the memo stale both read and the later write wins: harmless, the result is a
    bounded-staleness count either way. The memo is process-wide state; :func:`reset_library_memo` clears it.
    """
    global _memo
    memo = _memo
    if memo is not None and now() - memo[0] < LIBRARY_CHECK_TTL_S:
        return memo[1]
    result = read()
    _memo = (now(), result)
    return result


def reset_library_memo() -> None:
    """Forget the memoized library check (a test hook; a reseed is seen within the bound without it)."""
    global _memo
    _memo = None


def eligibility(request_kind: PlanAnswerKind, plan_kind: PlanAnswerKind, provenance: str) -> Verdict:
    """May a plan of *plan_kind* and *provenance* serve a request of *request_kind*? (the matrix)

    *provenance* is ``curated``, ``grown``, ``saved`` or ``unattested``. An ``unattested`` plan declared nothing, so
    whatever kind is passed with it is read as ``unknown``. A ``saved`` plan serves only the request of its own
    known kind (nexus-0vh80); its project and shape are judged by :func:`_judge`, which has the row.
    """
    if provenance == "unattested":
        plan_kind = _U
    if request_kind is _U:
        if provenance != "curated":
            return Verdict(False, "unknown_request_untrusted_plan")
        if plan_kind not in (_S, _U):
            return Verdict(False, "unknown_request_narrow_plan")
        return _OK
    if provenance == "saved":
        if plan_kind is _U:
            return Verdict(False, "saved_unknown_plan")
        return _OK if plan_kind is request_kind else Verdict(False, "kind_mismatch")
    if plan_kind is not _U:
        return _OK if plan_kind in _COMPATIBLE[request_kind] else Verdict(False, "kind_mismatch")
    if provenance == "curated":
        return _OK
    if request_kind in _UNKNOWN_PLAN_REQUESTS:
        if provenance == "unattested":
            return Verdict(False, "unattested_plan")  # nexus-rflvp: it declared nothing, so it serves nothing
        return _OK
    return Verdict(False, "untrusted_unknown_plan")


def declared_strategy(dimensions: Any) -> str | None:
    """The ``strategy`` a plan's stored dimensions declare (a JSON object string or a mapping); ``None`` if none."""
    if isinstance(dimensions, str):
        try:
            dimensions = json.loads(dimensions)
        except ValueError:
            return None
    if isinstance(dimensions, Mapping):
        value = dimensions.get("strategy")
        return value if isinstance(value, str) and value else None
    return None


def project_names(value: str | Iterable[str] | None) -> frozenset[str]:
    """The non-blank, whitespace-stripped project names in *value* (one name or several)."""
    if value is None:
        return frozenset()
    items = [value] if isinstance(value, str) else list(value)
    return frozenset(t for t in (i.strip() for i in items if isinstance(i, str)) if t)


def _judge(
    request_kind: PlanAnswerKind, tags: str | None, plan_json: Any, dimensions: Any = None, named_source_count: int = 0,
    *, plan_project: Any = None, request_project: str | Iterable[str] = "",
) -> tuple[PlanStanding, Verdict]:
    standing = classify_plan(tags, plan_json)
    verdict = eligibility(request_kind, standing.kind, standing.provenance)
    if not verdict.eligible and standing.flag is not None:
        verdict = Verdict(False, standing.flag)
    if verdict.eligible and standing.provenance == "saved":
        # nexus-0vh80: a saved plan serves the project that saved it, compared exactly after stripping whitespace
        # (the request has one name, or two when the session is in a linked worktree: its own directory and the
        # primary checkout's); no project on either side means there is no saving project to match. Then the shape
        # veto: a plan that ends in retrieval cannot claim an answering kind.
        saving = plan_project.strip() if isinstance(plan_project, str) else ""
        if not saving or saving not in project_names(request_project):
            verdict = Verdict(False, "saved_other_project")
        elif standing.kind in SHAPE_VETOED_KINDS and terminal_is_retrieval_only(plan_json):
            verdict = Verdict(False, "saved_retrieval_only")
    if (
        verdict.eligible and request_kind in _SINGLE_SOURCE_REQUESTS and named_source_count == 1 and standing.provenance == "curated"
        and declared_strategy(dimensions) in SINGLE_SOURCE_EXCLUDED_STRATEGIES
    ):
        verdict = Verdict(False, "single_source_synthesis")
    if (
        verdict.eligible and request_kind is _L and standing.recorded_kind is not None
        and standing.recorded_kind is not _L
    ):
        verdict = Verdict(False, "grown_route_not_lookup")
    if verdict.eligible and standing.provenance == "grown" and request_kind in _UNKNOWN_PLAN_REQUESTS:
        # nexus-a4g2a (halcyon, 2026-11-11, rule 6): a guard, not a measured effect. A grown plan that recorded no
        # kind serves no lookup or listing (its steps were planned for a request nothing classified), and a
        # listing takes only a grown plan that recorded discovery, as the matrix gives listings listing plans.
        # It only narrows: the worst case is an inline replan. Counted by reason (and recorded kind) to show
        # its cost as grown plans accrue.
        if standing.recorded_kind is None:
            verdict = Verdict(False, standing.flag or "grown_route_unrecorded")
        elif request_kind is _D and standing.recorded_kind is not _D:
            verdict = Verdict(False, "grown_route_not_discovery")
    return standing, verdict


def _safe_judge(
    request_kind: PlanAnswerKind, tags: Any, plan_json: Any, dimensions: Any = None, named_source_count: int = 0,
    *, plan_project: Any = None, request_project: str | Iterable[str] = "",
) -> tuple[PlanStanding, Verdict]:
    """:func:`_judge` that never raises: a fault is the ``judge_error`` verdict on an unattested standing.

    Callers on the production path (the matcher, ``nx_answer``) use this one, so a judging fault with the
    gate off cannot fail ``plan_match``; the fault is logged and counted under ``judge_error``.
    """
    try:
        return _judge(
            request_kind, tags, plan_json, dimensions, named_source_count,
            plan_project=plan_project, request_project=request_project,
        )
    except Exception:  # noqa: BLE001 — a judging fault must never fail the caller; logged and counted
        _log.warning("eligibility_judge_failed", exc_info=True)
        return PlanStanding(_U, "unattested", "judge_error"), Verdict(False, "judge_error")


def eligible_matches(
    request_kind: PlanAnswerKind, matches: Iterable[Any] | None, named_source_count: int = 0,
    request_project: str | Iterable[str] = "",
) -> list[Any]:
    """*matches* without the plans a request of *request_kind* may not use (quiet: no counting).

    *named_source_count* is how many sources the request names (the single-source synthesis rule reads it).
    *request_project* is the request's project name, or its names (the session's directory and the primary
    checkout's): a saved plan is kept only when its own project equals one of them.
    """
    kept = []
    for m in matches or ():
        _, verdict = _safe_judge(
            request_kind, getattr(m, "tags", None), getattr(m, "plan_json", None),
            getattr(m, "dimensions", None), named_source_count,
            plan_project=getattr(m, "project", None), request_project=request_project,
        )
        if verdict.eligible:
            kept.append(m)
    return kept


class EligibilityFilter:
    """Judges matcher candidates for one request and keeps the account the envelope reports.

    Mutable on purpose: the matcher fills the branch counts, ``nx_answer`` adds the returned summary and
    the fallback reason. With ``enforce`` False it judges and counts and admits everything, except the reasons
    in :attr:`enforced_reasons`.
    """

    def __init__(
        self, request_kind: PlanAnswerKind | str, decision: str = "unknown", enforce: bool = False,
        shadow_reason: str | None = None, named_source_count: int = 0, project: str = "",
        project_aliases: Iterable[str] = (),
    ) -> None:
        self.request_kind = coerce_kind(request_kind)
        #: The request's project (nexus-0vh80): a saved plan serves only a request from the project that saved it.
        #: ``nx_answer`` passes the session's directory name, the name a grown plan is filed under, and in
        #: *project_aliases* the primary checkout's name (a session in a linked worktree). Blank names count for
        #: nothing; with none left no saved plan is served.
        self.project = project
        self.request_projects = project_names(project) | project_names(project_aliases)
        #: How many distinct sources the request names (nexus-4xmpv): exactly one switches the single-source rule on.
        self.named_source_count = named_source_count
        self.decision = decision
        self.enforce = enforce
        if shadow_reason is not None and shadow_reason not in SHADOW_REASONS:
            raise ValueError(f"shadow_reason {shadow_reason!r} is not one of {sorted(SHADOW_REASONS)}")
        self.shadow_reason = shadow_reason
        # The library precondition's result (note_library); None until a check has run.
        self.library_check: str | None = None
        self.builtins_checked: int | None = None
        self.undeclared_builtins: int | None = None
        self.undeclared_builtin_names: list[str] = []
        self._branches: dict[str, dict[str, Any]] = {
            b: {"examined": 0, "excluded": 0, "reasons": {}} for b in BRANCHES
        }
        self._judged: dict[int, tuple[PlanStanding, Verdict]] = {}
        self._excluded_ids: list[int] = []
        self._enforced_excluded = 0
        #: nexus-q7dpt: per recorded kind, the ``grown_route_not_lookup`` exclusions (counted like ``excluded``).
        self._refused_recorded_kinds: dict[str, int] = {}
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
            row.get("dimensions") if isinstance(row, Mapping) else None, self.named_source_count,
            plan_project=row.get("project") if isinstance(row, Mapping) else None, request_project=self.request_projects,
        )
        if isinstance(plan_id, int):
            self._judged[plan_id] = (standing, verdict)
        if verdict.eligible:
            return True
        counts["excluded"] += 1
        counts["reasons"][verdict.reason] = counts["reasons"].get(verdict.reason, 0) + 1
        if verdict.reason in _RECORDED_KIND_REASONS and standing.recorded_kind is not None:
            kind = standing.recorded_kind.value
            self._refused_recorded_kinds[kind] = self._refused_recorded_kinds.get(kind, 0) + 1
        if isinstance(plan_id, int) and plan_id not in self._excluded_ids:
            self._excluded_ids.append(plan_id)
        if self.enforce or verdict.reason in self.enforced_reasons:
            self._enforced_excluded += 1
            return False
        return True

    @property
    def enforced_reasons(self) -> tuple[str, ...]:
        """The exclusion reasons that exclude although ``enforce`` is False (:data:`SHADOW_ENFORCED_REASONS`): the
        one-source rule while the request's extraction failed or was skipped. Empty when the filter enforces
        everything (``enforce``) or enforces nothing."""
        if self.enforce:
            return ()
        return tuple(sorted(SHADOW_ENFORCED_REASONS.get(self.shadow_reason or "", ())))

    @property
    def enforces_any(self) -> bool:
        """Whether any candidate can be dropped: the filter enforces, or a reason stays enforced in its shadow."""
        return self.enforce or bool(self.enforced_reasons)

    @property
    def enforced_excluded_total(self) -> int:
        """Candidates :meth:`admit` actually dropped (judged ineligible AND enforced), over all branches."""
        return self._enforced_excluded

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
            return _safe_judge(
                self.request_kind, getattr(m, "tags", None), getattr(m, "plan_json", None),
                getattr(m, "dimensions", None), self.named_source_count,
                plan_project=getattr(m, "project", None), request_project=self.request_projects,
            )

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

    def degrade_to_shadow(self, reason: str) -> None:
        """Stop enforcing for this request and say why; judging and counting go on (call before the matcher runs).

        *reason* must be in :data:`SHADOW_REASONS`: the envelope's vocabulary is a closed set, and a reason
        nobody listed would reach the wire unreviewed.
        """
        if reason not in SHADOW_REASONS:
            raise ValueError(f"shadow_reason {reason!r} is not one of {sorted(SHADOW_REASONS)}")
        self.enforce = False
        self.shadow_reason = reason

    def note_library(self, check: BuiltinCheck | None) -> None:
        """Record the library precondition's result: the counts, or ``unreadable`` when there was none."""
        if check is None:
            self.library_check = "unreadable"
            return
        self.library_check = "checked"
        self.builtins_checked = check.checked
        self.undeclared_builtins = check.undeclared
        self.undeclared_builtin_names = list(check.undeclared_names)

    def to_wire(self) -> dict[str, Any]:
        return {
            "request_kind": self.request_kind.value,
            "kind_decision": self.decision,
            "enforced": self.enforce,
            "shadow_reason": self.shadow_reason,
            "enforced_reasons": list(self.enforced_reasons),
            "library_check": self.library_check,
            "builtins_checked": self.builtins_checked,
            "undeclared_builtins": self.undeclared_builtins,
            "undeclared_builtin_names": list(self.undeclared_builtin_names),
            "branches": {b: {**c, "reasons": dict(c["reasons"])} for b, c in self._branches.items()},
            "excluded_total": self.excluded_total,
            "enforced_excluded_total": self._enforced_excluded,
            "refused_recorded_kinds": dict(sorted(self._refused_recorded_kinds.items())),
            "excluded_plan_ids": self._excluded_ids[:_MAX_LISTED_IDS],
            "returned": dict(self._returned) if self._returned is not None else None,
            "selected": dict(self._selected) if self._selected is not None else None,
            "fallback_reason": self._fallback,
        }


def fallback_reason(
    *, force_dynamic: bool, matches: Sequence[Any], hit: bool, filt: EligibilityFilter | None,
) -> str | None:
    """Why ``nx_answer`` went to the inline planner; ``None`` when a library plan was selected.

    ``all_candidates_ineligible`` only when the gate refused something (``enforce``, or a reason that stays
    enforced in its shadow): a candidate that was only reported was kept, so exclusion is not why nothing matched.
    """
    if force_dynamic:
        return "force_dynamic"
    if hit:
        return None
    if not matches:
        if filt is not None and filt.enforced_excluded_total:
            return "all_candidates_ineligible"
        return "no_plan_candidates"
    return "below_confidence_floor"
