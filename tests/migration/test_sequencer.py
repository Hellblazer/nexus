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
_ONNX = "bge-base-en-v15-768"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _cls(collection: str, leg: str, *, has_data: bool = True) -> CollectionClassification:
    return CollectionClassification(
        collection=collection,
        leg=leg,  # type: ignore[arg-type]
        model=_ONNX,
        dim=768,
        support="supported-onnx",
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


def _noop_model_gate(classifications, *, voyage_key_present, exempt=frozenset()) -> None:  # type: ignore[no-untyped-def]
    return None


# --------------------------------------------------------------------------
# Dirty T2 blocks T3
# --------------------------------------------------------------------------


def test_dirty_t2_blocks_t3_etl_never_starts() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
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
        _cls("code__a__bge-base-en-v15-768__v1", "local"),
        _cls("knowledge__b__bge-base-en-v15-768__v1", "cloud"),
    )

    def _run_leg(leg: str) -> MigrationReport:
        if leg == "local":
            return _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"])
        return _failed_leg("cloud", "knowledge__b__bge-base-en-v15-768__v1")

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
        _cls("code__a__bge-base-en-v15-768__v1", "local"),
        _cls("knowledge__b__bge-base-en-v15-768__v1", "cloud"),
    )

    def _run_leg(leg: str) -> MigrationReport:
        if leg == "local":
            return _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"])
        return _ok_leg("cloud", ["knowledge__b__bge-base-en-v15-768__v1"])

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
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=lambda leg: _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"]),
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
        _cls("code__a__bge-base-en-v15-768__v1", "local"),
        _cls("code__b__bge-base-en-v15-768__v1", "local"),
        _cls("knowledge__c__bge-base-en-v15-768__v1", "cloud"),
    )

    def _run_leg(leg: str) -> MigrationReport:
        if leg == "local":
            return _ok_leg(
                "local",
                ["code__a__bge-base-en-v15-768__v1", "code__b__bge-base-en-v15-768__v1"],
            )
        return _ok_leg("cloud", ["knowledge__c__bge-base-en-v15-768__v1"])

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
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    seen_phase: dict[str, str] = {}

    def _run_t2(_s):  # type: ignore[no-untyped-def]
        seen_phase["t2"] = current_phase()
        return _CLEAN_T2

    def _run_leg(leg: str) -> MigrationReport:
        seen_phase["leg"] = current_phase()
        return _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"])

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
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    order: list[str] = []

    run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: order.append("t2") or _CLEAN_T2,  # type: ignore[func-returns-value]
        run_leg=lambda leg: order.append("leg") or _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"]),  # type: ignore[func-returns-value]
        voyage_key_present=False,
        quiesce_check=lambda: order.append("quiesce"),
        model_gate=lambda c, *, voyage_key_present, exempt=frozenset(): order.append("models"),
        started_at=_FIXED_STARTED_AT,
    )
    assert order == ["quiesce", "models", "t2", "leg"]


def test_model_gate_block_stops_before_t2() -> None:
    from nexus.migration.pregate import ModelPreGateBlocked

    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    t2_calls: list[int] = []

    def _model_gate(classifications, *, voyage_key_present, exempt=frozenset()) -> None:  # type: ignore[no-untyped-def]
        raise ModelPreGateBlocked([("code__a__bge-base-en-v15-768__v1", "unservable")])

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: t2_calls.append(1) or _CLEAN_T2,  # type: ignore[func-returns-value]
        run_leg=lambda leg: _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"]),
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_model_gate,
        started_at=_FIXED_STARTED_AT,
    )
    assert outcome.ok is False
    assert t2_calls == []  # blocked before T2
    assert "unservable" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


def test_t2_raise_transitions_to_failed_not_stuck_migrating() -> None:
    # An in-process T2 raise must end at migrated-failed, never a false
    # 'migrating' (CRITICAL-1): the read surfaces would otherwise lie forever.
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    leg_calls: list[str] = []

    def _run_t2(_s):  # type: ignore[no-untyped-def]
        raise RuntimeError("service crashed mid-T2")

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=_run_t2,
        run_leg=lambda leg: leg_calls.append(leg) or _ok_leg("local", ["x"]),  # type: ignore[func-returns-value]
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
    )
    assert outcome.ok is False
    assert leg_calls == []  # T3 never started
    assert "service crashed mid-T2" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"  # NOT stuck at migrating


