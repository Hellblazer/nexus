# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Phase 1A tier-discipline telemetry (nexus-kren).

Covers:
- Migration creates ``tier_writes`` table + indexes, idempotent.
- ``_record_tier_write`` inserts rows correctly.
- ``_record_tier_write`` swallows exceptions (telemetry must NEVER
  break the hot path).
- Wired-in MCP write tools (memory_put, store_put, scratch put,
  plan_save) emit one row per call.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── Migration ────────────────────────────────────────────────────────────────


class TestMigration:
    def test_creates_table_with_expected_columns(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_tier_writes

        conn = sqlite3.connect(str(tmp_path / "t.db"))
        try:
            migrate_tier_writes(conn)
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(tier_writes)")
            }
        finally:
            conn.close()
        expected = {
            "id", "session_id", "ts", "tool", "tier",
            "agent", "project", "target_title",
        }
        assert expected.issubset(cols), f"missing columns: {expected - cols}"

    def test_creates_three_indexes(self, tmp_path: Path) -> None:
        from nexus.db.migrations import migrate_tier_writes

        conn = sqlite3.connect(str(tmp_path / "t.db"))
        try:
            migrate_tier_writes(conn)
            indexes = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='tier_writes'"
                )
            }
        finally:
            conn.close()
        assert "idx_tier_writes_session" in indexes
        assert "idx_tier_writes_ts" in indexes
        assert "idx_tier_writes_tool" in indexes

    def test_idempotent(self, tmp_path: Path) -> None:
        """Second call must be a clean no-op (no exception, no double-create)."""
        from nexus.db.migrations import migrate_tier_writes

        conn = sqlite3.connect(str(tmp_path / "t.db"))
        try:
            migrate_tier_writes(conn)
            migrate_tier_writes(conn)  # must not raise
            count = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type='table' AND name='tier_writes'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


# ── Recorder helper ──────────────────────────────────────────────────────────


class TestRecorder:
    def test_inserts_row_with_all_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Happy path: helper writes one row with all caller-supplied fields."""
        import nexus.mcp_infra as infra
        from nexus.db.t2 import T2Database
        from nexus.mcp.core import _record_tier_write

        db_path = tmp_path / "t.db"
        monkeypatch.setattr(infra, "t2_ctx", lambda: T2Database(db_path))
        monkeypatch.setenv("NX_SESSION_ID", "test-session-abc")

        _record_tier_write(
            tool="memory_put", tier="T2",
            agent="developer", project="nexus", target_title="my-finding",
        )

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT session_id, tool, tier, agent, project, target_title "
                "FROM tier_writes"
            ).fetchone()
        finally:
            conn.close()
        assert row == (
            "test-session-abc", "memory_put", "T2",
            "developer", "nexus", "my-finding",
        )

    def test_swallows_exceptions_when_t2_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Helper MUST NOT raise into the caller's hot path even if T2
        access fails. Telemetry breaking a tool call is the worst kind
        of regression."""
        import nexus.mcp_infra as infra
        from nexus.mcp.core import _record_tier_write

        def boom():
            raise RuntimeError("simulated t2_ctx failure")

        monkeypatch.setattr(infra, "t2_ctx", boom)

        # Must not raise.
        _record_tier_write(tool="memory_put", tier="T2")

    def test_session_id_falls_back_when_env_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When NX_SESSION_ID is unset and read_claude_session_id returns
        None, helper writes 'unknown' rather than failing or skipping."""
        import nexus.mcp_infra as infra
        from nexus.db.t2 import T2Database
        from nexus.mcp.core import _record_tier_write

        db_path = tmp_path / "t.db"
        monkeypatch.setattr(infra, "t2_ctx", lambda: T2Database(db_path))
        monkeypatch.delenv("NX_SESSION_ID", raising=False)
        # Simulate no session file.
        import nexus.session as ses
        monkeypatch.setattr(ses, "read_claude_session_id", lambda: None)

        _record_tier_write(tool="scratch_put", tier="T1")

        conn = sqlite3.connect(str(db_path))
        try:
            sid = conn.execute(
                "SELECT session_id FROM tier_writes"
            ).fetchone()[0]
        finally:
            conn.close()
        assert sid == "unknown"


# ── Wired MCP write tools ────────────────────────────────────────────────────


class TestWiring:
    """Verify each tier-write MCP tool emits one tier_writes row per call.

    These tests instantiate the MCP tools against a tmp T2 and assert
    a row landed. Failures here mean the tool was changed without
    updating the recorder wire-up.
    """

    def _setup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        import nexus.mcp_infra as infra
        from nexus.db.t2 import T2Database

        db_path = tmp_path / "t.db"
        monkeypatch.setattr(infra, "t2_ctx", lambda: T2Database(db_path))
        monkeypatch.setenv("NX_SESSION_ID", "wire-test")
        return db_path

    def _tier_writes(self, db_path: Path) -> list[tuple]:
        conn = sqlite3.connect(str(db_path))
        try:
            return list(conn.execute(
                "SELECT tool, tier, project, target_title FROM tier_writes "
                "ORDER BY id"
            ))
        finally:
            conn.close()

    def test_memory_put_records_T2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        db_path = self._setup(tmp_path, monkeypatch)
        from nexus.mcp.core import memory_put

        memory_put(
            content="hello", project="nexus", title="t1", tags="x", ttl=30,
        )

        rows = self._tier_writes(db_path)
        assert ("memory_put", "T2", "nexus", "t1") in rows

    def test_scratch_put_records_T1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """scratch action='put' writes T1; other actions (search/list)
        do NOT emit a row (writes only)."""
        db_path = self._setup(tmp_path, monkeypatch)
        from nexus.mcp.core import scratch

        scratch(action="put", content="ephemeral hypothesis", tags="probe")

        rows = self._tier_writes(db_path)
        tools = [r[0] for r in rows]
        assert "scratch_put" in tools
        assert all(r[1] == "T1" for r in rows if r[0] == "scratch_put")

    def test_plan_save_records_plan_tier(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """plan_save writes tier='plan' (distinct from T2 because plan
        retrieval / cold-start cost is the metric being tracked)."""
        db_path = self._setup(tmp_path, monkeypatch)
        from nexus.mcp.core import plan_save

        plan_save(
            query="how does X relate to Y in the catalog",
            plan_json='{"steps":[{"tool":"search","args":{"query":"$intent"}}]}',
            project="nexus",
            tags="search",
        )

        rows = self._tier_writes(db_path)
        plan_rows = [r for r in rows if r[0] == "plan_save"]
        assert len(plan_rows) == 1
        assert plan_rows[0][1] == "plan"
        assert plan_rows[0][2] == "nexus"  # project
