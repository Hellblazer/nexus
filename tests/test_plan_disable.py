# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-mrzp: ``nx plan disable`` / ``enable`` soft-disable a plan row.

Follow-up to nexus-la28 (PR #345 / #358). The la28 PR shipped
list/show/delete/reseed but explicitly deferred ``disable`` because it
required a schema migration plus matcher-filter wiring.

Ported to the engine substrate (nexus-i711w Stage 2 sub-stage A3): the
SQLite ``PlanLibrary`` was deleted, so every test here runs against
``T2Database(tmp).plans`` — a real engine-backed ``HttpPlanLibrary`` on
the per-test tenant the autouse ``_pin_t2_substrate`` fixture mints.
The old ``TestDisabledAtMigration`` class (the ``disabled_at`` column
migration on the local SQLite snapshot) died with the store; the
engine-side column lives in the plans Liquibase changeset and its
behaviour is pinned by ``tests/db/test_http_plan_library_integration.py``
(test_i / test_l) plus this file.

This file covers:

  * Public API on the plan library: ``set_plan_disabled`` /
    ``set_plan_enabled`` round-trip ``disabled_at``.
  * Matcher integration: ``search_plans`` (FTS lane) and
    ``list_active_plans`` (T1 cosine populate source) skip rows with
    ``disabled_at`` set.
  * CLI: ``nx plan disable <id>`` / ``nx plan enable <id>``;
    ``nx plan list`` skips disabled by default and shows them with
    ``--include-disabled``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2 import T2Database


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def plan_db(tmp_path: Path) -> T2Database:
    """Engine-backed T2Database; ``plan_db.plans`` is HttpPlanLibrary."""
    database = T2Database(tmp_path / "plans.db")
    yield database
    database.close()


def _seed_plan(
    db: T2Database, *, name: str, query: str, project: str = "",
    tags: str = "", plan_json: str = '{"steps": []}',
) -> int:
    """Save one plan through the public API and return its row id.

    ``save_plan`` without verb/scope leaves dimensions NULL, so rows do
    not collide on the (project, dimensions) uniqueness the dimensional
    save path enforces; distinct *project* values keep that true even if
    a caller adds dimensions later.
    """
    return db.plans.save_plan(
        query=query, plan_json=plan_json, project=project,
        tags=tags, name=name,
    )


# ── TestDisabledAtMigration DELETED (nexus-i711w Stage 2 sub-stage A3) ──────
#
# Its three tests pinned the ``disabled_at`` column DDL on the local SQLite
# snapshot (`apply_pending` + `_migrate_plans_disabled_at_if_needed`), which
# died with the SQLite PlanLibrary. The engine-side column is created by the
# plans Liquibase changeset; that new rows carry NULL and that the column
# round-trips is asserted behaviourally below and in
# tests/db/test_http_plan_library_integration.py::TestPlansMVV::test_i.


# ── Library API ────────────────────────────────────────────────────────────


class TestSetPlanDisabled:
    def test_set_plan_disabled_stamps_timestamp(self, plan_db: T2Database):
        plan_id = _seed_plan(plan_db, name="r", query="q")
        ok = plan_db.plans.set_plan_disabled(plan_id)
        assert ok is True

        row = plan_db.plans.get_plan(plan_id)
        assert row["disabled_at"] is not None
        assert row["disabled_at"].startswith("20")  # ISO-8601-ish

    def test_set_plan_disabled_with_reason_appends_tag(
        self, plan_db: T2Database,
    ):
        plan_id = _seed_plan(plan_db, name="r", query="q", tags="orig")
        plan_db.plans.set_plan_disabled(plan_id, reason="A/B test - keep retired")

        tags = plan_db.plans.get_plan(plan_id)["tags"]
        assert "orig" in tags
        assert "disable-reason:" in tags
        assert "A/B test" in tags

    def test_set_plan_disabled_missing_id_returns_false(
        self, plan_db: T2Database,
    ):
        ok = plan_db.plans.set_plan_disabled(99999)
        assert ok is False

    def test_set_plan_enabled_clears_timestamp(self, plan_db: T2Database):
        plan_id = _seed_plan(plan_db, name="r", query="q")
        plan_db.plans.set_plan_disabled(plan_id)
        ok = plan_db.plans.set_plan_enabled(plan_id)
        assert ok is True

        row = plan_db.plans.get_plan(plan_id)
        assert row["disabled_at"] is None


# ── Matcher filter ─────────────────────────────────────────────────────────


class TestMatcherFiltersDisabled:
    def test_search_plans_skips_disabled(self, plan_db: T2Database):
        active_id = _seed_plan(
            plan_db, name="active", query="hybrid retrieval factual",
            project="proj-a",
        )
        disabled_id = _seed_plan(
            plan_db, name="disabled", query="hybrid retrieval factual",
            project="proj-b",
        )
        plan_db.plans.set_plan_disabled(disabled_id)
        results = plan_db.plans.search_plans(query="hybrid retrieval", limit=10)
        ids = [r["id"] for r in results]
        assert active_id in ids
        assert disabled_id not in ids

    def test_list_active_plans_skips_disabled(self, plan_db: T2Database):
        active_id = _seed_plan(plan_db, name="a", query="qa", project="p1")
        disabled_id = _seed_plan(plan_db, name="d", query="qd", project="p2")
        plan_db.plans.set_plan_disabled(disabled_id)
        rows = plan_db.plans.list_active_plans()
        ids = [r["id"] for r in rows]
        assert active_id in ids
        assert disabled_id not in ids


# ── CLI ────────────────────────────────────────────────────────────────────


class TestPlanDisableCli:
    """CLI behaviour against the live plan library.

    ``_open_plan_library`` constructs ``HttpPlanLibrary()`` from the same
    env the autouse substrate pin sets (per-test tenant token), so the
    CliRunner invocation reads and writes exactly the rows the ``plan_db``
    fixture seeds — the seam the old ``default_db_path`` patch stood in
    for is gone with the local snapshot (nexus-i711w sub-stage A3).
    """

    def test_disable_command_round_trips(
        self, runner: CliRunner, plan_db: T2Database,
    ):
        plan_id = _seed_plan(plan_db, name="cli-target", query="q")

        result = runner.invoke(main, ["plan", "disable", str(plan_id)])
        assert result.exit_code == 0, result.output
        assert "disabled" in result.output.lower()

        row = plan_db.plans.get_plan(plan_id)
        assert row["disabled_at"] is not None

    def test_disable_with_reason_records_tag(
        self, runner: CliRunner, plan_db: T2Database,
    ):
        plan_id = _seed_plan(plan_db, name="cli-r", query="q", tags="orig")

        result = runner.invoke(main, [
            "plan", "disable", str(plan_id),
            "--reason", "regression in Phase 2",
        ])
        assert result.exit_code == 0, result.output

        tags = plan_db.plans.get_plan(plan_id)["tags"]
        assert "regression in Phase 2" in tags

    def test_disable_unknown_id_fails(
        self, runner: CliRunner, plan_db: T2Database,
    ):
        result = runner.invoke(main, ["plan", "disable", "99999"])
        assert result.exit_code != 0
        assert "no plan" in result.output.lower()

    def test_enable_command_clears_disabled(
        self, runner: CliRunner, plan_db: T2Database,
    ):
        plan_id = _seed_plan(plan_db, name="t", query="q")

        r1 = runner.invoke(main, ["plan", "disable", str(plan_id)])
        assert r1.exit_code == 0, r1.output
        r2 = runner.invoke(main, ["plan", "enable", str(plan_id)])
        assert r2.exit_code == 0, r2.output

        row = plan_db.plans.get_plan(plan_id)
        assert row["disabled_at"] is None

    def test_list_skips_disabled_by_default(
        self, runner: CliRunner, plan_db: T2Database,
    ):
        _seed_plan(plan_db, name="active-row", query="qa", project="p1")
        disabled_id = _seed_plan(
            plan_db, name="disabled-row", query="qd", project="p2",
        )

        r1 = runner.invoke(main, ["plan", "disable", str(disabled_id)])
        assert r1.exit_code == 0, r1.output
        result = runner.invoke(main, ["plan", "list"])
        assert result.exit_code == 0, result.output
        assert "active-row" in result.output
        assert "disabled-row" not in result.output

    def test_list_include_disabled_shows_marker(
        self, runner: CliRunner, plan_db: T2Database,
    ):
        disabled_id = _seed_plan(plan_db, name="disabled-row", query="qd")

        r1 = runner.invoke(main, ["plan", "disable", str(disabled_id)])
        assert r1.exit_code == 0, r1.output
        result = runner.invoke(main, ["plan", "list", "--include-disabled"])
        assert result.exit_code == 0, result.output
        assert "disabled-row" in result.output
