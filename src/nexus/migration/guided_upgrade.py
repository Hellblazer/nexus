# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx guided-upgrade`` Stage-2 logic — provision + version-pin + health-gate
the engine-service, then hand off to the existing ``nx migrate-to-service``.

RDR-002. conexus owns the design; this module is the engine-side host. The
detect / migrate / validate / unlock / rollback machinery already exists
(:mod:`nexus.migration.detection`, :func:`nexus.migration.driver.run_guided_upgrade`,
``nx migrate-to-service``) and is REUSED, never rebuilt. This module adds only
the new pre-flight + provisioning + readiness-contract pieces.

ez5.2 (this commit): :func:`detect_pending_migration` — the pre-flight a
command runs BEFORE provisioning a service, so a fresh user short-circuits to
a no-op instead of standing up a service for an empty footprint.
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
    open_read_legs,
    voyage_key_available,
)

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PreflightDetection:
    """The verdict of the pre-provision detection step.

    ``needs_migration`` is the single gate the command branches on: True iff
    at least one data-bearing legacy Chroma collection exists. A fresh user
    (no legs, or only empty collections) yields ``False`` and the command must
    no-op WITHOUT provisioning a service.
    """

    report: DetectionReport
    needs_migration: bool

    @property
    def data_bearing_count(self) -> int:
        """Number of non-empty collections across all detected legs."""
        return sum(1 for c in self.report.classifications if c.has_data)

    @property
    def classified_unsupported_count(self) -> int:
        """Number of collections classified ``unsupported`` by detection.

        This is the RAW classification count — it INCLUDES legacy minilm-384
        collections that RDR-162 auto-remaps (re-embeds into a bge-768 target)
        rather than blocks. It is therefore NOT the count of genuinely-blocked
        collections; a consumer needing the blocked set must filter
        ``report.unsupported`` by :func:`cross_model_remappable`. Kept as a
        coarse informational signal only.
        """
        return len(self.report.unsupported)

    @property
    def total_count(self) -> int:
        """Total classified collections (data-bearing or not)."""
        return len(self.report.classifications)


def detect_pending_migration(
    *,
    local_path: str | Path | None = None,
    voyage_key_present: bool | None = None,
    open_legs: Callable[[str | Path | None], tuple[Any, Any]] | None = None,
    close_leg: Callable[[Any], None] | None = None,
) -> PreflightDetection:
    """Detect whether a pre-RDR-160 Chroma footprint exists to migrate.

    Opens the local + cloud read legs, classifies the footprint via the
    existing :func:`classify_collections`, then CLOSES the legs before
    returning — the WAL local leg is a single-opener and the downstream ETL
    must be the sole opener (same invariant the driver enforces).

    ``open_legs`` / ``close_leg`` are injection seams for tests; production
    uses :func:`open_read_legs` and :func:`_close_quietly`. ``voyage_key_present``
    defaults to the deployment-mode probe.
    """
    key_present = (
        voyage_key_available() if voyage_key_present is None else voyage_key_present
    )
    _open = open_legs if open_legs is not None else open_read_legs
    _close = close_leg if close_leg is not None else close_read_client

    local, cloud = _open(local_path)
    try:
        report = classify_collections(
            local_client=local,
            cloud_client=cloud,
            voyage_key_present=key_present,
        )
    finally:
        # Close only the legs that were actually opened — an absent leg is
        # never dispatched to ``_close`` (so injected close hooks need not
        # tolerate ``None``).
        for client in (local, cloud):
            if client is not None:
                _close(client)

    needs = len(report.legs_with_data) > 0
    _log.info(
        "guided_upgrade_preflight",
        needs_migration=needs,
        total=len(report.classifications),
        data_bearing=sum(1 for c in report.classifications if c.has_data),
        unsupported=len(report.unsupported),
    )
    return PreflightDetection(report=report, needs_migration=needs)
