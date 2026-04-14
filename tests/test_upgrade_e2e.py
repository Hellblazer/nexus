# SPDX-License-Identifier: AGPL-3.0-or-later
"""E2E tests for the complete upgrade mechanism (RDR-076, Phase 6).

Validates all 9 success criteria end-to-end.
"""
from __future__ import annotations

import json
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


# ── SC-1: _nexus_version table ──────────────────────────────────────────────


class TestSC1VersionTable:
    def test_fresh_install_creates_version_table(self, tmp_path: Path) -> None:
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, "4.1.2")

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == "4.1.2"
        conn.close()

    def test_t2database_populates_version(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "memory.db"
        db = T2Database(db_path)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        conn.close()
        db.close()


# ── SC-2: Version-gated migration execution ─────────────────────────────────


class TestSC2VersionGating:
    def test_migrations_have_version_tags(self) -> None:
        from nexus.db.migrations import MIGRATIONS, _parse_version

        for m in MIGRATIONS:
            ver = _parse_version(m.introduced)
            assert ver > (0, 0, 0), f"Migration {m.name!r} has invalid version"

    def test_only_newer_migrations_run(self) -> None:
        from nexus.db.migrations import apply_pending

        conn = sqlite3.connect(":memory:")
        apply_pending(conn, "2.0.0")  # Only 1.10.0 migration should run

        from nexus.db import migrations

        migrations._upgrade_done.clear()

        # Upgrade to 3.7.0 — should run 2.8.0 and 3.7.0 migrations
        apply_pending(conn, "3.7.0")
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == "3.7.0"


# ── SC-3: nx upgrade command flags ──────────────────────────────────────────


class TestSC3UpgradeFlags:
    def test_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0

    def test_force(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            runner.invoke(main, ["upgrade"])

            from nexus.db import migrations

            migrations._upgrade_done.clear()

            result = runner.invoke(main, ["upgrade", "--force"])
        assert result.exit_code == 0

    def test_auto_always_exits_zero(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch(
                "nexus.commands.upgrade.apply_pending",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0


# ── SC-4: doctor --check-schema ─────────────────────────────────────────────


class TestSC4DoctorSchema:
    def test_healthy_db_passes(self, runner: CliRunner, tmp_path: Path) -> None:
        from nexus.db.migrations import apply_pending

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, "4.1.2")
        conn.close()

        with patch("nexus.commands._helpers.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-schema"])
        assert result.exit_code == 0
        assert "passed" in result.output.lower()


# ── SC-5: MCP version divergence warning ────────────────────────────────────


class TestSC5McpVersionCheck:
    def test_no_crash_on_missing_db(self) -> None:
        from nexus.mcp_infra import check_version_compatibility

        with patch(
            "nexus.mcp_infra.default_db_path",
            return_value=Path("/nonexistent.db"),
        ):
            check_version_compatibility()


# ── SC-6: Domain stores delegate to module-level functions ──────────────────


class TestSC6Delegation:
    def test_memory_store_delegates(self, tmp_path: Path) -> None:
        from nexus.db.t2.memory_store import MemoryStore

        db_path = tmp_path / "memory.db"
        store = MemoryStore(db_path)
        store.put("test", "title1", "content")
        result = store.get("test", "title1")
        assert result is not None
        store.close()

    def test_plan_library_delegates(self, tmp_path: Path) -> None:
        from nexus.db.t2.plan_library import PlanLibrary

        db_path = tmp_path / "memory.db"
        lib = PlanLibrary(db_path)
        lib.save_plan("test query", '{"plan": "test"}')
        lib.close()


# ── SC-7: Single-line migration addition ────────────────────────────────────


class TestSC7RegistryPattern:
    def test_all_migrations_have_required_fields(self) -> None:
        from nexus.db.migrations import MIGRATIONS

        for m in MIGRATIONS:
            assert hasattr(m, "introduced")
            assert hasattr(m, "name")
            assert hasattr(m, "fn")
            assert callable(m.fn)


# ── SC-8: hooks.json SessionStart ───────────────────────────────────────────


class TestSC8HooksJson:
    def test_upgrade_auto_first(self) -> None:
        hooks_path = Path(__file__).parent.parent / "nx" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        startup_hooks = next(
            h["hooks"]
            for h in data["hooks"]["SessionStart"]
            if "startup" in h["matcher"]
        )
        assert startup_hooks[0]["command"] == "nx upgrade --auto"
        assert startup_hooks[0]["timeout"] == 30


# ── SC-9: Existing install bootstrapping ────────────────────────────────────


class TestSC9Bootstrap:
    def test_existing_install_seeds_pre_registry(self, tmp_path: Path) -> None:
        from nexus.db.migrations import PRE_REGISTRY_VERSION, apply_pending

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        # Simulate existing install with all tables
        conn.executescript(
            """\
            CREATE TABLE memory (
                id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL,
                session TEXT, agent TEXT, content TEXT NOT NULL, tags TEXT,
                timestamp TEXT NOT NULL, ttl INTEGER,
                access_count INTEGER DEFAULT 0 NOT NULL, last_accessed TEXT DEFAULT ''
            );
            CREATE VIRTUAL TABLE memory_fts USING fts5(
                title, content, tags, content='memory', content_rowid='id'
            );
            """
        )
        conn.execute(
            "INSERT INTO memory (project, title, content, tags, timestamp) "
            "VALUES ('test', 'note1', 'content', '', '2026-01-01')"
        )
        conn.commit()

        apply_pending(conn, PRE_REGISTRY_VERSION)

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == PRE_REGISTRY_VERSION
        conn.close()

    def test_fresh_install_seeds_zero(self) -> None:
        from nexus.db.migrations import apply_pending

        conn = sqlite3.connect(":memory:")
        apply_pending(conn, "4.1.2")

        # All base tables created
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "memory" in tables
        assert "plans" in tables
        assert "topics" in tables
