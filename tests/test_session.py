"""AC1: Session ID is a valid UUID4, written to and readable from a PID-scoped file."""
import json
import os
import re
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.session import (
    _stable_pid,
    find_ancestor_session,
    generate_session_id,
    read_session_id,
    sweep_stale_sessions,
    write_session_file,
    write_session_record,
)


def test_generate_session_id_is_uuid4() -> None:
    sid = generate_session_id()
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", sid)


def test_generate_session_id_unique() -> None:
    assert generate_session_id() != generate_session_id()


def test_write_and_read_session_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = generate_session_id()

    path = write_session_file(sid, ppid=99999)
    assert path.exists()
    assert path.read_text() == sid

    recovered = read_session_id(ppid=99999)
    assert recovered == sid


def test_read_session_id_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert read_session_id(ppid=99998) is None


def test_session_file_is_pid_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    write_session_file("session-a", ppid=1001)
    write_session_file("session-b", ppid=1002)

    assert read_session_id(ppid=1001) == "session-a"
    assert read_session_id(ppid=1002) == "session-b"


# ── Behavior 1: _stable_pid() env var path ────────────────────────────────────

def test_stable_pid_env_var_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """When NX_SESSION_PID is set, _stable_pid() returns that value and ignores getsid(0)."""
    monkeypatch.setenv("NX_SESSION_PID", "77777")
    with patch("nexus.session.os.getsid", return_value=99999):
        result = _stable_pid()
    assert result == 77777


# ── Behavior 2: _stable_pid() getsid fallback ────────────────────────────────

def test_stable_pid_falls_back_to_getsid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When NX_SESSION_PID is unset, _stable_pid() returns os.getsid(0)."""
    monkeypatch.delenv("NX_SESSION_PID", raising=False)
    with patch("nexus.session.os.getsid", return_value=55555) as mock_getsid:
        result = _stable_pid()
    assert result == 55555
    mock_getsid.assert_called_once_with(0)


# ── Behavior 3: _stable_pid() invalid env var falls back ─────────────────────

def test_stable_pid_invalid_env_var_falls_back_to_getsid(monkeypatch: pytest.MonkeyPatch) -> None:
    """When NX_SESSION_PID is non-integer, _stable_pid() silently falls back to getsid(0)."""
    monkeypatch.setenv("NX_SESSION_PID", "not-a-number")
    with patch("nexus.session.os.getsid", return_value=44444):
        result = _stable_pid()
    assert result == 44444


# ── write_session_record ──────────────────────────────────────────────────────

def test_write_session_record_creates_json_file(tmp_path: Path) -> None:
    """write_session_record writes a parseable JSON record at sessions/{ppid}.session."""
    sessions = tmp_path / "sessions"
    path = write_session_record(sessions, ppid=1234, session_id="uuid-abc",
                                host="127.0.0.1", port=51000, server_pid=9999, tmpdir="/tmp/x")
    assert path == sessions / "1234.session"
    record = json.loads(path.read_text())
    assert record["session_id"] == "uuid-abc"
    assert record["server_host"] == "127.0.0.1"
    assert record["server_port"] == 51000
    assert record["server_pid"] == 9999
    assert record["tmpdir"] == "/tmp/x"
    assert "created_at" in record


def test_write_session_record_mode_600(tmp_path: Path) -> None:
    """Session record file is created with permissions 0o600."""
    sessions = tmp_path / "sessions"
    path = write_session_record(sessions, ppid=1235, session_id="s",
                                host="127.0.0.1", port=1, server_pid=1)
    assert oct(path.stat().st_mode)[-3:] == "600"


# ── find_ancestor_session ─────────────────────────────────────────────────────

def test_find_ancestor_session_returns_none_when_no_files(tmp_path: Path) -> None:
    """No session files → returns None."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    result = find_ancestor_session(sessions_dir=sessions, start_pid=os.getpid())
    assert result is None


def test_find_ancestor_session_finds_immediate_ancestor(tmp_path: Path) -> None:
    """Finds a valid JSON record written for the current PID."""
    sessions = tmp_path / "sessions"
    pid = os.getpid()
    write_session_record(sessions, ppid=pid, session_id="found-it",
                         host="127.0.0.1", port=51001, server_pid=8888)
    result = find_ancestor_session(sessions_dir=sessions, start_pid=pid)
    assert result is not None
    assert result["session_id"] == "found-it"
    assert result["server_host"] == "127.0.0.1"
    assert result["server_port"] == 51001


def test_find_ancestor_session_ignores_stale_records(tmp_path: Path) -> None:
    """Records older than 24h are ignored (and the orphan is cleaned up)."""
    sessions = tmp_path / "sessions"
    pid = os.getpid()
    path = write_session_record(sessions, ppid=pid, session_id="stale",
                                host="127.0.0.1", port=51002, server_pid=99)
    # Backdate the created_at field
    record = json.loads(path.read_text())
    record["created_at"] = time.time() - (25 * 3600)
    path.write_text(json.dumps(record))

    result = find_ancestor_session(sessions_dir=sessions, start_pid=pid)
    assert result is None
    # Stale file should have been cleaned up
    assert not path.exists()


def test_find_ancestor_session_ignores_bare_string_files(tmp_path: Path) -> None:
    """Legacy bare-UUID session files (non-JSON) are skipped gracefully."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    pid = os.getpid()
    (sessions / f"{pid}.session").write_text("bare-uuid-not-json")
    result = find_ancestor_session(sessions_dir=sessions, start_pid=pid)
    assert result is None


# ── sweep_stale_sessions ──────────────────────────────────────────────────────

def test_sweep_stale_sessions_removes_old_records(tmp_path: Path) -> None:
    """Records older than max_age_hours are removed."""
    sessions = tmp_path / "sessions"
    path = write_session_record(sessions, ppid=5555, session_id="old",
                                host="127.0.0.1", port=51003, server_pid=101)
    record = json.loads(path.read_text())
    record["created_at"] = time.time() - (25 * 3600)
    path.write_text(json.dumps(record))

    sweep_stale_sessions(sessions_dir=sessions)
    assert not path.exists()


def test_sweep_stale_sessions_keeps_fresh_records(tmp_path: Path) -> None:
    """Records younger than max_age_hours are not removed."""
    sessions = tmp_path / "sessions"
    path = write_session_record(sessions, ppid=5556, session_id="fresh",
                                host="127.0.0.1", port=51004, server_pid=102)

    sweep_stale_sessions(sessions_dir=sessions)
    assert path.exists()


def test_sweep_stale_sessions_noop_on_missing_dir(tmp_path: Path) -> None:
    """sweep_stale_sessions does not raise when sessions_dir does not exist."""
    sweep_stale_sessions(sessions_dir=tmp_path / "nonexistent")  # must not raise


def test_sweep_stale_sessions_skips_non_json_files(tmp_path: Path) -> None:
    """Non-JSON files (e.g. legacy bare-UUID) are ignored silently."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "9999.session").write_text("bare-uuid")
    sweep_stale_sessions(sessions_dir=sessions)  # must not raise
    assert (sessions / "9999.session").exists()  # untouched
