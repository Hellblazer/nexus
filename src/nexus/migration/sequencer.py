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
        from nexus.collection_rename import remap_collection_references  # noqa: PLC0415  — circular-dep avoidance (nexus.collection_rename)

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


# ═══════════════════════════════════════════════════════════════════════════
# RDR-180 LAND-THEN-TRANSFORM (nexus-jxizy.10.7): the NEW sequencing driver.
#
# :func:`run_sequenced_migration` above (RDR-159 P2, T2-then-T3-per-leg) is
# LEFT IN PLACE — ``driver.run_guided_upgrade`` (the CLI-facing guided-upgrade
# engine) and its full test surface (``tests/migration/test_driver.py``,
# ``tests/migration/test_e2e_oracle.py`` — RDR-159's epic-exit "e2e oracle",
# ``tests/migration/test_detection.py``'s CLI wiring,
# ``tests/migration/test_migration_contract.py``'s surface pin) all still
# depend on it verbatim. Rewiring ``driver.py`` onto the new flow needs (a)
# bead nexus-jxizy.10.8's pregate evolution (the width-block removal / landing
# manifest becoming a pregate OUTPUT) and (b) the ".10.10 --guided gate
# rewrite" bead that owns ``test_e2e_oracle.py`` / ``rehearse_guided.sh`` —
# both explicitly OUT of THIS bead's boundary. Retiring
# ``run_sequenced_migration`` in place here would have broken all four of
# those surfaces for no reason this bead can responsibly resolve.
#
# :func:`run_land_then_transform_migration` below is the NEW machinery
# (design T2 ``nexus_rdr/180-land-transform-design`` + its reconciliation,
# the BINDING obligations ledger on this bead) — fully built and unit-tested
# against fakes, ready for the follow-on integration bead to wire real
# collaborators (``staging_land.HttpStagingStore`` + SQLite/Chroma source
# readers) into ``driver.py`` once .10.8/.10.9/.10.10 land.
# ═══════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class LandThenTransformOutcome:
    """The result of one LAND-THEN-TRANSFORM sequenced migration run.

    ``ok`` is True only on a full T2-clean + every-collection-promoted +
    finalize-verified + staging-cleared run. ``blocked_reason`` names the
    first hard stop (quiesce / model gate / census / land / dirty T2 / a
    failed collection / finalize / verify / clear).

    ``collections_attempted`` / ``collections_ok`` are the LAND-THEN-
    TRANSFORM counterpart to :class:`SequenceOutcome`'s retired
    ``legs_attempted`` / ``legs_ok`` (design Q4: "T2-before-T3 ordering:
    MOOT" — there is no more separate per-leg T3 copy; land+promote runs
    uniformly across every detected leg's collections).
    """

    ok: bool
    phase: str
    collections_total: int
    collections_done: int
    t2_total_failed: int | None
    collections_attempted: tuple[str, ...]
    collections_ok: tuple[str, ...]
    blocked_reason: str | None
    t2_report: dict[str, Any] | None
    #: The ``/v1/staging/finalize`` envelope from the (at most one) finalize
    #: call this run made. ``None`` when the run never reached finalize.
    finalize_report: dict[str, Any] | None = None


