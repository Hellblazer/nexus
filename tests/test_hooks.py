"""Session hook tests: session_start and session_end lifecycle."""
import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.hooks import session_end, session_end_flush, session_start


def _no_daemon(**_kwargs):
    """Force ``t2_index_write``'s direct-fallback path (RDR-128 P3).

    SessionEnd flush routes its T2 writes through the daemon; tests have
    no daemon, so make the reachability probe fail and let the write land
    on the autouse-isolated tmp ``memory.db``."""
    from nexus.daemon.t2_client import T2DaemonNotReachableError
    raise T2DaemonNotReachableError("no daemon in tests")


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
        patch("nexus.hooks.write_claude_session_id"),
    ):
        output = session_start()

    assert "test-uuid" in output
    assert "Nexus ready" in output


# ── #435 legacy session.lock cleanup ─────────────────────────────────────────





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
        patch("nexus.hooks.write_claude_session_id"),
        patch("nexus.hooks.generate_session_id", return_value="fresh-uuid"),
    ):
        output = session_start()
    assert "fresh-uuid" in output


# ── session_end ──────────────────────────────────────────────────────────────
















def test_session_end_db_error_doesnt_crash(tmp_path: Path) -> None:
    """Storage errors during flush are caught gracefully.

    RDR-128 P3: session_end_flush now routes its writes through
    ``mcp_infra.t2_index_write``; force a storage error out of that path
    and assert the hook still returns its summary rather than crashing."""
    import sqlite3

    sessions = tmp_path / "sessions"
    sessions.mkdir()

    def _boom(_write_fn):
        raise sqlite3.OperationalError("disk I/O error")

    with patch("nexus.mcp_infra.t2_index_write", _boom):
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

        with patch("nexus.daemon.t2_client.make_t2_client", _no_daemon):
            output = session_end_flush()

        assert "Flushed 0" in output
        assert "Expired 0" in output









# ── nx hook session-end-flush CLI subcommand ────────────────────────────────


def test_session_end_flush_cli_subcommand(tmp_path, monkeypatch):
    """The new CLI subcommand routes to session_end_flush, not session_end."""
    from click.testing import CliRunner

    from nexus.commands.hook import hook_group

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.delenv("NX_SESSION_ID", raising=False)

    with patch("nexus.daemon.t2_client.make_t2_client", _no_daemon):
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
