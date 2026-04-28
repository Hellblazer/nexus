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


# ── nx plan list / show / delete / reseed (nexus-la28) ─────────────────────


def _seed_plan_row(
    db_path: Path,
    *,
    name: str,
    query: str,
    verb: str,
    scope: str = "global",
    project: str = "",
    tags: str = "",
    dimensions_json: str | None = None,
    plan_json: str = '{"steps": []}',
) -> int:
    """Insert a fully-populated plan row directly via SQL."""
    if dimensions_json is None:
        dimensions_json = _json_dumps(
            {"verb": verb, "scope": scope, "strategy": "default"}
        )
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        "INSERT INTO plans "
        "(project, query, plan_json, outcome, tags, created_at, "
        " name, verb, scope, dimensions) "
        "VALUES (?, ?, ?, 'success', ?, datetime('now'), ?, ?, ?, ?)",
        (project, query, plan_json, tags, name, verb, scope, dimensions_json),
    )
    conn.commit()
    plan_id = cursor.lastrowid
    conn.close()
    return plan_id


def _json_dumps(obj):
    import json as _json
    return _json.dumps(obj)


class TestPlanList:
    def test_list_empty_db(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = tmp_path / "memory.db"
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "list"])
        assert result.exit_code == 0
        assert "not found" in result.output.lower()

    def test_list_one_builtin_one_grown(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        builtin_id = _seed_plan_row(
            db_path, name="research-default", query="research walkthrough",
            verb="research", tags="builtin-template,rdr-078",
        )
        grown_id = _seed_plan_row(
            db_path, name="grown-1", query="auto-grown", verb="research",
            scope="personal", project="personal", tags="",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "list"])
        assert result.exit_code == 0
        assert "builtin" in result.output
        assert "grown" in result.output
        assert "research-default" in result.output
        assert "grown-1" in result.output
        assert str(builtin_id) in result.output
        assert str(grown_id) in result.output

    def test_list_origin_filter(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        _seed_plan_row(
            db_path, name="research-default", query="r",
            verb="research", tags="builtin-template",
        )
        _seed_plan_row(
            db_path, name="grown-x", query="g", verb="research",
            scope="personal", project="personal", tags="",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                main, ["plan", "list", "--origin", "grown"],
            )
        assert result.exit_code == 0
        assert "grown-x" in result.output
        assert "research-default" not in result.output

    def test_list_name_substring(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        _seed_plan_row(
            db_path, name="hybrid-factual-lookup", query="h",
            verb="lookup", tags="builtin-template",
        )
        _seed_plan_row(
            db_path, name="research-default", query="r",
            verb="research", tags="builtin-template",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                main, ["plan", "list", "--name", "hybrid"],
            )
        assert result.exit_code == 0
        assert "hybrid-factual-lookup" in result.output
        assert "research-default" not in result.output

    def test_list_json(self, runner: CliRunner, tmp_path: Path) -> None:
        import json as _json
        db_path = _seed_plans(tmp_path)
        _seed_plan_row(
            db_path, name="some-plan", query="q", verb="research",
            tags="builtin-template",
        )
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "list", "--json"])
        assert result.exit_code == 0
        data = _json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "some-plan"
        assert data[0]["origin"] == "builtin"


class TestPlanShow:
    def test_show_by_id(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(
            db_path, name="show-target", query="q",
            verb="research", tags="builtin-template",
            plan_json='{"steps": [{"tool": "search", "args": {}}]}',
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "show", str(plan_id)])
        assert result.exit_code == 0
        assert "show-target" in result.output
        assert "search" in result.output  # plan_json content rendered

    def test_show_by_name_substring(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        _seed_plan_row(
            db_path, name="hybrid-factual-lookup", query="q",
            verb="lookup", tags="builtin-template",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "show", "hybrid"])
        assert result.exit_code == 0
        assert "hybrid-factual-lookup" in result.output

    def test_show_no_match(self, runner: CliRunner, tmp_path: Path) -> None:
        db_path = _seed_plans(tmp_path)
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "show", "missing"])
        assert result.exit_code != 0
        assert "no plan" in result.output.lower()


class TestPlanDelete:
    def test_delete_with_yes_flag(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(
            db_path, name="grown-doomed", query="q", verb="research",
            scope="personal", project="personal",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                main, ["plan", "delete", str(plan_id), "-y"],
            )
        assert result.exit_code == 0, result.output
        assert "Removed 1" in result.output

        # Verify the row is actually gone.
        conn = sqlite3.connect(str(db_path))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert cnt == 0

    def test_delete_missing_id(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "delete", "9999", "-y"])
        assert result.exit_code != 0
        assert "no plan" in result.output.lower()

    def test_delete_aborts_without_yes(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(
            db_path, name="grown-x", query="q", verb="research",
            scope="personal", project="personal",
        )
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            # Decline the confirmation prompt.
            result = runner.invoke(
                main, ["plan", "delete", str(plan_id)], input="n\n",
            )
        assert result.exit_code != 0  # Click abort returns non-zero
        # Verify the row is still there.
        conn = sqlite3.connect(str(db_path))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert cnt == 1


class TestPlanReseed:
    def test_reseed_idempotent(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        # First run installs the builtin set.
        first = runner.invoke(main, ["plan", "reseed"])
        assert first.exit_code == 0, first.output
        # Second run is a no-op (idempotent).
        second = runner.invoke(main, ["plan", "reseed"])
        assert second.exit_code == 0, second.output
        assert "Seeded 0" in second.output

    def test_reseed_force_clears_builtins(
        self, runner: CliRunner, tmp_path: Path, monkeypatch,
    ) -> None:
        db_path = _seed_plans(tmp_path)
        monkeypatch.setattr(
            "nexus.commands._helpers.default_db_path", lambda: db_path,
        )
        # Seed once.
        runner.invoke(main, ["plan", "reseed"])

        # Add a non-builtin grown row to verify --force only deletes
        # builtins.
        grown_id = _seed_plan_row(
            db_path, name="grown-survivor", query="q", verb="research",
            scope="personal", project="personal", tags="",
        )

        result = runner.invoke(main, ["plan", "reseed", "--force"])
        assert result.exit_code == 0, result.output
        assert "removed" in result.output.lower()

        # Grown survivor is still in the table.
        conn = sqlite3.connect(str(db_path))
        cnt = conn.execute(
            "SELECT COUNT(*) FROM plans WHERE id = ?", (grown_id,),
        ).fetchone()[0]
        conn.close()
        assert cnt == 1, "non-builtin row must survive --force reseed"
