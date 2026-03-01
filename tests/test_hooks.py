"""Session hook tests: session_start and session_end lifecycle."""
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.hooks import session_end, session_start


# ── session_start ────────────────────────────────────────────────────────────

def _patch_session_start(tmp_path: Path, *, ancestor=None, server_raises=False):
    """Return a context manager bundle that patches session_start dependencies."""
    db_path = tmp_path / "memory.db"
    server_result = ("127.0.0.1", 51823, 9900, str(tmp_path / "t1_tmp"))

    patches = [
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=ancestor),
        patch("nexus.hooks.write_claude_session_id"),
    ]
    if ancestor is None:
        if server_raises:
            patches.append(patch("nexus.hooks.start_t1_server",
                                 side_effect=RuntimeError("chroma not found")))
        else:
            patches.append(patch("nexus.hooks.start_t1_server", return_value=server_result))
        patches.append(patch("nexus.hooks.write_session_record"))
    return patches


@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_no_pm_no_entries(mock_repo, mock_sid, tmp_path: Path) -> None:
    """Non-PM repo with no memory entries outputs fallback message."""
    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", return_value=("127.0.0.1", 51823, 9900, "/tmp/x")),
        patch("nexus.hooks.write_session_record"),
    ):
        output = session_start()

    assert "test-uuid" in output
    assert "No memory entries" in output


@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_with_memory_entries(mock_repo, mock_sid, tmp_path: Path) -> None:
    """Non-PM repo with memory entries lists them."""
    from nexus.db.t2 import T2Database

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.put(project="myrepo", title="findings.md", content="some content")

    with (
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", return_value=("127.0.0.1", 51823, 9900, "/tmp/x")),
        patch("nexus.hooks.write_session_record"),
    ):
        output = session_start()

    assert "findings.md" in output
    assert "Recent memory" in output


@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_db_unavailable(mock_repo, mock_sid) -> None:
    """When T2 database raises, outputs graceful fallback."""
    import sqlite3

    with (
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", return_value=("127.0.0.1", 51823, 9900, "/tmp/x")),
        patch("nexus.hooks.write_session_record"),
        patch("nexus.hooks._open_t2", side_effect=sqlite3.Error("disk I/O error")),
    ):
        output = session_start()

    assert "memory unavailable" in output


@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
@patch("nexus.hooks._infer_repo", return_value="myrepo")
def test_session_start_pm_project(mock_repo, mock_sid, tmp_path: Path) -> None:
    """PM project triggers pm_resume instead of memory listing."""
    from nexus.db.t2 import T2Database

    db_path = tmp_path / "memory.db"
    with T2Database(db_path) as db:
        db.put(project="myrepo", title="BLOCKERS.md", content="no blockers", tags="pm")

    with (
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", return_value=("127.0.0.1", 51823, 9900, "/tmp/x")),
        patch("nexus.hooks.write_session_record"),
    ):
        output = session_start()

    assert "test-uuid" in output


def test_session_start_adopts_ancestor_session(tmp_path: Path) -> None:
    """Child agent adopts ancestor session ID — no new server started."""
    ancestor = {
        "session_id": "parent-session-uuid",
        "server_host": "127.0.0.1",
        "server_port": 51823,
        "server_pid": 9900,
    }
    mock_start = MagicMock()

    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=ancestor),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", mock_start),
        patch("nexus.hooks._infer_repo", return_value="myrepo"),
    ):
        output = session_start()

    assert "parent-session-uuid" in output
    mock_start.assert_not_called()  # should NOT start a new server


def test_session_start_server_failure_is_graceful(tmp_path: Path) -> None:
    """If start_t1_server raises, session_start completes without crashing."""
    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", side_effect=RuntimeError("chroma not found")),
        patch("nexus.hooks.generate_session_id", return_value="fallback-uuid"),
        patch("nexus.hooks._infer_repo", return_value="myrepo"),
    ):
        output = session_start()  # must not raise

    assert "fallback-uuid" in output


# ── session_end ──────────────────────────────────────────────────────────────

def _make_session_record(sessions_dir: Path, ppid: int, server_pid: int = 9900) -> dict:
    """Write a valid JSON session record and return it."""
    from nexus.session import write_session_record
    tmpdir = str(sessions_dir / "t1_tmp")
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    write_session_record(
        sessions_dir, ppid=ppid,
        session_id="test-session-uuid",
        host="127.0.0.1", port=51823,
        server_pid=server_pid,
        tmpdir=tmpdir,
    )
    return json.loads((sessions_dir / f"{ppid}.session").read_text())


def test_session_end_no_session_record(tmp_path: Path) -> None:
    """When no session record exists, session_end completes gracefully."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    db_path = tmp_path / "memory.db"

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks._default_db_path", return_value=db_path),
    ):
        output = session_end()

    assert "Flushed 0" in output
    assert "Expired 0" in output


def test_session_end_with_session_record(tmp_path: Path) -> None:
    """When this process owns the session record, T1 is flushed and server stopped."""
    sessions = tmp_path / "sessions"
    ppid = os.getppid()
    _make_session_record(sessions, ppid=ppid, server_pid=9900)

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []
    mock_stop = MagicMock()
    db_path = tmp_path / "memory.db"

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
        patch("nexus.hooks.stop_t1_server", mock_stop),
    ):
        output = session_end()

    assert "Flushed 0" in output
    mock_stop.assert_called_once_with(9900)
    # Session file should be cleaned up
    assert not (sessions / f"{ppid}.session").exists()


def test_session_end_flushes_flagged_entries(tmp_path: Path) -> None:
    """Flagged T1 entries are flushed to T2."""
    from nexus.db.t2 import T2Database

    sessions = tmp_path / "sessions"
    ppid = os.getppid()
    _make_session_record(sessions, ppid=ppid)

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = [
        {"content": "hypothesis A", "flush_project": "proj", "flush_title": "hyp.md", "tags": ""},
    ]
    db_path = tmp_path / "memory.db"

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
        patch("nexus.hooks.stop_t1_server"),
    ):
        output = session_end()

    assert "Flushed 1" in output
    mock_t1.clear.assert_called_once()

    with T2Database(db_path) as db:
        entry = db.get(project="proj", title="hyp.md")
    assert entry is not None
    assert entry["content"] == "hypothesis A"


def test_session_end_child_does_not_stop_server(tmp_path: Path) -> None:
    """Child agent's session_end does NOT stop the server (no own session record)."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    db_path = tmp_path / "memory.db"

    ancestor = {
        "session_id": "parent-session-uuid",
        "server_host": "127.0.0.1",
        "server_port": 51823,
        "server_pid": 9900,
    }
    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []
    mock_stop = MagicMock()

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        # No own session file: getppid() file does not exist in sessions/
        patch("nexus.hooks.find_ancestor_session", return_value=ancestor),
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
        patch("nexus.hooks.stop_t1_server", mock_stop),
    ):
        output = session_end()

    assert "Session ended" in output
    mock_stop.assert_not_called()  # child must not stop parent's server


def test_session_end_db_error_doesnt_crash(tmp_path: Path) -> None:
    """Storage errors during flush are caught gracefully."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "nonexistent_dir" / "memory.db"),
    ):
        output = session_end()

    assert "Session ended" in output


# ── _infer_repo ──────────────────────────────────────────────────────────────

def test_infer_repo_git_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When not in a git repo, falls back to cwd name."""
    from nexus.hooks import _infer_repo

    monkeypatch.chdir(tmp_path)
    name = _infer_repo()
    assert name == tmp_path.name
