"""Session hook tests: session_start and session_end lifecycle."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.hooks import session_end, session_end_flush, session_start


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


def test_session_start_does_not_overwrite_current_session_when_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested subprocess (NX_SESSION_ID set in env) must NOT stomp the
    parent's ``current_session`` flat file. Without this guard, every
    operator ``claude -p`` call would overwrite the parent's UUID with
    its own transient one, and the parent's shell-side ``nx scratch``
    would fall back to EphemeralClient for the rest of the conversation.
    """
    monkeypatch.setenv("NX_SESSION_ID", "parent-uuid-keep-me")
    mock_write = MagicMock()

    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_session_by_id", return_value=None),
        patch("nexus.hooks.start_t1_server", side_effect=RuntimeError("no T1")),
        patch("nexus.hooks.write_claude_session_id", mock_write),
        patch("nexus.hooks._infer_repo", return_value="myrepo"),
    ):
        output = session_start(claude_session_id="my-own-transient-uuid")

    # The hook should have used the inherited UUID (not the stdin one)
    assert "parent-uuid-keep-me" in output
    # And must NOT have written current_session — that would stomp the parent
    mock_write.assert_not_called()


def test_session_start_writes_current_session_when_top_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Top-level Claude session (no NX_SESSION_ID inherited) writes
    ``current_session`` as today, populating the cross-tree pointer
    that shell tools and subagents rely on.
    """
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    mock_write = MagicMock()

    with (
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.find_session_by_id", return_value=None),
        patch("nexus.hooks.start_t1_server", side_effect=RuntimeError("no T1")),
        patch("nexus.hooks.write_claude_session_id", mock_write),
        patch("nexus.hooks._infer_repo", return_value="myrepo"),
    ):
        session_start(claude_session_id="top-level-uuid")

    mock_write.assert_called_once_with("top-level-uuid")


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
    """When this process owns the session record AND the MCP-owned-T1
    opt-out is set, T1 is flushed and server stopped via the legacy
    hook path. Default-on (4.12.0) leaves chroma teardown to nx-mcp;
    this test pins the opt-out fallback path."""
    sessions = tmp_path / "sessions"
    _make_session_record(sessions, session_id="end-test-uuid", server_pid=9900)
    monkeypatch.setenv("NX_SESSION_ID", "end-test-uuid")
    # Explicit opt-out of MCP-ownership so session_end's chroma-stop
    # block runs (the flag is default-on as of 4.12.0).
    monkeypatch.setenv("NEXUS_MCP_OWNS_T1", "0")

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


# ── session_end_flush (RDR-094 Phase B / nexus-2b9r) ────────────────────────


class TestSessionEndFlush:
    """The split-out flush function does T1 flush + T2 expire and never
    touches chroma. Phase 4's hooks.json swap (Phase C / nexus-l828)
    points at this function so the SessionEnd path cannot race the
    MCP-owned chroma teardown."""

    def test_flush_returns_summary_when_no_record(self, tmp_path, monkeypatch):
        sessions = tmp_path / "sessions"
        sessions.mkdir()
        monkeypatch.delenv("NX_SESSION_ID", raising=False)

        with (
            patch("nexus.hooks.SESSIONS_DIR", sessions),
            patch("nexus.hooks.find_session_by_id", return_value=None),
            patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        ):
            output = session_end_flush()

        assert "Flushed 0" in output
        assert "Expired 0" in output

    def test_flush_drains_flagged_entries_to_t2(self, tmp_path, monkeypatch):
        from nexus.db.t2 import T2Database

        sessions = tmp_path / "sessions"
        _make_session_record(sessions, session_id="flush-only-uuid")
        monkeypatch.setenv("NX_SESSION_ID", "flush-only-uuid")

        mock_t1 = MagicMock()
        mock_t1.flagged_entries.return_value = [
            {
                "content": "evidence A",
                "flush_project": "p",
                "flush_title": "a.md",
                "tags": "",
            },
        ]
        db_path = tmp_path / "memory.db"

        with (
            patch("nexus.hooks.SESSIONS_DIR", sessions),
            patch("nexus.hooks._default_db_path", return_value=db_path),
            patch("nexus.hooks._open_t1", return_value=mock_t1),
            patch("nexus.hooks.stop_t1_server") as mock_stop,
        ):
            output = session_end_flush()

        assert "Flushed 1" in output
        mock_t1.clear.assert_called_once()
        # The flush function MUST NOT touch chroma -- this is the
        # whole point of the split.
        mock_stop.assert_not_called()
        # ...and must NOT delete the session record.
        assert (sessions / "flush-only-uuid.session").exists()

        with T2Database(db_path) as db:
            entry = db.get(project="p", title="a.md")
        assert entry is not None
        assert entry["content"] == "evidence A"

    def test_flush_does_not_touch_chroma_even_when_record_owned(
        self, tmp_path, monkeypatch,
    ):
        """Owner-detection lives in session_end, not in session_end_flush."""
        sessions = tmp_path / "sessions"
        _make_session_record(
            sessions, session_id="owner-uuid", server_pid=12345,
        )
        monkeypatch.setenv("NX_SESSION_ID", "owner-uuid")

        mock_t1 = MagicMock()
        mock_t1.flagged_entries.return_value = []

        with (
            patch("nexus.hooks.SESSIONS_DIR", sessions),
            patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
            patch("nexus.hooks._open_t1", return_value=mock_t1),
            patch("nexus.hooks.stop_t1_server") as mock_stop,
        ):
            session_end_flush()

        mock_stop.assert_not_called()
        # Session file must survive a flush-only run.
        assert (sessions / "owner-uuid.session").exists()


def test_session_end_default_on_skips_chroma_without_env_var(
    tmp_path, monkeypatch,
):
    """RDR-094 Phase 4 default-on (4.12.0): absence of
    NEXUS_MCP_OWNS_T1 means MCP owns chroma. session_end must NOT
    call stop_t1_server even when this process owns the record."""
    sessions = tmp_path / "sessions"
    _make_session_record(sessions, session_id="default-on-uuid", server_pid=42)
    monkeypatch.setenv("NX_SESSION_ID", "default-on-uuid")
    monkeypatch.delenv("NEXUS_MCP_OWNS_T1", raising=False)

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
        patch("nexus.hooks.stop_t1_server") as mock_stop,
    ):
        output = session_end()

    assert "Session ended" in output
    mock_stop.assert_not_called(), "default-on: hook must not stop chroma"
    # Session file must survive when MCP owns T1.
    assert (sessions / "default-on-uuid.session").exists()


def test_session_end_skips_chroma_when_mcp_owns_t1(
    tmp_path, monkeypatch,
):
    """Explicit NEXUS_MCP_OWNS_T1=1 still gates correctly under
    default-on (back-compat sentinel for callers that set the flag
    explicitly even though it's the new default).
    """
    sessions = tmp_path / "sessions"
    _make_session_record(sessions, session_id="mcp-owned-uuid", server_pid=42)
    monkeypatch.setenv("NX_SESSION_ID", "mcp-owned-uuid")
    monkeypatch.setenv("NEXUS_MCP_OWNS_T1", "1")

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
        patch("nexus.hooks.stop_t1_server") as mock_stop,
    ):
        output = session_end()

    assert "Session ended" in output
    mock_stop.assert_not_called()
    # Session file must survive when MCP owns T1.
    assert (sessions / "mcp-owned-uuid.session").exists()


# ── nx hook session-end-flush CLI subcommand ────────────────────────────────


def test_session_end_flush_cli_subcommand(tmp_path, monkeypatch):
    """The new CLI subcommand routes to session_end_flush, not session_end."""
    from click.testing import CliRunner

    from nexus.commands.hook import hook_group

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.delenv("NX_SESSION_ID", raising=False)

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks.find_session_by_id", return_value=None),
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks.stop_t1_server") as mock_stop,
    ):
        runner = CliRunner()
        result = runner.invoke(hook_group, ["session-end-flush"])

    assert result.exit_code == 0
    assert "Flushed 0" in result.output
    assert "Expired 0" in result.output
    # Critical: the flush subcommand never calls stop_t1_server.
    mock_stop.assert_not_called()


# ── session lock stale cleanup ───────────────────────────────────────────────


def test_session_start_writes_pid_to_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session_start writes its PID into session.lock for stale detection.

    Default-on (4.12.0) routes the hook through skip_t1, which bypasses
    the lock acquisition because nx-mcp owns chroma. Opt out explicitly
    so this test exercises the legacy lock-creation path that's still
    relevant when ``NEXUS_MCP_OWNS_T1=0``.
    """
    monkeypatch.setenv("NEXUS_MCP_OWNS_T1", "0")
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


def test_session_start_clears_stale_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """session_start removes a stale session.lock before acquiring.

    Same opt-out as ``test_session_start_writes_pid_to_lock``: default-on
    bypasses the lock entirely.
    """
    monkeypatch.setenv("NEXUS_MCP_OWNS_T1", "0")
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
