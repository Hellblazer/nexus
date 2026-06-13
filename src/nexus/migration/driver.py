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

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import structlog

from nexus.migration.detection import (
    DetectionReport,
    classify_collections,
    open_read_legs,
    voyage_key_available,
)
from nexus.migration.orchestrator import EtlSources
from nexus.migration.sequencer import SequenceOutcome, run_sequenced_migration
from nexus.migration.validation import (
    ValidationOutcome,
    compose_validation_checks,
    validate_migration,
)
from nexus.migration.vector_etl import MigrationReport, migrate_cloud, migrate_local

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
    from nexus.migration.chroma_read import (  # noqa: PLC0415
        open_cloud_read_client,
        open_local_read_client,
    )

    if leg == "cloud":
        return open_cloud_read_client()
    from nexus.config import nexus_config_dir  # noqa: PLC0415

    path = Path(local_path) if local_path else nexus_config_dir() / "chroma"
    return open_local_read_client(path)


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
) -> GuidedUpgradeResult:
    """Run the full detect → sequence → validate guided upgrade.

    ``sources`` (T2 + catalog SQLite paths) drives the T2 ``migrate all``;
    ``vector_client`` is the pgvector write client; ``catalog_client`` backs the
    manifest-orphan validation leg; ``t2_db_path`` is the source SQLite the
    taxonomy floor reads. ``voyage_key_present`` defaults to the deployment-mode
    probe. ``on_progress(done, total)`` / ``on_leg_result(result)`` are pure
    progress sinks (the CLI wires ``click.echo``).

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

    # 2. SEQUENCE — quiesce → pre-gate → T2 → T3-per-leg. The per-leg ETL opens
    #    + closes its OWN read client internally (we closed ours above).
    def _run_leg(leg: str) -> MigrationReport:
        if leg == "cloud":
            return migrate_cloud(vector_client, on_result=on_leg_result)
        return migrate_local(local_path, vector_client, on_result=on_leg_result)

    sequence = run_sequenced_migration(
        detection,
        sources=sources,
        run_leg=_run_leg,
        voyage_key_present=key_present,
        on_progress=on_progress,
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
    dims = tuple(
        sorted({c.dim for c in detection.classifications if c.has_data and c.dim})
    )
    reopen = reopen_leg or (lambda leg: _default_reopen_leg(leg, local_path))

    opened: dict[str, Any] = {}
    by_collection: dict[str, Any] = {}
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
        )
        validation = validate_migration(
            taxonomy_check=checks.taxonomy_check,
            count_check=checks.count_check,
            manifest_orphan_check=checks.manifest_orphan_check,
            stale_aspects_count=stale_aspects_count,
        )
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
    if client is None:
        return
    close = getattr(client, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()
