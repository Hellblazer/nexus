# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 §A8 / nexus-rv7x6: ``nx plan repair`` subcommand tests.

Six legacy migrations whose bodies mutated row content moved out of
``apply_pending`` and into ``nexus.plans.repair`` helpers, dispatched
from new ``nx plan repair`` subcommands. The substrate keeps only the
DDL portions; these tests cover the verb-side behaviour the migrations
used to ship.

The helper-level tests in ``tests/test_migrations.py`` and
``tests/test_plan_library.py`` exercise the underlying
``repair_*`` functions directly. This file focuses on the CLI
surface: each subcommand registered, runs against the patched
``default_db_path``, and emits the diagnostic payload.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.commands.plan import plan as plan_cmd


def _seed_plans_schema(db_path: Path) -> sqlite3.Connection:
    """Open *db_path* and seed the full post-RDR-092 plans schema with
    the columns each repair verb touches."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project TEXT NOT NULL DEFAULT '',
            query TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            outcome TEXT DEFAULT 'success',
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            ttl INTEGER,
            verb TEXT DEFAULT '',
            scope TEXT DEFAULT 'global',
            name TEXT DEFAULT '',
            dimensions TEXT,
            scope_tags TEXT NOT NULL DEFAULT '',
            match_text TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.commit()
    return conn


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


class TestRepairGroupRegistration:
    def test_repair_is_a_group(self) -> None:
        repair = plan_cmd.commands["repair"]
        assert hasattr(repair, "commands"), "nx plan repair must be a group"
        for sub in (
            "scope-tags", "dimensions", "match-text",
            "retire-legacy", "builtin-bindings", "all",
        ):
            assert sub in repair.commands, f"missing subcommand: {sub}"


class TestRepairScopeTags:
    def test_backfills_empty_scope_tags(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (query, plan_json, created_at) VALUES (?, ?, ?)",
            (
                "q",
                '{"steps":[{"tool":"search","args":{"corpus":"rdr__arcaneum"}}]}',
                "2026-05-21T00:00:00Z",
            ),
        )
        conn.commit()
        conn.close()

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["repair", "scope-tags"])
        assert result.exit_code == 0, result.output
        assert "backfilled: 1" in result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT scope_tags FROM plans WHERE query='q'").fetchone()
        conn.close()
        assert row[0] == "rdr__arcaneum"

    def test_rewashes_all_sentinel(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (query, plan_json, scope_tags, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("q", '{"steps":[]}', "all", "2026-05-21T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["repair", "scope-tags"])
        assert result.exit_code == 0, result.output
        assert "rewashed: 1" in result.output


class TestRepairDimensions:
    def test_backfills_null_dimension_rows(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (query, plan_json, created_at) VALUES (?, ?, ?)",
            ("analyze the ranker", "{}", "2026-05-21T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["repair", "dimensions"])
        assert result.exit_code == 0, result.output
        assert "backfilled: 1" in result.output
        assert "0 rows need review" in result.output


class TestRepairMatchText:
    def test_backfills_match_text(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (query, plan_json, verb, scope, name, created_at) "
            "VALUES (?, '{}', 'analyze', 'global', 'default', ?)",
            ("Analyze foo", "2026-05-21T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["repair", "match-text"])
        assert result.exit_code == 0, result.output
        assert "backfilled: 1" in result.output


class TestRepairRetireLegacy:
    def test_deletes_operation_shape_rows(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        conn = _seed_plans_schema(db_path)
        legacy = json.dumps({"steps": [{"step": 1, "operation": "search"}]})
        modern = json.dumps({"steps": [{"tool": "search"}]})
        conn.execute(
            "INSERT INTO plans (query, plan_json, created_at) VALUES (?, ?, ?)",
            ("legacy", legacy, "2026-05-21T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO plans (query, plan_json, created_at) VALUES (?, ?, ?)",
            ("modern", modern, "2026-05-21T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["repair", "retire-legacy"])
        assert result.exit_code == 0, result.output
        assert "deleted: 1" in result.output

        conn = sqlite3.connect(str(db_path))
        kept = [r[0] for r in conn.execute(
            "SELECT query FROM plans ORDER BY query"
        ).fetchall()]
        conn.close()
        assert kept == ["modern"]


class TestRepairBuiltinBindings:
    def test_dry_no_op_when_no_legacy_rows(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        _seed_plans_schema(db_path).close()
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["repair", "builtin-bindings"])
        assert result.exit_code == 0, result.output
        # No matching rows => backfilled:0 or skipped reason.
        assert "backfilled: 0" in result.output or "skipped" in result.output


class TestRepairAll:
    def test_runs_every_subcommand_in_order(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        _seed_plans_schema(db_path).close()
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["repair", "all"])
        assert result.exit_code == 0, result.output
        # Each pass announces itself with [name].
        for name in (
            "scope_tags", "dimensions", "match_text",
            "retire_legacy", "builtin_bindings",
        ):
            assert f"[{name}]" in result.output


class TestNoDbFile:
    def test_each_verb_clean_noop_when_no_db(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        missing = tmp_path / "nope.db"
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=missing,
        ):
            for sub in ("scope-tags", "dimensions", "match-text",
                        "retire-legacy", "builtin-bindings", "all"):
                result = runner.invoke(plan_cmd, ["repair", sub])
                assert result.exit_code == 0, (sub, result.output)
                assert "not found" in result.output, (sub, result.output)
