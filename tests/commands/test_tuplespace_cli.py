# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for ``nx tuplespace`` CLI (RDR-110 Step 11, nexus-90pe).

Exercises every subcommand against a real ``tuples.db`` plus an
``EphemeralClient`` chroma backend, with the registry pointed at the
repo's bundled builtin subspace YAMLs (``nx/tuplespace/builtin/``).
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import chromadb
import pytest
from click.testing import CliRunner

from nexus.cli import main
from nexus.commands import tuplespace as ts_cmd


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
  default_lease_seconds: 60
read:
  default_floor: 0.0
  default_n: 5
tiers: [project]
retention_seconds: 86400
"""

_LOCKS_YAML = """
name: locks/<resource>
tier: project
content_type: text
embed_from: content
dimensions:
  resource: { type: string, required: true }
  holder:   { type: string, required: true }
take:
  enabled: true
  mode: exact
  match_keys: [resource]
  default_lease_seconds: 30
read:
  default_floor: 0.0
  default_n: 1
tiers: [project]
retention_seconds: 3600
"""


@pytest.fixture
def builtin_dir(tmp_path: Path) -> Path:
    d = tmp_path / "builtin"
    d.mkdir()
    (d / "tasks.yml").write_text(_TASKS_YAML)
    (d / "locks.yml").write_text(_LOCKS_YAML)
    return d


@pytest.fixture
def env(tmp_path: Path, builtin_dir: Path, monkeypatch):
    """Wire env vars + a chroma index so CLI commands hit a real backend."""
    db_path = tmp_path / "tuples.db"
    monkeypatch.setenv("NX_TUPLES_DB", str(db_path))
    monkeypatch.setenv("NX_TUPLESPACE_BUILTIN_DIR", str(builtin_dir))
    monkeypatch.delenv("NX_STORAGE_MODE", raising=False)

    # Replace the persistent chroma builder with an in-process Ephemeral one
    # so we don't write a real ``chroma/`` directory under the user's
    # config. The shared-process state of EphemeralClient is fine here
    # because each test gets a fresh client + chroma collections are
    # name-scoped to the per-test ``tuples.db``.
    client = chromadb.EphemeralClient()

    def _build_index_test(registry):
        from nexus.tuplespace.index import TupleIndex

        return TupleIndex.from_registry(registry, client)

    monkeypatch.setattr(ts_cmd, "_build_index", _build_index_test)
    yield {"db_path": db_path, "client": client}

    for coll in client.list_collections():
        client.delete_collection(coll.name)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_list_subspaces_plain(runner: CliRunner, env) -> None:
    result = runner.invoke(main, ["tuplespace", "list-subspaces"])
    assert result.exit_code == 0, result.output
    assert "tasks/<project>" in result.output
    assert "locks/<resource>" in result.output


def test_list_subspaces_json(runner: CliRunner, env) -> None:
    result = runner.invoke(main, ["tuplespace", "list-subspaces", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "tasks/<project>" in payload["subspaces"]
    assert "locks/<resource>" in payload["subspaces"]


def test_show_schema_unknown(runner: CliRunner, env) -> None:
    result = runner.invoke(main, ["tuplespace", "show-schema", "nope/whatever"])
    assert result.exit_code == 1
    assert "unknown subspace" in result.output.lower()


def test_show_schema_known(runner: CliRunner, env) -> None:
    result = runner.invoke(main, ["tuplespace", "show-schema", "tasks/demo", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "tasks/<project>"
    assert "status" in payload["dimensions"]


def test_stats_empty(runner: CliRunner, env) -> None:
    result = runner.invoke(main, ["tuplespace", "stats"])
    assert result.exit_code == 0, result.output
    assert "tuples.db not found" in result.output
    assert "tuplespace:" in result.output
    assert "2 subspaces" in result.output


def test_out_then_stats(runner: CliRunner, env) -> None:
    out_result = runner.invoke(
        main,
        [
            "tuplespace", "out", "tasks/demo",
            '{"status": "open", "priority": "P1", "created_by": "alice"}',
            "--content", "ship the landing surface",
        ],
    )
    assert out_result.exit_code == 0, out_result.output
    tid = out_result.output.strip()
    assert len(tid) == 32  # 32-hex tuple_id

    stats_result = runner.invoke(main, ["tuplespace", "stats", "--json"])
    assert stats_result.exit_code == 0, stats_result.output
    payload = json.loads(stats_result.output)
    assert payload["summary"]["tuples"] == 1
    assert payload["per_subspace"][0]["subspace"] == "tasks/demo"
    assert payload["per_subspace"][0]["total"] == 1


def test_read_returns_tuple(runner: CliRunner, env) -> None:
    runner.invoke(
        main,
        [
            "tuplespace", "out", "tasks/demo",
            '{"status": "open", "priority": "P1", "created_by": "alice"}',
            "--content", "ship the landing surface",
        ],
    )
    result = runner.invoke(
        main,
        ["tuplespace", "read", "tasks/demo", "--query", "ship", "-n", "5"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert len(payload["results"]) >= 1
    assert payload["results"][0]["content"].startswith("ship")


def test_take_ack_round_trip(runner: CliRunner, env) -> None:
    runner.invoke(
        main,
        [
            "tuplespace", "out", "locks/build",
            '{"resource": "build", "holder": "ci"}',
            "--content", "ci has build lock",
        ],
    )
    take_result = runner.invoke(
        main,
        [
            "tuplespace", "take", "locks/build",
            "--claimant", "agent-1",
            "--where", '{"resource": "build"}',
        ],
    )
    assert take_result.exit_code == 0, take_result.output
    payload = json.loads(take_result.output)
    assert payload["claimed"] is True
    claim_id = payload["claim_id"]

    ack_result = runner.invoke(
        main, ["tuplespace", "ack", claim_id, "--claimant", "agent-1"]
    )
    assert ack_result.exit_code == 0, ack_result.output
    assert "ok" in ack_result.output


def test_nack_returns_tuple_to_available(runner: CliRunner, env) -> None:
    runner.invoke(
        main,
        [
            "tuplespace", "out", "locks/db",
            '{"resource": "db", "holder": "worker"}',
            "--content", "db lock",
        ],
    )
    take_result = runner.invoke(
        main,
        [
            "tuplespace", "take", "locks/db",
            "--claimant", "agent-x",
            "--where", '{"resource": "db"}',
        ],
    )
    claim_id = json.loads(take_result.output)["claim_id"]

    nack_result = runner.invoke(
        main, ["tuplespace", "nack", claim_id, "--claimant", "agent-x"]
    )
    assert nack_result.exit_code == 0, nack_result.output

    # available count should be back to 1 (the nack released the claim)
    stats_result = runner.invoke(main, ["tuplespace", "stats", "locks/db", "--json"])
    payload = json.loads(stats_result.output)
    assert payload["available"] == 1
    assert payload["claimed"] == 0


def test_daemon_mode_refuses_writes(runner: CliRunner, env, monkeypatch) -> None:
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    result = runner.invoke(
        main,
        [
            "tuplespace", "out", "tasks/demo",
            '{"status": "open", "priority": "P1", "created_by": "alice"}',
            "--content", "x",
        ],
    )
    assert result.exit_code == 2
    assert "daemon" in result.output.lower()


def test_daemon_mode_allows_list(runner: CliRunner, env, monkeypatch) -> None:
    monkeypatch.setenv("NX_STORAGE_MODE", "daemon")
    result = runner.invoke(main, ["tuplespace", "list-subspaces"])
    assert result.exit_code == 0, result.output
    assert "tasks/<project>" in result.output


def test_banner_summary_no_db(env) -> None:
    s = ts_cmd.banner_summary()
    # Fixture builtin_dir installs exactly two YAMLs (tasks.yml + locks.yml).
    assert s["subspaces"] == 2
    assert s["tuples"] == 0
    assert s["active_claims"] == 0
    line = ts_cmd.banner_line()
    assert "tuplespace:" in line
    assert "subspaces" in line
    assert "active claims" in line


def test_banner_summary_with_tuples(runner: CliRunner, env) -> None:
    runner.invoke(
        main,
        [
            "tuplespace", "out", "tasks/demo",
            '{"status": "open", "priority": "P1", "created_by": "alice"}',
            "--content", "task A",
        ],
    )
    runner.invoke(
        main,
        [
            "tuplespace", "out", "tasks/demo",
            '{"status": "open", "priority": "P2", "created_by": "bob"}',
            "--content", "task B",
        ],
    )
    s = ts_cmd.banner_summary()
    assert s["tuples"] == 2
    assert s["active_claims"] == 0
    line = ts_cmd.banner_line()
    assert "2 tuples" in line
    assert "0 active claims" in line
