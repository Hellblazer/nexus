# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-b03o: `nx upgrade` surfaces a one-liner advisory when the
name-vs-embed-dim doctor check finds mislabeled collections.

The advisory is informational only — it does NOT fail the upgrade.
Pre-4.32 local-mode installs wrote 384d MiniLM vectors into voyage-named
collections; the operator needs to know about it so they can run
`nx collection rename` (the actual fix lives in nexus-j9ey)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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


# NO _no_real_daemon_nudge fixture: it existed only to stop these in-process
# `nx upgrade` runs from spawning a detached T2 daemon via
# `_cycle_daemon_to_current` (nexus-scoo5). That function retired with the
# daemon (nexus-i711w Stage 2 sub-stage B), so there is no spawn to suppress.


class TestUpgradeAdvisory:
    def test_no_advisory_when_no_mismatches(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Clean install: no mislabeled collections → no advisory text."""
        db_path = tmp_path / "memory.db"
        clean_report = {
            "pass": True, "checked": 5, "mismatches": [],
            "empty": [], "skipped_non_conformant": 0, "unknown_token": [],
        }
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
            patch(
                "nexus.commands.catalog_cmds.doctor._run_name_vs_embed_dim",
                return_value=clean_report,
            ),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        assert "mislabeled" not in result.output
        assert "Advisory" not in result.output

    def test_advisory_skipped_in_auto_mode(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Auto mode (hook-driven) suppresses advisory output — hooks
        have short timeouts and the user isn't watching."""
        db_path = tmp_path / "memory.db"
        dirty_report = {
            "pass": False, "checked": 1,
            "mismatches": [
                {"collection": "code__x__voyage-code-3__v1",
                 "claimed_model": "voyage-code-3",
                 "expected_dim": 1024, "actual_dim": 384},
            ],
            "empty": [], "skipped_non_conformant": 0, "unknown_token": [],
        }
        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
            patch(
                "nexus.commands.catalog_cmds.doctor._run_name_vs_embed_dim",
                return_value=dirty_report,
            ),
        ):
            result = runner.invoke(main, ["upgrade", "--auto"])
        assert result.exit_code == 0
        assert "mislabeled" not in result.output

    def test_advisory_tolerates_check_failure(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """If the doctor check raises (e.g. T3 unavailable on a fresh
        install), the advisory is silent — upgrade must not fail because
        an advisory check blew up."""
        db_path = tmp_path / "memory.db"

        def _boom():
            raise RuntimeError("T3 not initialized")

        with (
            patch("nexus.commands.upgrade._db_path", return_value=db_path),
            patch("nexus.commands.upgrade.T3_UPGRADES", []),
            patch(
                "nexus.commands.catalog_cmds.doctor._run_name_vs_embed_dim",
                side_effect=_boom,
            ),
        ):
            result = runner.invoke(main, ["upgrade"])
        assert result.exit_code == 0
        assert "mislabeled" not in result.output
