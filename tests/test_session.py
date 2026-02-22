"""AC1: Session ID is a valid UUID4, written to and readable from a PID-scoped file."""
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.session import _stable_pid, generate_session_id, read_session_id, write_session_file


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
