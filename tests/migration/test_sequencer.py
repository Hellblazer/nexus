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


# ═══════════════════════════════════════════════════════════════════════════
# RDR-180 LAND-THEN-TRANSFORM (nexus-jxizy.10.7) — the NEW sequencing driver
# (:func:`run_land_then_transform_migration`). Everything above this line
# pins the RETAINED RDR-159 ``run_sequenced_migration`` (still ``driver.py``'s
# live contract — see the module-level comment in sequencer.py for why it is
# not retired in place). This section is new, additive coverage for the new
# function; it does not replace anything above.
#
# The guided migration now drives ONE survivable order:
#
#   quiesce -> model gate -> pre-land census -> land -> T2 non-chash ETL
#     -> per-collection (embed_fill, promote) -> finalize (once per wave)
#     -> verify -> clear staging -> mark_migrated.
#
# Locked properties pinned here:
#
# * the census runs BEFORE any row lands — a census failure touches nothing;
# * a dirty T2 (``total_failed > 0``) blocks promote — finalize's catalog-doc
#   FK / topic-label resolution would be vacuous against it;
# * a promote failure for one collection does not clear staging (resume);
# * finalize runs exactly once per sequencer invocation ("one wave"); a
#   second invocation (a later wave, e.g. a late-landed collection) finalizes
#   again;
# * a verify (count-parity) failure blocks WITHOUT clearing staging;
# * staging is cleared ONLY on a fully verified, all-collections-ok run;
# * ``collections_done`` updates monotonically toward ``collections_total``;
# * the sentinel is set to ``migrating`` BEFORE any step runs.
# ═══════════════════════════════════════════════════════════════════════════

from nexus.migration.sequencer import (  # noqa: E402 — appended section, deliberately grouped with its own tests
    LandThenTransformOutcome,
    run_land_then_transform_migration,
)


