# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P3.T (nexus-ue6g7.19) — the validation gate + unlock/rollback.

RDR-159 §Approach P3 + Sequencing steps 8-9. After P2's sequencer leaves the
sentinel at ``migrated`` (T3 copied), the migration is NOT done: P3 runs the
NON-VACUOUS validation and decides UNLOCK vs BLOCK. Three blocking legs, ALL must
be clean to unlock:

* **taxonomy floor** — ``verify_taxonomy_consistency``: every
  ``topic_assignments.source_collection`` must resolve to a migrated collection.
  Runs the floor REGARDLESS of the other legs (never short-circuited away);
* **counts** — ``verify_counts``: source==target per collection. A mismatch OR
  an indeterminate (nothing to verify) BLOCKS unlock (never a silent pass);
* **manifest-orphans** — orphans in the migrated catalog BLOCK unlock.

On clean validation the sentinel is CLEARED (``clear_state`` → serving normal).
On any block the sentinel STAYS ``migrated-failed`` (still degraded-LOUD), the
report surfaces that ``migrate vectors --rollback`` (RF-5, copy-not-move keeps
Chroma intact) is available, and rollback is NOT auto-invoked.

Stale ``document_aspects`` is ADVISORY-ONLY: the report names the count and
points at ``nx enrich aspects``, but it NEVER blocks unlock.

The gate is a pure decision over INJECTED check results, so the contract is
pinned without a live service / Chroma / T2. State transitions use the real P1a
sentinel under an isolated ``NEXUS_CONFIG_DIR``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.migration.state import begin_migration, current_phase, mark_migrated
from nexus.migration.validation import ValidationOutcome, validate_migration

_FIXED_STARTED_AT = "2026-06-13T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _set_migrated() -> None:
    """Simulate P2 success: sentinel sits at ``migrated`` awaiting validation."""
    begin_migration(collections_total=2, started_at=_FIXED_STARTED_AT)
    mark_migrated()


# Clean check fakes (overridden per test).
def _clean_taxonomy() -> list[str]:
    return []


def _clean_counts() -> dict[str, tuple[int, int]]:
    return {"code__a__minilm-l6-v2-384__v1": (10, 10)}


def _clean_manifest() -> int:
    return 0


# --------------------------------------------------------------------------
# Clean validation → unlock
# --------------------------------------------------------------------------


def test_clean_validation_unlocks_and_clears_sentinel() -> None:
    _set_migrated()
    outcome = validate_migration(
        taxonomy_check=_clean_taxonomy,
        count_check=_clean_counts,
        manifest_orphan_check=_clean_manifest,
        stale_aspects_count=0,
    )
    assert isinstance(outcome, ValidationOutcome)
    assert outcome.unlocked is True
    assert outcome.verdict == "verified"
    assert outcome.blocking_reasons == ()
    assert outcome.rollback_available is False
    # UNLOCK cleared the sentinel → serving normal.
    assert current_phase() == "not-migrating"


# --------------------------------------------------------------------------
# Taxonomy floor blocks + runs regardless of order
# --------------------------------------------------------------------------


def test_taxonomy_orphan_blocks_unlock() -> None:
    _set_migrated()
    outcome = validate_migration(
        taxonomy_check=lambda: ["knowledge__x__minilm-l6-v2-384__v1"],
        count_check=_clean_counts,
        manifest_orphan_check=_clean_manifest,
    )
    assert outcome.unlocked is False
    assert outcome.verdict == "blocked"
    assert outcome.taxonomy_orphans == ("knowledge__x__minilm-l6-v2-384__v1",)
    assert any("taxonomy" in r for r in outcome.blocking_reasons)
    assert outcome.rollback_available is True
    assert current_phase() == "migrated-failed"  # marker stays, still degraded-LOUD


