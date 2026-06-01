# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH-1061 E2: nx upgrade --dry-run honestly reports deferred/gated migration work.

Scenario: ``_nexus_version.cli_version`` matches the installed version (``pending_t2
= []`` by the version-range filter), but the PK migration ``migrate_document_aspects_pk_to_doc_id``
would trip the ``_check_high_volume_orphans`` gate on next daemon start (raising
``MigrationError``) OR is deferred because the catalog is absent (raising
``MigrationRetry``).

In both cases ``nx upgrade --dry-run`` must NOT say "No pending migrations" — it must
surface the deferred/gated step and the remediation.

Fix scope: in ``_run_upgrade``'s dry-run path, detect known gated/deferred conditions
(high-volume orphans, catalog-absent PK migration) even when the version-range filter
shows ``pending_t2 = []``.

E2 explicitly states: "If a true fix needs more than reporting, implement the accurate
reporting and flag the deeper reconciliation as a follow-up in your report."
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_module_state() -> None:
    from nexus.db import migrations
    from nexus.db.t2 import catalog_taxonomy, memory_store, plan_library

    migrations._upgrade_done.clear()
    memory_store._migrated_paths.clear()
    plan_library._migrated_paths.clear()
    catalog_taxonomy._migrated_paths.clear()


@pytest.fixture(autouse=True)
def _no_real_daemon_nudge():
    with (
        patch("nexus.commands.upgrade._cycle_daemon_to_current"),
        patch("nexus.commands.upgrade._quiesce_daemon"),
    ):
        yield


def _make_db_at_current_version(tmp_path: Path):
    """Create a memory.db where cli_version == installed version but aspects PK
    migration is not yet applied (document_aspects table has legacy PK).

    Returns (db_path, conn, current_version_str).
    """
    from nexus.commands.upgrade import _current_version
    from nexus.db.migrations import bootstrap_version

    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")

    # Bootstrap base tables + version table
    bootstrap_version(conn)

    # Forcibly set cli_version to current so the version-range filter says "no pending"
    current = _current_version()
    conn.execute(
        "UPDATE _nexus_version SET value=? WHERE key='cli_version'",
        (current,),
    )
    conn.commit()

    # Create document_aspects with the PRE-migration schema (legacy PK on collection, source_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS document_aspects ("
        "  collection  TEXT NOT NULL,"
        "  source_path TEXT NOT NULL,"
        "  doc_id      TEXT NOT NULL DEFAULT '',"
        "  aspect_type TEXT,"
        "  aspect_data TEXT,"
        "  PRIMARY KEY (collection, source_path)"  # OLD pk — not yet migrated to doc_id
        ")"
    )

    return db_path, conn, current


