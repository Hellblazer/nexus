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


@pytest.fixture(autouse=True)
def _no_real_daemon_nudge():
    """Patch the post-upgrade daemon cycle (nexus-5ldk1) AND the RDR-185
    precondition stage for all upgrade tests so they never shell out to the
    real host daemons or cycle a live supervisor (the precondition stage's
    production defaults read the REAL lease and, on a version mismatch,
    would stop/start the box's live service — never from a unit test).
    Yields the daemon-cycle mock so tests can assert whether the nudge
    fired."""
    with (
        patch("nexus.commands.upgrade._cycle_daemon_to_current") as m,
        patch("nexus.commands.upgrade._converge_preconditions"),
    ):
        yield m


class TestUpgradeCommand:
    """Tests for the ``nx upgrade`` CLI command."""

    def test_upgrade_default(self, runner: CliRunner, tmp_path: Path) -> None:
        """Default invocation runs pending migrations and reports results."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),  # avoid cloud call
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0

    def test_upgrade_dry_run(self, runner: CliRunner, tmp_path: Path) -> None:
        """--dry-run lists pending migrations without executing."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower() or "pending" in result.output.lower()

    def test_upgrade_cycles_daemon_on_success(
        self, runner: CliRunner, tmp_path: Path, _no_real_daemon_nudge,
    ) -> None:
        """nexus-5ldk1: a successful (non-dry-run) upgrade brings a stale
        daemon to the just-installed version."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        assert _no_real_daemon_nudge.called, "upgrade did not cycle the daemon"

    def test_upgrade_dry_run_does_not_cycle_daemon(
        self, runner: CliRunner, tmp_path: Path, _no_real_daemon_nudge,
    ) -> None:
        """--dry-run installs nothing, so it must not touch the daemon."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert not _no_real_daemon_nudge.called, "dry-run must not cycle the daemon"

    def test_upgrade_force(self, runner: CliRunner, tmp_path: Path) -> None:
        """--force resets version gate to 0.0.0 and re-runs."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
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
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
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
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
            result = runner.invoke(main, ["upgrade", "--dry-run"])
        assert result.exit_code == 0
        assert "cannot determine" in result.output.lower()

    def test_upgrade_up_to_date(self, runner: CliRunner, tmp_path: Path) -> None:
        """When already current, reports up to date."""
        db_path = tmp_path / "memory.db"
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
        ):
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

class TestSubstrateBridgeRetired:
    """RDR-185 P4.2 (nexus-n7u38.29): the nexus-0rwwv bridge is RETIRED.

    The bridge pointed ``nx upgrade`` at ``nx guided-upgrade`` when a
    substrate cutover was pending, and offered to chain into it. P4.0
    registered the substrate rung and P4.0b made provisioning a
    precondition, so the walk IS the cutover — the pointer would advertise
    a verb P4.1 demoted out of ``--help`` (breaking "one story, one verb")
    and re-answer a DATA-rung question from an ad-hoc re-sample (the third
    mechanism Gap-4 bans).

    These are RETIREMENT pins: the probe must not run, its output must not
    reach the user, and the walk must never chain into the demoted verb —
    each pinned with the probe patched to a LOUD return, so a re-introduced
    bridge fails rather than passing vacuously.
    """

    def test_pending_cutover_never_advertises_the_demoted_verb(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_MIGRATION_NOTICE", "1")
        with (
            patch("nexus.commands.upgrade._db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
            patch("nexus.migration.guided_upgrade.pending_migration_notice",
                  return_value="A one-time storage migration is pending: run nx guided-upgrade") as notice,
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        # Not merely absent from the output — never probed at all.
        notice.assert_not_called()
        assert "guided-upgrade" not in result.output

    def test_auto_mode_never_probes(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        monkeypatch.setenv("NX_MIGRATION_NOTICE", "1")
        with (
            patch("nexus.commands.upgrade._db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
            patch("nexus.migration.guided_upgrade.pending_migration_notice") as notice,
        ):
            result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0
        notice.assert_not_called()

    def test_never_chains_into_guided_upgrade_even_on_a_tty(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        # The chain prompt is gone: a knob at a derivable point. The walk
        # already converged the cutover in this same invocation, and the
        # genuine decisions it cannot derive (source-gone, billed re-embed)
        # are consented INSIDE the rung. Nothing may prompt to run the
        # demoted verb, and nothing may invoke it.
        from unittest.mock import MagicMock

        monkeypatch.setenv("NX_MIGRATION_NOTICE", "1")
        guided = MagicMock()
        with (
            patch("nexus.commands.upgrade._db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
            patch("nexus.migration.guided_upgrade.pending_migration_notice",
                  return_value="A one-time storage migration is pending"),
            patch("nexus.commands.guided_upgrade_cmd.guided_upgrade_cmd", guided),
            patch("click.confirm", return_value=True) as confirm,
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        guided.assert_not_called()
        confirm.assert_not_called()
