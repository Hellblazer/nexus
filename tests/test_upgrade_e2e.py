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


@pytest.fixture(autouse=True)
def _no_real_daemon_nudge():
    """nexus-scoo5: stop in-process ``nx upgrade`` tests from spawning a real
    detached T2 daemon under the per-test config dir.

    ``upgrade._cycle_daemon_to_current()`` shells out to ``nx daemon t2
    ensure-running`` (spawning a detached ``nx daemon t2 start``), and
    ``_quiesce_daemon()`` shells out to ``nx daemon t2 stop``. Neither
    belongs in these migration-logic/exit-code e2e tests; the spawn was the
    source of the leaked ``test_force0`` orphan daemons. Mirrors
    ``test_upgrade_cmd._no_real_daemon_nudge``. The conftest
    ``_reap_spawned_daemons`` backstop covers anything that slips through."""
    with (
        patch("nexus.commands.upgrade._cycle_daemon_to_current"),
        patch("nexus.commands.upgrade._quiesce_daemon"),
    ):
        yield


# ── SC-1: _nexus_version table ──────────────────────────────────────────────


class TestSC1VersionTable:
    def test_fresh_install_creates_version_table(self, tmp_path: Path) -> None:
        from nexus.catalog.catalog import Catalog
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import apply_pending

        # RDR-170: the lower-bound-only runner attempts every registered
        # migration, including the je0b PK steps that require a catalog. Init one
        # so the run completes and stamps the canonical version cleanly.
        Catalog.init(tmp_path / "catalog")
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, _current_version())

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == _current_version()
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

    def test_only_newer_migrations_run(self, monkeypatch) -> None:
        """RDR-170: gating is by the LOWER bound only — a migration runs iff
        ``introduced > last_seen``. ``current_version`` no longer caps the upper
        end (that upper bound was the nexus-j25po dormancy bug). A second pass at
        the same version runs nothing new. Uses a clean monkeypatched registry
        for determinism (no catalog-absent defer noise)."""
        from nexus.db import migrations
        from nexus.db.migrations import Migration, apply_pending

        ran: list[str] = []
        monkeypatch.setattr(
            migrations,
            "MIGRATIONS",
            [
                Migration("1.10.0", "a", lambda c: ran.append("a")),
                Migration("2.8.0", "b", lambda c: ran.append("b")),
                Migration("3.7.0", "c", lambda c: ran.append("c")),
            ],
        )
        migrations._upgrade_done.clear()

        conn = sqlite3.connect(":memory:")
        apply_pending(conn, "3.7.0")  # current >= all introduced
        assert ran == ["a", "b", "c"]

        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row[0] == "3.7.0"

        # Second pass at the same version: nothing new (all <= last_seen).
        migrations._upgrade_done.clear()
        ran.clear()
        apply_pending(conn, "3.7.0")
        assert ran == []


# ── SC-3: nx upgrade command flags ──────────────────────────────────────────


class TestSC3UpgradeFlags:
    def test_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0

    def test_force(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
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
        from nexus.catalog.catalog import Catalog
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import apply_pending

        # nexus-4s2o: je0b PK migrations require a catalog to complete.
        Catalog.init(tmp_path / "catalog")
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, _current_version())
        conn.close()

        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-schema"])
        assert result.exit_code == 0
        assert "passed" in result.output.lower()


class TestRDR170FrozenBranchReporting:
    """RDR-170 Approach step 3: ``nx doctor --check-schema`` and ``nx upgrade
    --dry-run`` must REPORT a registered step whose ``introduced`` exceeds the
    package version as pending (the dormancy-inverse of nexus-j25po). These
    guard the RDR-142 reporting-lie class in both CLI surfaces against
    re-introduction of a package-version upper bound."""

    @staticmethod
    def _sentinel_registry() -> list:
        from nexus.db.migrations import Migration

        # introduced (99.0.0) far above the package version → a frozen-branch
        # "ahead of release" step. A package-version upper bound would exclude it.
        return [Migration("99.0.0", "rdr170 frozen-branch sentinel", lambda c: None)]

    def _healthy_db(self, tmp_path: Path) -> "Path":
        from nexus.catalog.catalog import Catalog
        from nexus.commands.upgrade import _current_version
        from nexus.db.migrations import apply_pending

        Catalog.init(tmp_path / "catalog")  # je0b PK migrations need a catalog
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        apply_pending(conn, _current_version())  # stored = real registry max
        conn.close()
        return db_path

    def test_doctor_check_schema_reports_ahead_of_version_step(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ) -> None:
        import nexus.db.migrations as _m

        db_path = self._healthy_db(tmp_path)
        monkeypatch.setattr(_m, "MIGRATIONS", self._sentinel_registry())

        with patch("nexus.config.default_db_path", return_value=db_path):
            result = runner.invoke(main, ["doctor", "--check-schema"])
        out = result.output.lower()
        assert "pending migrations" in out, out
        assert "all checks passed" not in out

    def test_upgrade_dry_run_reports_ahead_of_version_step(
        self, runner: CliRunner, tmp_path: Path, monkeypatch
    ) -> None:
        import nexus.db.migrations as _m

        db_path = self._healthy_db(tmp_path)
        sentinel = self._sentinel_registry()
        # Patch BOTH refs: the source (expected_t2_schema_version computes
        # registry_max from it) and the upgrade module-level import (pending_t2).
        monkeypatch.setattr(_m, "MIGRATIONS", sentinel)
        monkeypatch.setattr("nexus.commands.upgrade.MIGRATIONS", sentinel)

        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        out = result.output.lower()
        assert "up to date" not in out, out
        assert "pending migrations" in out
        assert "frozen-branch sentinel" in out


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
        hooks_path = Path(__file__).parent.parent / "conexus" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        startup_hooks = next(
            h["hooks"]
            for h in data["hooks"]["SessionStart"]
            if "startup" in h["matcher"]
        )
        assert startup_hooks[0]["command"].startswith("nx upgrade --auto")
        assert startup_hooks[0]["timeout"] == 30

    def test_pretooluse_bash_timeout_is_short(self) -> None:
        """PreToolUse Bash timeout must stay short.

        ``pre_close_verification_hook.sh`` is advisory (read stdin, JSON
        out, exit 0); the body completes in <100 ms. A long timeout is a
        footgun: a future bug or filesystem stall would block every
        ``Bash`` tool call by that ceiling. Pinning low so any drift
        toward "minutes" trips this test instead of the user.

        Earlier shape used ``timeout: 300`` which would have masked a
        five-minute stall in the hook with no operator visibility. The
        bound here matches the SessionStart fast-path hooks (5 s).
        """
        hooks_path = Path(__file__).parent.parent / "conexus" / "hooks" / "hooks.json"
        data = json.loads(hooks_path.read_text())
        bash_blocks = [
            h for h in data["hooks"].get("PreToolUse", [])
            if h.get("matcher") == "Bash"
        ]
        assert bash_blocks, "PreToolUse Bash matcher missing from hooks.json"
        for block in bash_blocks:
            for hook in block.get("hooks", []):
                timeout = hook.get("timeout")
                assert isinstance(timeout, int), (
                    f"PreToolUse Bash hook missing/invalid timeout: {hook!r}"
                )
                assert timeout <= 10, (
                    f"PreToolUse Bash timeout {timeout}s is too high. "
                    f"This hook is advisory and should never need >5 s. "
                    f"A long ceiling masks real stalls — keep it tight "
                    f"(<=10 s)."
                )


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
