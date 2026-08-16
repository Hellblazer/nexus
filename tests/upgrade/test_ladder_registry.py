# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P0.1 (nexus-n7u38.1): ordered ladder registry + RQ2 hard edges.

RDR-155 P4b re-ground: the t2-schema / substrate-etl rungs (and their
co-resident axes) died with the migration machinery, leaving chash-rekey as
the sole surviving rung. nexus-lgdel.l1 retired that rung too — the ladder
is rung-less (RUNG_ORDER == ()) until a future data transition needs one.
The surviving edge set: package → everything. The graph validator and
registry mechanics are unchanged and stay pinned here.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nexus.upgrade_ladder.protocol import ConvergeOutcome, ConvergeResult, ProgressReporter, RungStatus
from nexus.upgrade_ladder.registry import (
    ALL_RUNGS,
    CO_RESIDENT_AXES,
    HARD_EDGES,
    PRECONDITION_ENGINE,
    PRECONDITION_PACKAGE,
    PRECONDITION_PROCESS,
    RUNG_ORDER,
    LadderOrderError,
    LadderRegistry,
    default_registry,
    expand_edges,
    validate_hard_edges,
)


@dataclass
class StubRung:
    """Minimal Protocol-conformant rung for registry tests."""

    name: str

    def detect(self) -> RungStatus:
        return RungStatus(applicable=True, converged=True)

    def converge(self, report: ProgressReporter) -> ConvergeResult:
        return ConvergeResult(ConvergeOutcome.COMPLETED)

    def verify(self) -> bool:
        return True


# ── The edge constants themselves (drift guards) ────────────────────────────


def test_package_edge_precedes_everything() -> None:
    assert (PRECONDITION_PACKAGE, ALL_RUNGS) in HARD_EDGES


def test_co_resident_axes_registry_is_empty_post_p4b() -> None:
    """RQ2 edges 4+5 lived INSIDE the substrate-ETL rung; that rung retired
    at RDR-155 P4b, so the co-residency registry is empty — an entry
    re-appearing here must come with a rung that actually hosts it."""
    assert CO_RESIDENT_AXES == {}


def test_preconditions_are_not_rungs() -> None:
    """Package/engine/process are STATELESS preconditions (re-derived from
    on-disk state each invocation, RDR-185 Constraints) — they must never
    appear in the data-rung order."""
    for precondition in (PRECONDITION_PACKAGE, PRECONDITION_ENGINE, PRECONDITION_PROCESS):
        assert precondition not in RUNG_ORDER


def test_hooks_config_axis_has_no_assigned_position() -> None:
    """The hooks/config axis ladder position is DELIBERATELY unassigned until
    the P3 decision spike (nexus-n7u38.22); encoding one now would silently
    assume the answer the gate flagged as a genuine ambiguity."""
    assert not any("hook" in name for name in RUNG_ORDER)
    assert not any("hook" in node for edge in HARD_EDGES for node in edge)


# ── Graph validation: acyclic, canonical order is topological ────────────────


def test_hard_edges_validate_clean() -> None:
    validate_hard_edges()  # must not raise on the shipped constants


def test_expanded_edges_are_acyclic_and_order_is_topological() -> None:
    edges = expand_edges(HARD_EDGES, RUNG_ORDER)
    position = {name: i for i, name in enumerate(RUNG_ORDER)}
    for before, after in edges:
        if before in position and after in position:
            assert position[before] < position[after], (
                f"RUNG_ORDER violates hard edge {before} → {after}"
            )
        elif after in position:
            # precondition → rung edge: preconditions converge before the walk
            assert before.startswith("precondition:")


def test_star_edges_expand_to_every_other_rung() -> None:
    # synthetic multi-rung order: the expansion mechanics are order-generic
    edges = expand_edges((("first", ALL_RUNGS),), ("first", "second", "third"))
    assert ("first", "second") in edges and ("first", "third") in edges
    assert ("first", "first") not in edges  # never self-edges


def test_cycle_detection_fires_on_synthetic_cycle() -> None:
    """Non-vacuity: the validator must actually detect a cycle, not merely
    pass because today's constants happen to be clean."""
    with pytest.raises(LadderOrderError, match="cycle"):
        validate_hard_edges(
            edges=(("a", "b"), ("b", "c"), ("c", "a")),
            order=("a", "b", "c"),
        )


def test_order_violation_detection_fires() -> None:
    """Non-vacuity: an order that contradicts an edge must be rejected."""
    with pytest.raises(LadderOrderError, match="order"):
        validate_hard_edges(edges=(("b", "a"),), order=("a", "b"))


# ── LadderRegistry behaviour ─────────────────────────────────────────────────


def test_registry_preserves_registration_order() -> None:
    rungs = (StubRung("alpha"), StubRung("beta"), StubRung("gamma"))
    registry = LadderRegistry(rungs)
    assert [r.name for r in registry] == ["alpha", "beta", "gamma"]
    assert len(registry) == 3
    assert registry.rungs == rungs


def test_registry_rejects_duplicate_names() -> None:
    with pytest.raises(LadderOrderError, match="duplicate"):
        LadderRegistry((StubRung("same"), StubRung("same")))


def test_registry_allows_synthetic_names_with_no_canonical_edges() -> None:
    """Interim rungs MAY wrap existing verbs under non-canonical names
    (Decision-Space option 2) — only edges over KNOWN names are enforced,
    and RUNG_ORDER is currently empty (nexus-lgdel.l1: no canonical rung
    survives), so an all-synthetic registration is the norm, not a special
    case alongside a canonical name."""
    registry = LadderRegistry(
        (StubRung("interim-wrapped-verb-a"), StubRung("interim-wrapped-verb-b"))
    )
    assert len(registry) == 2


def test_default_registry_is_empty() -> None:
    """The production registry post-nexus-lgdel.l1: rung-less. RDR-180's
    chash-rekey rung (the ladder's last surviving data rung, RDR-155 P4b
    D-D) retired with the legacy-identity era it existed to converge
    installs out of."""
    registry = default_registry()
    assert [r.name for r in registry] == list(RUNG_ORDER) == []
