# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus-ntbg: hook duration_ms telemetry capture.

Verifies:
- T2 hook_telemetry table schema (created via _TELEMETRY_SCHEMA_SQL)
- Telemetry.log_hook_event / query_slow_hooks / trim_hook_telemetry
- Migration migrate_hook_telemetry on a legacy DB without the table
- nx/hooks/scripts/hook_telemetry.py threshold gating + write
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from nexus.db.t2 import T2Database
from nexus.db.migrations import migrate_hook_telemetry


@pytest.fixture()
def t2():
    with tempfile.TemporaryDirectory() as d:
        db = T2Database(Path(d) / "t2.db")
        yield db


# ── Schema & method tests ──────────────────────────────────────────────────


def test_hook_telemetry_table_exists(t2):
    cols = {
        r[1]
        for r in t2.telemetry.conn.execute(
            "PRAGMA table_info(hook_telemetry)"
        ).fetchall()
    }
    assert cols == {
        "ts", "hook_event_name", "tool_name",
        "duration_ms", "session_id", "cwd",
    }


def test_log_hook_event_inserts_row(t2):
    t2.telemetry.log_hook_event(
        hook_event_name="PostToolUse",
        tool_name="Bash",
        duration_ms=2500,
        session_id="sess-test",
        cwd="/tmp/test",
    )
    rows = t2.telemetry.conn.execute(
        "SELECT hook_event_name, tool_name, duration_ms, session_id, cwd "
        "FROM hook_telemetry"
    ).fetchall()
    assert rows == [("PostToolUse", "Bash", 2500, "sess-test", "/tmp/test")]


def test_query_slow_hooks_filters_by_threshold(t2):
    for d in (500, 2500, 5500):
        t2.telemetry.log_hook_event("PostToolUse", "Bash", d)
    rows = t2.telemetry.query_slow_hooks(threshold_ms=2000, days=7)
    durations = sorted(r["duration_ms"] for r in rows)
    assert durations == [2500, 5500]


def test_query_slow_hooks_returns_newest_first(t2):
    import time
    t2.telemetry.log_hook_event("PostToolUse", "Bash", 3000)
    time.sleep(0.01)
    t2.telemetry.log_hook_event("PostToolUse", "Edit", 4000)
    rows = t2.telemetry.query_slow_hooks(days=7)
    assert [r["tool_name"] for r in rows] == ["Edit", "Bash"]


def test_query_slow_hooks_respects_limit(t2):
    for i in range(10):
        t2.telemetry.log_hook_event("PostToolUse", f"tool{i}", 3000)
    rows = t2.telemetry.query_slow_hooks(limit=3, days=7)
    assert len(rows) == 3


def test_trim_hook_telemetry_deletes_old(t2):
    from datetime import UTC, datetime, timedelta
    old_ts = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    fresh_ts = datetime.now(UTC).isoformat()
    with t2.telemetry._lock:
        t2.telemetry.conn.executemany(
            "INSERT INTO hook_telemetry "
            "(ts, hook_event_name, tool_name, duration_ms) VALUES (?, ?, ?, ?)",
            [(old_ts, "PostToolUse", "Old", 3000),
             (fresh_ts, "PostToolUse", "Fresh", 3000)],
        )
        t2.telemetry.conn.commit()
    deleted = t2.telemetry.trim_hook_telemetry(days=30)
    assert deleted == 1
    remaining = t2.telemetry.conn.execute(
        "SELECT tool_name FROM hook_telemetry"
    ).fetchall()
    assert remaining == [("Fresh",)]


def test_query_slow_hooks_rejects_bad_days(t2):
    with pytest.raises(ValueError):
        t2.telemetry.query_slow_hooks(days=0)


def test_trim_hook_telemetry_rejects_bad_days(t2):
    with pytest.raises(ValueError):
        t2.telemetry.trim_hook_telemetry(days=0)


# ── Migration test (legacy DB without the table) ───────────────────────────


