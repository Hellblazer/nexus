# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P2 (nexus-ue6g7.16): the T2-then-T3 sequencing driver.

The guided migration's orchestration step. It drives the proven primitives in
the ONE survivable order RDR-159 §Approach P2 + the P1→P2 seam mandate, wrapping
zero new ETL:

  1. fresh-user short-circuit — no data-bearing leg → clean no-op success, the
     sentinel is never set and nothing is touched;
  2. ``begin_migration`` — set ``phase=migrating`` FIRST so every separate
     process (read surfaces, aspect workers, ``nx index``) observes it and
     suspends/degrades before any data moves;
  3. quiesce write-lock audit — no foreign aspect-worker write-lock survives the
     now-visible sentinel (else the RF-6 T3 count window is false-failing);
  4. per-collection-model pre-gate — an unservable model fails BEFORE any ETL;
  5. T2 ``migrate all`` — REQUIRE ``summary.total_failed == 0`` before T3 starts
     (a dirty T2 keeps the downstream manifest validation non-vacuous);
  6. T3 ``migrate vectors`` for EVERY detected leg — REFUSE partial-leg success
     (a single-leg success is not multi-leg success);
  7. ``mark_migrated`` / ``mark_failed``; ``collections_done/total`` drives the
     progress surface throughout.

Every external dependency (the T2 callable, the per-leg ETL, the two pre-gates)
is injected so the sequencing contract is testable without a live service /
Chroma. State transitions use the real P1a sentinel.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import structlog

from nexus.migration.detection import DetectionReport
from nexus.migration.orchestrator import migrate_all
from nexus.migration.pregate import assert_models_supported
from nexus.migration.quiesce import assert_quiescent_for_migration
from nexus.migration.state import (
    begin_migration,
    current_phase,
    mark_failed,
    mark_migrated,
    record_progress,
)
from nexus.migration.vector_etl import MigrationReport

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SequenceOutcome:
    """The result of one sequenced migration run.

    ``ok`` is True only on a full T2-clean + all-legs-ok run. ``blocked_reason``
    names the first hard stop (quiesce / model gate / dirty T2 / partial leg).
    """

    ok: bool
    phase: str
    collections_total: int
    collections_done: int
    t2_total_failed: int | None
    legs_attempted: tuple[str, ...]
    legs_ok: tuple[str, ...]
    blocked_reason: str | None
    t2_report: dict[str, Any] | None


def _migrated_count(report: MigrationReport) -> int:
    return sum(1 for r in report.results if r.status == "migrated")


