# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 3 step 2 (nexus-jypmj.3.2): the pure parts of the one coverage repair.

RDR-232 section 4, "If required coverage is missing": emit targeted retrieval queries for the missing facets
against the primary source and the explicit scope first; merge the results within the same evidence budgets;
recompute the ledger once; generate once with all covered evidence and the list of unresolved facets. The
repair has its own allowance inside the original monotonic deadline and one shared USD accumulator; when it
is unaffordable it is skipped. :meth:`~nexus.plans.coverage_ledger.CoverageBoundary.run` orchestrates;
this module holds the constants, the report, the query builder and the two affordability rules.

**The queries are deterministic and cost no model call.** A repair query is the facet's own text, which is
what the request asked for and what the first assessment found nothing for. The order is the RDR's: every
unsupported facet against the primary source's collections first (a named source is the strongest claim on
the answer), then against the corpora the plan's own retrieval steps used, which are already narrowed to the
caller's explicit scope. The targets are therefore never wider than the scope that governed the plan: a
primary collection the caller's scope excludes is not asked (a corpus scope by collection, a catalog-subtree scope
by the documents the named source holds), a subtree scope rides every query that can take one, a plan target
carries the plan step's own ``where`` / ``content_type`` / ``author`` filters, and a retrieved hit outside the
caller's scope is dropped (the coverage ledger does that). Contradicted facets are not repair targets
(``CoverageResult.repair_facet_ids`` excludes them): a repair would be a search for agreement.

**The bounds are starting points, not measurements** (Phase 3 step 4 owns re-fitting them), each a constant:

* :data:`MAX_REPAIR_QUERIES` (6): the whole repair, not per facet. Six unsupported facets against two targets
  would be twelve; the order above decides which six run.
* :data:`REPAIR_RESULTS_PER_QUERY` (5) and :data:`MAX_REPAIR_ITEMS` (12): chunks asked for, and chunks that
  may join the reduction's input. The supplement sits under the reserved share (50) and the positional cap (100).
* :data:`REPAIR_DEADLINE_SHARE` (0.3) of the deadline left when the repair starts, capped at
  :data:`REPAIR_ALLOWANCE_CAP_S` (90 s), and the repair is not started under :data:`MIN_REPAIR_ALLOWANCE_S`
  (20 s). The allowance covers the retrieval (at most :data:`REPAIR_RETRIEVAL_SHARE`, 0.4, of it) and the
  reassessment, so generation keeps at least the other 0.7 of what was left.
* USD: one accumulator. The repair is not started when nothing is left, nor when what is left is under the
  estimated cost of the reassessment (the mean of the assessment calls already paid for times the calls
  planned; an unknown cost never fabricates a refusal). Retrieval is a known-zero ``sql`` step.

Pure: stdlib and the RDR-232 value types.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from nexus.plans.request_coverage import Facet

__all__ = [
    "MAX_REPAIR_ITEMS",
    "MAX_REPAIR_QUERIES",
    "MIN_REPAIR_ALLOWANCE_S",
    "MIN_REPAIR_QUERY_S",
    "NOTE_PREFIX",
    "REPAIR_ALLOWANCE_CAP_S",
    "REPAIR_DEADLINE_SHARE",
    "REPAIR_RESULTS_PER_QUERY",
    "REPAIR_RETRIEVAL_SHARE",
    "RepairHit",
    "RepairOutcome",
    "RepairQuery",
    "RepairReport",
    "RepairTarget",
    "build_repair_queries",
    "repair_allowance",
    "unresolved_note",
    "usd_affordable",
]

#: Targeted queries one repair may issue (all facets, all targets).
MAX_REPAIR_QUERIES: int = 6
#: Chunks one query asks for.
REPAIR_RESULTS_PER_QUERY: int = 5
#: New chunks that may join the terminal reduction's input.
MAX_REPAIR_ITEMS: int = 12
#: The repair may use this share of the deadline left when it starts ...
REPAIR_DEADLINE_SHARE: float = 0.3
#: ... and never more than this many seconds.
REPAIR_ALLOWANCE_CAP_S: float = 90.0
#: No repair is started with a smaller allowance (room for a query, a fetch and one assessment call).
MIN_REPAIR_ALLOWANCE_S: float = 20.0
#: The retrieval phase may use this share of the allowance; the reassessment gets the rest.
REPAIR_RETRIEVAL_SHARE: float = 0.4
#: No query is started with less of the retrieval allowance than this.
MIN_REPAIR_QUERY_S: float = 1.0

