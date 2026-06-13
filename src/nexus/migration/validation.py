# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-159 P3 (nexus-ue6g7.20): the validation gate + unlock/rollback.

After P2's sequencer leaves the sentinel at ``migrated`` (T3 copied), the
migration is NOT done. This module runs the NON-VACUOUS validation and decides
whether to UNLOCK (clear the sentinel → serving normal) or BLOCK (leave the
sentinel ``migrated-failed`` → still degraded-LOUD, rollback offered).

Three blocking legs, ALL must be clean to unlock (RDR-159 §Approach P3, steps
8-9):

* **taxonomy floor** (``verify_taxonomy_consistency``) — every
  ``topic_assignments.source_collection`` resolves to a migrated collection. The
  floor runs REGARDLESS of the other legs (this gate never short-circuits, so a
  single report carries every failure);
* **counts** (``verify_counts``) — source==target per collection. A mismatch OR
  an indeterminate (nothing verifiable) BLOCKS unlock — never a silent pass;
* **manifest-orphans** — orphan chunks in the migrated catalog BLOCK unlock
  (wired in P3.M / nexus-ue6g7.21 via the P-1b ``manifest_orphans`` callable,
  which must run AFTER ``manifest_backfill`` to be non-vacuous).

Stale ``document_aspects`` is ADVISORY-ONLY: the report names the count and
points at ``nx enrich aspects``; it NEVER blocks unlock (enrichment degrades
until re-extraction, but knowledge is still served — RDR-159, nexus-f1m8s).

Rollback (RF-5, ``vector_etl.rollback_collections``) is OFFERED on a block, not
auto-invoked: copy-not-move keeps Chroma immutable, so the user can roll the
pgvector copy back to a fully-working pre-upgrade state and re-run.

The gate is a pure decision over injected check results; the caller composes the
real ``verify_*`` / ``manifest_orphans`` wiring.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import structlog

from nexus.migration.state import clear_state, mark_failed

_log = structlog.get_logger(__name__)


class ValidationCheckVacuous(RuntimeError):
    """A validation leg could not be performed non-vacuously.

    Raised by a check (e.g. the manifest-orphan check when the migrated catalog
    is empty because T2 has not run) to signal that a clean result would be a
    FALSE pass. The gate treats it as a hard BLOCK with a loud reason — never a
    silent unlock (RDR-159 gap #2 / RF-3).
    """


@dataclass(frozen=True)
class ValidationOutcome:
    """The validation verdict + the evidence behind it.

    ``unlocked`` is True only when every blocking leg is clean. ``advisory_notes``
    carries non-blocking guidance (e.g. the stale-aspects pointer).
    """

    unlocked: bool
    verdict: str  # "verified" | "blocked"
    blocking_reasons: tuple[str, ...]
    taxonomy_orphans: tuple[str, ...]
    count_mismatches: tuple[str, ...]
    count_indeterminate: bool
    manifest_orphan_count: int
    manifest_vacuous: bool
    stale_aspects: int
    advisory_notes: tuple[str, ...]
    rollback_available: bool


def validate_migration(
    *,
    taxonomy_check: Callable[[], list[str]],
    count_check: Callable[[], dict[str, tuple[int, int]]],
    manifest_orphan_check: Callable[[], int],
    stale_aspects_count: int = 0,
    unlock: Callable[[], None] = clear_state,
    on_block: Callable[[str], object] = mark_failed,
) -> ValidationOutcome:
    """Run the three validation legs and UNLOCK or BLOCK.

    Every leg runs (no short-circuit) so the returned outcome carries the full
    failure picture. On a clean verdict ``unlock()`` clears the sentinel; on any
    block ``on_block(reason)`` leaves it ``migrated-failed`` and rollback is
    offered (not invoked). ``stale_aspects_count`` is advisory and never blocks.
    """
    # The taxonomy floor runs first and unconditionally — it is the non-vacuous
    # check the production vacuous-pass (RDR-159 gap #2) lacked.
    taxonomy_orphans = tuple(taxonomy_check())
    counts = count_check()
    count_mismatches = tuple(
        name for name, (src, tgt) in counts.items() if src != tgt
    )
    count_indeterminate = len(counts) == 0
    # The manifest leg may signal it could not validate (empty migrated catalog
    # → a clean zero would be a FALSE pass). Treat that as a hard block, never a
    # silent unlock.
    manifest_vacuous = False
    manifest_orphan_count = 0
    manifest_vacuous_reason = ""
    try:
        manifest_orphan_count = int(manifest_orphan_check())
    except ValidationCheckVacuous as exc:
        manifest_vacuous = True
        manifest_vacuous_reason = str(exc)

    blocking_reasons: list[str] = []
    if taxonomy_orphans:
        blocking_reasons.append(
            f"taxonomy: {len(taxonomy_orphans)} unresolved source_collection(s) "
            f"do not map to a migrated collection: {list(taxonomy_orphans)}"
        )
    if count_mismatches:
        blocking_reasons.append(
            f"counts: {len(count_mismatches)} collection(s) with source!=target "
            f"chunk-count mismatch: {list(count_mismatches)}"
        )
    if count_indeterminate:
        blocking_reasons.append(
            "counts: indeterminate (no collections were verifiable) — refusing "
            "to unlock an unverifiable migration"
        )
    if manifest_vacuous:
        blocking_reasons.append(f"manifest: {manifest_vacuous_reason}")
    if manifest_orphan_count > 0:
        blocking_reasons.append(
            f"manifest: {manifest_orphan_count} orphan chunk(s) in the migrated "
            "catalog (a chunk with no owning document) — re-run after backfill"
        )

    # Advisory-only: stale aspects degrade enrichment but never block serving.
    advisory_notes: list[str] = []
    if stale_aspects_count > 0:
        advisory_notes.append(
            f"{stale_aspects_count} stale document_aspects row(s) — run "
            "`nx enrich aspects` to refresh (advisory; does not block unlock or "
            "serving)"
        )

    if blocking_reasons:
        reason = "; ".join(blocking_reasons)
        _log.warning("validation_blocked_unlock", reasons=blocking_reasons)
        on_block(reason)
        return ValidationOutcome(
            unlocked=False,
            verdict="blocked",
            blocking_reasons=tuple(blocking_reasons),
            taxonomy_orphans=taxonomy_orphans,
            count_mismatches=count_mismatches,
            count_indeterminate=count_indeterminate,
            manifest_orphan_count=manifest_orphan_count,
            manifest_vacuous=manifest_vacuous,
            stale_aspects=stale_aspects_count,
            advisory_notes=tuple(advisory_notes),
            rollback_available=True,
        )

    _log.info("validation_clean_unlock")
    unlock()
    return ValidationOutcome(
        unlocked=True,
        verdict="verified",
        blocking_reasons=(),
        taxonomy_orphans=(),
        count_mismatches=(),
        count_indeterminate=False,
        manifest_orphan_count=manifest_orphan_count,
        manifest_vacuous=False,
        stale_aspects=stale_aspects_count,
        advisory_notes=tuple(advisory_notes),
        rollback_available=False,
    )
