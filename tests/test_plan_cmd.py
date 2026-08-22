# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx plan`` CLI commands. RDR-092 Phase 0d.2.

Ported to the engine substrate (nexus-i711w Stage 2 sub-stage A3): the
SQLite ``PlanLibrary`` was deleted, so seeding goes through
``T2Database(tmp).plans`` — a real engine-backed ``HttpPlanLibrary`` on
the per-test tenant the autouse ``_pin_t2_substrate`` fixture mints.
``_open_plan_library`` in the CLI constructs ``HttpPlanLibrary()`` from
the same env, so the CliRunner invocations read and write exactly the
rows the ``plan_db`` fixture seeds (the shape test_plan_disable.py
established).

The ``nx plan repair`` verb group DIED with the SQLite store
([21098] verb fates: `nx plan repair` D) — its tests are tombstoned
below, not ported.
"""
from __future__ import annotations

import json

from pathlib import Path

import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.db.t2 import T2Database


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


# The autouse ``_pin_plans_to_local_snapshot`` fixture
# (NX_STORAGE_BACKEND_PLANS=sqlite) was DELETED with the seam it pinned
# (nexus-i711w sub-stage A3): T2Database and ``_open_plan_library`` now
# construct HttpPlanLibrary unconditionally, so there is no local snapshot
# for the pin to select.


@pytest.fixture
def plan_db(tmp_path: Path) -> T2Database:
    """Engine-backed T2Database; ``plan_db.plans`` is HttpPlanLibrary."""
    database = T2Database(tmp_path / "plans.db")
    yield database
    database.close()


def _seed_plan_row(
    db: T2Database,
    *,
    name: str,
    query: str,
    verb: str,
    scope: str = "global",
    project: str = "",
    tags: str = "",
    plan_json: str = '{"steps": []}',
) -> int:
    """Seed one plan through the public save path and return its row id.

    ``dimensions`` is left NULL so rows never collide on the
    ``UNIQUE (project, dimensions)`` dedupe the dimensional save path
    enforces; ``verb`` / ``scope`` / ``name`` are stored as plain columns
    (all the CLI verbs under test read).
    """
    return db.plans.save_plan(
        query=query,
        plan_json=plan_json,
        project=project,
        tags=tags,
        name=name,
        verb=verb,
        scope=scope,
    )


# ── TestPlanRepair DELETED (nexus-i711w Stage 2 sub-stage A3) ───────────────
#
# Its three tests (idempotent backfill, low-conf-first report, missing-DB
# clean exit) drove ``nx plan repair dimensions``, which re-ran the
# ``_backfill_plan_dimensions`` heuristic against the local SQLite T2 file.
# The whole ``nx plan repair`` group (and its `_open_plans_db` sqlite3
# helper) was deleted with the SQLite PlanLibrary ([21098] verb fates);
# the live library is engine-served and content repairs are engine-side
# operations. The SQLite seed helpers ``_seed_plans`` /
# ``_insert_null_dim_plan`` died with them.


# ── nx plan list / show / delete / reseed (nexus-la28) ─────────────────────


class TestPlanList:
    def test_list_empty_library(self, runner: CliRunner) -> None:
        """A fresh tenant has no plans; list says so instead of erroring.

        (Was ``test_list_empty_db`` asserting the missing-local-file
        message — that state died with the local snapshot.)
        """
        result = runner.invoke(main, ["plan", "list"])
        assert result.exit_code == 0, result.output
        assert "no plans match" in result.output.lower()

    def test_list_one_builtin_one_grown(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        builtin_id = _seed_plan_row(
            plan_db, name="research-default", query="research walkthrough",
            verb="research", tags="builtin-template,rdr-078",
        )
        grown_id = _seed_plan_row(
            plan_db, name="grown-1", query="auto-grown", verb="research",
            scope="personal", project="personal", tags="",
        )

        result = runner.invoke(main, ["plan", "list"])
        assert result.exit_code == 0, result.output
        assert "builtin" in result.output
        assert "grown" in result.output
        assert "research-default" in result.output
        assert "grown-1" in result.output
        assert str(builtin_id) in result.output
        assert str(grown_id) in result.output

    def test_list_origin_filter(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        _seed_plan_row(
            plan_db, name="research-default", query="r",
            verb="research", tags="builtin-template",
        )
        _seed_plan_row(
            plan_db, name="grown-x", query="g", verb="research",
            scope="personal", project="personal", tags="",
        )

        result = runner.invoke(main, ["plan", "list", "--origin", "grown"])
        assert result.exit_code == 0, result.output
        assert "grown-x" in result.output
        assert "research-default" not in result.output

    def test_list_name_substring(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        _seed_plan_row(
            plan_db, name="hybrid-factual-lookup", query="h",
            verb="lookup", tags="builtin-template",
        )
        _seed_plan_row(
            plan_db, name="research-default", query="r",
            verb="research", tags="builtin-template",
        )

        result = runner.invoke(main, ["plan", "list", "--name", "hybrid"])
        assert result.exit_code == 0, result.output
        assert "hybrid-factual-lookup" in result.output
        assert "research-default" not in result.output

    def test_list_json(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        _seed_plan_row(
            plan_db, name="some-plan", query="q", verb="research",
            tags="builtin-template",
        )
        result = runner.invoke(main, ["plan", "list", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "some-plan"
        assert data[0]["origin"] == "builtin"


class TestPlanShow:
    def test_show_by_id(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        plan_id = _seed_plan_row(
            plan_db, name="show-target", query="q",
            verb="research", tags="builtin-template",
            plan_json='{"steps": [{"tool": "search", "args": {}}]}',
        )

        result = runner.invoke(main, ["plan", "show", str(plan_id)])
        assert result.exit_code == 0, result.output
        assert "show-target" in result.output
        assert "search" in result.output  # plan_json content rendered

    def test_show_by_name_substring(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        _seed_plan_row(
            plan_db, name="hybrid-factual-lookup", query="q",
            verb="lookup", tags="builtin-template",
        )

        result = runner.invoke(main, ["plan", "show", "hybrid"])
        assert result.exit_code == 0, result.output
        assert "hybrid-factual-lookup" in result.output

    def test_show_no_match(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        result = runner.invoke(main, ["plan", "show", "missing"])
        assert result.exit_code != 0
        assert "no plan" in result.output.lower()


class TestPlanDelete:
    def test_delete_with_yes_flag(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        plan_id = _seed_plan_row(
            plan_db, name="grown-doomed", query="q", verb="research",
            scope="personal", project="personal",
        )

        result = runner.invoke(main, ["plan", "delete", str(plan_id), "-y"])
        assert result.exit_code == 0, result.output
        assert "Removed 1" in result.output

        # Verify the row is actually gone (engine read replaces the old
        # raw-SQL COUNT against the local file).
        assert plan_db.plans.get_plan(plan_id) is None

    def test_delete_missing_id(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        result = runner.invoke(main, ["plan", "delete", "999999999", "-y"])
        assert result.exit_code != 0
        assert "no plan" in result.output.lower()

    def test_delete_aborts_without_yes(
        self, runner: CliRunner, plan_db: T2Database,
    ) -> None:
        plan_id = _seed_plan_row(
            plan_db, name="grown-x", query="q", verb="research",
            scope="personal", project="personal",
        )
        # Decline the confirmation prompt.
        result = runner.invoke(
            main, ["plan", "delete", str(plan_id)], input="n\n",
        )
        assert result.exit_code != 0  # Click abort returns non-zero
        # Verify the row is still there.
        assert plan_db.plans.get_plan(plan_id) is not None


class TestPlanReseed:
    def test_reseed_idempotent(self, runner: CliRunner) -> None:
        """Reseed against the live (engine-served) library is idempotent.

        Service mode skips the old missing-local-file check, so no
        ``default_db_path`` patch is needed: the four-tier loader dedupes
        on ``UNIQUE (project, dimensions)`` against the fresh tenant.
        """
        # First run installs the builtin set.
        first = runner.invoke(main, ["plan", "reseed"])
        assert first.exit_code == 0, first.output
        # Second run is a no-op (idempotent).
        second = runner.invoke(main, ["plan", "reseed"])
        assert second.exit_code == 0, second.output
        assert "Seeded 0" in second.output

    # test_reseed_force_clears_builtins DELETED (nexus-i711w sub-stage A3):
    # its subject was the --force raw-SQL builtin purge against the local
    # SQLite snapshot ("grown row survives the DELETE"). In service mode —
    # the only mode left — reseed_cmd REFUSES --force by design (nexus-o02xe),
    # and that refusal is pinned by
    # test_plan_reseed_force_refuses_in_service_mode below.


# ── nexus-o02xe: service-mode facade routing (RDR-179 Phase 1) ───────────────
#
# Every `nx plan` verb used to hardcode PlanLibrary(path=default_db_path()) —
# in service mode that is the frozen pre-migration SQLite snapshot, so the CLI
# read (and reseed/repair WROTE) a dead file while the live library sat in the
# engine. These tests pin the routing: service backend -> HttpPlanLibrary,
# raw-SQL verbs refuse loudly.


class _StubHttpPlanLibrary:
    """Records calls; stands in for HttpPlanLibrary (no network)."""

    instances: list["_StubHttpPlanLibrary"] = []

    def __init__(self) -> None:
        self.calls: list[str] = []
        _StubHttpPlanLibrary.instances.append(self)

    def list_plans(self, limit=20, project="", *, include_disabled=False):
        self.calls.append("list_plans")
        return []

    def get_plan(self, plan_id):
        self.calls.append(f"get_plan:{plan_id}")
        return {"id": plan_id, "name": "svc-row", "query": "live service row"}

    def delete_plan(self, plan_id):
        self.calls.append(f"delete_plan:{plan_id}")
        return 1

    def close(self) -> None:
        self.calls.append("close")


@pytest.fixture()
def _service_plans(monkeypatch, tmp_path):
    """Pin plans to the service backend with a stubbed HTTP client, and point
    the local default DB at a nonexistent path — the pre-fix code would echo
    'T2 database not found' instead of consulting the service."""
    monkeypatch.setenv("NX_STORAGE_BACKEND_PLANS", "service")
    _StubHttpPlanLibrary.instances.clear()
    monkeypatch.setattr(
        "nexus.db.t2.http_plan_library.HttpPlanLibrary", _StubHttpPlanLibrary
    )
    monkeypatch.setattr(
        "nexus.commands._helpers.default_db_path",
        lambda: tmp_path / "nonexistent" / "memory.db",
    )


def test_plan_list_routes_to_service(runner, _service_plans):
    result = runner.invoke(main, ["plan", "list"])
    assert result.exit_code == 0, result.output
    assert "T2 database not found" not in result.output
    assert _StubHttpPlanLibrary.instances, "HttpPlanLibrary never constructed"
    assert "list_plans" in _StubHttpPlanLibrary.instances[0].calls


def test_plan_delete_routes_to_service(runner, _service_plans):
    result = runner.invoke(main, ["plan", "delete", "7", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Removed 1 row(s)." in result.output
    calls = _StubHttpPlanLibrary.instances[0].calls
    assert "get_plan:7" in calls and "delete_plan:7" in calls


# test_plan_repair_refuses_in_service_mode DELETED (nexus-i711w sub-stage
# A3): its subject was the service-mode REFUSAL of `nx plan repair` — the
# whole repair group is deleted, so `nx plan repair ...` is now a Click
# usage error, not a routed verb with a refusal branch.


def _stub_seed(monkeypatch, summary):
    seen: dict = {}

    def _fake(*, reconcile: bool = False):
        seen["reconcile"] = reconcile
        return summary
    monkeypatch.setattr("nexus.commands.catalog.seed_plan_templates", _fake)
    return seen


def test_plan_reseed_force_reconciles(runner, monkeypatch):
    """--force is the reconcile leg now (nexus-f1mbo). It used to refuse
    unconditionally — its raw-SQL purge died with the SQLite plan library
    (nexus-i711w Stage 2 sub-stage A3) and was never replaced, which is
    how an edited template lost its only route into an existing library."""
    from nexus.commands.catalog import _SeedSummary

    seen = _stub_seed(monkeypatch, _SeedSummary(inserted=1, updated=3, protected=[]))
    result = runner.invoke(main, ["plan", "reseed", "--force"])

    assert result.exit_code == 0, result.output
    assert seen["reconcile"] is True
    assert "Seeded 1 new builtin row(s)." in result.output
    assert "Reconciled 3 drifted row(s)." in result.output


def test_plan_reseed_without_force_stays_insert_only(runner, monkeypatch):
    """And says so when it inserted nothing — the silent 'Seeded 0' is what
    made the frozen library look healthy."""
    from nexus.commands.catalog import _SeedSummary

    seen = _stub_seed(monkeypatch, _SeedSummary(inserted=0, updated=0, protected=[]))
    result = runner.invoke(main, ["plan", "reseed"])

    assert result.exit_code == 0, result.output
    assert seen["reconcile"] is False
    assert "--force" in result.output
