# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-185 P1.2: legacy chunk-id census — the Gap-5 closer.

The 2026-07-16 work-instance incident (GH #1408): 18 pre-RDR-108
legacy-id collections sat invisible for months and only surfaced ON
migration day, as a BLOCK with an impossible remedy. This census makes
that era-debt visible in ``nx doctor`` from the release that ships it —
months before migration day.

Detect-only in P1: the census REPORTS; remediation is the P2 substrate
rung's wire re-id (which converges this debt unattended). Deliberately
NOT a registered walk rung — a pending rung with no remediation would
fail ``nx upgrade`` on installs that work fine today. When the P2
substrate-etl rung lands, this census becomes part of its ``detect()``.

Reuses the proven detection machinery (``_probe_legacy_ids`` samples
every data-bearing collection during ``classify_collections``) behind
the same gates as the migration bridge notice: the cheap file-level
``legacy_footprint_pending()`` gate first (never opens the store on
non-Chroma / already-migrated / kill-switched installs), then the full
read-leg classification.

Those gates are necessary but NOT sufficient, and P4 proved it live
(bead nexus-6or3m): they establish that a legacy footprint EXISTS, which
RDR-176 guarantees forever, and existence is not debt. Whether the debt
is still OUTSTANDING is a fact about the TARGET, so asking it costs a
further probe — paid only when there IS legacy debt to weigh (never on a
conformant install) and only when a service is provisioned to have
converged into (never on the un-provisioned GH #1408 shape). Normally one
``/v1/vectors/stats`` round trip, though ``list_collections`` degrades to
a per-collection fan-out against a pre-catalog-005 engine.

SCOPE, precisely: this census answers "does an outstanding legacy chunk
id remain in a TARGET collection". It does NOT see an unreflected remap
cascade (vectors re-identified, local stores still pointing at the old
chashes) — count equality cannot. That half belongs to the substrate
rung's ``verify()``/``detect()``, and ``nx doctor`` prints both rows: a
clean census row beside a pending ladder row is a coherent PAIR, not a
contradiction. Anything reading this row alone reads half the answer.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from nexus.migration.detection import CollectionClassification
    from nexus.upgrade_ladder.rungs.substrate_etl import SourceProgress

_log = structlog.get_logger(__name__)


def _chroma_footprint_present() -> bool:
    """Cheap file-level census gate: kill switch + local Chroma directory.

    Deliberately NOT ``legacy_footprint_pending()`` (P1 critique High): its
    service-evidence early-outs read "provisioned" as "migrated", which goes
    silent on the hybrid provisioned-but-unmigrated state — a service exists
    but legacy-id collections were never re-identified (they CANNOT migrate;
    GH #1390 blocks them). That is the GH #1408 recurrence shape the census
    exists to keep visible. Era-debt lives in the Chroma leg regardless of
    service provisioning, so the only gates are the kill switch and the
    directory existing at all.
    """
    if os.environ.get("NX_MIGRATION_NOTICE") == "0":
        return False
    try:
        from nexus.migration.detection import resolve_default_local_leg  # noqa: PLC0415 — deferred: the migration module dies at RDR-155 P4b

        return Path(resolve_default_local_leg()).is_dir()
    except Exception as exc:  # noqa: BLE001 — best-effort gate; a broken resolver must not break nx doctor
        _log.debug("census_footprint_gate_failed", error=str(exc))
        return False


@dataclass(frozen=True)
class LegacyCollection:
    """One collection holding OUTSTANDING pre-RDR-108 (non-32-char) chunk ids."""

    collection: str
    leg: str
    source_count: int
    reason: str
    #: Why the migration cannot converge this one on its own — "" when a plain
    #: `nx upgrade` will. Non-empty means the row must NOT imply the upgrade
    #: handles it (bead nexus-mq42b: a keyless voyage collection is skipped by
    #: the planner, so telling its owner to run the upgrade is a dead end).
    blocked_reason: str = ""


def _no_target_provisioned() -> bool:
    """On-disk evidence that this install has NO service to have converged INTO.

    The rung reuses ``_default_target_counts`` behind a PRECONDITION leg
    (nexus-6nmrc: provisioning runs before the walk), so its WARNING docstring
    can say "every exception reaching here is unexpected by construction". The
    census has no such precondition — it runs on any install with a Chroma
    directory, and "no service at all" is the NORMAL state for the exact
    un-provisioned, legacy-bearing population Gap-5 exists to protect (GH
    #1408). Probing there would fire ``substrate_target_counts_failed`` at
    WARNING on every single ``nx doctor``, forever, turning a deliberately loud
    signal into noise — and pay a doomed HTTP attempt on a read-only path to
    learn what a file already answers.

    Reuses the precondition's own file-level test rather than re-deriving a
    second notion of "is there a service" (same rule as the convergence test
    itself). No service provisioned means nothing can have converged, which is
    exactly what the caller then reports: all debt outstanding.
    """
    from nexus.config import default_db_path  # noqa: PLC0415 — deferred to avoid import cycle
    from nexus.upgrade_ladder.preconditions import _default_provisioned  # noqa: PLC0415 — deferred to avoid import cycle

    return not _default_provisioned(default_db_path().parent)


def _default_progress(
    classifications: list["CollectionClassification"],
) -> "SourceProgress":
    """Production probe: ONE ``/v1/vectors/stats`` round trip.

    A probe that cannot tell answers "nothing is converged" — era debt stays
    VISIBLE rather than being silently certified away by an unreachable
    service. The same direction ``_default_target_counts`` takes for the
    converge path, for the same reason.

    Answers WITHOUT probing when no service is provisioned: see
    :func:`_no_target_provisioned`. Same answer, no round trip, no false alarm.
    (The credential-gated set is still computed there — it is a fact about the
    source world and the deployment's key, and needs no target at all.)
    """
    from nexus.migration.detection import voyage_key_available  # noqa: PLC0415 — deferred, detection is heavy
    from nexus.upgrade_ladder.rungs.substrate_etl import (  # noqa: PLC0415 — deferred, avoids an import cycle (the rung reads this module's footprint gate)
        _default_membership,
        _default_target_counts,
        source_progress,
    )

    try:
        counts = None if _no_target_provisioned() else _default_target_counts()
        return source_progress(
            classifications,
            voyage_key_present=voyage_key_available(),
            target_counts=counts,  # None => nothing converged; the debt is real
            # nexus-146xx.7: the census must agree with the rung's detect() on
            # membership-converged (tidtd-shaped) legs, or doctor's census rows
            # and the rung's pending status split-brain on the same install.
            membership_fn=None if _no_target_provisioned() else _default_membership,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort: a failed probe reports debt, never hides it
        # Includes SubstrateTargetCollision (nexus-fffey): a world the migration
        # must refuse is a world where nothing has converged. `nx upgrade` is
        # where that refusal is loud; `nx doctor` must still print its rows.
        _log.warning("legacy_id_census_convergence_probe_failed", error=str(exc))
        from nexus.upgrade_ladder.rungs.substrate_etl import SourceProgress  # noqa: PLC0415 — deferred, avoids an import cycle

        return SourceProgress()


def legacy_id_census(
    *,
    progress_fn: "Callable[[list[CollectionClassification]], SourceProgress] | None" = None,
) -> list[LegacyCollection] | None:
    """Census of OUTSTANDING legacy-chunk-id debt on a Chroma-mode install.

    Returns ``None`` when NOT APPLICABLE — no legacy Chroma footprint
    (fresh install, already-migrated with service evidence, kill switch)
    or the probe failed (best-effort: a broken store must not break
    ``nx doctor``). Returns ``[]`` when nothing is outstanding. Non-empty =
    pending era-debt.

    OUTSTANDING is the operative word, and it is why this consults the TARGET
    (bead nexus-6or3m, the third instance of the class). Holding legacy ids is
    a property of the SOURCE, which RDR-176 keeps byte-untouched forever as the
    rollback target — so a source-derived census reports era debt for the rest
    of the install's life, including on a fully converged one. Gap-5's promise
    ("the debt is visible the day it exists") then inverts into "the debt is
    visible forever, including when it does not exist", telling a user who did
    everything right that they still owe work — the same stays-homework failure
    RDR-185 exists to end. A collection whose target already holds its rows is
    no longer OUTSTANDING era debt (see the module docstring for what that does
    and does not cover — an unreflected cascade is the ladder row's half, not
    this one's).
    """
    from nexus.migration.guided_upgrade import (  # noqa: PLC0415 — deferred: the whole bridge dies with the migration module at RDR-155 P4b
        detect_pending_migration_memoized,
    )

    if not _chroma_footprint_present():
        return None
    try:
        detection = detect_pending_migration_memoized()
    except Exception as exc:  # noqa: BLE001 — best-effort census; a broken store must not break nx doctor
        _log.warning("legacy_id_census_probe_failed", error=str(exc))
        return None
    classifications = list(detection.report.classifications)
    legacy = [c for c in classifications if c.legacy_ids]
    if not legacy:
        return []  # no era debt ever existed: no need to ask the target anything
    progress = (progress_fn or _default_progress)(classifications)
    return [
        LegacyCollection(
            collection=c.collection,
            leg=str(c.leg),
            source_count=c.source_count,
            reason=c.reason,
            blocked_reason=(
                "no Voyage key is configured, so this deployment wires no "
                "embedder for it — `nx upgrade` skips it"
                if c.collection in progress.credential_gated
                else ""
            ),
        )
        for c in legacy
        if c.collection not in progress.converged
    ]
