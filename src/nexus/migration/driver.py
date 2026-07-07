# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P4 (nexus-ue6g7.24): the guided-upgrade engine entry point.

``run_guided_upgrade`` is the ONE function both surfaces call — the nexus CLI
(``nx migrate-to-service``) and the deferred conexus veneer (``conexus
upgrade``). It owns the full lifecycle that wraps the P0-P3 primitives in the
single survivable order, so neither surface re-derives the seam:

  1. DETECT     open the Chroma read legs, classify the footprint, then CLOSE
                them BEFORE any ETL (the local leg is WAL single-opener — the
                ETL must be the only opener, so detection cannot still hold it);
  2. SEQUENCE   ``run_sequenced_migration`` drives quiesce → pre-gate → T2 →
                T3-per-leg, leaving the sentinel ``migrated`` (or
                ``migrated-failed`` on any block / partial leg);
  3. VALIDATE   on a clean ``migrated``, REOPEN the data-bearing read legs and
                run the non-vacuous validation gate (taxonomy floor + counts +
                manifest-orphans); clean → unlock (clear sentinel), block →
                leave ``migrated-failed`` + offer rollback. Reopened read legs
                are closed in a ``finally``.

The engine touches NO data of its own and adds NO orchestration beyond
sequencing + lifecycle: every ETL / state / validation primitive is the proven
P0-P3 code. Collaborators (clients, paths) are injected; the low-level engine
functions are module globals so a unit test monkeypatches ``driver.<name>``
without a live service / Chroma (the existing nexus CLI-wiring test idiom).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog

from nexus.migration.detection import (
    DetectionReport,
    classify_collections,
    close_read_client,
    cross_model_remappable,
    cross_model_target_model,
    open_read_legs,
    voyage_key_available,
)
from nexus.migration.orchestrator import EtlSources
from nexus.migration.sequencer import SequenceOutcome, run_sequenced_migration
from nexus.migration.state import mark_failed
from nexus.migration.validation import (
    ValidationOutcome,
    compose_validation_checks,
    validate_migration,
)
from nexus.migration.vector_etl import (
    MigrationReport,
    _dim_for_collection,
    cross_model_target_name,
    migrate_cloud,
    migrate_local,
)

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class GuidedUpgradeResult:
    """The verdict of one guided-upgrade run + the evidence behind it.

    ``ok`` is the single user-facing success bit: True only on a fresh-user
    no-op OR a sequence that reached ``migrated`` AND validated clean (unlocked).
    ``validation`` is ``None`` when the sequence never reached validation (a
    fresh-user no-op, or a sequence block/partial-leg that left the sentinel
    ``migrated-failed`` before any validation could run).
    """

    detection: DetectionReport
    sequence: SequenceOutcome
    validation: ValidationOutcome | None
    ok: bool

    @property
    def rollback_available(self) -> bool:
        """Whether a rollback is offered (a validated block left a pgvector copy
        the user can undo to return to the immutable Chroma source)."""
        return self.validation is not None and self.validation.rollback_available


class _CompositeReadClient:
    """A read client routing ``get_collection`` to the leg that holds the name.

    ``verify_counts`` (the P3 count leg) takes ONE read client + a collection
    list, but a migration can span both legs. This composite preserves the
    single-read-client seam ``compose_validation_checks`` pins: it maps each
    collection name to the reopened read client for its source leg, so a
    two-leg validation reuses the unchanged seam instead of forking it.
    """

    def __init__(self, by_collection: dict[str, Any]) -> None:
        self._by_collection = by_collection

    def get_collection(self, name: str) -> Any:
        try:
            client = self._by_collection[name]
        except KeyError as exc:  # a collection with no reopened source leg
            raise RuntimeError(
                f"count validation: no source read leg for collection {name!r} "
                "— cannot verify its source==target count"
            ) from exc
        return client.get_collection(name)


