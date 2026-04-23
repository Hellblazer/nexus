# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx plan`` CLI commands. RDR-092 Phase 0d.2.

``nx plan repair`` is the Day-2 ops command that re-runs the
:func:`nexus.db.migrations._backfill_plan_dimensions` heuristic
against the live T2 DB and surfaces low-confidence rows so the
operator can correct the heuristic's edge cases manually.
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


def _seed_plans(tmp_path: Path) -> Path:
    """Return a DB path with a minimal schema + a NULL-dimension row."""
    from nexus.db.migrations import apply_pending
    from nexus.commands.upgrade import _current_version

    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    apply_pending(conn, _current_version())
    conn.close()
    return db_path


def _insert_null_dim_plan(db_path: Path, query: str, tags: str = "") -> int:
    """Insert a row bypassing the migration so dimensions is NULL again."""
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        "INSERT INTO plans (query, plan_json, outcome, tags, created_at) "
        "VALUES (?, '{}', 'success', ?, datetime('now'))",
        (query, tags),
    )
    conn.commit()
    # Strip the dimensions set by apply_pending's inline backfill.
    conn.execute(
        "UPDATE plans SET dimensions = NULL, verb = NULL, "
        "name = NULL, scope = NULL WHERE id = ?",
        (cursor.lastrowid,),
    )
    conn.commit()
    conn.close()
    return cursor.lastrowid


class TestPlanRepair:
    def test_repair_idempotent(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Running ``nx plan repair`` twice in a row must be a no-op the
        second time: the first run backfills the NULL rows, the second
        finds nothing to do.
        """
        db_path = _seed_plans(tmp_path)
        _insert_null_dim_plan(db_path, "analyze the ranker")

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            first = runner.invoke(main, ["plan", "repair"])
        assert first.exit_code == 0, first.output
        assert "backfilled" in first.output.lower()

        # Second run: nothing to backfill.
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            second = runner.invoke(main, ["plan", "repair"])
        assert second.exit_code == 0, second.output
        # "nothing to do" / "0 backfilled" — must not assert the exact
        # phrasing, but the number must be zero.
        assert "0 backfilled" in second.output.lower() or (
            "nothing" in second.output.lower()
        )

    def test_repair_lists_low_conf_first(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Low-confidence rows (wh-fallback hits) should surface at the
        top of the report so the operator can audit them first.
        """
        db_path = _seed_plans(tmp_path)
        # Two NULL-dim rows: one stem-match (analyze), one wh-fallback
        # (only "what" matches).
        _insert_null_dim_plan(db_path, "analyze the ranker output")
        _insert_null_dim_plan(db_path, "what about the graph")

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "repair"])
        assert result.exit_code == 0, result.output
        output = result.output.lower()
        # The low-conf row is named and appears in the review section.
        assert "backfill-low-conf" in output
        assert "what about the graph" in output
        # The high-conf row is counted but not individually listed in
        # the review surface.
        assert "1 low-conf row" in output or "1 row needs review" in output

    def test_repair_no_db_file(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """When T2 DB doesn't exist yet, repair exits cleanly with a
        'nothing to do' message rather than a traceback.
        """
        db_path = tmp_path / "nonexistent" / "memory.db"
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "repair"])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()
