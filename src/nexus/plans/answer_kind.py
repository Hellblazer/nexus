# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Plan answer-kind declaration — RDR-232 Phase 2 (nexus-jypmj.2.1).

A plan states the kind of answer it can serve in a validated, versioned
top-level ``plan_json`` field named ``answer_kind``. The field is never part
of ``dimensions``: canonical dimensions are plan identity and the database
uniqueness key, so a kind there would fork plans and break seed
reconciliation.

``PlanAnswerKind`` is the *routing capability* of a plan. It is a different
type from :class:`nexus.plans.answer_shape.AnswerShape`, which classifies the
terminal string a run produced. The two share no member names on purpose.

Stored shape (``ANSWER_KIND_VERSION`` 1)::

    {"version": 1, "kind": "lookup", "provenance": "curated"}
    {"version": 1, "kind": "synthesis", "provenance": "grown", "route": "heuristic"}
    {"version": 1, "kind": "unknown", "provenance": "grown"}

* ``curated`` is a reviewed template (the builtin YAMLs declare it with a
  template-level ``answer_kind: <kind>`` that the seed loader folds in).
* ``grown`` is a plan the inline planner produced. It records a kind only
  when the request route that produced it was known, and ``route`` then
  says how that was decided (``explicit``, ``heuristic`` or ``model-assisted``,
  the RDR's three words). With no recorded route the kind is ``unknown``.

Who may write a declaration (nexus-jypmj.2.1 fix round). Only the seed loader
writes ``curated`` and only the ``nx_answer`` grow site writes ``grown``. The
``plan_save`` door STRIPS any embedded ``answer_kind`` before storing, so a
caller cannot forge either one; a row it saves declares nothing. Read paths
(``nx plan hygiene``, the matcher, the session cache) never refuse or retire a
row for its declaration: a bad one decodes to ``unknown``.

Provenance rule the eligibility step (``nexus.plans.eligibility``,
nexus-jypmj.2.3) applies, failing closed:

* a row is **curated** only if its tags carry ``builtin-template`` AND its
  declaration says ``curated``; a ``curated`` declaration on a row without the
  tag is not trusted (treat it as grown);
* a row is **grown** if its tags say ``grown`` OR its declaration says
  ``grown``, whichever is the less trusted reading;
* a **grown** row's recorded kind counts only with an ``explicit`` route: a
  ``heuristic`` or ``model-assisted`` stamp is the request's guess, and the RDR
  relaxes the restriction for recorded provenance only "after review", so
  eligibility reads such a row as ``unknown`` (``unreviewed_route``) until a
  reviewed decision widens ``eligibility.TRUSTED_ROUTES``;
* a row with no declaration is **unattested** (provenance ``None``): it is
  neither curated nor grown, and is eligible only where an ``unknown`` plan is;
* ``provenance: grown`` on an ``unknown`` kind records ORIGIN only. It is not
  the RDR's "recorded route provenance" (that is ``route``), and it never
  relaxes the unknown-plan restriction.

The session cache carries ``answer_kind``, ``answer_provenance`` and
``answer_route`` metadata beside each cached plan for that rule.

Decoding never raises and never reads the plan text: an absent, malformed,
wrongly versioned or unsupported declaration is ``unknown``. No migration
may guess a kind from the steps and promote it to a hard constraint. Nothing
in this module filters plans; ``nexus.plans.eligibility`` does (nexus-jypmj.2.3).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = [
    "ANSWER_KIND_KEY",
    "ANSWER_KIND_VERSION",
    "GOLDEN_LABEL_TO_KIND",
    "PROVENANCES",
    "ROUTE_SOURCES",
    "AnswerKindDeclaration",
    "AnswerKindError",
    "PlanAnswerKind",
    "decode_answer_kind",
    "decode_declaration",
    "declaration_payload",
    "golden_label_kind",
    "grown_plan_json",
    "strip_declaration",
    "validate_declaration",
]

#: The top-level ``plan_json`` key that holds the declaration.
ANSWER_KIND_KEY: str = "answer_kind"

#: The only declaration version this build reads. A different version
#: decodes to ``unknown`` until a versioned migration says otherwise.
ANSWER_KIND_VERSION: int = 1

#: Where a declaration came from: a reviewed template, or the inline planner.
PROVENANCES: frozenset[str] = frozenset({"curated", "grown"})

#: How a grown plan's route kind was decided (RDR-232: explicit, heuristic or
#: model-assisted). Required for a grown plan to record any kind but unknown.
ROUTE_SOURCES: frozenset[str] = frozenset({"explicit", "heuristic", "model-assisted"})


class PlanAnswerKind(str, Enum):
    """Closed starter vocabulary for a plan's routing capability (RDR-232 section 1).

    Not an answer-object-family registry. RDR-189 P2 may later define the
    durable families; these values then become derived capabilities of them
    through a versioned migration.
    """

    LOOKUP = "lookup"
    SYNTHESIS = "synthesis"
    DISCOVERY_LISTING = "discovery/listing"
    COMPARISON = "comparison"
    AUDIT = "audit"
    DEBUG = "debug"
    UNKNOWN = "unknown"


#: The golden-set request labels (``scripts/bench/schema.py`` ``ANSWER_KINDS``,
#: hand-assigned and never renamed) mapped to the plan vocabulary. They are the
#: same seven concepts; only the listing kind is spelled differently
#: (``discovery`` in the golden files, ``discovery/listing`` here). The mapping
#: is a bijection onto :class:`PlanAnswerKind`, ``unknown`` included.
GOLDEN_LABEL_TO_KIND: dict[str, PlanAnswerKind] = {
    "lookup": PlanAnswerKind.LOOKUP,
    "synthesis": PlanAnswerKind.SYNTHESIS,
    "discovery": PlanAnswerKind.DISCOVERY_LISTING,
    "comparison": PlanAnswerKind.COMPARISON,
    "audit": PlanAnswerKind.AUDIT,
    "debug": PlanAnswerKind.DEBUG,
    "unknown": PlanAnswerKind.UNKNOWN,
}


def golden_label_kind(label: str) -> PlanAnswerKind:
    """The :class:`PlanAnswerKind` a golden-set label names; ``KeyError`` for a label not in the map."""
    return GOLDEN_LABEL_TO_KIND[label]


class AnswerKindError(ValueError):
    """Raised by :func:`validate_declaration` for a declaration that is not well formed."""


@dataclass(frozen=True)
class AnswerKindDeclaration:
    """A decoded declaration. ``provenance`` is ``None`` when nothing valid was declared."""

    kind: PlanAnswerKind = PlanAnswerKind.UNKNOWN
    provenance: str | None = None
    route: str | None = None
    version: int | None = None


_ALLOWED_KEYS: frozenset[str] = frozenset({"version", "kind", "provenance", "route"})


def declaration_payload(
    kind: PlanAnswerKind, provenance: str, route: str | None = None,
) -> dict[str, Any]:
    """Build the stored declaration object (not validated; see :func:`validate_declaration`)."""
    payload: dict[str, Any] = {
        "version": ANSWER_KIND_VERSION, "kind": kind.value, "provenance": provenance,
    }
    if route is not None:
        payload["route"] = route
    return payload


def validate_declaration(raw: Any) -> AnswerKindDeclaration:
    """Return the declaration *raw* states, or raise :class:`AnswerKindError` naming the fault.

    Used where a declaration is being WRITTEN (template validation, plan
    save). Reading a stored row goes through :func:`decode_declaration`,
    which never raises.
    """
    if not isinstance(raw, dict):
        raise AnswerKindError(
            f"{ANSWER_KIND_KEY} must be an object, got {type(raw).__name__}"
        )
    extra = sorted(set(raw) - _ALLOWED_KEYS)
    if extra:
        raise AnswerKindError(f"{ANSWER_KIND_KEY} has unsupported field(s) {extra}")
    version = raw.get("version")
    if type(version) is not int or version != ANSWER_KIND_VERSION:
        raise AnswerKindError(
            f"{ANSWER_KIND_KEY}.version must be {ANSWER_KIND_VERSION}, got {version!r}"
        )
    kind_raw = raw.get("kind")
    try:
        kind = PlanAnswerKind(kind_raw)
    except (ValueError, TypeError):
        raise AnswerKindError(
            f"{ANSWER_KIND_KEY}.kind {kind_raw!r} is not one of "
            f"{sorted(k.value for k in PlanAnswerKind)}"
        ) from None
    provenance = raw.get("provenance")
    if not isinstance(provenance, str) or provenance not in PROVENANCES:
        raise AnswerKindError(
            f"{ANSWER_KIND_KEY}.provenance must be one of {sorted(PROVENANCES)}, got {provenance!r}"
        )
    route = raw.get("route")
    if provenance == "curated":
        if route is not None:
            raise AnswerKindError(f"{ANSWER_KIND_KEY}.route is only for grown plans")
    elif kind is not PlanAnswerKind.UNKNOWN:
        if not isinstance(route, str) or route not in ROUTE_SOURCES:
            raise AnswerKindError(
                f"{ANSWER_KIND_KEY}: a grown plan records kind {kind.value!r} only with route "
                f"provenance, one of {sorted(ROUTE_SOURCES)}, got {route!r}"
            )
    elif route is not None:
        raise AnswerKindError(f"{ANSWER_KIND_KEY}: a grown unknown carries no route")
    return AnswerKindDeclaration(
        kind=kind, provenance=provenance, route=route, version=version,
    )


def _as_plan(plan_json: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if isinstance(plan_json, dict):
        return plan_json
    if not isinstance(plan_json, str) or not plan_json:
        return None
    try:
        plan = json.loads(plan_json)
    except (TypeError, ValueError, RecursionError):
        return None
    return plan if isinstance(plan, dict) else None


def decode_declaration(plan_json: str | dict[str, Any] | None) -> AnswerKindDeclaration:
    """Decode the declaration in *plan_json*; anything absent or invalid is ``unknown``."""
    plan = _as_plan(plan_json)
    if plan is None or ANSWER_KIND_KEY not in plan:
        return AnswerKindDeclaration()
    try:
        return validate_declaration(plan[ANSWER_KIND_KEY])
    except AnswerKindError:
        return AnswerKindDeclaration()


def decode_answer_kind(plan_json: str | dict[str, Any] | None) -> PlanAnswerKind:
    """The kind *plan_json* declares, ``UNKNOWN`` when it declares none or an invalid one."""
    return decode_declaration(plan_json).kind


def strip_declaration(plan: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return *plan* without its ``answer_kind`` key, and whether a key was removed.

    The ``plan_save`` door uses this: a caller never declares a kind, so any
    embedded value (valid, malformed or an unrelated key of the same name) is
    dropped rather than refused, and an old plan carrying one still saves.
    """
    if ANSWER_KIND_KEY not in plan:
        return plan, False
    return {k: v for k, v in plan.items() if k != ANSWER_KIND_KEY}, True


def grown_plan_json(
    plan_json: str,
    *,
    route_kind: PlanAnswerKind | str | None,
    route_source: str | None,
) -> str:
    """Return *plan_json* with the grown-plan declaration the inline planner's output gets.

    The kind is recorded only when the route that produced the plan was
    known: *route_kind* a real kind (not ``UNKNOWN``) and *route_source* one
    of :data:`ROUTE_SOURCES`. Otherwise the plan is recorded ``unknown`` with
    grown provenance. A declaration already in the planner's output is
    replaced, never trusted. A *plan_json* that is not a JSON object comes
    back unchanged. A *route_kind* given as a string is coerced to
    :class:`PlanAnswerKind`; one outside the vocabulary is treated as no
    route, so a bad string from the caller records ``unknown`` instead of
    aborting the grow save.
    """
    plan = _as_plan(plan_json)
    if plan is None:
        return plan_json
    if route_kind is not None and not isinstance(route_kind, PlanAnswerKind):
        try:
            route_kind = PlanAnswerKind(route_kind)
        except (ValueError, TypeError):
            route_kind = None
    known = (
        route_kind is not None
        and route_kind is not PlanAnswerKind.UNKNOWN
        and isinstance(route_source, str)
        and route_source in ROUTE_SOURCES
    )
    stamped = dict(plan)
    stamped[ANSWER_KIND_KEY] = (
        declaration_payload(route_kind, "grown", route_source)  # type: ignore[arg-type]
        if known
        else declaration_payload(PlanAnswerKind.UNKNOWN, "grown")
    )
    return json.dumps(stamped)