def test_taxonomy_floor_runs_regardless_of_other_legs() -> None:
    _set_migrated()
    called = {"taxonomy": False}

    def _taxonomy() -> list[str]:
        called["taxonomy"] = True
        return []

    # Even with a manifest orphan that would block, the taxonomy floor still runs.
    validate_migration(
        taxonomy_check=_taxonomy,
        count_check=_clean_counts,
        manifest_orphan_check=lambda: 5,
    )
    assert called["taxonomy"] is True  # never short-circuited away


# --------------------------------------------------------------------------
# Counts: mismatch and indeterminate both block
# --------------------------------------------------------------------------


def test_count_mismatch_blocks_unlock() -> None:
    _set_migrated()
    outcome = validate_migration(
        taxonomy_check=_clean_taxonomy,
        count_check=lambda: {"code__a__minilm-l6-v2-384__v1": (10, 9)},
        manifest_orphan_check=_clean_manifest,
    )
    assert outcome.unlocked is False
    assert outcome.count_mismatches == ("code__a__minilm-l6-v2-384__v1",)
    assert any("count" in r.lower() for r in outcome.blocking_reasons)
    assert current_phase() == "migrated-failed"


def test_indeterminate_counts_block_unlock_never_a_silent_pass() -> None:
    _set_migrated()
    outcome = validate_migration(
        taxonomy_check=_clean_taxonomy,
        count_check=lambda: {},  # nothing verifiable → indeterminate
        manifest_orphan_check=_clean_manifest,
    )
    assert outcome.unlocked is False
    assert outcome.count_indeterminate is True
    assert any("indeterminate" in r.lower() for r in outcome.blocking_reasons)
    assert current_phase() == "migrated-failed"


# --------------------------------------------------------------------------
# Manifest orphans block
# --------------------------------------------------------------------------


def test_manifest_orphans_block_unlock() -> None:
    _set_migrated()
    outcome = validate_migration(
        taxonomy_check=_clean_taxonomy,
        count_check=_clean_counts,
        manifest_orphan_check=lambda: 3,
    )
    assert outcome.unlocked is False
    assert outcome.manifest_orphan_count == 3
    assert any("manifest" in r.lower() for r in outcome.blocking_reasons)
    assert current_phase() == "migrated-failed"


# --------------------------------------------------------------------------
# Stale aspects: ADVISORY-ONLY, never blocks
# --------------------------------------------------------------------------


def test_stale_aspects_is_advisory_only_and_does_not_block_unlock() -> None:
    _set_migrated()
    outcome = validate_migration(
        taxonomy_check=_clean_taxonomy,
        count_check=_clean_counts,
        manifest_orphan_check=_clean_manifest,
        stale_aspects_count=42,
    )
    # Otherwise-clean → STILL unlocks despite 42 stale aspects.
    assert outcome.unlocked is True
    assert outcome.stale_aspects == 42
    assert any("42" in n and "nx enrich aspects" in n for n in outcome.advisory_notes)
    # The stale count is NOT a blocking reason.
    assert all("aspect" not in r.lower() for r in outcome.blocking_reasons)
    assert current_phase() == "not-migrating"


# --------------------------------------------------------------------------
# Multiple blocks accumulate (no short-circuit) + rollback offered
# --------------------------------------------------------------------------


def test_multiple_blocks_accumulate_and_offer_rollback() -> None:
    _set_migrated()
    outcome = validate_migration(
        taxonomy_check=lambda: ["knowledge__x__minilm-l6-v2-384__v1"],
        count_check=lambda: {"code__a__minilm-l6-v2-384__v1": (10, 8)},
        manifest_orphan_check=lambda: 2,
        stale_aspects_count=7,
    )
    assert outcome.unlocked is False
    # All three legs reported (no short-circuit).
    assert len(outcome.blocking_reasons) == 3
    assert outcome.rollback_available is True
    # Stale-aspects still advisory even on the blocked path.
    assert outcome.stale_aspects == 7
    assert current_phase() == "migrated-failed"
