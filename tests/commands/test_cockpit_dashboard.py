# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end test for ``nx cockpit dashboard`` and ``nx cockpit show <panel>``.

Drives the CLI through Click's CliRunner against a real seeded tuples.db
and a per-test bindings profiles dir. Asserts on ``result.stdout`` (not
``result.output``) per Click 8.2 invariant enforced project-wide.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import chromadb
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.tuplespace.api import out
from nexus.tuplespace.index import TupleIndex
from nexus.tuplespace.registry import Registry
from nexus.tuplespace.store import open_tuples_db


_TASKS_YAML = """
name: tasks/<project>
tier: project
content_type: text
embed_from: content
dimensions:
  status:     { type: enum, values: [open, in_progress, done, cancelled], required: true }
  priority:   { type: enum, values: [P0, P1, P2, P3, P4], required: true }
  created_by: { type: string, required: true }
take:
  enabled: true
  mode: semantic
  floor: 0.0
  margin: 0.0
  default_lease_seconds: 300
read:
  default_floor: 0.0
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""


_PROFILE_YAML = """\
profile: dashboardtest
bindings:
  - name: rule_a
    match:
      subspace: hook_events/notification
      op: out
    action:
      kind: log
      marker: dash_hit
"""


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch):
    """Set up a tuples.db with at least one tuple + a bindings profiles dir."""
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    (builtin / "tasks.yml").write_text(_TASKS_YAML)
    registry = Registry.load(builtin)

    db_path = tmp_path / "tuples.db"
    conn = open_tuples_db(db_path)
    conn.row_factory = sqlite3.Row

    client = chromadb.EphemeralClient()
    index = TupleIndex.from_registry(registry, client)

    out(
        conn=conn, index=index, registry=registry,
        subspace="tasks/demo",
        content="ship the dashboard",
        dimensions={"status": "open", "priority": "P1", "created_by": "alice"},
    )
    conn.close()

    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "dashboardtest.yml").write_text(_PROFILE_YAML)

    monkeypatch.setenv("NX_COCKPIT_TUPLES_DB", str(db_path))
    monkeypatch.setenv("NX_COCKPIT_PROFILES_DIR", str(profiles_dir))

    yield {"db_path": db_path, "profiles_dir": profiles_dir}

    for coll in client.list_collections():
        client.delete_collection(coll.name)


def test_dashboard_renders_three_panels(runner: CliRunner, seeded):
    result = runner.invoke(main, ["cockpit", "dashboard"])
    assert result.exit_code == 0, result.stdout
    # All three panel titles appear
    assert "Active Claims" in result.stdout
    assert "Recent Events" in result.stdout
    assert "Active Bindings" in result.stdout
    # No em-dashes in any rendered output
    assert "—" not in result.stdout


def test_show_single_panel_recent_events(runner: CliRunner, seeded):
    result = runner.invoke(main, ["cockpit", "show", "recent-events"])
    assert result.exit_code == 0, result.stdout
    assert "Recent Events" in result.stdout
    # The seeded tuple produced one 'out' event on tasks/demo
    assert "tasks/demo" in result.stdout


def test_show_single_panel_active_bindings(runner: CliRunner, seeded):
    result = runner.invoke(main, ["cockpit", "show", "active-bindings"])
    assert result.exit_code == 0, result.stdout
    assert "Active Bindings" in result.stdout
    assert "rule_a" in result.stdout


def test_show_single_panel_active_claims(runner: CliRunner, seeded):
    result = runner.invoke(main, ["cockpit", "show", "active-claims"])
    assert result.exit_code == 0, result.stdout
    assert "Active Claims" in result.stdout
    # No active claims in seed -> placeholder
    assert "no data" in result.stdout.lower() or "(empty)" in result.stdout.lower()


def test_show_unknown_panel_errors(runner: CliRunner, seeded):
    result = runner.invoke(main, ["cockpit", "show", "nope"])
    assert result.exit_code != 0


def test_dashboard_missing_db_errors_clearly(runner: CliRunner, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NX_COCKPIT_TUPLES_DB", str(tmp_path / "missing.db"))
    monkeypatch.setenv("NX_COCKPIT_PROFILES_DIR", str(tmp_path / "noprof"))
    result = runner.invoke(main, ["cockpit", "dashboard"])
    assert result.exit_code != 0
    assert "tuples.db" in result.stdout.lower() or "tuples.db" in (result.stderr or "")
