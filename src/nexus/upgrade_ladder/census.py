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
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

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
    """One collection holding pre-RDR-108 (non-32-char) chunk ids."""

    collection: str
    leg: str
    source_count: int
    reason: str


def legacy_id_census() -> list[LegacyCollection] | None:
    """Census of legacy-chunk-id collections on a Chroma-mode install.

    Returns ``None`` when NOT APPLICABLE — no legacy Chroma footprint
    (fresh install, already-migrated with service evidence, kill switch)
    or the probe failed (best-effort: a broken store must not break
    ``nx doctor``). Returns ``[]`` for a Chroma-mode install whose
    collections are all conformant. Non-empty = pending era-debt.
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
    return [
        LegacyCollection(
            collection=c.collection,
            leg=str(c.leg),
            source_count=c.source_count,
            reason=c.reason,
        )
        for c in detection.report.classifications
        if c.legacy_ids
    ]
