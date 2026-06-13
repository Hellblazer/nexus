# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P2.T (nexus-ue6g7.15) — T2-then-T3 sequencing + refuse-partial-leg.

RDR-159 §Approach P2 + §Sequencing (steps 6-7). The guided migration drives the
proven primitives in ONE survivable order:

  begin_migration (phase=migrating, so every poller suspends/degrades FIRST)
    → quiesce write-lock audit (no foreign writer survives the visible sentinel)
    → per-collection-model pre-gate (unservable model fails before any ETL)
    → T2 ``migrate all`` — REQUIRE summary.total_failed == 0 before T3
    → T3 ``migrate vectors`` for EVERY detected leg — REFUSE partial-leg success
    → mark_migrated / mark_failed; collections_done/total drives the surface.

Locked properties pinned here:

* a dirty T2 (``total_failed > 0``) BLOCKS T3 — the vector ETL never starts, so
  the downstream manifest validation stays non-vacuous;
* a single-leg success is NOT accepted when detection found >1 data-bearing leg
  (refuse partial-leg);
* ``collections_done`` updates monotonically toward ``collections_total``;
* the sentinel is set to ``migrating`` BEFORE T2 runs (the pollers observe it),
  and the pre-gates run BEFORE T2.

Dependencies (T2 callable, per-leg ETL, pre-gates) are injected, so the test
pins the SEQUENCING contract without a real service / Chroma. State transitions
use the real P1a sentinel under an isolated ``NEXUS_CONFIG_DIR``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.migration.detection import CollectionClassification, DetectionReport
from nexus.migration.quiesce import MigrationQuiesceBlocked
from nexus.migration.sequencer import SequenceOutcome, run_sequenced_migration
from nexus.migration.state import current_phase, read_state
from nexus.migration.vector_etl import CollectionResult, MigrationReport

_FIXED_STARTED_AT = "2026-06-13T00:00:00+00:00"
_ONNX = "minilm-l6-v2-384"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _cls(collection: str, leg: str, *, has_data: bool = True) -> CollectionClassification:
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model=_ONNX,
        dim=384,
        support="supported-onnx-384",
        source_count=10 if has_data else 0,
        has_data=has_data,
        reason="",
    )


def _detection(*classifications: CollectionClassification) -> DetectionReport:
    return DetectionReport(classifications=tuple(classifications))


def _ok_leg(leg: str, collections: list[str]) -> MigrationReport:
    return MigrationReport(
        leg=leg,  # type: ignore[arg-type]
        results=tuple(
            CollectionResult(c, 10, 10, "migrated") for c in collections
        ),
    )


def _failed_leg(leg: str, collection: str) -> MigrationReport:
    return MigrationReport(
        leg=leg,  # type: ignore[arg-type]
        results=(CollectionResult(collection, 10, 0, "failed", "upsert error"),),
    )


_CLEAN_T2 = {"summary": {"total_failed": 0}}
_DIRTY_T2 = {"summary": {"total_failed": 3}}

# Common injected pre-gates that pass (overridden per test where needed).
def _noop_quiesce() -> None:
    return None


def _noop_model_gate(classifications, *, voyage_key_present) -> None:  # type: ignore[no-untyped-def]
    return None


# --------------------------------------------------------------------------
# Dirty T2 blocks T3
# --------------------------------------------------------------------------


def test_dirty_t2_blocks_t3_etl_never_starts() -> None:
    det = _detection(_cls("code__a__minilm-l6-v2-384__v1", "local"))
    leg_calls: list[str] = []

    outcome = run_sequenced_migration(
        det,
        sources=None,  # unused — run_t2 is injected
        run_t2=lambda _sources: _DIRTY_T2,
        run_leg=lambda leg: leg_calls.append(leg) or _ok_leg(leg, ["x"]),  # type: ignore[func-returns-value]
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )

    assert isinstance(outcome, SequenceOutcome)
    assert outcome.ok is False
    assert leg_calls == []  # T3 NEVER started on a dirty T2
    assert outcome.t2_total_failed == 3
    assert "total_failed" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"
    assert read_state().failure is not None


# --------------------------------------------------------------------------
# Refuse partial-leg
# --------------------------------------------------------------------------


def test_partial_leg_success_is_refused_when_multiple_legs_detected() -> None:
    det = _detection(
        _cls("code__a__minilm-l6-v2-384__v1", "local"),
        _cls("knowledge__b__minilm-l6-v2-384__v1", "cloud"),
    )

    def _run_leg(leg: str) -> MigrationReport:
        if leg == "local":
            return _ok_leg("local", ["code__a__minilm-l6-v2-384__v1"])
        return _failed_leg("cloud", "knowledge__b__minilm-l6-v2-384__v1")

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=_run_leg,
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )

    assert outcome.ok is False  # one leg ok is NOT multi-leg success
    assert outcome.legs_attempted == ("cloud", "local")
    assert outcome.legs_ok == ("local",)
    assert current_phase() == "migrated-failed"


