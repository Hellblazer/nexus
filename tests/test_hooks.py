"""Session hook tests: session_start and session_end lifecycle."""
import json
import os
import time
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
def test_session_start_returns_session_id(mock_sid, tmp_path: Path) -> None:
    """session_start returns the session ID line (T2 memory is surfaced by separate hook)."""
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
    assert "Nexus ready" in output


def test_session_start_adopts_existing_session_for_same_uuid(tmp_path: Path) -> None:
    """Subagent with the same Claude session UUID adopts the existing T1 server.

    Pre-fix this was achieved via a PPID walk (broken — it adopted the
    login shell's session). Post-fix it falls out of UUID-keyed lookup:
    the subagent's hook is invoked with the parent's session_id (read
    from current_session flat file), find_session_by_id returns the
    existing record, no new server is started.
    """
    existing = {
        "session_id": "parent-session-uuid",
        "server_host": "127.0.0.1",
        "server_port": 51823,
        "server_pid": 9900,
        "tmpdir": "",
        "created_at": 0,
    }
    mock_start = MagicMock()

    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_session_by_id", return_value=existing),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", mock_start),
        patch("nexus.hooks._infer_repo", return_value="myrepo"),
    ):
        output = session_start(claude_session_id="parent-session-uuid")

    assert "parent-session-uuid" in output
    mock_start.assert_not_called()  # should NOT start a new server


def test_session_start_skips_t1_when_env_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NEXUS_SKIP_T1 honoured: claude_dispatch (and similar one-shot
    `claude -p` callers) sets this so the subprocess does not pay the
    chroma startup cost. The T1 client falls back to EphemeralClient
    when no server record is found, which is the intended behaviour for
    stateless operator subprocesses.
    """
    monkeypatch.setenv("NEXUS_SKIP_T1", "1")
    mock_start = MagicMock()
    mock_write_record = MagicMock()

    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_session_by_id", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", mock_start),
        patch("nexus.hooks.write_session_record_by_id", mock_write_record),
        patch("nexus.hooks._infer_repo", return_value="myrepo"),
        patch("nexus.hooks.generate_session_id", return_value="skip-t1-uuid"),
    ):
        output = session_start()

    assert "skip-t1-uuid" in output
    mock_start.assert_not_called()  # server NOT started
    mock_write_record.assert_not_called()  # no session record written


def test_session_start_server_failure_is_graceful(tmp_path: Path) -> None:
    """If start_t1_server raises, session_start completes without crashing."""
    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_session_by_id", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", side_effect=RuntimeError("chroma not found")),
        patch("nexus.hooks.generate_session_id", return_value="fallback-uuid"),
        patch("nexus.hooks._infer_repo", return_value="myrepo"),
    ):
        output = session_start()  # must not raise

    assert "fallback-uuid" in output


# ── session_end ──────────────────────────────────────────────────────────────

def _make_session_record(
    sessions_dir: Path,
    session_id: str = "test-session-uuid",
    server_pid: int = 9900,
) -> dict:
    """Write a valid UUID-keyed JSON session record and return it."""
    from nexus.session import write_session_record_by_id
    tmpdir = str(sessions_dir / "t1_tmp")
    Path(tmpdir).mkdir(parents=True, exist_ok=True)
    write_session_record_by_id(
        sessions_dir, session_id=session_id,
        host="127.0.0.1", port=51823,
        server_pid=server_pid,
        tmpdir=tmpdir,
    )
    return json.loads((sessions_dir / f"{session_id}.session").read_text())


def test_session_end_no_session_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no session record exists, session_end completes gracefully."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    db_path = tmp_path / "memory.db"
    monkeypatch.delenv("NX_SESSION_ID", raising=False)

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.find_session_by_id", return_value=None),
        patch("nexus.hooks._default_db_path", return_value=db_path),
    ):
        output = session_end()

    assert "Flushed 0" in output
    assert "Expired 0" in output


def test_session_end_with_session_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When this process owns the session record, T1 is flushed and server stopped."""
    sessions = tmp_path / "sessions"
    _make_session_record(sessions, session_id="end-test-uuid", server_pid=9900)
    monkeypatch.setenv("NX_SESSION_ID", "end-test-uuid")

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []
    mock_stop = MagicMock()
    db_path = tmp_path / "memory.db"

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
        patch("nexus.hooks.stop_t1_server", mock_stop),
    ):
        output = session_end()

    assert "Flushed 0" in output
    mock_stop.assert_called_once_with(9900)
    # Session file should be cleaned up
    assert not (sessions / "end-test-uuid.session").exists()