def _default_reopen_leg(leg: str, local_path: str | Path | None) -> Any:
    """Reopen one source read leg for the validation count check.

    The ETL opened + closed its own read client per leg; validation needs a
    FRESH read client (the detection clients were closed before the ETL ran).
    """
    from nexus.migration.chroma_read import (  # noqa: PLC0415  — command-local import (nexus.migration.chroma_read)
        open_cloud_read_client,
        open_local_read_client,
    )

    if leg == "cloud":
        return open_cloud_read_client()
    # nexus-id750: a None local_path resolves via the product's env-aware
    # default inside open_local_read_client (this fallback used to hardcode
    # the never-written <config>/chroma).
    return open_local_read_client(local_path)


def run_guided_upgrade(
    *,
    sources: EtlSources,
    vector_client: Any,
    catalog_client: Any,
    t2_db_path: str | Path,
    local_path: str | Path | None = None,
    voyage_key_present: bool | None = None,
    stale_aspects_count: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
    on_leg_result: Callable[[Any], None] | None = None,
    reopen_leg: Callable[[str], Any] | None = None,
    run_t2: Callable[[Any], dict[str, Any]] | None = None,
) -> GuidedUpgradeResult:
    """Run the full detect → sequence → validate guided upgrade.

    ``sources`` (T2 + catalog SQLite paths) drives the T2 ``migrate all``;
    ``vector_client`` is the pgvector write client; ``catalog_client`` backs the
    manifest-orphan validation leg; ``t2_db_path`` is the source SQLite the
    taxonomy floor reads. ``voyage_key_present`` defaults to the deployment-mode
    probe. ``on_progress(done, total)`` / ``on_leg_result(result)`` are pure
    progress sinks (the CLI wires ``click.echo``).

    ``run_t2`` (RDR-178 Gap 7, nexus-1sx01): an override for the T2
    ``migrate all`` step, threaded straight to
    :func:`nexus.migration.sequencer.run_sequenced_migration`'s own
    ``run_t2`` seam. The guided-upgrade CLI uses this to pass
    ``functools.partial(migrate_all, skip_stores=...)`` once its
    already-migrated pre-flight (:func:`detect_already_migrated`) has
    identified stores with no newer local writes since a clean report.
    ``None`` (the default) preserves the prior behavior exactly — the
    sequencer's own default (``migrate_all`` unconditionally) applies.

    Returns a :class:`GuidedUpgradeResult`. The sentinel is left ``migrated``
    only on a clean unlock-failure window that cannot occur here: a clean run
    clears it, any block leaves ``migrated-failed`` with rollback offered.
    """
    key_present = (
        voyage_key_available() if voyage_key_present is None else voyage_key_present
    )

    # 1. DETECT — open read legs, classify, then CLOSE before any ETL (the local
    #    leg is a WAL single-opener; the ETL reopens it and must be the sole
    #    opener).
    local, cloud = open_read_legs(local_path)
    try:
        detection = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=key_present,
        )
    finally:
        for client in (local, cloud):
            _close_quietly(client)

    # RDR-162 P2: a legacy collection the service cannot serve under its current
    # name (e.g. minilm-384 after RDR-160) is auto-migrated by re-embedding its
    # STORED chunk text into a model-remapped target — not blocked with the
    # re-index diagnostic. Policy lives here (the orchestrator), not in the ETL:
    # build the source→target map, pass it down, and the sequencer exempts these
    # from the pre-gate and re-points their references after they verify.
    # nexus-gilf2: the target model is mode + content-type aware (voyage models
    # in cloud mode, bge-768 in local) so the MIXED migrant (ran local, migrates
    # onto a voyage-mode service) re-embeds into a WIRED model instead of hitting
    # the pebfx.2 fail-loud guard.
    target_names = {
        c.collection: cross_model_target_name(
            c.collection,
            cross_model_target_model(c.collection, voyage_key_present=key_present),
        )
        for c in detection.classifications
        if cross_model_remappable(c)
    }
    if target_names:
        _log.info("guided_upgrade_cross_model_targets", targets=target_names)

    # 2. SEQUENCE — quiesce → pre-gate → T2 → T3-per-leg. The per-leg ETL opens
    #    + closes its OWN read client internally (we closed ours above).
    def _run_leg(leg: str) -> MigrationReport:
        if leg == "cloud":
            return migrate_cloud(
                vector_client, on_result=on_leg_result, target_names=target_names,
            )
        return migrate_local(
            local_path, vector_client, on_result=on_leg_result,
            target_names=target_names,
        )

    # Only pass run_t2 through when explicitly given — omitting the kwarg
    # entirely when it is None preserves the exact prior call shape for
    # every existing caller/test (the sequencer's own default applies).
    _seq_kwargs: dict[str, Any] = {}
    if run_t2 is not None:
        _seq_kwargs["run_t2"] = run_t2

    sequence = run_sequenced_migration(
        detection,
        sources=sources,
        run_leg=_run_leg,
        voyage_key_present=key_present,
        on_progress=on_progress,
        cross_model_targets=target_names,
        **_seq_kwargs,
    )

    # A fresh-user no-op (nothing data-bearing) is a clean success with no
    # migration and no validation to run.
    if sequence.phase == "not-migrating":
        return GuidedUpgradeResult(detection, sequence, validation=None, ok=True)

    # A block / partial-leg left the sentinel migrated-failed BEFORE T3
    # completed — there is nothing validated to gate; rollback is the per-leg
    # ETL's concern, surfaced via the dry-run/CLI, not this gate.
    if not sequence.ok:
        return GuidedUpgradeResult(detection, sequence, validation=None, ok=False)

    # 3. VALIDATE — reopen ONLY the data-bearing legs, build a composite read
    #    client routing each migrated collection to its source leg, run the
    #    gate, and always close the reopened legs.
    legs = sorted(detection.legs_with_data)
    migrated_collections = [c.collection for c in detection.classifications if c.has_data]
    # RDR-162 P2: a cross-model collection's chunks landed in its bge-768 TARGET,
    # so its validation dim is the TARGET dim (768), not the source dim (384).
    # The count check (below, via target_names) reads the source Chroma count and
    # the TARGET pgvector count; the taxonomy/manifest legs see the remapped refs.
    dims = tuple(
        sorted(
            {
                _dim_for_collection(target_names.get(c.collection, c.collection))[0]
                or c.dim
                for c in detection.classifications
                if c.has_data and (c.dim or c.collection in target_names)
            }
        )
    )
    reopen = reopen_leg or (lambda leg: _default_reopen_leg(leg, local_path))

    opened: dict[str, Any] = {}
    by_collection: dict[str, Any] = {}
    try:
        try:
            for leg in legs:
                opened[leg] = reopen(leg)
            for c in detection.classifications:
                if c.has_data:
                    by_collection[c.collection] = opened[c.leg]
            read_client = _CompositeReadClient(by_collection)
            checks = compose_validation_checks(
                t2_db_path=t2_db_path,
                read_client=read_client,
                vector_client=vector_client,
                catalog_client=catalog_client,
                collections=migrated_collections,
                dims=dims,
                target_names=target_names,
            )
            validation = validate_migration(
                taxonomy_check=checks.taxonomy_check,
                count_check=checks.count_check,
                manifest_orphan_check=checks.manifest_orphan_check,
                stale_aspects_count=stale_aspects_count,
            )
        except Exception as exc:  # noqa: BLE001 — recorded to the sentinel, never silent
            # The T3 copy is done (sentinel == `migrated`) but a validation-leg
            # SETUP step raised (e.g. a reopened read leg vanished, or the gate
            # itself crashed). Leaving the sentinel `migrated` would strand an
            # UNVALIDATED migration looking clean — the P2/P3 CRITICAL-1 class.
            # Transition to `migrated-failed` (degraded-LOUD, rollback offered)
            # before re-raising so the operator gets the recovery path.
            reason = f"validation could not be performed: {exc}"
            _log.error("guided_upgrade_validation_setup_raised", error=str(exc))
            mark_failed(reason)
            raise
    finally:
        for client in opened.values():
            _close_quietly(client)

    _log.info(
        "guided_upgrade_complete",
        sequence_ok=sequence.ok,
        unlocked=validation.unlocked,
        legs=legs,
        collections=len(migrated_collections),
    )
    return GuidedUpgradeResult(
        detection, sequence, validation, ok=validation.unlocked
    )


def _close_quietly(client: Any | None) -> None:
    # Delegates to the canonical leg-teardown primitive (RDR-002 ez5.3:
    # de-duplicated with the guided-upgrade pre-flight). Thin alias preserved
    # for the existing call sites in this module.
    close_read_client(client)
