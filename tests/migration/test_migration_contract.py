# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 P4.C (nexus-ue6g7.25) — cross-repo contract test.

The deferred conexus ``conexus upgrade`` veneer (and the nexus CLI) consume a
STABLE ``nexus.migration`` entry-point surface — they call the engine, never
re-implement it (RDR-159 §Components, §Alternatives: thin-veneer-not-reimplement).
This test pins the SIGNATURES of every consumed entry point so a nexus refactor
cannot silently break the veneer out from under it (the RDR-152 parity-tripwire
discipline applied across the repo boundary).

Non-vacuous by construction: each assertion pins the EXACT keyword-parameter
name set (and the dataclass field set), so a rename or re-signature of any
consumed surface FAILS this test — the failure is the tripwire.

Scope: only the PUBLIC surface the veneer + CLI actually consume. Adding a new
private helper to ``nexus.migration`` does not trip it; renaming a consumed
parameter does. If the contract legitimately changes, this test and the veneer
move in the SAME change (the locked invariant, continuation §7).

Deliberately NOT pinned: private helpers such as
``driver._CompositeReadClient``. The veneer calls ``run_guided_upgrade`` and
never constructs the composite itself — two-leg routing is the engine's
internal concern. Pinning a private impl detail here would over-couple the
veneer to nexus internals, the opposite of the thin-veneer boundary.

