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
            default_bindings TEXT DEFAULT '',
            parent_dims TEXT DEFAULT '',
            use_count INTEGER NOT NULL DEFAULT 0,
            last_used TEXT,
            match_count INTEGER NOT NULL DEFAULT 0,
            match_conf_sum REAL NOT NULL DEFAULT 0.0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            scope_tags TEXT NOT NULL DEFAULT '',
            match_text TEXT NOT NULL DEFAULT '',
            disabled_at TEXT
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


# ── #1069: repair scope-tags project-column fallback ───────────────────────


class TestRepairScopeTagsProjectFallback:
    """repair_scope_tags recovers empty-scope corpus:all rows from their
    project column when _infer_scope_tags yields '' (#1069).
    """

    def test_repair_recovers_empty_scope_from_project_column(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """A corpus:all plan with scope_tags='' and a populated project
        column must have scope_tags set to the project value after repair.
        """
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (project, query, plan_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "canon-conductor-compose",
                "resolve t3 collection name",
                '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}',
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
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE query='resolve t3 collection name'"
        ).fetchone()
        conn.close()
        assert row[0] == "canon-conductor-compose"

    def test_repair_leaves_empty_when_project_also_empty(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """When both scope_tags='' and project='', repair must not invent a tag."""
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (project, query, plan_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "",
                "generic search everything",
                '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}',
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
        assert "backfilled: 0" in result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE query='generic search everything'"
        ).fetchone()
        conn.close()
        assert row[0] == ""

    def test_repair_project_fallback_idempotent(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """Running repair twice on the same corpus:all + project row must not
        change the scope_tags after the first pass.
        """
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (project, query, plan_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "my-project",
                "corpus all with project",
                '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}',
                "2026-05-21T00:00:00Z",
            ),
        )
        conn.commit()
        conn.close()

        patch_target = "nexus.commands._helpers.default_db_path"
        with patch(patch_target, return_value=db_path):
            runner.invoke(plan_cmd, ["repair", "scope-tags"])
        with patch(patch_target, return_value=db_path):
            runner.invoke(plan_cmd, ["repair", "scope-tags"])

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE query='corpus all with project'"
        ).fetchone()
        conn.close()
        assert row[0] == "my-project"

    def test_repair_drops_agnostic_project_sentinel(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """A project value of 'all' must be dropped as a sentinel, not stored."""
        conn = _seed_plans_schema(db_path)
        conn.execute(
            "INSERT INTO plans (project, query, plan_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                "all",
                "agnostic project plan",
                '{"steps":[{"tool":"search","args":{"corpus":"all"}}]}',
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

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE query='agnostic project plan'"
        ).fetchone()
        conn.close()
        assert row[0] == ""


# ── #1073: nx plan set-scope command ───────────────────────────────────────


class TestSetScopeCommand:
    """Tests for the ``nx plan set-scope <plan_id> <tags>`` command (#1073)."""

    def _seed_with_plan(
        self, db_path: Path, *, project: str = "", scope_tags: str = "",
    ) -> int:
        """Insert a minimal plan via PlanLibrary (so FTS/triggers are set up)
        and return its id.  Uses scope_tags=None to bypass save_plan's
        own normalization (we want to seed a specific raw value for testing
        set-scope against).
        """
        from nexus.db.t2.plan_library import PlanLibrary  # noqa: PLC0415

        lib = PlanLibrary(path=db_path)
        try:
            row_id = lib.save_plan(
                query="test plan query",
                plan_json='{"steps":[{"tool":"search","args":{"corpus":"all"}}]}',
                project=project,
            )
            # Override scope_tags to the requested raw value so tests can
            # start from a known state (e.g. empty string even when project
            # would be inferred).
            if scope_tags != "":
                lib.set_scope_tags(row_id, scope_tags)
            else:
                # Force-clear scope_tags in case save_plan set it from project.
                with lib._lock:
                    lib.conn.execute(
                        "UPDATE plans SET scope_tags = '' WHERE id = ?", (row_id,)
                    )
                    lib.conn.commit()
        finally:
            lib.close()
        return row_id

    def test_set_scope_is_registered(self) -> None:
        assert "set-scope" in plan_cmd.commands, (
            "nx plan set-scope command must be registered"
        )

    def test_set_scope_writes_normalized_tags(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """set-scope <id> <tag> stores the normalized tag (#1073)."""
        plan_id = self._seed_with_plan(db_path)
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                plan_cmd, ["set-scope", str(plan_id), "canon-chat"]
            )
        assert result.exit_code == 0, result.output
        assert "canon-chat" in result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        conn.close()
        assert row[0] == "canon-chat"

    def test_set_scope_normalizes_hash_suffix(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """set-scope applies _normalize_scope_string to each entry (#1073)."""
        plan_id = self._seed_with_plan(db_path)
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                plan_cmd,
                ["set-scope", str(plan_id), "rdr__arcaneum-2ad2825c,knowledge__delos-deadbeef"],
            )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        conn.close()
        assert row[0] == "knowledge__delos,rdr__arcaneum"

    def test_set_scope_drops_all_sentinel(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """set-scope drops the 'all' sentinel (#1073)."""
        plan_id = self._seed_with_plan(db_path)
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                plan_cmd, ["set-scope", str(plan_id), "all,rdr__arcaneum"]
            )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        conn.close()
        assert row[0] == "rdr__arcaneum"

    def test_set_scope_idempotent(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """Running set-scope twice with the same value leaves scope_tags unchanged (#1073)."""
        plan_id = self._seed_with_plan(db_path)
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            runner.invoke(plan_cmd, ["set-scope", str(plan_id), "canon-chat"])
            result = runner.invoke(
                plan_cmd, ["set-scope", str(plan_id), "canon-chat"]
            )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        conn.close()
        assert row[0] == "canon-chat"

    def test_set_scope_from_project_stamps_project_column(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """set-scope --from-project stamps scope_tags from the plans.project column (#1073)."""
        plan_id = self._seed_with_plan(db_path, project="canon-conductor-compose")
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                plan_cmd, ["set-scope", str(plan_id), "--from-project"]
            )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        conn.close()
        assert row[0] == "canon-conductor-compose"

    def test_set_scope_from_project_drops_all_sentinel(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """--from-project drops 'all' when the project column equals that sentinel (#1073)."""
        plan_id = self._seed_with_plan(db_path, project="all")
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(
                plan_cmd, ["set-scope", str(plan_id), "--from-project"]
            )
        assert result.exit_code == 0, result.output

        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT scope_tags FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        conn.close()
        assert row[0] == ""

    def test_set_scope_missing_plan_exits_nonzero(
        self, runner: CliRunner, db_path: Path,
    ) -> None:
        """set-scope on a non-existent id exits with code 1."""
        _seed_plans_schema(db_path).close()
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=db_path,
        ):
            result = runner.invoke(plan_cmd, ["set-scope", "99999", "canon-chat"])
        assert result.exit_code == 1

    def test_set_scope_no_db_exits_cleanly(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """set-scope exits 0 with a 'not found' message when the DB file is absent."""
        missing = tmp_path / "nope.db"
        with patch(
            "nexus.commands._helpers.default_db_path", return_value=missing,
        ):
            result = runner.invoke(plan_cmd, ["set-scope", "1", "canon-chat"])
        assert result.exit_code == 0
        assert "not found" in result.output
