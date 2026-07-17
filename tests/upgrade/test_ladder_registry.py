# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P0.1 (nexus-n7u38.1): ordered ladder registry + RQ2 hard edges.

The five hard ordering edges (RDR-185 Research RQ2) are encoded as DATA in
``nexus.upgrade_ladder.registry`` and enforced mechanically here — the
``test_lifecycle_gate.py`` discipline: a docs rule alone degrades to hope.

Edges: package → everything; engine → substrate ETL; T2 schema → all T2
reads; chunk-identity → T3 ETL; embedder-era + chunk-identity CO-RESIDENT
inside the substrate-ETL rung (the last two are encoded as co-residency,
satisfied by construction rather than by sequencing).
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
    RUNG_SUBSTRATE_ETL,
    RUNG_T2_SCHEMA,
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


def test_engine_edge_targets_substrate_etl() -> None:
    assert (PRECONDITION_ENGINE, RUNG_SUBSTRATE_ETL) in HARD_EDGES


def test_t2_schema_edge_precedes_all_rungs() -> None:
    assert (RUNG_T2_SCHEMA, ALL_RUNGS) in HARD_EDGES


def test_chunk_identity_and_embedder_are_co_resident_in_substrate_etl() -> None:
    """RQ2 edges 4+5: chunk-identity → T3 ETL and embedder-era are in-flight
    transforms INSIDE the substrate-ETL rung — one rung, never sequential."""
    assert CO_RESIDENT_AXES["chunk-identity"] == RUNG_SUBSTRATE_ETL
    assert CO_RESIDENT_AXES["embedder-era"] == RUNG_SUBSTRATE_ETL


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
    edges = expand_edges(((RUNG_T2_SCHEMA, ALL_RUNGS),), RUNG_ORDER)
    assert (RUNG_T2_SCHEMA, RUNG_SUBSTRATE_ETL) in edges
    assert (RUNG_T2_SCHEMA, RUNG_T2_SCHEMA) not in edges  # never self-edges


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


def test_registry_accepts_canonical_order() -> None:
    registry = LadderRegistry((StubRung(RUNG_T2_SCHEMA), StubRung(RUNG_SUBSTRATE_ETL)))
    assert [r.name for r in registry] == [RUNG_T2_SCHEMA, RUNG_SUBSTRATE_ETL]


def test_registry_rejects_hard_edge_violation() -> None:
    """substrate-etl before t2-schema contradicts the T2-schema→all edge —
    the registry must refuse to hold rungs in an unwalkable order."""
    with pytest.raises(LadderOrderError, match=RUNG_T2_SCHEMA):
        LadderRegistry((StubRung(RUNG_SUBSTRATE_ETL), StubRung(RUNG_T2_SCHEMA)))


def test_registry_allows_synthetic_names_alongside_canonical() -> None:
    """Interim rungs MAY wrap existing verbs under non-canonical names
    (Decision-Space option 2) — only edges over KNOWN names are enforced."""
    registry = LadderRegistry(
        (StubRung(RUNG_T2_SCHEMA), StubRung("interim-wrapped-verb"), StubRung(RUNG_SUBSTRATE_ETL))
    )
    assert len(registry) == 3


def test_default_registry_order_is_canonical() -> None:
    """The production registry validates clean and only ever holds canonical
    rungs in canonical order (rungs land P1+: t2-schema .8, substrate-etl P2)."""
    registry = default_registry()
    names = [r.name for r in registry]
    canonical_positions = [RUNG_ORDER.index(n) for n in names if n in RUNG_ORDER]
    assert canonical_positions == sorted(canonical_positions)