def _catalog_db_for(tmp_path: Path) -> Path:
    """Create a minimal catalog .catalog.db so _check_deferred_migrations bypasses
    the catalog-absent branch and reaches the high-volume orphan SELECT.
    """
    import sqlite3 as _sqlite3
    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    cat_db = cat_dir / ".catalog.db"
    # Minimal schema — just needs to exist; _check_deferred_migrations only checks
    # catalog_db_path.exists(), not the schema contents.
    conn = _sqlite3.connect(str(cat_db))
    conn.execute("CREATE TABLE IF NOT EXISTS documents (tumbler TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    return cat_db


class TestUpgradeDryRunHighVolumeOrphans:
    """When high-volume orphans exist in document_aspects, --dry-run must surface them.

    The orphan branch in _check_deferred_migrations only runs when the catalog
    DB EXISTS (so the catalog-absent early-return is bypassed).  These tests
    create the catalog DB AND inject orphan rows, then verify the orphan SELECT
    branch is executed and surfaced in dry-run output.
    """

    def test_dry_run_reports_gated_migration(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run must not say 'no pending' when high-volume orphan gate would fire."""
        from nexus.db.migrations import _HIGH_VOLUME_ORPHAN_THRESHOLD

        db_path, conn, current = _make_db_at_current_version(tmp_path)

        # Inject high-volume orphans (doc_id='', count > threshold)
        threshold = _HIGH_VOLUME_ORPHAN_THRESHOLD
        rows = [
            ("knowledge__test__bge__v1", f"/path/to/doc_{i}.md", "")
            for i in range(threshold + 5)
        ]
        conn.executemany(
            "INSERT INTO document_aspects (collection, source_path, doc_id) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

        # Create the catalog DB so _check_deferred_migrations reaches the orphan SELECT
        # (without it, the catalog-absent branch returns early before checking orphans).
        cat_db = _catalog_db_for(tmp_path)

        from nexus.db.migrations import _upgrade_done
        _upgrade_done.clear()

        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch(
                "nexus.db.migrations._catalog_db_path_from_conn",
                return_value=cat_db,
            ),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])

        # Must NOT say "No pending migrations" — high-volume orphan gate exists
        assert "No pending migrations" not in result.output, (
            f"Expected dry-run to report high-volume orphan gate, not 'No pending'.\n"
            f"Output:\n{result.output}"
        )

    def test_dry_run_mentions_remediation_for_orphans(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run output must include remediation for the high-volume orphan gate."""
        from nexus.db.migrations import _HIGH_VOLUME_ORPHAN_THRESHOLD

        db_path, conn, current = _make_db_at_current_version(tmp_path)

        threshold = _HIGH_VOLUME_ORPHAN_THRESHOLD
        rows = [
            ("knowledge__test__v1", f"/doc_{i}.md", "")
            for i in range(threshold + 5)
        ]
        conn.executemany(
            "INSERT INTO document_aspects (collection, source_path, doc_id) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()

        # Create the catalog DB so the orphan SELECT branch actually runs.
        cat_db = _catalog_db_for(tmp_path)

        from nexus.db.migrations import _upgrade_done
        _upgrade_done.clear()

        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch(
                "nexus.db.migrations._catalog_db_path_from_conn",
                return_value=cat_db,
            ),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])

        output = result.output
        # Must mention orphans, high-volume threshold, or the migration name
        assert (
            "orphan" in output.lower()
            or "high-volume" in output.lower()
            or "high_volume" in output.lower()
            or "document_aspects" in output.lower()
            or "NEXUS_MIGRATION_HIGH_VOLUME_THRESHOLD" in output
            or "deferred" in output.lower()
            or "gated" in output.lower()
        ), (
            f"Expected high-volume orphan info in --dry-run output:\n{output}"
        )


class TestUpgradeDryRunCatalogAbsent:
    """When catalog is absent, the PK migration is deferred; --dry-run must report this."""

    def test_dry_run_reports_catalog_absent_deferral(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run must not say 'no pending' when catalog-absent defer would fire."""
        db_path, conn, current = _make_db_at_current_version(tmp_path)
        conn.commit()
        conn.close()

        from nexus.db.migrations import _upgrade_done
        _upgrade_done.clear()

        # No catalog present in tmp_path — the PK migration would raise MigrationRetry
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.db.migrations._catalog_db_path_from_conn", return_value=tmp_path / "nonexistent" / ".catalog.db"),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])

        # Must NOT silently say "No pending migrations"
        assert "No pending migrations" not in result.output, (
            f"Expected dry-run to surface catalog-absent deferral, not 'No pending'.\n"
            f"Output:\n{result.output}"
        )

    def test_dry_run_mentions_deferred_pk_migration(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run output for catalog-absent case must mention the deferred migration."""
        db_path, conn, current = _make_db_at_current_version(tmp_path)
        conn.commit()
        conn.close()

        from nexus.db.migrations import _upgrade_done
        _upgrade_done.clear()

        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.db.migrations._catalog_db_path_from_conn", return_value=tmp_path / "nonexistent" / ".catalog.db"),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])

        output = result.output
        # Must mention migration details or deferred status
        assert (
            "deferred" in output.lower()
            or "catalog" in output.lower()
            or "PK" in output
            or "document_aspects" in output.lower()
            or "gated" in output.lower()
        ), (
            f"Expected deferred migration info in --dry-run output:\n{output}"
        )


class TestUpgradeDryRunNoFalsePositive:
    """When all migrations are truly complete, --dry-run must still say 'no pending'."""

    def test_clean_db_reports_no_pending(self, runner: CliRunner, tmp_path: Path) -> None:
        """A fully-migrated DB with no deferred steps must report 'no pending'."""
        from nexus.catalog.catalog import Catalog
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import apply_pending

        # Fully migrate with catalog present so deferred steps complete
        cat_dir = tmp_path / "catalog"
        Catalog.init(cat_dir)

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        current = _current_version()
        apply_pending(conn, current)
        conn.close()

        from nexus.db.migrations import _upgrade_done
        _upgrade_done.clear()

        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            result = runner.invoke(main, ["upgrade", "--dry-run"])

        assert "No pending migrations" in result.output, (
            f"Clean fully-migrated DB must report 'No pending migrations'.\n"
            f"Output:\n{result.output}"
        )
        assert result.exit_code == 0