def test_leg_raise_is_treated_as_failed_leg() -> None:
    # A leg raising is a failed leg: remaining legs still run, refuse-partial
    # fires, and the sentinel ends migrated-failed (never stuck migrating).
    det = _detection(
        _cls("code__a__bge-base-en-v15-768__v1", "local"),
        _cls("knowledge__b__bge-base-en-v15-768__v1", "cloud"),
    )

    def _run_leg(leg: str) -> MigrationReport:
        if leg == "cloud":
            raise RuntimeError("cloud leg upsert exploded")
        return _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"])

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
    assert outcome.ok is False
    assert outcome.legs_attempted == ("cloud", "local")  # both attempted
    assert outcome.legs_ok == ("local",)  # cloud raised → not ok
    assert current_phase() == "migrated-failed"


def test_quiesce_block_stops_before_t2() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    t2_calls: list[int] = []

    def _quiesce() -> None:
        raise MigrationQuiesceBlocked([4321], Path("/tmp/locks"))

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: t2_calls.append(1) or _CLEAN_T2,  # type: ignore[func-returns-value]
        run_leg=lambda leg: _ok_leg("local", ["code__a__bge-base-en-v15-768__v1"]),
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


# --------------------------------------------------------------------------
# RDR-162 P2 cross-model remap wiring
# --------------------------------------------------------------------------

_SRC = "knowledge__a__minilm-l6-v2-384__v1"
_TGT = "knowledge__a__bge-base-en-v15-768__v1"


def _cross_model_leg(leg: str) -> MigrationReport:
    """A leg result for a cross-model collection: status migrated, target set."""
    return MigrationReport(
        leg=leg,  # type: ignore[arg-type]
        results=(CollectionResult(_SRC, 10, 10, "migrated", target_collection=_TGT),),
    )


def test_cross_model_remap_called_after_verified_leg() -> None:
    det = _detection(_cls(_SRC, "local"))
    calls: list[tuple[str, str]] = []
    exempt_seen: list[frozenset] = []

    def _gate(classifications, *, voyage_key_present, exempt=frozenset()) -> None:  # type: ignore[no-untyped-def]
        exempt_seen.append(exempt)

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=_cross_model_leg,
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_gate,
        started_at=_FIXED_STARTED_AT,
        cross_model_targets={_SRC: _TGT},
        remap_refs=lambda s, t: calls.append((s, t)),
    )

    assert outcome.ok is True
    assert calls == [(_SRC, _TGT)]  # remap fired, source -> target
    assert exempt_seen == [frozenset({_SRC})]  # gate exempted the cross-model coll
    assert current_phase() != "migrated-failed"


def test_remap_failure_demotes_leg_and_marks_failed() -> None:
    det = _detection(_cls(_SRC, "local"))

    def _boom(_s: str, _t: str) -> None:
        raise RuntimeError("service rename_collection 500")

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=_cross_model_leg,
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
        cross_model_targets={_SRC: _TGT},
        remap_refs=_boom,
    )

    assert outcome.ok is False  # remap failure demotes the leg
    assert outcome.legs_attempted == ("local",)
    assert outcome.legs_ok == ()  # demoted out of legs_ok
    assert current_phase() == "migrated-failed"  # sentinel correct, not stuck


def test_same_model_leg_never_calls_remap() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    calls: list[tuple[str, str]] = []

    outcome = run_sequenced_migration(
        det,
        sources=None,
        run_t2=lambda _s: _CLEAN_T2,
        run_leg=lambda leg: _ok_leg(leg, ["code__a__bge-base-en-v15-768__v1"]),
        voyage_key_present=False,
        quiesce_check=_noop_quiesce,
        model_gate=_noop_model_gate,
        started_at=_FIXED_STARTED_AT,
        remap_refs=lambda s, t: calls.append((s, t)),
    )

    assert outcome.ok is True
    assert calls == []  # byte-for-byte path: no target_collection, no remap
