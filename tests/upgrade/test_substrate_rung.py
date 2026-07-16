# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P4.0 (nexus-x3z00): the substrate-ETL rung, assembled.

P2 built every part (census, planner, seam, wire re-id, cascade,
rollback-via-map) but nothing assembled them into a Rung the walk can
reach. This is that assembly: detect from the live census/classification,
converge via ``run_substrate_migration`` (genuine decisions resolved
FIRST), verify the post-state authoritatively (the check the .14 resume
path explicitly delegates upward).

N/A shapes detect-and-skip (f0pmd): a service-mode install with no Chroma
footprint, a conformant install, an empty footprint.
"""
from __future__ import annotations

import inspect
import pathlib
from typing import Any

import pytest

from nexus.migration.detection import CollectionClassification
from nexus.migration.etl_ports import EtlRunResult
from nexus.migration.remap_cascade import StoreCascadeResult
from nexus.upgrade_ladder.protocol import ConvergeOutcome, Rung, RungStatus
from nexus.upgrade_ladder.registry import RUNG_SUBSTRATE_ETL, default_registry
from nexus.upgrade_ladder.rungs.substrate_etl import (
    SubstrateEtlRung,
    SubstratePlan,
    _default_cost_gate,
)


def _cls(
    name: str,
    *,
    legacy: bool = False,
    support: str = "supported-voyage-1024",
    model: str | None = "voyage-context-3",
    count: int = 10,
) -> CollectionClassification:
    return CollectionClassification(
        collection=name,
        leg="local",
        model=model,
        dim=1024 if model else None,
        support=support,  # type: ignore[arg-type]
        source_count=count,
        has_data=count > 0,
        legacy_ids=legacy,
    )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event: str, **fields: object) -> None:
        self.events.append(event)


def _rung(**kwargs: Any) -> SubstrateEtlRung:
    defaults: dict[str, Any] = {
        "footprint_fn": lambda: True,
        "classify_fn": lambda: [_cls("knowledge__old", legacy=True)],
        "voyage_key_fn": lambda: True,
    }
    defaults.update(kwargs)
    return SubstrateEtlRung(**defaults)


# ── protocol conformance + registration ──────────────────────────────────────


def test_rung_satisfies_the_protocol() -> None:
    assert isinstance(_rung(), Rung)
    assert _rung().name == RUNG_SUBSTRATE_ETL


def test_registered_in_the_default_registry_after_t2_schema() -> None:
    """The RQ2 hard edge, live: t2-schema precedes substrate-etl."""
    names = [r.name for r in default_registry()]
    assert names == ["t2-schema", RUNG_SUBSTRATE_ETL]


# ── detect ───────────────────────────────────────────────────────────────────


def test_detect_pending_on_legacy_footprint() -> None:
    status = _rung().detect()
    assert status.applicable and status.pending
    assert "knowledge__old" in status.pending_detail
    assert "legacy" in status.pending_detail.lower()


def test_detect_not_applicable_without_a_chroma_footprint() -> None:
    """Service-mode / fresh install: no footprint gate fires — the census's
    own cheap file-level check, never opening a store."""
    def _must_not_classify() -> list[CollectionClassification]:
        raise AssertionError("classification must not run without a footprint")

    status = _rung(footprint_fn=lambda: False, classify_fn=_must_not_classify).detect()
    assert status.applicable is False
    assert not status.pending


def test_detect_converged_on_a_conformant_footprint() -> None:
    status = _rung(classify_fn=lambda: [_cls("code__ok")]).detect()
    assert status.applicable is True
    assert status.converged is True


def test_detect_converged_on_an_empty_footprint() -> None:
    status = _rung(classify_fn=lambda: [_cls("knowledge__empty", legacy=True, count=0)]).detect()
    assert status.converged is True


def test_detect_reports_genuine_decisions_as_pending_detail() -> None:
    """A source-gone decision is WORK (it needs an operator answer), so the
    rung is pending and says why — never silently converged."""
    status = _rung(
        classify_fn=lambda: [_cls("knowledge__present")],
        prior_collections_fn=lambda: frozenset({"knowledge__present", "knowledge__gone"}),
    ).detect()
    assert status.pending
    assert "knowledge__gone" in status.pending_detail
    assert "decision" in status.pending_detail.lower()


def test_detect_is_read_only() -> None:
    """The doctor surface: detect classifies and plans, never migrates."""
    migrated = {"n": 0}
    rung = _rung(migrate_fn=lambda *a, **k: migrated.__setitem__("n", migrated["n"] + 1))
    rung.detect()
    rung.detect()
    assert migrated["n"] == 0


def test_detect_degrades_loudly_on_a_broken_census() -> None:
    def _boom() -> list[CollectionClassification]:
        raise RuntimeError("store unreadable")

    with pytest.raises(RuntimeError, match="store unreadable"):
        _rung(classify_fn=_boom).detect()


# ── converge ─────────────────────────────────────────────────────────────────


def test_converge_runs_the_migration_and_completes(tmp_path: pathlib.Path) -> None:
    calls: list[Any] = []

    def _migrate(plan, **kwargs):
        calls.append(plan)
        return ([], [])

    rung = _rung(migrate_fn=_migrate)
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.COMPLETED
    assert len(calls) == 1
    assert calls[0].legs[0].source_collection == "knowledge__old"


def test_converge_defers_on_an_unresolved_genuine_decision(
    tmp_path: pathlib.Path,
) -> None:
    """Consent is never implicit: a source-gone decision the operator has
    not answered DEFERS the rung (non-fatal, records nothing, position
    pinned) rather than guessing or failing the upgrade."""
    migrated = {"n": 0}
    rung = _rung(
        classify_fn=lambda: [_cls("knowledge__present")],
        prior_collections_fn=lambda: frozenset({"knowledge__present", "knowledge__gone"}),
        migrate_fn=lambda *a, **k: migrated.__setitem__("n", migrated["n"] + 1),
    )
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "knowledge__gone" in result.detail
    assert migrated["n"] == 0  # nothing ran


def test_converge_defers_when_the_cost_gate_declines() -> None:
    """The billed re-embed keeps its existing consent gate: a declined cost
    prompt DEFERS (the operator's answer is 'not now'), never proceeds."""
    migrated = {"n": 0}
    rung = _rung(
        classify_fn=lambda: [
            _cls("knowledge__notes__all-minilm-l6-v2__v1", support="unsupported", model=None)
        ],
        cost_gate_fn=lambda plan: False,  # operator declined
        migrate_fn=lambda *a, **k: migrated.__setitem__("n", migrated["n"] + 1),
    )
    result = rung.converge(_Recorder())
    assert result.outcome is ConvergeOutcome.DEFERRED
    assert "cost" in result.detail.lower() or "declin" in result.detail.lower()
    assert migrated["n"] == 0


def test_cost_gate_sees_an_unbilled_plan_for_a_same_model_leg() -> None:
    """Derivable transitions stay promptless: the gate is consulted but the
    plan it receives is NOT billed (a same-model re-id leg), so the
    production gate returns True without prompting (nexus-cewad: nothing
    billed → no prompt)."""
    seen: list[Any] = []

    def _gate(plan) -> bool:
        seen.append(plan)
        return True

    _rung(cost_gate_fn=_gate, migrate_fn=lambda *a, **k: ([], [])).converge(_Recorder())
    assert len(seen) == 1
    assert seen[0].billed_reembed is False  # no bill => the real gate never prompts


def test_production_cost_gate_is_promptless_when_nothing_is_billed() -> None:
    """The production default gate, directly: an unbilled plan proceeds
    without any click.confirm (which would abort a non-TTY run)."""
    assert _default_cost_gate(SubstratePlan(legs=[], billed_reembed=False)) is True


def test_converge_fails_loud_on_a_failed_leg() -> None:
    """A leg that genuinely failed is a rung FAILURE (raises → the runner
    records FAILED), never a silent completion."""
    rung = _rung(
        migrate_fn=lambda *a, **k: ([EtlRunResult(False, 5, 2, "upsert failed")], [])
    )
    with pytest.raises(RuntimeError, match="upsert failed"):
        rung.converge(_Recorder())


def test_converge_fails_loud_on_a_failed_cascade_store() -> None:
    rung = _rung(
        migrate_fn=lambda *a, **k: (
            [EtlRunResult(True, 5, 5)],
            [StoreCascadeResult("chash_index", False, reason="no such table")],
        )
    )
    with pytest.raises(RuntimeError, match="chash_index"):
        rung.converge(_Recorder())


# ── verify ───────────────────────────────────────────────────────────────────


def test_verify_true_when_the_census_is_clean() -> None:
    """Authoritative post-state: no legacy-id collection remains."""
    assert _rung(classify_fn=lambda: [_cls("code__ok")]).verify() is True


def test_verify_false_while_legacy_ids_remain() -> None:
    assert _rung().verify() is False  # the default fixture has a legacy collection


def test_verify_true_when_not_applicable() -> None:
    """An N/A rung (no footprint) verifies trivially — nothing to check."""
    assert _rung(footprint_fn=lambda: False).verify() is True


def test_verify_is_independent_of_converge_bookkeeping() -> None:
    """RDR-142: verify re-reads the WORLD, never a return value the converge
    handed it (the resume path's full-count check delegates here)."""
    source = inspect.getsource(SubstrateEtlRung.verify)
    assert "self._classify" in source or "_census" in source
    assert "result" not in source  # no converge bookkeeping consulted