def run_land_then_transform_migration(
    detection: DetectionReport,
    *,
    sources: Any,
    census_check: Callable[[], None],
    land: Callable[[], dict[str, int]],
    embed_fill: Callable[[str], dict[str, Any]],
    promote: Callable[[str], dict[str, Any]],
    finalize: Callable[[], dict[str, Any]],
    verify: Callable[[dict[str, Any]], None],
    clear_staging: Callable[[], None],
    voyage_key_present: bool,
    run_t2: "Callable[[Any], dict[str, Any]] | None" = None,
    quiesce_check: Callable[[], None] = assert_quiescent_for_migration,
    model_gate: Callable[..., None] = assert_models_supported,
    on_progress: Callable[[int, int], None] | None = None,
    started_at: str | None = None,
    cross_model_targets: dict[str, str] | None = None,
) -> LandThenTransformOutcome:
    """Drive the LAND-THEN-TRANSFORM sequence for a detected Chroma footprint.

      quiesce -> model gate -> PRE-LAND SOURCE CENSUS -> LAND (pointer stores
      + chunk content) -> NON-CHASH T2 ETL -> per collection (embed_fill,
      promote) -> finalize (ONCE per wave) -> verify -> clear staging ->
      mark_migrated.

    Every collaborator is injected — the caller wires the real
    :class:`~nexus.migration.staging_land.HttpStagingStore` + SQLite/Chroma
    source readers; a unit test wires fakes:

    * ``census_check()`` — the pre-land every-column census
      (:func:`nexus.migration.staging_land.source_census` in production).
      Raises to block BEFORE any row lands.
    * ``land()`` — lands every pointer store + every data-bearing
      collection's chunks (honest land-time classification: the SAME
      ``vector_etl.cross_model_target_name`` + nexus-nb7hr measured-dim
      override policy the retired per-leg ETL used, resolved once at land
      time — reconciliation H1). Returns a landed-count dict (opaque to the
      sequencer; carried into the log only). Raises to block — a partial
      land leaves nexus untouched (staging's upsert-on-natural-key landing
      converges on re-run).
    * ``embed_fill(collection)`` / ``promote(collection)`` — per collection,
      in that order (promote refuses NULL embeddings, obligation ledger item
      1). Each may raise; a raise demotes that ONE collection (recorded, not
      fatal to the run) so every collection is attempted and the
      refuse-partial check below sees the full picture.
    * ``finalize()`` — called ONCE, after every collection has been
      attempted (one "wave"; design reconciliation C2). Raises to block
      (never cleared on a failed finalize).
    * ``verify(finalize_report)`` — raises on a count-parity / census
      mismatch. Never called on a blocked finalize; never clears staging on
      a mismatch (resume semantics).
    * ``clear_staging()`` — called ONLY after a verified, fully-successful
      run. NEVER called on any block (the crash matrix's "crash-during-land"
      / "crash-mid-promote" / mismatch cells all retain staging for resume).

    ``run_t2`` (RDR-180 nexus-jxizy.10.7 T2-exclusion split) defaults to
    :func:`nexus.migration.orchestrator.migrate_all_guided` — the non-chash
    T2 store list (``chash`` / ``aspects_queue`` excluded wholesale, the
    ``aspects`` slot running highlights+promotion-log only; see that
    function's docstring for the full split rationale, including the
    documented residual on the three still-monolithic mixed stores).
    Obligation ledger items 2+3: this MUST complete (``total_failed == 0``)
    before the first ``finalize`` — catalog documents / topic labels must
    resolve before the manifest promote's FK / topic-assignment resolution.

    ``cross_model_targets`` maps each cross-model source collection to its
    model-remapped target, passed to ``model_gate`` as the ``exempt`` set.
    Unlike the retired :func:`run_sequenced_migration`'s ``remap_refs``, there
    is no client-side reference re-point left to inject here — ``promote`` /
    ``finalize`` repoint catalog/topic references server-side via the in-DB
    ``chash_alias`` join (design Q3).
    """
    if run_t2 is None:
        from nexus.migration.orchestrator import migrate_all_guided  # noqa: PLC0415 — deferred to avoid import cycle / CLI startup cost

        run_t2 = migrate_all_guided

    total = sum(1 for c in detection.classifications if c.has_data)
    collections = tuple(
        sorted(c.collection for c in detection.classifications if c.has_data)
    )

    # 1. Fresh user: nothing data-bearing -> no-op success. Never set the
    #    sentinel, never touch T2/T3/staging.
    if total == 0:
        _log.info("land_then_transform_fresh_user_noop")
        return LandThenTransformOutcome(
            ok=True,
            phase="not-migrating",
            collections_total=0,
            collections_done=0,
            t2_total_failed=None,
            collections_attempted=(),
            collections_ok=(),
            blocked_reason=None,
            t2_report=None,
        )

    # 2. Set the sentinel FIRST so every poller suspends/degrades before any
    #    data moves.
    begin_migration(collections_total=total, started_at=started_at)

    def _fail(reason: str, *, finalize_report: dict[str, Any] | None = None) -> LandThenTransformOutcome:
        mark_failed(reason)
        return LandThenTransformOutcome(
            ok=False,
            phase=current_phase(),
            collections_total=total,
            collections_done=0,
            t2_total_failed=None,
            collections_attempted=(),
            collections_ok=(),
            blocked_reason=reason,
            t2_report=None,
            finalize_report=finalize_report,
        )

    # 3. Quiesce write-lock audit (RF-6) — unchanged.
    try:
        quiesce_check()
    except Exception as exc:  # noqa: BLE001 — MigrationQuiesceBlocked (or any audit failure); surfaced via log + _fail() to caller
        _log.warning("land_then_transform_quiesce_blocked", error=str(exc))
        return _fail(str(exc))

    # 4. Per-collection-model pre-gate (gates S1 + C3) — before any ETL.
    exempt = frozenset(cross_model_targets or {})
    try:
        model_gate(
            detection.classifications,
            voyage_key_present=voyage_key_present,
            exempt=exempt,
        )
    except Exception as exc:  # noqa: BLE001 — ModelPreGateBlocked (or any gate failure); surfaced via log + _fail() to caller
        _log.warning("land_then_transform_model_gate_blocked", error=str(exc))
        return _fail(str(exc))

    # 5. PRE-LAND SOURCE CENSUS — fail BEFORE a single row lands (Hal
    #    directive; the missed-leg killer).
    try:
        census_check()
    except Exception as exc:  # noqa: BLE001 — StagingCensusError (or any census failure); surfaced via log + _fail() to caller
        reason = f"pre-land source census blocked: {exc}"
        _log.error("land_then_transform_census_blocked", error=str(exc))
        return _fail(reason)

    # 6. LAND all stores — pointer stores + chunk content, honest land-time
    #    classification. Nothing has been promoted yet: a raise here leaves
    #    staging partially populated (upsert-idempotent on natural key) and
    #    nexus untouched — the crash matrix's "crash-during-land" cell.
    try:
        land_summary = land()
    except Exception as exc:  # noqa: BLE001 — recorded, never silent
        reason = f"landing failed: {exc}; nexus untouched, staging may hold a partial (idempotent) load"
        _log.error("land_then_transform_land_failed", error=str(exc))
        return _fail(reason)
    _log.info("land_then_transform_land_complete", summary=land_summary)

    # 7. NON-CHASH T2 ETLs — memory/plans/taxonomy-topics/telemetry-non-
    #    chash/catalog-documents. REQUIRE total_failed == 0 before promoting
    #    (obligation ledger items 2+3: catalog docs + topics must resolve
    #    before the first finalize).
    try:
        t2_report = run_t2(sources)
    except Exception as exc:  # noqa: BLE001 — recorded to the sentinel, never silent
        reason = f"T2 non-chash migrate-all raised unexpectedly: {exc}; promote not started"
        _log.error("land_then_transform_t2_raised", error=str(exc))
        return _fail(reason)
    t2_total_failed = int(t2_report.get("summary", {}).get("total_failed", 0))
    if t2_total_failed != 0:
        reason = (
            f"T2 non-chash migrate-all reported total_failed={t2_total_failed}; "
            "promote not started (finalize's catalog-doc FK / topic-label "
            "resolution would be vacuous against a dirty T2)."
        )
        _log.warning("land_then_transform_t2_dirty_blocks_promote", total_failed=t2_total_failed)
        mark_failed(reason)
        return LandThenTransformOutcome(
            ok=False,
            phase=current_phase(),
            collections_total=total,
            collections_done=0,
            t2_total_failed=t2_total_failed,
            collections_attempted=(),
            collections_ok=(),
            blocked_reason=reason,
            t2_report=t2_report,
        )

    # 8. Per collection: embed_fill THEN promote. A raise demotes ONLY that
    #    collection (recorded, run continues) so refuse-partial sees the full
    #    picture and the sentinel never gets stuck `migrating`.
    attempted: list[str] = []
    ok_collections: list[str] = []
    done = 0
    for collection in collections:
        attempted.append(collection)
        try:
            embed_fill(collection)
            promote(collection)
        except Exception as exc:  # noqa: BLE001 — per-collection failure, recorded below
            _log.error("land_then_transform_promote_failed", collection=collection, error=str(exc))
            continue
        done += 1
        shown = min(done, total)
        record_progress(collections_done=shown, collections_total=total)
        if on_progress is not None:
            on_progress(shown, total)
        ok_collections.append(collection)

    # 9. finalize — ONE call for this wave, regardless of per-collection
    #    outcome (design reconciliation C2: idempotent re-runnable; a
    #    caller re-invoking this function for a late-landed collection is a
    #    second wave and re-finalizes on its own call).
    try:
        finalize_report = finalize()
    except Exception as exc:  # noqa: BLE001 — recorded, staging kept for resume
        reason = f"finalize failed: {exc}; staging retained for resume"
        _log.error("land_then_transform_finalize_failed", error=str(exc))
        return _fail(reason)

    # 10. verify — count-parity / census gate. NEVER clears staging on a
    #     mismatch (resume semantics).
    try:
        verify(finalize_report)
    except Exception as exc:  # noqa: BLE001 — recorded, staging kept for resume
        reason = f"verify failed: {exc}; staging retained for resume"
        _log.error("land_then_transform_verify_failed", error=str(exc))
        return _fail(reason, finalize_report=finalize_report)

    # 11. Refuse partial: every detected collection must have promoted OK.
    full_success = set(ok_collections) == set(collections)
    if not full_success:
        missing = sorted(set(collections) - set(ok_collections))
        reason = (
            f"partial promotion: detected collections {list(collections)} but "
            f"only {ok_collections} promoted (failed/incomplete: {missing}). "
            "Refusing partial success — re-run; landing/promote are idempotent. "
            "Staging retained for resume."
        )
        _log.warning(
            "land_then_transform_partial_promote_refused",
            collections=list(collections), ok=ok_collections,
        )
        mark_failed(reason)
        return LandThenTransformOutcome(
            ok=False,
            phase=current_phase(),
            collections_total=total,
            collections_done=done,
            t2_total_failed=t2_total_failed,
            collections_attempted=tuple(attempted),
            collections_ok=tuple(ok_collections),
            blocked_reason=reason,
            t2_report=t2_report,
            finalize_report=finalize_report,
        )

    # Clean run: staging cleared ONLY now.
    try:
        clear_staging()
    except Exception as exc:  # noqa: BLE001 — recorded; the migration itself is complete and verified
        reason = (
            f"staging clear failed: {exc}; migration is complete and verified — "
            "staging rows are inert leftovers, safe to clear manually "
            "(clear is idempotent TRUNCATE)"
        )
        _log.error("land_then_transform_clear_staging_failed", error=str(exc))
        return _fail(reason, finalize_report=finalize_report)

    mark_migrated()
    return LandThenTransformOutcome(
        ok=True,
        phase=current_phase(),
        collections_total=total,
        collections_done=done,
        t2_total_failed=t2_total_failed,
        collections_attempted=tuple(attempted),
        collections_ok=tuple(ok_collections),
        blocked_reason=None,
        t2_report=t2_report,
        finalize_report=finalize_report,
    )
