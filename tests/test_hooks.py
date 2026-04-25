"""Session hook tests: session_start and session_end lifecycle."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.hooks import session_end, session_end_flush, session_start


# ── session_start ────────────────────────────────────────────────────────────
#
# RDR-094 Phase F (4.13.0 / nexus-2lm0) deleted the hook-side chroma
# spawn block: ``start_t1_server``, ``write_session_record_by_id``,
# ``find_ancestor_session``, the session.lock acquisition, and the
# watchdog spawn all moved to nx-mcp's FastMCP lifespan. The hook now
# does sweep + UUID resolution + (optionally) current_session write.
# These tests pin that minimal contract.


@patch("nexus.hooks.generate_session_id", return_value="test-uuid")
def test_session_start_returns_session_id(_mock_sid, tmp_path: Path) -> None:
    """session_start returns the session ID line."""
    with (
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.write_claude_session_id"),
    ):
        output = session_start()

    assert "test-uuid" in output
    assert "Nexus ready" in output


def test_session_start_does_not_overwrite_current_session_when_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
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
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.write_claude_session_id", mock_write),
    ):
        output = session_start(claude_session_id="my-own-transient-uuid")

    assert "parent-uuid-keep-me" in output
    mock_write.assert_not_called()


def test_session_start_writes_current_session_when_top_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Top-level Claude session (no NX_SESSION_ID inherited) writes
    ``current_session``, populating the cross-tree pointer that shell
    tools and subagents rely on."""
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    mock_write = MagicMock()

    with (
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.write_claude_session_id", mock_write),
    ):
        session_start(claude_session_id="top-level-uuid")

    mock_write.assert_called_once_with("top-level-uuid")


def test_session_start_uses_inherited_session_id(tmp_path: Path, monkeypatch) -> None:
    """When ``NX_SESSION_ID`` is set in env, the hook uses it verbatim
    rather than generating a new UUID or honouring the stdin payload.
    Subagents inherit the parent's ID this way."""
    monkeypatch.setenv("NX_SESSION_ID", "inherited-uuid")
    with (
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.write_claude_session_id"),
    ):
        output = session_start(claude_session_id="ignored-stdin-uuid")
    assert "inherited-uuid" in output
    assert "ignored-stdin-uuid" not in output


def test_session_start_falls_back_to_generated_uuid(tmp_path, monkeypatch) -> None:
    """No NX_SESSION_ID env and no stdin payload: generate a fresh UUID
    so invocations outside Claude Code (e.g. ``nx hook session-start``
    from a script) still produce a usable session pointer."""
    monkeypatch.delenv("NX_SESSION_ID", raising=False)
    with (
        patch("nexus.hooks.sweep_stale_sessions"),
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.generate_session_id", return_value="fresh-uuid"),
    ):
        output = session_start()
    assert "fresh-uuid" in output


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


def test_session_end_owner_path_does_not_stop_chroma(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RDR-094 Phase F (unconditional as of 4.13.0): session_end is a
    thin wrapper around session_end_flush. nx-mcp owns chroma
    teardown via its lifespan/atexit/signal-handler chain; the hook
    must NEVER call stop_t1_server even when this process owns the
    session record."""
    sessions = tmp_path / "sessions"
    _make_session_record(sessions, session_id="end-test-uuid", server_pid=9900)
    monkeypatch.setenv("NX_SESSION_ID", "end-test-uuid")

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []
    db_path = tmp_path / "memory.db"

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks._default_db_path", return_value=db_path),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
    ):
        output = session_end()

    assert "Flushed 0" in output
    # Session file must survive: the MCP server's lifespan (or the
    # watchdog as belt-and-braces) is responsible for unlinking it.
    assert (sessions / "end-test-uuid.session").exists()


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
        ):
            output = session_end_flush()

        assert "Flushed 1" in output
        mock_t1.clear.assert_called_once()
        # The flush function MUST NOT delete the session record. nx-mcp
        # owns chroma teardown (lifespan + signal + atexit); hook just
        # flushes.
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
        ):
            session_end_flush()

        # Session file must survive a flush-only run.
        assert (sessions / "owner-uuid.session").exists()


def test_session_end_never_stops_chroma_phase_f(
    tmp_path, monkeypatch,
):
    """RDR-094 Phase F (unconditional as of 4.13.0): session_end is
    a thin wrapper around session_end_flush. The hook must NOT call
    stop_t1_server -- nx-mcp owns chroma teardown via lifespan +
    signal handler + atexit chain, with the watchdog as belt-and-
    braces. Regression sentinel for the gate removal."""
    sessions = tmp_path / "sessions"
    _make_session_record(sessions, session_id="phase-f-uuid", server_pid=42)
    monkeypatch.setenv("NX_SESSION_ID", "phase-f-uuid")

    mock_t1 = MagicMock()
    mock_t1.flagged_entries.return_value = []

    with (
        patch("nexus.hooks.SESSIONS_DIR", sessions),
        patch("nexus.hooks._default_db_path", return_value=tmp_path / "memory.db"),
        patch("nexus.hooks._open_t1", return_value=mock_t1),
    ):
        output = session_end()

    assert "Session ended" in output
    # Session file must survive: lifespan / watchdog owns the unlink.
    assert (sessions / "phase-f-uuid.session").exists()


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
    ):
        runner = CliRunner()
        result = runner.invoke(hook_group, ["session-end-flush"])

    assert result.exit_code == 0
    assert "Flushed 0" in result.output
    assert "Expired 0" in result.output


# ── session lock stale cleanup ───────────────────────────────────────────────


# Phase F (RDR-094 / nexus-2lm0) deleted the hook-side chroma-spawn
# block, including the session.lock acquisition. The lock guarded
# concurrent siblings from each calling start_t1_server. Now nx-mcp's
# lifespan owns spawn, the hook does no T1 work, and there is no
# lock to test. The pre-Phase-F tests
# (test_session_start_writes_pid_to_lock,
# test_session_start_clears_stale_lock) were removed with the code
# they covered.


# ── _infer_repo ──────────────────────────────────────────────────────────────

def test_infer_repo_git_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When not in a git repo, falls back to cwd name."""
    from nexus.hooks import _infer_repo

    monkeypatch.chdir(tmp_path)
    name = _infer_repo()
    assert name == tmp_path.name
