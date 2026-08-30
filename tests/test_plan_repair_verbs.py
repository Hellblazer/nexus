# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx plan set-scope`` CLI tests (#1073) — the survivor of this module.

HISTORY / TOMBSTONE (nexus-i711w Stage 2 sub-stage A3): this file was born
as the RDR-120 §A8 / nexus-rv7x6 test suite for the ``nx plan repair``
group (scope-tags / dimensions / match-text / retire-legacy /
builtin-bindings / all), whose six subcommands re-ran legacy content
migrations against the LOCAL SQLite plans snapshot. The repair group and
its helper module ``nexus.plans.repair`` were DELETED with the SQLite
PlanLibrary ([21098] verb fates: ``nx plan repair`` D) — the live library
is engine-served and content repairs are engine-side operations. Deleted
with them:

  * TestRepairGroupRegistration / TestRepairScopeTags / TestRepairDimensions
    / TestRepairMatchText / TestRepairRetireLegacy / TestRepairBuiltinBindings
    / TestRepairAll / TestNoDbFile — CLI surface of the deleted verbs.
  * TestRepairScopeTagsProjectFallback — the #1069 project-column recovery
    INSIDE ``repair_scope_tags``; its interactive counterpart survives as
    ``nx plan set-scope --from-project`` (tested below).
  * test_set_scope_no_db_exits_cleanly — the "local .db file absent" branch
    no longer exists; the engine-served CLI's failure mode is the
    ``plans service unavailable`` ClickException in ``_open_plan_library``.

``nx plan set-scope`` SURVIVES, routed through ``HttpPlanLibrary`` — these
tests run against the per-test engine tenant: seeds go through
``T2Database(tmp).plans`` and the CLI reads the same tenant via the env the
autouse ``_pin_t2_substrate`` fixture sets.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.commands.plan import plan as plan_cmd
from nexus.db.t2 import T2Database


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def plan_db(tmp_path: Path) -> T2Database:
    """Engine-backed T2Database; ``plan_db.plans`` is HttpPlanLibrary."""
    database = T2Database(tmp_path / "plans.db")
    yield database
    database.close()


# ── #1073: nx plan set-scope command ───────────────────────────────────────


class TestSetScopeCommand:
    """Tests for the ``nx plan set-scope <plan_id> <tags>`` command (#1073)."""

    @staticmethod
    def _seed_with_plan(db: T2Database, *, project: str = "") -> int:
        """Save a corpus:all plan and return its id.

        Inference yields ``scope_tags=''`` for corpus:all regardless of
        *project* (the #1069 fallback was removed — nexus-89uc4), so the
        row starts from the known agnostic state set-scope is tested
        against; no raw override needed.
        """
        return db.plans.save_plan(
            query="test plan query",
            plan_json='{"steps":[{"tool":"search","args":{"corpus":"all"}}]}',
            project=project,
            verb="query",
        )

    def test_set_scope_is_registered(self) -> None:
        assert "set-scope" in plan_cmd.commands, (
            "nx plan set-scope command must be registered"
        )

    def test_set_scope_writes_normalized_tags(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        """set-scope <id> <tag> stores the normalized tag (#1073)."""
        plan_id = self._seed_with_plan(plan_db)
        result = runner.invoke(
            plan_cmd, ["set-scope", str(plan_id), "canon-chat"]
        )
        assert result.exit_code == 0, result.output
        assert "canon-chat" in result.output

        row = plan_db.plans.get_plan(plan_id)
        assert row["scope_tags"] == "canon-chat"

    def test_set_scope_normalizes_hash_suffix(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        """set-scope applies _normalize_scope_string to each entry (#1073)."""
        plan_id = self._seed_with_plan(plan_db)
        result = runner.invoke(
            plan_cmd,
            ["set-scope", str(plan_id),
             "rdr__arcaneum-2ad2825c,knowledge__delos-deadbeef"],
        )
        assert result.exit_code == 0, result.output

        row = plan_db.plans.get_plan(plan_id)
        assert row["scope_tags"] == "knowledge__delos,rdr__arcaneum"

    def test_set_scope_drops_all_sentinel(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        """set-scope drops the 'all' sentinel (#1073)."""
        plan_id = self._seed_with_plan(plan_db)
        result = runner.invoke(
            plan_cmd, ["set-scope", str(plan_id), "all,rdr__arcaneum"]
        )
        assert result.exit_code == 0, result.output

        row = plan_db.plans.get_plan(plan_id)
        assert row["scope_tags"] == "rdr__arcaneum"

    def test_set_scope_idempotent(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        """Running set-scope twice with the same value leaves scope_tags unchanged (#1073)."""
        plan_id = self._seed_with_plan(plan_db)
        runner.invoke(plan_cmd, ["set-scope", str(plan_id), "canon-chat"])
        result = runner.invoke(
            plan_cmd, ["set-scope", str(plan_id), "canon-chat"]
        )
        assert result.exit_code == 0, result.output

        row = plan_db.plans.get_plan(plan_id)
        assert row["scope_tags"] == "canon-chat"

    def test_set_scope_from_project_stamps_project_column(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        """set-scope --from-project stamps scope_tags from the plan's project column (#1073)."""
        plan_id = self._seed_with_plan(
            plan_db, project="canon-conductor-compose",
        )
        result = runner.invoke(
            plan_cmd, ["set-scope", str(plan_id), "--from-project"]
        )
        assert result.exit_code == 0, result.output

        row = plan_db.plans.get_plan(plan_id)
        assert row["scope_tags"] == "canon-conductor-compose"

    def test_set_scope_from_project_drops_all_sentinel(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        """--from-project drops 'all' when the project column equals that sentinel (#1073)."""
        plan_id = self._seed_with_plan(plan_db, project="all")
        result = runner.invoke(
            plan_cmd, ["set-scope", str(plan_id), "--from-project"]
        )
        assert result.exit_code == 0, result.output

        row = plan_db.plans.get_plan(plan_id)
        assert row["scope_tags"] == ""

    def test_set_scope_missing_plan_exits_nonzero(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        """set-scope on a non-existent id exits with code 1."""
        result = runner.invoke(plan_cmd, ["set-scope", "99999", "canon-chat"])
        assert result.exit_code == 1
        assert "no plan" in result.output.lower()
