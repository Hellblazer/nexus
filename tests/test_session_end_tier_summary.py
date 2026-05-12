# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Phase 1C: SessionEnd launcher tier-write summary (nexus-a52i).

The launcher daemonizes and detaches stdio after main(), so the
summary must print BEFORE the fork. This test suite locks in:

- Sessions with writes get a one-line stderr summary at close.
- Sessions with zero writes are silent (no noise on transactional runs).
- Failures (missing table, unreadable DB, no session resolvable) are
  swallowed — telemetry must never break session close.
"""
from __future__ import annotations

import io
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _seed(db_path: Path, rows: list[tuple]) -> None:
    """Insert tier_writes rows. Each row: (session_id, tool, tier)."""
    from nexus.db.migrations import migrate_tier_writes

    conn = sqlite3.connect(str(db_path))
    try:
        migrate_tier_writes(conn)
        ts = datetime.now(timezone.utc).isoformat()
        for sid, tool, tier in rows:
            conn.execute(
                "INSERT INTO tier_writes "
                "(session_id, ts, tool, tier) VALUES (?, ?, ?, ?)",
                (sid, ts, tool, tier),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def isolated_t2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    from nexus.commands import _helpers
    db = tmp_path / "t.db"
    monkeypatch.setattr("nexus.config.default_db_path", lambda: db)
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    import nexus.session
    monkeypatch.setattr(nexus.session, "read_claude_session_id", lambda: None)
    return db


class TestPrintTierStatusSummary:
    def test_prints_summary_when_session_has_writes(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from nexus._session_end_launcher import _print_tier_status_summary

        sid = "abc12345-1111-2222-3333-1234567890ab"
        monkeypatch.setenv("NX_SESSION_ID", sid)
        _seed(isolated_t2, [
            (sid, "memory_put", "T2"),
            (sid, "memory_put", "T2"),
            (sid, "scratch_put", "T1"),
            (sid, "store_put", "T3"),
        ])

        _print_tier_status_summary()

        out = capsys.readouterr().err
        assert "nx tier writes" in out
        assert sid[:8] in out
        assert "total=4" in out
        assert "T1=1" in out
        assert "T2=2" in out
        assert "T3=1" in out
        # plan not in this run, must be omitted
        assert "plan=" not in out

    def test_silent_when_zero_writes(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Zero writes ⇒ no output. Transactional sessions don't need
        the noise."""
        from nexus._session_end_launcher import _print_tier_status_summary

        sid = "empty-session-no-writes-zzzzzzzz"
        monkeypatch.setenv("NX_SESSION_ID", sid)
        _seed(isolated_t2, [
            ("other-session", "memory_put", "T2"),  # for someone else
        ])

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_silent_when_no_session_resolvable(
        self,
        isolated_t2: Path,
        capsys: pytest.CaptureFixture,
    ) -> None:
        """No NX_SESSION_ID, no claude session file → silent (the launcher
        would have nothing meaningful to summarise)."""
        from nexus._session_end_launcher import _print_tier_status_summary

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_silent_when_db_missing(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DB file absent → silent. Fresh installs that haven't done any
        tier writes yet shouldn't error at close."""
        from nexus.commands import _helpers
        from nexus._session_end_launcher import _print_tier_status_summary

        # Path that does NOT exist.
        absent = tmp_path / "missing.db"
        monkeypatch.setattr("nexus.config.default_db_path", lambda: absent)
        monkeypatch.setenv("NX_SESSION_ID", "any")

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_silent_when_table_not_initialized(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DB exists but tier_writes table not yet created (no writes ever) → silent."""
        from nexus.commands import _helpers
        from nexus._session_end_launcher import _print_tier_status_summary

        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()
        monkeypatch.setattr("nexus.config.default_db_path", lambda: empty)
        monkeypatch.setenv("NX_SESSION_ID", "any")

        _print_tier_status_summary()
        assert capsys.readouterr().err == ""

    def test_swallows_unexpected_exception(
        self,
        capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Any unexpected error in the summary path must NOT propagate.
        Telemetry breaking the launcher is the worst regression class."""
        from nexus._session_end_launcher import _print_tier_status_summary

        # Sabotage: make default_db_path raise.
        from nexus.commands import _helpers

        def boom():
            raise RuntimeError("simulated default_db_path failure")

        monkeypatch.setattr("nexus.config.default_db_path", boom)
        monkeypatch.setenv("NX_SESSION_ID", "any")

        # Must not raise.
        _print_tier_status_summary()
        assert capsys.readouterr().err == ""
