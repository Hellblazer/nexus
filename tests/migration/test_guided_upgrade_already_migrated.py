# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-178 Gap 7 (nexus-1sx01) — already-migrated detection for
``nx guided-upgrade``.

The 2026-07-01 production incident: a guided-upgrade re-run re-shipped
158k catalog rows to patch a 270-row hole because neither guided-upgrade
nor the migrate legs recognized an already-migrated T2 store. This module
covers the GUIDED-UPGRADE detection half only — per-T2-store "already
migrated" detection sourced from the existing RDR-153
``<config>/migration-reports/*.json`` artifacts + a cheap local-SQLite
freshness probe. It does NOT implement migrate-leg delta shipping
(nexus-s3dd4, Wave 2) or T3 (Chroma-source) already-migrated detection —
Chroma is copy-not-move and retained forever, so ``detect_pending_migration``
keeps reporting "needs migration" until that separate work lands.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus.migration.etl_registry import LADDER_ORDER
from nexus.migration.guided_upgrade import (
    FRESHNESS_PROBES,
    AlreadyMigratedPlan,
    StoreMigrationStatus,
    detect_already_migrated,
)

ALL_STORES = tuple(LADDER_ORDER)
T0 = "2026-07-01T12:00:00+00:00"
BEFORE_T0 = "2026-07-01T10:00:00+00:00"
AFTER_T0 = "2026-07-01T14:00:00+00:00"


# ── fixture builders ─────────────────────────────────────────────────────────


def _report(
    *,
    stores: dict[str, int],
    completed_at: str = T0,
    verification: str | None = "verified",
    migration_id: str = "r1",
) -> dict:
    """A minimal RDR-153 report dict covering *stores* -> failed-row-count."""
    report: dict = {
        "schema_version": "1",
        "migration_id": migration_id,
        "started_at": completed_at,
        "completed_at": completed_at,
        "source": {"sqlite": "x"},
        "target": {"service_url": "x"},
        "stores": [
            {
                "store": store,
                "tables": [
                    {
                        "table": store,
                        "read": 10,
                        "written": 10,
                        "skipped": 0,
                        "flagged": 0,
                        "failed": failed,
                        "issues": [],
                    }
                ],
            }
            for store, failed in stores.items()
        ],
        "summary": {"total_failed": sum(stores.values())},
    }
    if verification is not None:
        report["verification"] = verification
    return report


def _write_report(reports_dir: Path, report: dict, *, name: str | None = None) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"migration-{name or report['migration_id']}.json"
    path.write_text(json.dumps(report))
    return path


def _make_sqlite(tmp_path: Path, *, rows: dict[str, str], filename: str = "t2.db") -> Path:
    """One row per store in *rows* (store -> ISO timestamp), written into
    that store's anchor table (:data:`FRESHNESS_PROBES`). Stores omitted
    from *rows* get no table at all (freshness probe degrades to
    trust-the-report, per the WORK spec's explicit fallback)."""
    db = tmp_path / filename
    conn = sqlite3.connect(db)
    try:
        for store, ts in rows.items():
            table, column = FRESHNESS_PROBES[store]
            conn.execute(f"CREATE TABLE {table} ({column} TEXT)")  # noqa: S608 — fixed internal test fixture, no external input
            conn.execute(f"INSERT INTO {table} ({column}) VALUES (?)", (ts,))
        conn.commit()
    finally:
        conn.close()
    return db


def _all_before(tmp_path: Path) -> Path:
    return _make_sqlite(tmp_path, rows={s: BEFORE_T0 for s in ALL_STORES})


# ── AlreadyMigratedPlan ──────────────────────────────────────────────────────


class TestAlreadyMigratedPlan:
    def test_skip_and_run_partition_the_statuses(self) -> None:
        plan = AlreadyMigratedPlan(
            statuses=(
                StoreMigrationStatus("memory", True, "memory: already migrated"),
                StoreMigrationStatus("plans", False, "plans: will migrate"),
            )
        )
        assert plan.skip_stores == frozenset({"memory"})
        assert plan.run_stores == frozenset({"plans"})
        assert plan.all_skipped is False
        assert plan.summary_lines() == ["memory: already migrated", "plans: will migrate"]

    def test_all_skipped_true_only_when_nothing_runs(self) -> None:
        plan = AlreadyMigratedPlan(
            statuses=(StoreMigrationStatus("memory", True, "x"),)
        )
        assert plan.all_skipped is True

    def test_all_skipped_false_when_no_statuses(self) -> None:
        # An empty plan is NOT "everything covered" — it is "nothing was
        # even evaluated", a degenerate case that must not read as a pass.
        assert AlreadyMigratedPlan(statuses=()).all_skipped is False


# ── detect_already_migrated: (a) full-coverage no-op ─────────────────────────


class TestFullCoverageNoOp:
    def test_all_stores_skip_when_report_clean_and_no_newer_writes(
        self, tmp_path: Path,
    ) -> None:
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(stores={s: 0 for s in ALL_STORES}, completed_at=T0),
        )
        db = _all_before(tmp_path)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert plan.all_skipped is True
        assert plan.skip_stores == frozenset(ALL_STORES)
        assert plan.run_stores == frozenset()
        for line in plan.summary_lines():
            assert "already migrated" in line
            assert T0 in line

    def test_no_reports_directory_means_everything_runs(self, tmp_path: Path) -> None:
        db = _all_before(tmp_path)
        plan = detect_already_migrated(
            sqlite_path=db, reports_dir=tmp_path / "does-not-exist",
        )
        assert plan.all_skipped is False
        assert plan.run_stores == frozenset(ALL_STORES)
        for line in plan.summary_lines():
            assert "no migration report found" in line


# ── detect_already_migrated: (b) newer local writes ───────────────────────────


class TestNewerLocalWritesRunsOnlyThatStore:
    def test_one_store_newer_than_report_only_that_store_runs(
        self, tmp_path: Path,
    ) -> None:
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(stores={s: 0 for s in ALL_STORES}, completed_at=T0),
        )
        rows = {s: BEFORE_T0 for s in ALL_STORES}
        rows["memory"] = AFTER_T0  # newer than the report
        db = _make_sqlite(tmp_path, rows=rows)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert plan.run_stores == frozenset({"memory"})
        assert plan.skip_stores == frozenset(ALL_STORES) - {"memory"}
        memory_status = next(s for s in plan.statuses if s.store == "memory")
        assert "local writes newer" in memory_status.line

    def test_store_with_no_freshness_probe_table_trusts_the_report(
        self, tmp_path: Path,
    ) -> None:
        # "catalog" has no anchor row at all in this SQLite fixture (table
        # absent) — freshness cannot be confirmed, so the WORK spec's
        # explicit fallback applies: report-presence alone is trusted.
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(stores={s: 0 for s in ALL_STORES}, completed_at=T0),
        )
        rows = {s: BEFORE_T0 for s in ALL_STORES if s != "catalog"}
        db = _make_sqlite(tmp_path, rows=rows)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert plan.all_skipped is True
        catalog_status = next(s for s in plan.statuses if s.store == "catalog")
        assert catalog_status.skip is True


# ── detect_already_migrated: (c) --force ──────────────────────────────────────


class TestForceBypassesDetection:
    def test_force_runs_everything_without_reading_reports_or_sqlite(
        self, tmp_path: Path,
    ) -> None:
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(stores={s: 0 for s in ALL_STORES}, completed_at=T0),
        )
        # A path that does not exist — force must never touch it.
        missing_db = tmp_path / "does-not-exist.db"

        plan = detect_already_migrated(
            sqlite_path=missing_db, reports_dir=reports_dir, force=True,
        )

        assert plan.all_skipped is False
        assert plan.run_stores == frozenset(ALL_STORES)
        assert plan.skip_stores == frozenset()
        for line in plan.summary_lines():
            assert "--force" in line


# ── detect_already_migrated: (d) failed / indeterminate is NOT migrated ──────


class TestFailedOrIndeterminateReportIsNotMigrated:
    def test_store_with_failed_rows_is_not_covered(self, tmp_path: Path) -> None:
        reports_dir = tmp_path / "migration-reports"
        stores = {s: 0 for s in ALL_STORES}
        stores["catalog"] = 3
        _write_report(reports_dir, _report(stores=stores, completed_at=T0))
        db = _all_before(tmp_path)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert "catalog" in plan.run_stores
        catalog_status = next(s for s in plan.statuses if s.store == "catalog")
        assert "failed" in catalog_status.line

    def test_indeterminate_verification_taints_the_whole_report(
        self, tmp_path: Path,
    ) -> None:
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(
                stores={s: 0 for s in ALL_STORES},
                completed_at=T0,
                verification="indeterminate",
            ),
        )
        db = _all_before(tmp_path)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert plan.run_stores == frozenset(ALL_STORES)
        for status in plan.statuses:
            assert "verification" in status.line

    def test_mismatch_verification_taints_the_whole_report(
        self, tmp_path: Path,
    ) -> None:
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(
                stores={s: 0 for s in ALL_STORES},
                completed_at=T0,
                verification="mismatch",
            ),
        )
        db = _all_before(tmp_path)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert plan.run_stores == frozenset(ALL_STORES)

    def test_single_store_report_with_no_verification_key_is_trusted(
        self, tmp_path: Path,
    ) -> None:
        # RDR-153's single-store artifacts (``migrate <store>``) never carry
        # a top-level "verification" key — the WORK spec's "per-store
        # success entries" fallback: judge on the store's own failed count.
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(stores={"memory": 0}, completed_at=T0, verification=None),
            name="memory-only",
        )
        db = _all_before(tmp_path)

        plan = detect_already_migrated(
            sqlite_path=db, reports_dir=reports_dir, stores=("memory",),
        )

        assert plan.all_skipped is True


# ── report evidence quality ───────────────────────────────────────────────────


class TestReportEvidenceQuality:
    def test_corrupt_report_file_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        reports_dir = tmp_path / "migration-reports"
        reports_dir.mkdir(parents=True)
        (reports_dir / "migration-corrupt.json").write_text("{not json")
        _write_report(
            reports_dir,
            _report(stores={s: 0 for s in ALL_STORES}, completed_at=T0),
            name="good",
        )
        db = _all_before(tmp_path)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert plan.all_skipped is True

    def test_latest_report_by_completed_at_wins_over_older_clean_one(
        self, tmp_path: Path,
    ) -> None:
        reports_dir = tmp_path / "migration-reports"
        _write_report(
            reports_dir,
            _report(stores={s: 0 for s in ALL_STORES}, completed_at=BEFORE_T0),
            name="old-clean",
        )
        stores_with_failure = {s: 0 for s in ALL_STORES}
        stores_with_failure["memory"] = 7
        _write_report(
            reports_dir,
            _report(stores=stores_with_failure, completed_at=AFTER_T0),
            name="new-dirty",
        )
        db = _all_before(tmp_path)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        # The NEWER report is dirty for memory -> memory must run, even
        # though an older report for the same store was clean.
        assert "memory" in plan.run_stores

    def test_newer_clean_report_recovers_from_an_older_dirty_one(
        self, tmp_path: Path,
    ) -> None:
        reports_dir = tmp_path / "migration-reports"
        stores_with_failure = {s: 0 for s in ALL_STORES}
        stores_with_failure["memory"] = 7
        _write_report(
            reports_dir,
            _report(stores=stores_with_failure, completed_at=BEFORE_T0),
            name="old-dirty",
        )
        _write_report(
            reports_dir,
            _report(stores={s: 0 for s in ALL_STORES}, completed_at=AFTER_T0),
            name="new-clean",
        )
        db = _all_before(tmp_path)

        plan = detect_already_migrated(sqlite_path=db, reports_dir=reports_dir)

        assert plan.all_skipped is True
