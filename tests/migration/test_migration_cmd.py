# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-159 (nexus-3g05n): ``nx migration`` — inspect / recover the sentinel.

The named escape hatch (RDR-159 §"Atomicity + crash recovery"): a CLI crash
between a clean T3 copy and the UNLOCK clear strands the ``migration.state``
sentinel at ``migrating`` / ``migrated-failed``, banner-wrapping every read
surface forever. ``nx migration --clear-state`` removes it; bare ``nx migration``
reports it read-only. Clearing is safe because a resumed migration recomputes
progress from live counts.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.migration_cmd import migration_cmd
from nexus.migration.state import (
    begin_migration,
    current_phase,
    mark_failed,
    read_state,
)


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    return cfg


def test_clear_state_removes_stranded_failed_sentinel() -> None:
    begin_migration(collections_total=4)
    mark_failed("boom mid-unlock")
    assert current_phase() == "migrated-failed"

    result = CliRunner().invoke(migration_cmd, ["--clear-state"])

    assert result.exit_code == 0
    assert current_phase() == "not-migrating"
    assert read_state() is None
    assert "Cleared" in result.output
    assert "migrated-failed" in result.output


def test_clear_state_migrating_requires_force() -> None:
    # A 'migrating' sentinel MAY be a live migration; clearing without --force is
    # refused (non-zero) and leaves the sentinel intact.
    begin_migration(collections_total=6)
    assert current_phase() == "migrating"

    result = CliRunner().invoke(migration_cmd, ["--clear-state"])

    assert result.exit_code != 0
    assert "--force" in result.output
    assert current_phase() == "migrating"  # unchanged
    assert read_state() is not None


def test_clear_state_migrating_with_force_clears() -> None:
    begin_migration(collections_total=6)
    assert current_phase() == "migrating"

    result = CliRunner().invoke(migration_cmd, ["--clear-state", "--force"])

    assert result.exit_code == 0
    assert current_phase() == "not-migrating"
    assert read_state() is None
    assert "Cleared" in result.output
    assert "migrating" in result.output


def test_clear_state_noop_when_absent() -> None:
    result = CliRunner().invoke(migration_cmd, ["--clear-state"])

    assert result.exit_code == 0
    assert current_phase() == "not-migrating"
    assert "No migration state to clear" in result.output


def test_status_shows_migrating_phase_and_progress() -> None:
    begin_migration(collections_total=7, started_at="2026-06-13T00:00:00+00:00")

    result = CliRunner().invoke(migration_cmd, [])

    assert result.exit_code == 0
    assert "migrating" in result.output
    assert "0/7" in result.output
    # Read-only: the status command never mutates the sentinel.
    after = read_state()
    assert after is not None
    assert after.phase == "migrating"
    assert after.collections_done == 0
    assert after.collections_total == 7


def test_status_reports_not_migrating_when_absent() -> None:
    result = CliRunner().invoke(migration_cmd, [])

    assert result.exit_code == 0
    assert "not-migrating" in result.output
    assert read_state() is None


def test_status_surfaces_failure_and_recovery_hint() -> None:
    begin_migration(collections_total=3)
    mark_failed("count mismatch on docs__x")

    result = CliRunner().invoke(migration_cmd, [])

    assert result.exit_code == 0
    assert "migrated-failed" in result.output
    assert "count mismatch on docs__x" in result.output
    assert "nx migration --clear-state" in result.output
