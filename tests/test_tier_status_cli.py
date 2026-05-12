# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Phase 1B nx tier-status CLI (nexus-a52i).

Covers:
- Default mode reads NX_SESSION_ID env, queries tier_writes, prints summary.
- --session, --last, --since, --json modes.
- Empty / missing-table cases produce clean output (no traceback).
- Mutual exclusion of --session/--last/--since.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner


def _seed_t2(db_path: Path, rows: list[tuple]) -> None:
    """Insert tier_writes rows into a fresh tmp T2 DB.

    Each row is (session_id, tool, tier, agent, project, target_title).
    Timestamps auto-stamp at row time.
    """
    from nexus.db.migrations import migrate_tier_writes
    conn = sqlite3.connect(str(db_path))
    try:
        migrate_tier_writes(conn)
        ts = datetime.now(timezone.utc).isoformat()
        for sid, tool, tier, agent, project, title in rows:
            conn.execute(
                "INSERT INTO tier_writes "
                "(session_id, ts, tool, tier, agent, project, target_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, ts, tool, tier, agent, project, title),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def isolated_t2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect default_db_path to a tmp file."""
    from nexus.commands import _helpers, tier_status as ts_mod
    db = tmp_path / "t.db"
    monkeypatch.setattr(_helpers, "default_db_path", lambda: db)
    monkeypatch.setattr(ts_mod, "default_db_path", lambda: db)
    return db


class TestDefaultSession:
    def test_default_uses_env_session_id(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd

        _seed_t2(isolated_t2, [
            ("sess-A", "memory_put", "T2", None, "nexus", "finding-1"),
            ("sess-A", "memory_put", "T2", None, "nexus", "finding-2"),
            ("sess-A", "scratch_put", "T1", None, None, "hypothesis"),
            ("sess-B", "memory_put", "T2", None, "other", "noise"),
        ])

        monkeypatch.setenv("NX_SESSION_ID", "sess-A")
        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 0, result.output
        assert "session sess-A" in result.output
        assert "total: 3" in result.output
        assert "T2" in result.output
        assert "T1" in result.output
        # sess-B's row must not leak into sess-A's count
        assert "noise" not in result.output

    def test_default_no_session_resolvable_exits_clean(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd

        # Pre-create the DB so the no-session-resolvable check fires
        # before the missing-DB check.
        sqlite3.connect(str(isolated_t2)).close()

        # Ensure no session resolvable.
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        import nexus.commands.tier_status as ts_mod
        monkeypatch.setattr(ts_mod, "read_claude_session_id", lambda: None)

        result = CliRunner().invoke(tier_status_cmd, [])
        assert result.exit_code == 1
        assert "No current session resolvable" in result.output


class TestExplicitFlags:
    def test_session_flag_overrides_env(
        self, isolated_t2: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("sess-X", "store_put", "T3", None, None, "permanent"),
            ("sess-X", "plan_save", "plan", None, "nexus", "research-default"),
            ("sess-Y", "memory_put", "T2", None, "nexus", "noise"),
        ])
        monkeypatch.setenv("NX_SESSION_ID", "sess-Y")  # ignored

        result = CliRunner().invoke(tier_status_cmd, ["--session", "sess-X"])
        assert result.exit_code == 0, result.output
        assert "session sess-X" in result.output
        assert "total: 2" in result.output
        assert "T3" in result.output
        assert "plan" in result.output
        assert "noise" not in result.output

    def test_last_n_aggregates_recent_sessions(self, isolated_t2: Path) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("sess-1", "memory_put", "T2", None, "nexus", "a"),
            ("sess-2", "memory_put", "T2", None, "nexus", "b"),
            ("sess-3", "memory_put", "T2", None, "nexus", "c"),
        ])

        result = CliRunner().invoke(tier_status_cmd, ["--last", "3"])
        assert result.exit_code == 0, result.output
        assert "last 3 session(s)" in result.output
        assert "total: 3" in result.output

    def test_json_output_is_valid_and_complete(self, isolated_t2: Path) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("sess-J", "memory_put", "T2", "developer", "nexus", "f1"),
            ("sess-J", "scratch_put", "T1", None, None, "h1"),
        ])

        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "sess-J", "--json"],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["session_id"] == "sess-J"
        assert payload["total_writes"] == 2
        assert payload["by_tier"]["T2"] == 1
        assert payload["by_tier"]["T1"] == 1
        assert payload["by_tier"]["T3"] == 0
        assert any(r["tool"] == "memory_put" and r["agent"] == "developer"
                   for r in payload["rows"])

    def test_mutually_exclusive_flags(self, isolated_t2: Path) -> None:
        from nexus.commands.tier_status import tier_status_cmd
        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "x", "--last", "5"],
        )
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output


class TestEmptyOrMissing:
    def test_empty_session_prints_no_writes(
        self, isolated_t2: Path,
    ) -> None:
        """Session with zero tier_writes prints '(no writes)' rather than
        empty output or traceback."""
        from nexus.commands.tier_status import tier_status_cmd
        _seed_t2(isolated_t2, [
            ("populated-sess", "memory_put", "T2", None, "nexus", "x"),
        ])

        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "empty-sess"],
        )
        assert result.exit_code == 0, result.output
        assert "no writes" in result.output

    def test_missing_table_treated_as_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If tier_writes table doesn't exist (no recorder writes ever),
        CLI prints zero rather than erroring on missing table."""
        from nexus.commands import _helpers, tier_status as ts_mod
        from nexus.commands.tier_status import tier_status_cmd

        # Empty DB — no tier_writes table.
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        monkeypatch.setattr(_helpers, "default_db_path", lambda: db)
        monkeypatch.setattr(ts_mod, "default_db_path", lambda: db)

        result = CliRunner().invoke(
            tier_status_cmd, ["--session", "any-sess"],
        )
        assert result.exit_code == 0, result.output
        assert "no writes" in result.output