def run_sequenced_migration(
    detection: DetectionReport,
    *,
    sources: Any,
    run_leg: Callable[[str], MigrationReport],
    voyage_key_present: bool,
    run_t2: Callable[[Any], dict[str, Any]] = migrate_all,
    quiesce_check: Callable[[], None] = assert_quiescent_for_migration,
    model_gate: Callable[..., None] = assert_models_supported,
    on_progress: Callable[[int, int], None] | None = None,
    started_at: str | None = None,
    cross_model_targets: dict[str, str] | None = None,
    remap_refs: Callable[[str, str], Any] | None = None,
) -> SequenceOutcome:
    """Drive the T2-then-T3 sequence for a detected Chroma footprint.

    ``run_leg(leg)`` runs one leg's vector ETL (the caller wires the real
    ``vector_etl.migrate_local`` / ``migrate_cloud`` with clients + creds) and
    returns its :class:`MigrationReport`. ``run_t2(sources)`` runs the T2
    ``migrate all`` and returns the RDR-153 report dict. Both pre-gates raise to
    block.

    ``cross_model_targets`` (RDR-162 P2) maps each cross-model source collection
    name to its model-remapped target. Its keys are passed to ``model_gate`` as
    the ``exempt`` set (those collections are re-embedded, not blocked). After a
    leg verifies, ``remap_refs(source, target)`` re-points the T2 + catalog
    references for every cross-model result — STRICTLY after the verified report,
    so a mid-migrate failure never leaves dangling references. ``remap_refs``
    defaults to :func:`nexus.collection_rename.remap_collection_references`.
    """
    targets = cross_model_targets or {}
    if remap_refs is None:
        from nexus.collection_rename import remap_collection_references  # noqa: PLC0415

        remap_refs = remap_collection_references
    total = sum(1 for c in detection.classifications if c.has_data)
    legs = tuple(sorted(detection.legs_with_data))

    # 1. Fresh user: nothing data-bearing → no-op success. Never set the
    #    sentinel, never touch T2/T3.
    if total == 0:
        _log.info("sequencer_fresh_user_noop")
        return SequenceOutcome(
            ok=True,
            # No migration ran; report not-migrating rather than echoing a
            # possibly-stale prior-run sentinel (HIGH-1: avoids ok=True paired
            # with a leftover migrated-failed phase).
            phase="not-migrating",
            collections_total=0,
            collections_done=0,
            t2_total_failed=None,
            legs_attempted=(),
            legs_ok=(),
            blocked_reason=None,
            t2_report=None,
        )

    # 2. Set the sentinel FIRST so every poller suspends/degrades before any
    #    data moves.
    begin_migration(collections_total=total, started_at=started_at)

    def _fail(reason: str) -> SequenceOutcome:
        mark_failed(reason)
        return SequenceOutcome(
            ok=False,
            phase=current_phase(),
            collections_total=total,
            collections_done=0,
            t2_total_failed=None,
            legs_attempted=(),
            legs_ok=(),
            blocked_reason=reason,
            t2_report=None,
        )

    # 3. Quiesce write-lock audit (RF-6).
    try:
        quiesce_check()
    except Exception as exc:  # noqa: BLE001 — MigrationQuiesceBlocked (or any audit failure); surfaced via log + _fail() to caller
        _log.warning("sequencer_quiesce_blocked", error=str(exc))
        return _fail(str(exc))

    # 4. Per-collection-model pre-gate (gates S1 + C3) — before any ETL.
    try:
        model_gate(
            detection.classifications,
            voyage_key_present=voyage_key_present,
            exempt=frozenset(targets),
        )
    except Exception as exc:  # noqa: BLE001 — ModelPreGateBlocked (or any gate failure); surfaced via log + _fail() to caller
        _log.warning("sequencer_model_gate_blocked", error=str(exc))
        return _fail(str(exc))

    # 5. T2 migrate-all FIRST — require total_failed == 0 before T3. An
    #    in-process raise (service crash, network error) is CATCHABLE — the
    #    process is alive, so transition the sentinel to migrated-failed rather
    #    than leaving a false `migrating` (which the escape hatch only covers
    #    for an uncatchable crash). CRITICAL-1.
    try:
        t2_report = run_t2(sources)
    except Exception as exc:  # noqa: BLE001 — recorded to the sentinel, never silent
        reason = f"T2 migrate-all raised unexpectedly: {exc}; T3 not started"
        _log.error("sequencer_t2_raised", error=str(exc))
        return _fail(reason)
    t2_total_failed = int(t2_report.get("summary", {}).get("total_failed", 0))
    if t2_total_failed != 0:
        reason = (
            f"T2 migrate-all reported total_failed={t2_total_failed}; "
            "T3 vector migration not started (manifest validation would be "
            "vacuous on a dirty T2)."
        )
        _log.warning("sequencer_t2_dirty_blocks_t3", total_failed=t2_total_failed)
        mark_failed(reason)
        return SequenceOutcome(
            ok=False,
            phase=current_phase(),
            collections_total=total,
            collections_done=0,
            t2_total_failed=t2_total_failed,
            legs_attempted=(),
            legs_ok=(),
            blocked_reason=reason,
            t2_report=t2_report,
        )

    # 6. T3 vector migration for EVERY detected leg. Run all legs (full picture),
    #    accumulate progress, then refuse partial-leg.
    legs_attempted: list[str] = []
    legs_ok: list[str] = []
    done = 0
    for leg in legs:
        legs_attempted.append(leg)
        # A leg raising is a failed leg, not a crash: log it, leave it out of
        # legs_ok, and continue so the refuse-partial check fires with the full
        # picture (and the sentinel ends migrated-failed, never stuck migrating).
        try:
            report = run_leg(leg)
        except Exception as exc:  # noqa: BLE001 — leg failure, recorded below
            _log.error("sequencer_leg_raised", leg=leg, error=str(exc))
            continue
        # RDR-162 P2: re-point references for every cross-model collection that
        # VERIFIED populated (status == "migrated"). Each collection's remap is
        # ordered strictly after ITS OWN verified populate (_migrate_one only
        # returns "migrated" past the post-write count check), so a reference is
        # never repointed at an unpopulated target. The remap is per-collection-
        # opportunistic, NOT leg-atomic: if collection B fails after A succeeded,
        # A's refs are already repointed while the leg is demoted. That is safe,
        # not a dangling ref — A's target IS populated, so A names a live
        # collection; copy-not-move keeps the source intact and the cascade
        # UPDATE is idempotent, so the demoted leg re-runs cleanly (A re-remaps to
        # the same target, B retries). A remap failure DEMOTES the leg (drops it
        # from legs_ok) so refuse-partial marks the run failed — never a sentinel
        # stuck `migrating`.
        remap_ok = True
        for r in report.results:
            if r.status == "migrated" and r.target_collection:
                try:
                    remap_refs(r.collection, r.target_collection)
                except Exception as exc:  # noqa: BLE001 — demotes the leg, recorded
                    remap_ok = False
                    _log.error(
                        "sequencer_remap_failed",
                        leg=leg,
                        source=r.collection,
                        target=r.target_collection,
                        error=str(exc),
                    )
        done += _migrated_count(report)
        # Clamp the surface value: the store can gain collections between
        # detection and ETL (idempotent re-run), and a >100% progress display
        # is misleading. Keep the true count in the log as a drift tripwire.
        if done > total:
            _log.warning(
                "sequencer_progress_overshoot",
                migrated=done,
                detected_total=total,
                leg=leg,
            )
        shown = min(done, total)
        record_progress(collections_done=shown, collections_total=total)
        if on_progress is not None:
            on_progress(shown, total)
        if report.ok and remap_ok:
            legs_ok.append(leg)
    done = min(done, total)

    # 7. Refuse partial-leg: every detected leg must be attempted AND ok.
    full_success = set(legs_ok) == set(legs)
    if full_success:
        mark_migrated()
        blocked_reason = None
    else:
        missing = sorted(set(legs) - set(legs_ok))
        blocked_reason = (
            f"partial-leg migration: detected legs {list(legs)} but only "
            f"{legs_ok} succeeded (failed/incomplete: {missing}). Refusing "
            "partial success — re-run; the upsert is idempotent."
        )
        _log.warning("sequencer_partial_leg_refused", legs=list(legs), ok=legs_ok)
        mark_failed(blocked_reason)

    return SequenceOutcome(
        ok=full_success,
        phase=current_phase(),
        collections_total=total,
        collections_done=done,
        t2_total_failed=t2_total_failed,
        legs_attempted=tuple(legs_attempted),
        legs_ok=tuple(legs_ok),
        blocked_reason=blocked_reason,
        t2_report=t2_report,
    )