def test_session_end_flushes_flagged_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Flagged T1 entries are flushed to T2."""
    from nexus.db.t2 import T2Database

    sessions = tmp_path / "sessions"
    _make_session_record(sessions, session_id="flush-test-uuid")
    monkeypatch.setenv("NX_SESSION_ID", "flush-test-uuid")

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = [
        {"content": "hypothesis A", "flush_project": "proj", "flush_title": "hyp.md", "tags": ""},
    ]
    db_path = tmp_path / "memory.db"

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
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


def test_session_end_child_does_not_stop_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Subagent's session_end uses the parent's record for flush but must NOT
    stop the parent's server. Distinguishing owner from non-owner: the on-disk
    session file is keyed by the parent's session_id; the subagent's process
    inherits the same session_id (via NX_SESSION_ID or current_session) but
    didn't write the file, so it should not delete the file or stop the
    server. Owner-vs-child detection here piggybacks on whether the session
    file exists at the time session_end runs — child agents typically run
    after the parent has already cleaned up.
    """
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    db_path = tmp_path / "memory.db"
    # Subagent inherits the parent's session_id but the session file does
    # NOT exist on disk (parent already cleaned up, or the subagent runs
    # before the parent writes — either way, no own_record).
    monkeypatch.setenv("NX_SESSION_ID", "parent-session-uuid")

    parent_record = {
        "session_id": "parent-session-uuid",
        "server_host": "127.0.0.1",
        "server_port": 51823,
        "server_pid": 9900,
        "tmpdir": "",
        "created_at": 0,
    }
    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []
    mock_stop = MagicMock()

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.find_session_by_id", return_value=parent_record),
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


# ── session lock stale cleanup ───────────────────────────────────────────────


def test_session_start_writes_pid_to_lock(tmp_path: Path) -> None:
    """session_start writes its PID into session.lock for stale detection."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", return_value=("127.0.0.1", 51823, 9900, "/tmp/x")),
        patch("nexus.hooks.write_session_record"),
        patch("nexus.hooks.generate_session_id", return_value="lock-test-uuid"),
    ):
        session_start()

    lock_file = sessions / "session.lock"
    assert lock_file.exists()
    pid_text = lock_file.read_text().strip()
    assert pid_text == str(os.getpid())


def test_session_start_clears_stale_lock(tmp_path: Path) -> None:
    """session_start removes a stale session.lock before acquiring."""
    from nexus.indexer import _LOCK_STALE_SECONDS

    sessions = tmp_path / "sessions"
    sessions.mkdir()

    # Create stale empty lock file
    lock_file = sessions / "session.lock"
    lock_file.touch()
    old_time = time.time() - _LOCK_STALE_SECONDS - 1
    os.utime(lock_file, (old_time, old_time))

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_ancestor_session", return_value=None),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.start_t1_server", return_value=("127.0.0.1", 51823, 9900, "/tmp/x")),
        patch("nexus.hooks.write_session_record"),
        patch("nexus.hooks.generate_session_id", return_value="lock-test-uuid"),
    ):
        session_start()  # must not deadlock on stale lock

    # Lock file should now contain current PID, not be stale
    assert lock_file.exists()
    assert lock_file.read_text().strip() == str(os.getpid())


# ── _infer_repo ──────────────────────────────────────────────────────────────

def test_infer_repo_git_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When not in a git repo, falls back to cwd name."""
    from nexus.hooks import _infer_repo

    monkeypatch.chdir(tmp_path)
    name = _infer_repo()
    assert name == tmp_path.name