def test_all_legs_ok_is_full_success() -> None:
    det = _detection(
        _cls("code__a__minilm-l6-v2-384__v1", "local"),
        _cls("knowledge__b__minilm-l6-v2-384__v1", "cloud"),
    )

    def _run_leg(leg: str) -> MigrationReport:
        if leg == "local":
            return _ok_leg("local", ["code__a__minilm-l6-v2-384__v1"])
        return _ok_leg("cloud", ["knowledge__b__minilm-l6-v2-384__v1"])

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=_run_leg,
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )

    assert outcome.ok is True
    assert outcome.legs_ok == ("cloud", "local")
    assert outcome.collections_done == 2
    assert outcome.collections_total == 2
    assert current_phase() == "migrated"


def test_single_leg_success_ok_when_only_one_leg_detected() -> None:
    det = _detection(_cls("code__a__minilm-l6-v2-384__v1", "local"))
    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=lambda leg: _ok_leg("local", ["code__a__minilm-l6-v2-384__v1"]),
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )
    assert outcome.ok is True
    assert current_phase() == "migrated"


# --------------------------------------------------------------------------
# Monotonic progress + phase/pre-gate ordering
# --------------------------------------------------------------------------


def test_progress_updates_monotonically_to_total() -> None:
    det = _detection(
        _cls("code__a__minilm-l6-v2-384__v1", "local"),
        _cls("code__b__minilm-l6-v2-384__v1", "local"),
        _cls("knowledge__c__minilm-l6-v2-384__v1", "cloud"),
    )

    def _run_leg(leg: str) -> MigrationReport:
        if leg == "local":
            return _ok_leg(
                "local",
                ["code__a__minilm-l6-v2-384__v1", "code__b__minilm-l6-v2-384__v1"],
            )
        return _ok_leg("cloud", ["knowledge__c__minilm-l6-v2-384__v1"])

    progress: list[tuple[int, int]] = []

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=_run_leg,
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        on_progress=lambda done, total: progress.append((done, total)),
        started_at=_FIXED_STARTED_AT,
    )

    assert outcome.collections_total == 3
    assert outcome.collections_done == 3
    # Monotonic non-decreasing done; constant total; ends at total.
    dones = [d for d, _ in progress]
    assert dones == sorted(dones)
    assert all(t == 3 for _, t in progress)
    assert progress[-1] == (3, 3)


def test_phase_is_migrating_when_t2_and_legs_run() -> None:
    det = _detection(_cls("code__a__minilm-l6-v2-384__v1", "local"))
    seen_phase: dict[str, str] = {}

    def _run_t2(_s):  # type: ignore[no-untyped-def]
        seen_phase["t2"] = current_phase()
        return _CLEAN_T2

    def _run_leg(leg: str) -> MigrationReport:
        seen_phase["leg"] = current_phase()
        return _ok_leg("local", ["code__a__minilm-l6-v2-384__v1"])

    run_sequenced_migration(
        det,
        sources=None,
        run_t2=_run_t2,
        run_leg=_run_leg,
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )
    # The sentinel is set to migrating BEFORE T2 and the legs run.
    assert seen_phase["t2"] == "migrating"
    assert seen_phase["leg"] == "migrating"


def test_pregates_run_before_t2_in_order() -> None:
    det = _detection(_cls("code__a__minilm-l6-v2-384__v1", "local"))
    order: list[str] = []

    run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: order.append("t2") or _CLEAN_T2,  # type: ignore[func-returns-value]
        run_leg=lambda leg: order.append("leg") or _ok_leg("local", ["code__a__minilm-l6-v2-384__v1"]),  # type: ignore[func-returns-value]
        voyage_key_present=False,
        quiesce_check=lambda: order.append("quiesce"),
        model_gate=lambda c, *, voyage_key_present: order.append("models"),
        started_at=_FIXED_STARTED_AT,
    )
    assert order == ["quiesce", "models", "t2", "leg"]


def test_quiesce_block_stops_before_t2() -> None:
    det = _detection(_cls("code__a__minilm-l6-v2-384__v1", "local"))
    t2_calls: list[int] = []

    def _quiesce() -> None:
        raise MigrationQuiesceBlocked([4321], Path("/tmp/locks"))

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: t2_calls.append(1) or _CLEAN_T2,  # type: ignore[func-returns-value]
        run_leg=lambda leg: _ok_leg("local", ["code__a__minilm-l6-v2-384__v1"]),
        voyage_key_present=False,
        quiesce_check=_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )
    assert outcome.ok is False
    assert t2_calls == []  # blocked before T2
    assert "4321" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


# --------------------------------------------------------------------------
# Fresh user no-op
# --------------------------------------------------------------------------


def test_fresh_user_no_op_success_without_touching_anything() -> None:
    det = _detection()  # no legs with data
    t2_calls: list[int] = []
    leg_calls: list[str] = []

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: t2_calls.append(1) or _CLEAN_T2,  # type: ignore[func-returns-value]
        run_leg=lambda leg: leg_calls.append(leg) or _ok_leg(leg, []),  # type: ignore[func-returns-value]
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )
    assert outcome.ok is True
    assert outcome.collections_total == 0
    assert t2_calls == []  # nothing to migrate — T2/T3 untouched
    assert leg_calls == []
    assert current_phase() == "not-migrating"  # no sentinel left behind