def test_migrate_hook_telemetry_creates_table_on_legacy_db():
    """Legacy DB lacking hook_telemetry gets the table on migration."""
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "legacy.db"
        # Build a minimal "legacy" T2 without the hook_telemetry table
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE memory (id INTEGER PRIMARY KEY)")
        conn.commit()

        # Verify the table doesn't exist yet
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='hook_telemetry'"
            ).fetchone()
            is None
        )

        migrate_hook_telemetry(conn)

        # Now the table exists
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='hook_telemetry'"
            ).fetchone()
            is not None
        )

        # Idempotent — second call is a no-op
        migrate_hook_telemetry(conn)
        conn.close()


# ── Hook script: threshold gating + write ──────────────────────────────────


def _hook_script_path() -> Path:
    return (
        Path(__file__).parent.parent
        / "nx" / "hooks" / "scripts" / "hook_telemetry.py"
    )


def _run_hook(payload: dict, env: dict) -> int:
    proc = subprocess.run(
        [sys.executable, str(_hook_script_path())],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=10,
    )
    return proc.returncode


def test_hook_script_below_threshold_does_not_write():
    with tempfile.TemporaryDirectory() as d:
        T2Database(Path(d) / "memory.db")  # creates table
        env = {
            "NEXUS_CONFIG_DIR": d,
            "NX_HOOK_TELEMETRY_THRESHOLD_MS": "2000",
        }
        rc = _run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "duration_ms": 500,
                "session_id": "s",
                "cwd": "/tmp",
            },
            env,
        )
        assert rc == 0
        conn = sqlite3.connect(str(Path(d) / "memory.db"))
        rows = conn.execute("SELECT COUNT(*) FROM hook_telemetry").fetchone()
        conn.close()
        assert rows == (0,)


def test_hook_script_above_threshold_writes_row():
    with tempfile.TemporaryDirectory() as d:
        T2Database(Path(d) / "memory.db")
        env = {
            "NEXUS_CONFIG_DIR": d,
            "NX_HOOK_TELEMETRY_THRESHOLD_MS": "1000",
        }
        rc = _run_hook(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "duration_ms": 3500,
                "session_id": "s1",
                "cwd": "/tmp/foo",
            },
            env,
        )
        assert rc == 0
        conn = sqlite3.connect(str(Path(d) / "memory.db"))
        row = conn.execute(
            "SELECT hook_event_name, tool_name, duration_ms, session_id, cwd "
            "FROM hook_telemetry"
        ).fetchone()
        conn.close()
        assert row == ("PostToolUse", "Bash", 3500, "s1", "/tmp/foo")


def test_hook_script_silent_when_db_missing():
    """Hook must never block tool execution — missing DB returns 0 silently."""
    with tempfile.TemporaryDirectory() as d:
        env = {"NEXUS_CONFIG_DIR": d}  # no DB created
        rc = _run_hook(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash",
             "duration_ms": 5000},
            env,
        )
        assert rc == 0


def test_hook_script_silent_on_invalid_input():
    """Garbage stdin returns 0 without crashing."""
    proc = subprocess.run(
        [sys.executable, str(_hook_script_path())],
        input="not json {{{",
        capture_output=True, text=True, timeout=10,
    )
    assert proc.returncode == 0


def test_hook_script_silent_when_duration_missing():
    """Hook payload without duration_ms field is a silent no-op."""
    with tempfile.TemporaryDirectory() as d:
        T2Database(Path(d) / "memory.db")
        env = {"NEXUS_CONFIG_DIR": d}
        rc = _run_hook(
            {"hook_event_name": "PostToolUse", "tool_name": "Bash"},  # no duration_ms
            env,
        )
        assert rc == 0
        conn = sqlite3.connect(str(Path(d) / "memory.db"))
        rows = conn.execute("SELECT COUNT(*) FROM hook_telemetry").fetchone()
        conn.close()
        assert rows == (0,)