_MAX_QUERY_CHARS = 500
_WS = re.compile(r"\s+")

RepairOutcome = Literal[
    "not_attempted", "completed", "no_new_evidence", "not_merged", "retrieval_failed", "no_target",
    "skipped_deadline", "skipped_budget", "unavailable",
]


@dataclass(frozen=True)
class RepairReport:
    """What the repair did, for the run summary (nexus-jypmj.3.5): outcome, the facets it targeted, the
    queries issued, chunks found and merged, and its latency. No query text, no facet text, no evidence.

    ``not_attempted`` is a ledger with no repairable gap (or repair switched off). ``unavailable`` is a
    boundary the runner gave no retrieval hooks. ``not_merged`` is repair evidence the reduction's input
    did not hold after the merge (a plan shape with no list-valued evidence argument): the ledger is then
    unchanged and nothing unassessed reaches the reduction.
    """

    outcome: RepairOutcome = "not_attempted"
    facet_ids: tuple[str, ...] = ()
    queries: int = 0
    items_found: int = 0
    items_merged: int = 0
    latency_ms: int = 0
    detail: str = ""
    #: Unresolved facets named to the terminal generation by the coverage note (``0``: no note was delivered).
    noted_facets: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome, "facet_ids": list(self.facet_ids), "queries": self.queries,
            "items_found": self.items_found, "items_merged": self.items_merged, "latency_ms": self.latency_ms,
            "detail": self.detail, "noted_facets": self.noted_facets,
        }


@dataclass(frozen=True)
class RepairTarget:
    """Where a repair query looks: a ``search`` corpus string, a catalog subtree when the plan's step (or the
    caller's scope) ran under one, and the plan step's other filters as sorted ``(name, value)`` pairs
    (``where``, ``content_type``, ``author``), so a repair never searches wider than the step it repairs."""

    corpus: str
    subtree: str = ""
    filters: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class RepairQuery:
    """One targeted retrieval call: the facet it is for, the tool, its arguments, and whether the target
    came from the primary source (``primary``) or the plan's own scope (``plan``)."""

    facet_id: str
    tool: str
    args: dict[str, Any]
    origin: Literal["primary", "plan"]


@dataclass(frozen=True)
class RepairHit:
    """One retrieved chunk the repair may fetch: its id, collection, catalog document and content hash."""

    id: str
    collection: str = ""
    tumbler: str = ""
    chash: str = ""


def repair_allowance(remaining_seconds: float | None) -> float:
    """Seconds the repair may use: the cap with no deadline, else :data:`REPAIR_DEADLINE_SHARE` of what is
    left inside the cap; ``0.0`` (unaffordable) when that falls under :data:`MIN_REPAIR_ALLOWANCE_S`."""
    if remaining_seconds is None:
        return REPAIR_ALLOWANCE_CAP_S
    allowance = min(REPAIR_ALLOWANCE_CAP_S, max(remaining_seconds, 0.0) * REPAIR_DEADLINE_SHARE)
    return allowance if allowance >= MIN_REPAIR_ALLOWANCE_S else 0.0


def usd_affordable(remaining_usd: float | None, estimated_cost: float | None) -> bool:
    """Whether the shared USD allowance can carry the repair. No allowance set: yes. Nothing left: no.
    An estimated reassessment cost larger than what is left: no. No estimate (no assessment call reported
    a cost): the stop-line alone, so an unknown cost never fabricates a refusal."""
    if remaining_usd is None:
        return True
    if remaining_usd <= 0:
        return False
    return estimated_cost is None or remaining_usd >= estimated_cost


#: Facets one coverage note lists, and the characters of each facet's wording it quotes.
_MAX_NOTE_FACETS = 12
_MAX_NOTE_FACET_CHARS = 300
NOTE_PREFIX = "[coverage note, not evidence]"


