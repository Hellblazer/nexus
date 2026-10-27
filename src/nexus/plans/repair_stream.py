# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-232 Phase 3 step 2 (nexus-jypmj.3.2): the repair supplement, merged at the runner.

The one targeted repair (:mod:`nexus.plans.coverage_repair`) retrieves evidence for the facets the first
assessment found unsupported. That evidence is not a plan step either, so it reaches the terminal reduction
the way the reserved primary stream does (:mod:`nexus.plans.reserved_stream`): the runner's operator
hydration (``_hydrate_operator_args``) merges it at the first operator that consumes retrieved evidence, in
both shapes an operator receives it (``ids`` auto-hydration and explicit or Pattern A lists).

Order and budget. The supplement goes AFTER the reserved items and AHEAD of the plan's own items, so the
same ``_OPERATOR_MAX_INPUTS`` positional cap that bounds every reduction (and that already cuts only
secondary items when a reserved stream merges) cuts the plan's tail before it cuts repair evidence. "The
same evidence budgets" is that cap plus the bundle prompt guard; the supplement has its own, smaller share
(:data:`nexus.plans.coverage_repair.MAX_REPAIR_ITEMS`) set by the caller that builds it. A plan item whose
text equals a supplement item (a source note aside) is dropped from the plan's list, not merged twice.

The merge happens once per run, at one plan step (``merged_step``); a re-hydration of that same step (the
boundary's own second hydration, the oversized-bundle fallback, the continuation envelope's replay) merges
again, so every route to the reduction sees the same evidence. A later operator is not given it again.

State lives in a ``ContextVar`` so the dispatcher, the bundle path and the continuation envelope (all
callers of ``_hydrate_operator_args``) share it without a signature change. ``plan_run`` creates one per
run when a coverage boundary is configured and returns it on ``PlanResult.repair_supplement``; the
continuation hand-off re-enters it (:func:`supplement_scope`) so the envelope's prompt is built from the
evidence the boundary assessed.

Pure: stdlib and the reserved stream's text helper.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from nexus.plans.reserved_stream import strip_source_note

__all__ = [
    "RepairSupplement",
    "SupplementItem",
    "active_supplement",
    "set_supplement_step",
    "supplement_scope",
]


@dataclass(frozen=True)
class SupplementItem:
    """One repaired-in evidence text with where it came from, so the ledger's anchors can attribute it
    (``stream`` is the retrieval stream it was read from: ``vector``)."""

    text: str
    chash: str = ""
    collection: str = ""
    tumbler: str = ""
    stream: str = "vector"


@dataclass
class RepairSupplement:
    """The repair evidence of one run, and where the runner has merged it."""

    items: tuple[SupplementItem, ...] = ()
    #: The coverage note (:func:`nexus.plans.coverage_repair.unresolved_note`): which requested facets the
    #: evidence leaves unresolved. It is not evidence; it leads the merged list so the reducer reads it first.
    note: str = ""
    merged: bool = False
    merged_step: int | None = None
    current_step: int | None = None
    deduped_secondary: int = 0
    _held: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        self._held = frozenset(strip_source_note(i.text) for i in self.items)

    def install(self, items: Sequence[SupplementItem]) -> None:
        """Replace the items (an empty sequence withdraws them). A step that already merged keeps its
        marker, so a re-hydration of it merges the new items."""
        self.items = tuple(items)
        self._held = frozenset(strip_source_note(i.text) for i in self.items)
        self.deduped_secondary = 0

    def install_note(self, note: str) -> None:
        """Set the coverage note (an empty string withdraws it)."""
        self.note = note

    def should_merge(self) -> bool:
        """Items or a note to merge, at the first evidence-consuming operator or a re-hydration of that same step."""
        return bool(self.items or self.note) and (not self.merged or self.merged_step == self.current_step)

    def merge(self, secondary: Sequence[Any]) -> list[Any]:
        """The note, then the supplement's texts, ahead of *secondary*, which loses any item the supplement holds."""
        kept = [s for s in secondary if not (isinstance(s, str) and strip_source_note(s) in self._held)]
        self.deduped_secondary = len(secondary) - len(kept)
        self.merged, self.merged_step = True, self.current_step
        return ([self.note] if self.note else []) + [i.text for i in self.items] + kept

    def provenance(self, text: str) -> SupplementItem | None:
        """The item whose text is *text* (a source note aside), for the ledger's anchors."""
        if not self.items or strip_source_note(text) not in self._held:
            return None
        bare = strip_source_note(text)
        return next((i for i in self.items if strip_source_note(i.text) == bare), None)

    def reset_run(self) -> None:
        self.merged, self.merged_step, self.current_step, self.deduped_secondary = False, None, None, 0


_ACTIVE: ContextVar[RepairSupplement | None] = ContextVar("nexus_repair_supplement", default=None)


def active_supplement() -> RepairSupplement | None:
    return _ACTIVE.get()


@contextlib.contextmanager
def supplement_scope(supplement: RepairSupplement | None) -> Iterator[None]:
    """Make *supplement* the one operator hydration merges for the duration of the block."""
    token = _ACTIVE.set(supplement)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def set_supplement_step(index: int | None) -> None:
    """Tell the active supplement which plan step is about to hydrate (no-op without one)."""
    supplement = _ACTIVE.get()
    if supplement is not None:
        supplement.current_step = index