class _FakeStaging:
    """Records every collaborator call, in order, on one shared log —
    stands in for a real ``HttpStagingStore`` + SQLite/Chroma source readers
    (the fakes ``run_land_then_transform_migration`` is designed to be
    tested against)."""

    def __init__(
        self,
        *,
        census_error: Exception | None = None,
        land_error: Exception | None = None,
        promote_errors: dict[str, Exception] | None = None,
        finalize_error: Exception | None = None,
        verify_error: Exception | None = None,
        clear_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._census_error = census_error
        self._land_error = land_error
        self._promote_errors = promote_errors or {}
        self._finalize_error = finalize_error
        self._verify_error = verify_error
        self._clear_error = clear_error

    def census_check(self) -> None:
        self.calls.append(("census", None))
        if self._census_error is not None:
            raise self._census_error

    def land(self) -> dict[str, int]:
        self.calls.append(("land", None))
        if self._land_error is not None:
            raise self._land_error
        return {"chash_index": 5}

    def embed_fill(self, collection: str) -> dict[str, Any]:
        self.calls.append(("embed_fill", collection))
        return {"filled": 1}

    def promote(self, collection: str) -> dict[str, Any]:
        self.calls.append(("promote", collection))
        if collection in self._promote_errors:
            raise self._promote_errors[collection]
        return {"promoted": 1}

    def finalize(self) -> dict[str, Any]:
        self.calls.append(("finalize", None))
        if self._finalize_error is not None:
            raise self._finalize_error
        return {"residual_mismatched": 0, "dangling_manifest": 0}

    def verify(self, report: dict[str, Any]) -> None:
        self.calls.append(("verify", report))
        if self._verify_error is not None:
            raise self._verify_error

    def clear_staging(self) -> None:
        self.calls.append(("clear", None))
        if self._clear_error is not None:
            raise self._clear_error


def _run_ltt(
    det: DetectionReport,
    staging: _FakeStaging,
    *,
    run_t2=lambda _s: _CLEAN_T2,
    quiesce_check=_noop_quiesce,
    model_gate=_noop_model_gate,
    on_progress=None,
    cross_model_targets=None,
) -> LandThenTransformOutcome:
    return run_land_then_transform_migration(
        det,
        sources=None,
        census_check=staging.census_check,
        land=staging.land,
        embed_fill=staging.embed_fill,
        promote=staging.promote,
        finalize=staging.finalize,
        verify=staging.verify,
        clear_staging=staging.clear_staging,
        voyage_key_present=False,
        run_t2=run_t2,
        quiesce_check=quiesce_check,
        model_gate=model_gate,
        on_progress=on_progress,
        started_at=_FIXED_STARTED_AT,
        cross_model_targets=cross_model_targets,
    )


# --------------------------------------------------------------------------
# (a) Happy path — exact call order
# --------------------------------------------------------------------------


def test_ltt_happy_path_exact_call_order_single_collection() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging()

    outcome = _run_ltt(det, staging)

    assert outcome.ok is True
    assert [name for name, _ in staging.calls] == [
        "census", "land", "embed_fill", "promote", "finalize", "verify", "clear",
    ]
    assert current_phase() == "migrated"


def test_ltt_happy_path_exact_call_order_multi_collection() -> None:
    det = _detection(
        _cls("code__a__bge-base-en-v15-768__v1", "local"),
        _cls("knowledge__b__bge-base-en-v15-768__v1", "cloud"),
    )
    staging = _FakeStaging()

    outcome = _run_ltt(det, staging)

    assert outcome.ok is True
    # census -> land -> per-collection(embed_fill, promote) sorted -> ONE
    # finalize -> ONE verify -> ONE clear (never per-collection).
    assert [name for name, _ in staging.calls] == [
        "census", "land",
        "embed_fill", "promote",
        "embed_fill", "promote",
        "finalize", "verify", "clear",
    ]
    collections_seen = [c for name, c in staging.calls if name == "embed_fill"]
    assert collections_seen == sorted(collections_seen)  # deterministic order
    assert outcome.collections_ok == tuple(sorted(collections_seen))
    assert outcome.collections_total == 2
    assert outcome.collections_done == 2


# --------------------------------------------------------------------------
# (b) Census block stops before any landing
# --------------------------------------------------------------------------


def test_ltt_census_block_stops_before_any_landing() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging(census_error=RuntimeError("chash-bearing column mystery_store.ref unclaimed"))

    outcome = _run_ltt(det, staging)

    assert outcome.ok is False
    assert [name for name, _ in staging.calls] == ["census"]  # land NEVER called
    assert "mystery_store.ref" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


def test_ltt_land_failure_stops_before_t2_and_promote() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging(land_error=RuntimeError("staging load 500"))
    t2_calls: list[int] = []

    outcome = _run_ltt(det, staging, run_t2=lambda _s: t2_calls.append(1) or _CLEAN_T2)

    assert outcome.ok is False
    assert [name for name, _ in staging.calls] == ["census", "land"]
    assert t2_calls == []
    assert "staging load 500" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


# --------------------------------------------------------------------------
# (c) Promote failure marks migrated-failed, staging NOT cleared
# --------------------------------------------------------------------------


def test_ltt_promote_failure_marks_failed_staging_not_cleared() -> None:
    det = _detection(
        _cls("code__a__bge-base-en-v15-768__v1", "local"),
        _cls("knowledge__b__bge-base-en-v15-768__v1", "cloud"),
    )
    staging = _FakeStaging(
        promote_errors={"knowledge__b__bge-base-en-v15-768__v1": RuntimeError("PromotePreconditionException: NULL embeddings")},
    )

    outcome = _run_ltt(det, staging)

    assert outcome.ok is False
    assert outcome.collections_attempted == (
        "code__a__bge-base-en-v15-768__v1", "knowledge__b__bge-base-en-v15-768__v1",
    )
    assert outcome.collections_ok == ("code__a__bge-base-en-v15-768__v1",)
    assert "clear" not in [name for name, _ in staging.calls]  # resume semantics: staging retained
    assert "finalize" in [name for name, _ in staging.calls]  # finalize still runs for the wave
    assert current_phase() == "migrated-failed"
    assert read_state().failure is not None


def test_ltt_all_collections_fail_promote_staging_not_cleared() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging(
        promote_errors={"code__a__bge-base-en-v15-768__v1": RuntimeError("nexus untouched — no rows promoted")},
    )

    outcome = _run_ltt(det, staging)

    assert outcome.ok is False
    assert outcome.collections_ok == ()
    assert "clear" not in [name for name, _ in staging.calls]


# --------------------------------------------------------------------------
# (d) Finalize after every wave — a second wave (late collection) re-finalizes
# --------------------------------------------------------------------------


def test_ltt_finalize_called_once_per_wave_and_again_on_a_later_wave() -> None:
    # Wave 1: one collection lands and promotes; finalize fires once.
    det1 = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging1 = _FakeStaging()
    outcome1 = _run_ltt(det1, staging1)
    assert outcome1.ok is True
    assert [name for name, _ in staging1.calls].count("finalize") == 1

    # Wave 2: a LATE collection is detected in a subsequent run — its own
    # sequencer invocation lands/promotes/finalizes independently. Finalize
    # is idempotent/re-runnable (design reconciliation C2): each wave gets
    # exactly one finalize call, never zero, never accumulated across waves.
    det2 = _detection(_cls("knowledge__late__bge-base-en-v15-768__v1", "cloud"))
    staging2 = _FakeStaging()
    outcome2 = _run_ltt(det2, staging2)
    assert outcome2.ok is True
    assert [name for name, _ in staging2.calls].count("finalize") == 1
    assert outcome2.finalize_report is not None


# --------------------------------------------------------------------------
# (e) Count-parity (verify) mismatch fails the run
# --------------------------------------------------------------------------


def test_ltt_verify_mismatch_fails_run_staging_not_cleared() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging(verify_error=RuntimeError("count-parity mismatch: source=10 staged=10 promoted=9"))

    outcome = _run_ltt(det, staging)

    assert outcome.ok is False
    assert "count-parity mismatch" in (outcome.blocked_reason or "")
    calls = [name for name, _ in staging.calls]
    assert calls == ["census", "land", "embed_fill", "promote", "finalize", "verify"]
    assert "clear" not in calls
    assert current_phase() == "migrated-failed"


def test_ltt_finalize_failure_never_reaches_verify_or_clear() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging(finalize_error=RuntimeError("finalize 500: residual_mismatched=3"))

    outcome = _run_ltt(det, staging)

    assert outcome.ok is False
    calls = [name for name, _ in staging.calls]
    assert calls == ["census", "land", "embed_fill", "promote", "finalize"]
    assert "verify" not in calls
    assert "clear" not in calls


def test_ltt_clear_failure_does_not_flip_a_verified_run_to_unmarked_migrated() -> None:
    # A clear failure happens AFTER verify passed — the migration itself is
    # complete and correct; only the housekeeping step failed. The sentinel
    # still records the failure (never silently swallowed) but the run is
    # NOT reported as a data-loss/partial-promote condition.
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging(clear_error=RuntimeError("staging clear 500"))

    outcome = _run_ltt(det, staging)

    assert outcome.ok is False  # the clear step itself failed — surfaced, not silent
    assert "staging clear 500" in (outcome.blocked_reason or "")
    assert [name for name, _ in staging.calls] == [
        "census", "land", "embed_fill", "promote", "finalize", "verify", "clear",
    ]


# --------------------------------------------------------------------------
# T2 dirty / raise gating (unchanged property, new wording)
# --------------------------------------------------------------------------


def test_ltt_dirty_t2_blocks_promote_never_reaches_land_collections() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging()

    outcome = _run_ltt(det, staging, run_t2=lambda _s: _DIRTY_T2)

    assert outcome.ok is False
    assert [name for name, _ in staging.calls] == ["census", "land"]  # promote never called
    assert outcome.t2_total_failed == 3
    assert "total_failed" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


def test_ltt_t2_raise_transitions_to_failed_not_stuck_migrating() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging()

    def _run_t2(_s):  # type: ignore[no-untyped-def]
        raise RuntimeError("service crashed mid-T2")

    outcome = _run_ltt(det, staging, run_t2=_run_t2)

    assert outcome.ok is False
    assert [name for name, _ in staging.calls] == ["census", "land"]
    assert "service crashed mid-T2" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


# --------------------------------------------------------------------------
# Pre-gate ordering + fresh-user no-op (unchanged properties)
# --------------------------------------------------------------------------


def test_ltt_pregates_run_before_census_in_order() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    order: list[str] = []
    staging = _FakeStaging()
    original_census = staging.census_check
    staging.census_check = lambda: order.append("census") or original_census()  # type: ignore[method-assign]

    _run_ltt(
        det, staging,
        quiesce_check=lambda: order.append("quiesce"),
        model_gate=lambda c, *, voyage_key_present, exempt=frozenset(): order.append("models"),
    )
    assert order == ["quiesce", "models", "census"]


def test_ltt_model_gate_block_stops_before_census() -> None:
    from nexus.migration.pregate import ModelPreGateBlocked

    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging()

    def _model_gate(classifications, *, voyage_key_present, exempt=frozenset()) -> None:  # type: ignore[no-untyped-def]
        raise ModelPreGateBlocked([("code__a__bge-base-en-v15-768__v1", "unservable")])

    outcome = _run_ltt(det, staging, model_gate=_model_gate)
    assert outcome.ok is False
    assert staging.calls == []  # census never reached
    assert "unservable" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


def test_ltt_quiesce_block_stops_before_census() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging()

    def _quiesce() -> None:
        raise MigrationQuiesceBlocked([4321], Path("/tmp/locks"))

    outcome = _run_ltt(det, staging, quiesce_check=_quiesce)
    assert outcome.ok is False
    assert staging.calls == []
    assert "4321" in (outcome.blocked_reason or "")
    assert current_phase() == "migrated-failed"


def test_ltt_fresh_user_no_op_success_without_touching_anything() -> None:
    det = _detection()  # no collections with data
    staging = _FakeStaging()
    t2_calls: list[int] = []

    outcome = _run_ltt(det, staging, run_t2=lambda _s: t2_calls.append(1) or _CLEAN_T2)

    assert outcome.ok is True
    assert outcome.collections_total == 0
    assert t2_calls == []
    assert staging.calls == []
    assert current_phase() == "not-migrating"


def test_ltt_progress_updates_monotonically_to_total() -> None:
    det = _detection(
        _cls("code__a__bge-base-en-v15-768__v1", "local"),
        _cls("code__b__bge-base-en-v15-768__v1", "local"),
        _cls("knowledge__c__bge-base-en-v15-768__v1", "cloud"),
    )
    staging = _FakeStaging()
    progress: list[tuple[int, int]] = []

    outcome = _run_ltt(det, staging, on_progress=lambda done, total: progress.append((done, total)))

    assert outcome.collections_total == 3
    assert outcome.collections_done == 3
    dones = [d for d, _ in progress]
    assert dones == sorted(dones)
    assert all(t == 3 for _, t in progress)
    assert progress[-1] == (3, 3)


def test_ltt_phase_is_migrating_during_land_t2_and_promote() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    seen_phase: dict[str, str] = {}
    staging = _FakeStaging()
    original_land = staging.land
    staging.land = lambda: seen_phase.setdefault("land", current_phase()) or original_land()  # type: ignore[method-assign]

    def _run_t2(_s):  # type: ignore[no-untyped-def]
        seen_phase["t2"] = current_phase()
        return _CLEAN_T2

    original_promote = staging.promote
    def _promote(c: str):  # type: ignore[no-untyped-def]
        seen_phase.setdefault("promote", current_phase())
        return original_promote(c)
    staging.promote = _promote  # type: ignore[method-assign]

    _run_ltt(det, staging, run_t2=_run_t2)
    assert seen_phase["land"] == "migrating"
    assert seen_phase["t2"] == "migrating"
    assert seen_phase["promote"] == "migrating"


def test_ltt_cross_model_targets_exempt_passed_to_model_gate() -> None:
    src = "knowledge__a__minilm-l6-v2-384__v1"
    det = _detection(_cls(src, "local"))
    staging = _FakeStaging()
    exempt_seen: list[frozenset] = []

    def _gate(classifications, *, voyage_key_present, exempt=frozenset()) -> None:  # type: ignore[no-untyped-def]
        exempt_seen.append(exempt)

    outcome = _run_ltt(
        det, staging, model_gate=_gate,
        cross_model_targets={src: "knowledge__a__bge-base-en-v15-768__v1"},
    )
    assert outcome.ok is True
    assert exempt_seen == [frozenset({src})]


# --------------------------------------------------------------------------
# (f) T2-exclusion split — the guided migrate-all store list excludes
# exactly the fully-landed stores.
# --------------------------------------------------------------------------


class TestGuidedT2ExclusionSplit:
    def test_guided_land_excluded_stores_pinned_exact_set(self) -> None:
        from nexus.migration.orchestrator import GUIDED_LAND_EXCLUDED_STORES

        # A future store addition to LADDER_ORDER must be deliberately
        # classified landed-vs-not here, never silently included/excluded.
        assert GUIDED_LAND_EXCLUDED_STORES == frozenset({"chash", "aspects_queue"})

    def test_guided_store_etls_covers_every_ladder_store(self) -> None:
        from nexus.migration.etl_registry import EtlSources, LADDER_ORDER
        from nexus.migration.orchestrator import build_guided_store_etls

        etls = build_guided_store_etls(
            EtlSources(sqlite_path=None, catalog_db_path=None),  # type: ignore[arg-type]
        )
        assert {e.store for e in etls} == set(LADDER_ORDER)

    def test_guided_aspects_slot_is_non_chash_runner(self) -> None:
        from nexus.migration.etl_registry import EtlSources
        from nexus.migration.orchestrator import _aspects_non_chash, build_guided_store_etls

        etls = build_guided_store_etls(
            EtlSources(sqlite_path=None, catalog_db_path=None),  # type: ignore[arg-type]
        )
        aspects_etl = next(e for e in etls if e.store == "aspects")
        assert aspects_etl.run is _aspects_non_chash

    def test_guided_taxonomy_slot_is_non_chash_runner(self) -> None:
        """RDR-180 nexus-jxizy.10.7 completion pass: taxonomy's guided slot
        now excludes topic_assignments (chash-bearing) at table grain."""
        from nexus.migration.etl_registry import EtlSources
        from nexus.migration.orchestrator import _taxonomy_non_chash, build_guided_store_etls

        etls = build_guided_store_etls(
            EtlSources(sqlite_path=None, catalog_db_path=None),  # type: ignore[arg-type]
        )
        taxonomy_etl = next(e for e in etls if e.store == "taxonomy")
        assert taxonomy_etl.run is _taxonomy_non_chash

    def test_guided_telemetry_slot_is_non_chash_runner(self) -> None:
        """RDR-180 nexus-jxizy.10.7 completion pass: telemetry's guided slot
        now excludes relevance_log + frecency (chash-bearing) at table grain."""
        from nexus.migration.etl_registry import EtlSources
        from nexus.migration.orchestrator import _telemetry_non_chash, build_guided_store_etls

        etls = build_guided_store_etls(
            EtlSources(sqlite_path=None, catalog_db_path=None),  # type: ignore[arg-type]
        )
        telemetry_etl = next(e for e in etls if e.store == "telemetry")
        assert telemetry_etl.run is _telemetry_non_chash

    def test_guided_catalog_slot_is_non_chash_runner(self) -> None:
        """RDR-180 nexus-jxizy.10.7 completion pass: catalog's guided slot
        now excludes document_chunks (chash-bearing manifest) at table grain."""
        from nexus.migration.etl_registry import EtlSources
        from nexus.migration.orchestrator import _catalog_non_chash, build_guided_store_etls

        etls = build_guided_store_etls(
            EtlSources(sqlite_path=None, catalog_db_path=None),  # type: ignore[arg-type]
        )
        catalog_etl = next(e for e in etls if e.store == "catalog")
        assert catalog_etl.run is _catalog_non_chash

    def test_guided_non_chash_runners_pinned_exact_set(self) -> None:
        """A future store addition to LADDER_ORDER must be deliberately
        classified into _GUIDED_NON_CHASH_RUNNERS or
        GUIDED_LAND_EXCLUDED_STORES — never silently left running its full
        legacy ETL (and therefore double-writing a stale legacy-id copy)
        under the guided path."""
        from nexus.migration.orchestrator import _GUIDED_NON_CHASH_RUNNERS

        assert set(_GUIDED_NON_CHASH_RUNNERS) == {
            "aspects", "taxonomy", "telemetry", "catalog",
        }

    def test_guided_slots_cover_every_chash_bearing_ladder_store(self) -> None:
        """Every LADDER_ORDER store that carries a chash-bearing table is
        EITHER wholesale-excluded (GUIDED_LAND_EXCLUDED_STORES) OR runs a
        non-chash-only runner (_GUIDED_NON_CHASH_RUNNERS) — no such store is
        silently left on the full legacy ETL under the guided path (the
        residual this completion pass closes). "memory" and "plans" are the
        only LADDER_ORDER stores with NO chash-bearing table and correctly
        run their normal legacy ETL unmodified under the guided path."""
        from nexus.migration.etl_registry import LADDER_ORDER
        from nexus.migration.orchestrator import (
            GUIDED_LAND_EXCLUDED_STORES,
            _GUIDED_NON_CHASH_RUNNERS,
        )

        classified = GUIDED_LAND_EXCLUDED_STORES | set(_GUIDED_NON_CHASH_RUNNERS)
        unclassified = set(LADDER_ORDER) - classified
        assert unclassified == {"memory", "plans"}

    def test_migrate_all_guided_folds_excluded_stores_into_skip_stores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.migration import orchestrator as orch
        from nexus.migration.etl_registry import EtlSources, LADDER_ORDER

        captured: dict[str, Any] = {}

        def _fake_migrate_all(sources, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return {"summary": {"total_failed": 0}}

        monkeypatch.setattr(orch, "migrate_all", _fake_migrate_all)
        sources = EtlSources(sqlite_path=None, catalog_db_path=None)  # type: ignore[arg-type]
        orch.migrate_all_guided(sources)

        assert captured["skip_stores"] == frozenset({"chash", "aspects_queue"})
        assert {e.store for e in captured["etls"]} == set(LADDER_ORDER)

    def test_migrate_all_guided_unions_caller_skip_stores(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from nexus.migration import orchestrator as orch
        from nexus.migration.etl_registry import EtlSources

        captured: dict[str, Any] = {}

        def _fake_migrate_all(sources, **kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)
            return {"summary": {"total_failed": 0}}

        monkeypatch.setattr(orch, "migrate_all", _fake_migrate_all)
        sources = EtlSources(sqlite_path=None, catalog_db_path=None)  # type: ignore[arg-type]
        orch.migrate_all_guided(sources, skip_stores=frozenset({"memory"}))

        assert captured["skip_stores"] == frozenset({"chash", "aspects_queue", "memory"})


def test_ltt_default_run_t2_is_migrate_all_guided() -> None:
    det = _detection(_cls("code__a__bge-base-en-v15-768__v1", "local"))
    staging = _FakeStaging()
    captured: dict[str, Any] = {}

    import nexus.migration.orchestrator as orch

    def _fake_migrate_all_guided(sources, **kwargs):  # type: ignore[no-untyped-def]
        captured["called"] = True
        return {"summary": {"total_failed": 0}}

    # run_land_then_transform_migration defers its import of migrate_all_guided
    # (import-cycle guard) — patch it at the source module so the default
    # resolves to the patched callable.
    real_migrate_all_guided = orch.migrate_all_guided
    try:
        orch.migrate_all_guided = _fake_migrate_all_guided  # type: ignore[assignment]
        outcome = run_land_then_transform_migration(
            det,
            sources=None,
            census_check=staging.census_check,
            land=staging.land,
            embed_fill=staging.embed_fill,
            promote=staging.promote,
            finalize=staging.finalize,
            verify=staging.verify,
            clear_staging=staging.clear_staging,
            voyage_key_present=False,
            quiesce_check=_noop_quiesce,
            model_gate=_noop_model_gate,
            started_at=_FIXED_STARTED_AT,
        )
    finally:
        orch.migrate_all_guided = real_migrate_all_guided  # type: ignore[assignment]

    assert outcome.ok is True
    assert captured.get("called") is True