def unresolved_note(unresolved: Sequence[tuple[Facet, str]]) -> str:
    """The note the terminal generation is handed: each requested facet the evidence leaves unresolved, with
    its state (``unsupported`` or ``contradicted``), and each facet whose evidence conflicts (state
    ``conflicted``: supported, and also contradicted). One block, single newlines, so it survives the join of an
    operator's evidence and reads as one item. The generator is told to report each unresolved point as
    unresolved and not to answer it from general knowledge: the RDR's "an explicit list of unresolved facets"
    (section 4, step 4); and to answer a conflicted point from the supporting evidence while saying that other
    evidence contradicts it."""
    shown = list(unresolved)[:_MAX_NOTE_FACETS]
    unsettled = [(f, s) for f, s in shown if s != "conflicted"]
    conflicted = [f for f, s in shown if s == "conflicted"]
    lines: list[str] = []
    if unsettled:
        lines.append(
            f"{NOTE_PREFIX} The request asks for the points below and the evidence supplied does not settle them. "
            "State each one as unresolved. Do not answer it from general knowledge and do not infer it."
        )
        for facet, state in unsettled:
            why = "the evidence contradicts it" if state == "contradicted" else "no evidence found"
            lines.append(f"- {facet.id}: {_WS.sub(' ', facet.text).strip()[:_MAX_NOTE_FACET_CHARS]} ({why})")
    if conflicted:
        lines.append(
            f"{'' if unsettled else NOTE_PREFIX + ' '}The evidence supplied conflicts on the points below: some items "
            "support them and other items contradict them. Answer each from the supporting evidence and say that "
            "other evidence contradicts it."
        )
        for facet in conflicted:
            lines.append(f"- {facet.id}: {_WS.sub(' ', facet.text).strip()[:_MAX_NOTE_FACET_CHARS]} (the evidence conflicts)")
    return "\n".join(lines)


def planned_reassessment_calls(facet_count: int, max_facets_per_call: int) -> int:
    """Calls the reassessment will make: the repair evidence is one stream, so one call per group of facets."""
    return max(1, math.ceil(facet_count / max(1, max_facets_per_call)))


def build_repair_queries(
    facets: Sequence[Facet], *, primary_collections: Sequence[str], plan_targets: Sequence[RepairTarget],
    max_queries: int, limit: int, primary_subtree: str = "",
) -> list[RepairQuery]:
    """The targeted queries, in the RDR's order, at most *max_queries*.

    Every facet against each primary collection first (under *primary_subtree* when the caller's scope is a
    catalog subtree), then every facet against each plan target. A target with a subtree, a ``content_type`` or
    an ``author`` uses ``query`` (the tool that takes them); any other uses ``search``. A target's ``where`` rides
    either. Repeats collapse; targets that differ only in filters do not.
    """
    origin: Literal["primary", "plan"]
    targets: list[tuple[Literal["primary", "plan"], RepairTarget]] = [
        ("primary", RepairTarget(c, primary_subtree)) for c in dict.fromkeys(primary_collections) if c]
    targets += [("plan", t) for t in plan_targets if t.corpus]
    out: list[RepairQuery] = []
    seen: set[tuple[str, str, str, tuple[tuple[str, str], ...]]] = set()
    for origin, target in targets:
        for f in facets:
            key = (f.id, target.corpus, target.subtree, target.filters)
            if key in seen:
                continue
            seen.add(key)
            if len(out) >= max_queries:
                return out
            text = _WS.sub(" ", f.text).strip()[:_MAX_QUERY_CHARS]
            filters = dict(target.filters)
            if target.subtree or filters.get("author") or filters.get("content_type"):
                args: dict[str, Any] = {"question": text, "corpus": target.corpus, "limit": limit, **filters}
                if target.subtree:
                    args["subtree"] = target.subtree
                out.append(RepairQuery(f.id, "query", args, origin))
            else:
                out.append(RepairQuery(f.id, "search", {"query": text, "corpus": target.corpus, "limit": limit, **filters}, origin))
    return out