Known limit: this pins parameter NAMES (the rename tripwire), not type
annotations or return-value semantics. A consumed function could change its
behaviour while keeping its signature; the veneer author owns the semantic
contract per parameter separately. Param-name pinning is the RDR-152
parity-tripwire discipline, chosen for stability over the brittleness of
type/return-shape assertions.
"""
from __future__ import annotations

import dataclasses
import inspect
from typing import Callable


def _params(fn: Callable) -> set[str]:
    """Parameter names of ``fn`` excluding ``self``."""
    return {p for p in inspect.signature(fn).parameters if p != "self"}


def _fields(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


# ── The engine entry point (driver) — what the veneer calls ────────────────


def test_run_guided_upgrade_signature():
    from nexus.migration.driver import run_guided_upgrade

    assert _params(run_guided_upgrade) == {
        "sources",
        "vector_client",
        "catalog_client",
        "t2_db_path",
        "local_path",
        "voyage_key_present",
        "stale_aspects_count",
        "on_progress",
        "on_leg_result",
        "reopen_leg",
        # RDR-178 Gap 7 (nexus-1sx01): optional T2-step override, threaded
        # straight to the sequencer's own `run_t2` seam. Additive + backward
        # compatible — default None reproduces the prior behavior exactly;
        # the veneer need not pass it to keep working.
        "run_t2",
    }


def test_guided_upgrade_result_shape():
    from nexus.migration.driver import GuidedUpgradeResult

    assert _fields(GuidedUpgradeResult) == {"detection", "sequence", "validation", "ok"}
    assert isinstance(
        inspect.getattr_static(GuidedUpgradeResult, "rollback_available"), property
    )


# ── Detection + dry-run preview (P0) ───────────────────────────────────────


def test_detection_surface():
    from nexus.migration import detection as d

    assert _params(d.classify_collections) == {
        "local_client",
        "cloud_client",
        "voyage_key_present",
    }
    assert _params(d.open_read_legs) == {"local_path"}
    assert _params(d.voyage_key_available) == set()
    assert _params(d.build_dry_run_preview) == {"report"}
    assert _params(d.render_dry_run_preview) == {"preview"}
    assert _fields(d.DetectionReport) == {"classifications", "voyage_key_present"}
    assert _fields(d.CollectionClassification) == {
        "collection",
        "leg",
        "model",
        "dim",
        "support",
        "source_count",
        "has_data",
        "reason",
        "measured_dim",  # nexus-nb7hr: ground-truth probe result
        "legacy_ids",  # nexus-sot7v / GH #1390: pre-RDR-108 short-id block
    }


# ── Sequencing (P2) ────────────────────────────────────────────────────────


def test_sequencer_surface():
    from nexus.migration.sequencer import SequenceOutcome, run_sequenced_migration

    assert _params(run_sequenced_migration) == {
        "detection",
        "sources",
        "run_leg",
        "voyage_key_present",
        "run_t2",
        "quiesce_check",
        "model_gate",
        "on_progress",
        "started_at",
        "cross_model_targets",
        "remap_refs",
    }
    assert _fields(SequenceOutcome) == {
        "ok",
        "phase",
        "collections_total",
        "collections_done",
        "t2_total_failed",
        "legs_attempted",
        "legs_ok",
        "blocked_reason",
        "t2_report",
    }


# ── Validation + unlock/rollback (P3) ──────────────────────────────────────


def test_validation_surface():
    from nexus.migration.validation import (
        ValidationChecks,
        ValidationOutcome,
        compose_validation_checks,
        validate_migration,
    )

    assert _params(compose_validation_checks) == {
        "t2_db_path",
        "read_client",
        "vector_client",
        "catalog_client",
        "collections",
        "dims",
        "target_names",
    }
    assert _params(validate_migration) == {
        "taxonomy_check",
        "count_check",
        "manifest_orphan_check",
        "stale_aspects_count",
        "unlock",
        "on_block",
    }
    assert _fields(ValidationChecks) == {
        "taxonomy_check",
        "count_check",
        "manifest_orphan_check",
    }
    assert _fields(ValidationOutcome) == {
        "unlocked",
        "verdict",
        "blocking_reasons",
        "taxonomy_orphans",
        "count_mismatches",
        "count_indeterminate",
        "manifest_orphan_count",
        "manifest_vacuous",
        "stale_aspects",
        "advisory_notes",
        "rollback_available",
    }


# ── ETL primitives (P-1 / RDR-155) the sequencer + validation drive ────────


def test_vector_etl_surface():
    from nexus.migration import vector_etl as v

    assert _params(v.migrate_local) == {
        "local_path",
        "vector_client",
        "collections",
        "dry_run",
        "page_size",
        "on_result",
        "target_names",
        "breaker",
    }
    assert _params(v.migrate_cloud) == {
        "vector_client",
        "tenant",
        "database",
        "api_key",
        "collections",
        "dry_run",
        "page_size",
        "on_result",
        "target_names",
        "breaker",
    }
    assert _params(v.rollback_collections) == {
        "read_client",
        "vector_client",
        "collections",
        "page_size",
        "remap_store",  # RDR-185 P2.4: rollback-via-map (gate r1)
    }
    assert _params(v.verify_counts) == {
        "read_client", "vector_client", "collections", "target_names",
    }
    assert _params(v.verify_taxonomy_consistency) == {
        "t2_db_path", "vector_client", "target_names",
    }


# ── Cross-process state sentinel (P1) ──────────────────────────────────────


def test_state_surface():
    from nexus.migration import state as s

    assert _params(s.begin_migration) == {"collections_total", "started_at"}
    assert _params(s.mark_migrated) == set()
    assert _params(s.mark_failed) == {"failure"}
    assert _params(s.clear_state) == set()
    assert _params(s.current_phase) == set()
    assert _params(s.is_migrating) == set()
    assert _params(s.read_state) == set()


# ── T2 orchestration callable (P-1a / RF-4) ────────────────────────────────


def test_orchestrator_surface():
    from nexus.migration.orchestrator import EtlSources, migrate_all

    assert _params(migrate_all) == {
        "sources",
        "count_source",
        "on_store",
        "on_store_failed",
        "on_progress",
        "migration_id",
        # RDR-178 Gap 7 (nexus-1sx01): already-migrated store skip seam.
        # Additive + backward compatible — default frozenset() reproduces
        # the prior unconditional behavior exactly.
        "skip_stores",
        # RDR-178 wave-2 P4 (nexus-s3dd4.5): verify-fill (delta) mode seam.
        # Additive + backward compatible — default False reproduces the
        # prior unconditional full-re-send behavior exactly.
        "verify_fill",
    }
    assert _fields(EtlSources) == {"sqlite_path", "catalog_db_path"}
