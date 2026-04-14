# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx upgrade`` CLI command (RDR-076, Phase 4)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _clear_upgrade_done() -> None:
    from nexus.db import migrations

    migrations._upgrade_done.clear()


@pytest.fixture()
def _tmp_db(tmp_path: Path) -> Path:
    """Return a temp DB path and patch default_db_path to use it."""
    return tmp_path / "memory.db"


class TestUpgradeCommand:
    """Tests for the ``nx upgrade`` CLI command."""

    def test_upgrade_default(self, runner: CliRunner, tmp_path: Path) -> None:
        """Default invocation runs pending migrations and reports results."""
        db_path = tmp_path / "memory.db"
        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0

    def test_upgrade_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run lists pending migrations without executing."""
        db_path = tmp_path / "memory.db"
        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "pending" in result.output.lower()

    def test_upgrade_force(self, runner: CliRunner, tmp_path: Path) -> None:
        """--force resets version gate to 0.0.0 and re-runs."""
        db_path = tmp_path / "memory.db"
        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            # First run to set up version
            runner.invoke(main, ["upgrade"])

            from nexus.db import migrations

            migrations._upgrade_done.clear()

            # Force re-run
            result = runner.invoke(main, ["upgrade", "--force"])
        assert result.exit_code == 0

        # Verify stored version is current after --force
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT value FROM _nexus_version WHERE key='cli_version'"
        ).fetchone()
        assert row is not None
        conn.close()

    def test_force_dry_run_shows_all_pending(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--force --dry-run shows all migrations as pending."""
        db_path = tmp_path / "memory.db"
        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            # First run to apply everything
            runner.invoke(main, ["upgrade"])

            from nexus.db import migrations

            migrations._upgrade_done.clear()

            # Force dry-run — should list migrations
            result = runner.invoke(main, ["upgrade", "--force", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "pending" in result.output.lower()

    def test_upgrade_auto_exits_zero_on_error(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--auto exits 0 even when apply_pending raises."""
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

    def test_upgrade_auto_skips_t3(self, runner: CliRunner, tmp_path: Path) -> None:
        """--auto mode skips T3 upgrade steps."""
        from nexus.db.migrations import T3UpgradeStep

        db_path = tmp_path / "memory.db"
        t3_called: list[bool] = []
        step = T3UpgradeStep(
            "0.0.1", "test step", lambda t3, tax: t3_called.append(True)
        )
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", [step]),
        ):
            result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0
        assert len(t3_called) == 0

    def test_dry_run_unresolvable_version(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """--dry-run with unresolvable CLI version emits distinct message."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade._current_version", return_value="0.0.0"),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert "cannot determine" in result.output.lower()

    def test_upgrade_up_to_date(self, runner: CliRunner, tmp_path: Path) -> None:
        """When already current, reports up to date."""
        db_path = tmp_path / "memory.db"
        with patch("nexus.commands.upgrade._db_path", return_value=db_path):
            # First run applies everything
            runner.invoke(main, ["upgrade"])

            from nexus.db import migrations

            migrations._upgrade_done.clear()

            # Second run — already current
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0


class TestT3UpgradeStep:
    """T3UpgradeStep dataclass and T3_UPGRADES list."""

    def test_dataclass_fields(self) -> None:
        from nexus.db.migrations import T3UpgradeStep

        fn = MagicMock()
        step = T3UpgradeStep(introduced="4.2.0", name="test", fn=fn)
        assert step.introduced == "4.2.0"
        assert step.name == "test"
        assert step.fn is fn

    def test_t3_upgrades_list_exists(self) -> None:
        from nexus.db.migrations import T3_UPGRADES

        assert isinstance(T3_UPGRADES, list)
