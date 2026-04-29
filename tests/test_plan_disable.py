# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mrzp: ``nx plan disable`` / ``enable`` soft-disable a plan row.

Follow-up to nexus-la28 (PR #345 / #358). The la28 PR shipped
list/show/delete/reseed but explicitly deferred ``disable`` because it
required a schema migration plus matcher-filter wiring.

This file covers:

  * Migration: ``disabled_at`` column added idempotently.
  * Public API on ``PlanLibrary``: ``set_plan_disabled`` /
    ``set_plan_enabled`` round-trip the column.
  * Matcher integration: ``search_plans`` (T2 FTS5 lane) and
    ``list_active_plans`` (T1 cosine populate source) skip rows with
    ``disabled_at IS NOT NULL``.
  * CLI: ``nx plan disable <id>`` / ``nx plan enable <id>``;
    ``nx plan list`` skips disabled by default and shows them with
    ``--include-disabled``.
"""
from __future__ import annotations

import json as _json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.migrations import apply_pending
from nexus.commands.upgrade import _current_version


# ── Helpers ────────────────────────────────────────────────────────────────


def _seed_plans(tmp_path: Path) -> Path:
    """Return a DB path with the plans schema fully migrated.

    Instantiates ``PlanLibrary`` so the un-registered migrations
    (``_migrate_plans_disabled_at_if_needed`` etc.) run and are not
    gated on the package version.
    """
    from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

    db_path = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db_path))
    apply_pending(conn, _current_version())
    conn.close()
    # Open PlanLibrary once to run the unconditional plans-table
    # migrations (project/ttl/scope_tags/disabled_at). Close immediately.
    lib = PlanLibrary(path=db_path)
    lib.close()
    return db_path


def _seed_plan_row(
    db_path: Path, *, name: str, query: str, verb: str = "research",
    scope: str = "global", project: str = "", tags: str = "",
    plan_json: str = '{"steps": []}',
) -> int:
    """Insert a plan row directly via SQL.

    Includes ``match_text`` (set to *query* for simplicity) so the
    plans_fts trigger indexes content the FTS5 lane can find. The real
    ``save_plan`` path synthesizes match_text from description + verb +
    name; for tests we just use the query string.
    """
    dimensions_json = _json.dumps(
        {"verb": verb, "scope": scope, "strategy": "default"}
    )
    conn = sqlite3.connect(str(db_path))
    cursor = conn.execute(
        "INSERT INTO plans "
        "(project, query, plan_json, outcome, tags, created_at, "
        " name, verb, scope, dimensions, match_text) "
        "VALUES (?, ?, ?, 'success', ?, datetime('now'), ?, ?, ?, ?, ?)",
        (project, query, plan_json, tags, name, verb, scope,
         dimensions_json, query),
    )
    conn.commit()
    plan_id = cursor.lastrowid
    conn.close()
    return plan_id


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# ── Migration ───────────────────────────────────────────────────────────────


class TestDisabledAtMigration:
    def test_disabled_at_column_present_after_migration(self, tmp_path: Path):
        db_path = _seed_plans(tmp_path)
        conn = sqlite3.connect(str(db_path))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()}
        conn.close()
        assert "disabled_at" in cols

    def test_existing_rows_have_null_disabled_at(self, tmp_path: Path):
        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(db_path, name="row-1", query="q-1")
        conn = sqlite3.connect(str(db_path))
        val = conn.execute(
            "SELECT disabled_at FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert val is None

    def test_migration_idempotent(self, tmp_path: Path):
        """Running apply_pending twice must not error or duplicate columns."""
        db_path = _seed_plans(tmp_path)
        conn = sqlite3.connect(str(db_path))
        # Re-apply
        apply_pending(conn, _current_version())
        cols = [r[1] for r in conn.execute("PRAGMA table_info(plans)").fetchall()]
        conn.close()
        assert cols.count("disabled_at") == 1


# ── Library API ────────────────────────────────────────────────────────────


class TestSetPlanDisabled:
    def test_set_plan_disabled_stamps_timestamp(self, tmp_path: Path):
        from nexus.db.t2.plan_library import PlanLibrary

        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(db_path, name="r", query="q")
        lib = PlanLibrary(path=db_path)
        try:
            ok = lib.set_plan_disabled(plan_id)
        finally:
            lib.close()
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        val = conn.execute(
            "SELECT disabled_at FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert val is not None
        assert val.startswith("20")  # ISO-8601-ish

    def test_set_plan_disabled_with_reason_appends_tag(self, tmp_path: Path):
        from nexus.db.t2.plan_library import PlanLibrary

        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(db_path, name="r", query="q", tags="orig")
        lib = PlanLibrary(path=db_path)
        try:
            lib.set_plan_disabled(plan_id, reason="A/B test - keep retired")
        finally:
            lib.close()

        conn = sqlite3.connect(str(db_path))
        tags = conn.execute(
            "SELECT tags FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert "orig" in tags
        assert "disable-reason:" in tags
        assert "A/B test" in tags

    def test_set_plan_disabled_missing_id_returns_false(self, tmp_path: Path):
        from nexus.db.t2.plan_library import PlanLibrary

        db_path = _seed_plans(tmp_path)
        lib = PlanLibrary(path=db_path)
        try:
            ok = lib.set_plan_disabled(99999)
        finally:
            lib.close()
        assert ok is False

    def test_set_plan_enabled_clears_timestamp(self, tmp_path: Path):
        from nexus.db.t2.plan_library import PlanLibrary

        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(db_path, name="r", query="q")
        lib = PlanLibrary(path=db_path)
        try:
            lib.set_plan_disabled(plan_id)
            ok = lib.set_plan_enabled(plan_id)
        finally:
            lib.close()
        assert ok is True

        conn = sqlite3.connect(str(db_path))
        val = conn.execute(
            "SELECT disabled_at FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert val is None


# ── Matcher filter ─────────────────────────────────────────────────────────


class TestMatcherFiltersDisabled:
    def test_search_plans_skips_disabled(self, tmp_path: Path):
        from nexus.db.t2.plan_library import PlanLibrary

        db_path = _seed_plans(tmp_path)
        # Distinct (project, dimensions) per row to satisfy the
        # UNIQUE partial index on the plans table.
        active_id = _seed_plan_row(
            db_path, name="active", query="hybrid retrieval factual",
            scope="global", project="proj-a",
        )
        disabled_id = _seed_plan_row(
            db_path, name="disabled", query="hybrid retrieval factual",
            scope="global", project="proj-b",
        )
        lib = PlanLibrary(path=db_path)
        try:
            lib.set_plan_disabled(disabled_id)
            results = lib.search_plans(query="hybrid retrieval", limit=10)
            ids = [r["id"] for r in results]
        finally:
            lib.close()
        assert active_id in ids
        assert disabled_id not in ids

    def test_list_active_plans_skips_disabled(self, tmp_path: Path):
        from nexus.db.t2.plan_library import PlanLibrary

        db_path = _seed_plans(tmp_path)
        active_id = _seed_plan_row(
            db_path, name="a", query="qa", scope="global", project="p1",
        )
        disabled_id = _seed_plan_row(
            db_path, name="d", query="qd", scope="personal", project="p2",
        )
        lib = PlanLibrary(path=db_path)
        try:
            lib.set_plan_disabled(disabled_id)
            rows = lib.list_active_plans()
            ids = [r["id"] for r in rows]
        finally:
            lib.close()
        assert active_id in ids
        assert disabled_id not in ids


# ── CLI ────────────────────────────────────────────────────────────────────


class TestPlanDisableCli:
    def test_disable_command_round_trips(
        self, runner: CliRunner, tmp_path: Path,
    ):
        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(db_path, name="cli-target", query="q")

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "disable", str(plan_id)])
        assert result.exit_code == 0, result.output
        assert "disabled" in result.output.lower()

        conn = sqlite3.connect(str(db_path))
        val = conn.execute(
            "SELECT disabled_at FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert val is not None

    def test_disable_with_reason_records_tag(
        self, runner: CliRunner, tmp_path: Path,
    ):
        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(
            db_path, name="cli-r", query="q", tags="orig",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, [
                "plan", "disable", str(plan_id),
                "--reason", "regression in Phase 2",
            ])
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        tags = conn.execute(
            "SELECT tags FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert "regression in Phase 2" in tags

    def test_disable_unknown_id_fails(
        self, runner: CliRunner, tmp_path: Path,
    ):
        db_path = _seed_plans(tmp_path)
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(main, ["plan", "disable", "9999"])
        assert result.exit_code != 0
        assert "no plan" in result.output.lower()

    def test_enable_command_clears_disabled(
        self, runner: CliRunner, tmp_path: Path,
    ):
        db_path = _seed_plans(tmp_path)
        plan_id = _seed_plan_row(db_path, name="t", query="q")

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            r1 = runner.invoke(main, ["plan", "disable", str(plan_id)])
            assert r1.exit_code == 0, r1.output
            r2 = runner.invoke(main, ["plan", "enable", str(plan_id)])
            assert r2.exit_code == 0, r2.output

        conn = sqlite3.connect(str(db_path))
        val = conn.execute(
            "SELECT disabled_at FROM plans WHERE id = ?", (plan_id,),
        ).fetchone()[0]
        conn.close()
        assert val is None

    def test_list_skips_disabled_by_default(
        self, runner: CliRunner, tmp_path: Path,
    ):
        db_path = _seed_plans(tmp_path)
        active_id = _seed_plan_row(
            db_path, name="active-row", query="qa",
            scope="global", project="p1",
        )
        disabled_id = _seed_plan_row(
            db_path, name="disabled-row", query="qd",
            scope="personal", project="p2",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            r1 = runner.invoke(main, ["plan", "disable", str(disabled_id)])
            assert r1.exit_code == 0, r1.output
            result = runner.invoke(main, ["plan", "list"])
        assert result.exit_code == 0, result.output
        assert "active-row" in result.output
        assert "disabled-row" not in result.output

    def test_list_include_disabled_shows_marker(
        self, runner: CliRunner, tmp_path: Path,
    ):
        db_path = _seed_plans(tmp_path)
        disabled_id = _seed_plan_row(
            db_path, name="disabled-row", query="qd",
        )

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            r1 = runner.invoke(main, ["plan", "disable", str(disabled_id)])
            assert r1.exit_code == 0, r1.output
            result = runner.invoke(
                main, ["plan", "list", "--include-disabled"],
            )
        assert result.exit_code == 0, result.output
        assert "disabled-row" in result.output
