# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the MCP-owned T1 chroma lifecycle (RDR-094 Phase 1+2).

Covers the feature-flagged path under ``NEXUS_MCP_OWNS_T1=1``:

  * ``_t1_chroma_init_if_owner`` spawn / reuse / nested-skip branches.
  * ``_t1_chroma_shutdown`` idempotency + skip-on-reuse / skip-on-nested.
  * ``_tcp_probe_alive`` happy path + connection-refused path.
  * The lifespan async context manager wires init + shutdown.

All tests mock the subprocess + filesystem boundaries so the suite runs
fast and deterministically. Live-I/O coverage lands in the RDR-094
spike harness.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@contextmanager
def _clean_owned_chroma():
    """Reset _OWNED_CHROMA before and after each test so module-scope
    state from one test does not leak into the next."""
    from nexus.mcp import core as core_mod

    saved = dict(core_mod._OWNED_CHROMA)
    core_mod._OWNED_CHROMA.clear()
    try:
        yield
    finally:
        core_mod._OWNED_CHROMA.clear()
        core_mod._OWNED_CHROMA.update(saved)


# ── _tcp_probe_alive ────────────────────────────────────────────────────────


class TestTcpProbeAlive:

    def test_returns_true_when_connect_succeeds(self):
        from nexus.mcp.core import _tcp_probe_alive

        # Bind a real ephemeral socket so the probe has something to
        # connect to. Port 0 lets the OS pick a free port.
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        host, port = sock.getsockname()
        try:
            assert _tcp_probe_alive(host, port, timeout=1.0) is True
        finally:
            sock.close()

    def test_returns_false_on_connection_refused(self):
        from nexus.mcp.core import _tcp_probe_alive

        # 127.0.0.1:1 is reserved as the well-known unused port; nothing
        # should ever listen there. ``timeout`` keeps the test fast.
        assert _tcp_probe_alive("127.0.0.1", 1, timeout=0.2) is False


# ── _t1_chroma_init_if_owner ────────────────────────────────────────────────


class TestInitIfOwner:

    def test_idempotent_when_already_owned(self, monkeypatch):
        """Second call is a no-op so lifespan + atexit both calling
        the init path is safe."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            core_mod._OWNED_CHROMA["session_id"] = "X"
            with patch("nexus.session.start_t1_server") as mock_start:
                core_mod._t1_chroma_init_if_owner()
            mock_start.assert_not_called()

    def test_nested_skip_when_ancestor_session_reachable(self, monkeypatch):
        """When NX_SESSION_ID is set AND an ancestor record's chroma is
        TCP-reachable, the nested MCP server skips spawn entirely."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.setenv("NX_SESSION_ID", "abc-123")
            ancestor_record = {
                "server_host": "127.0.0.1", "server_port": 12345,
            }
            with patch(
                "nexus.session.find_session_by_id", return_value=ancestor_record,
            ), patch.object(
                core_mod, "_tcp_probe_alive", return_value=True,
            ), patch("nexus.session.start_t1_server") as mock_start:
                core_mod._t1_chroma_init_if_owner()

            assert core_mod._OWNED_CHROMA.get("nested") is True
            mock_start.assert_not_called()

    def test_reuse_path_when_existing_record_reachable(self, monkeypatch):
        """FM-NEW-2: existing record for own session_id is reachable,
        so reuse instead of spawning. _OWNED_CHROMA is marked reused
        so the shutdown path skips cleanup."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.delenv("NX_SESSION_ID", raising=False)
            existing = {"server_host": "127.0.0.1", "server_port": 22222}
            with patch.object(
                core_mod, "_resolve_top_level_session_id", return_value="own-id",
            ), patch(
                "nexus.session.find_session_by_id", return_value=existing,
            ), patch.object(
                core_mod, "_tcp_probe_alive", return_value=True,
            ), patch("nexus.session.start_t1_server") as mock_start:
                core_mod._t1_chroma_init_if_owner()

            assert core_mod._OWNED_CHROMA.get("reused") is True
            assert core_mod._OWNED_CHROMA.get("session_id") == "own-id"
            mock_start.assert_not_called()

    def test_spawn_path_writes_record_with_dual_watch_watchdog(
        self, monkeypatch, tmp_path,
    ):
        """Fresh session: spawn chroma, write record, spawn watchdog
        with mcp_pid passed (RDR-094 FM-NEW-1 dual-watch)."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.delenv("NX_SESSION_ID", raising=False)
            spawn_calls: dict = {}

            def _fake_spawn_watchdog(**kwargs):
                spawn_calls.update(kwargs)
                return 7777

            with patch.object(
                core_mod, "_resolve_top_level_session_id",
                return_value="fresh-id",
            ), patch(
                "nexus.session.find_session_by_id", return_value=None,
            ), patch(
                "nexus.session.start_t1_server",
                return_value=("127.0.0.1", 33333, 4444, str(tmp_path / "td")),
            ), patch(
                "nexus.session.find_claude_root_pid", return_value=8888,
            ), patch(
                "nexus.session.spawn_t1_watchdog",
                side_effect=_fake_spawn_watchdog,
            ), patch(
                "nexus.session.write_session_record_by_id",
            ) as mock_write:
                core_mod._t1_chroma_init_if_owner()

            assert core_mod._OWNED_CHROMA["session_id"] == "fresh-id"
            assert core_mod._OWNED_CHROMA["server_pid"] == 4444
            # FM-NEW-1: watchdog gets BOTH claude_pid and mcp_pid.
            assert spawn_calls.get("claude_pid") == 8888
            assert spawn_calls.get("chroma_pid") == 4444
            assert spawn_calls.get("mcp_pid") > 0  # this process's pid
            mock_write.assert_called_once()

    def test_spawn_failure_logs_warning_and_returns(self, monkeypatch):
        """If start_t1_server raises, the init path logs and returns
        without populating _OWNED_CHROMA. T1 falls back to ephemeral."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.delenv("NX_SESSION_ID", raising=False)
            with patch.object(
                core_mod, "_resolve_top_level_session_id",
                return_value="x",
            ), patch(
                "nexus.session.find_session_by_id", return_value=None,
            ), patch(
                "nexus.session.start_t1_server",
                side_effect=RuntimeError("port-in-use"),
            ):
                core_mod._t1_chroma_init_if_owner()

            assert core_mod._OWNED_CHROMA == {}


# ── _t1_chroma_shutdown ─────────────────────────────────────────────────────


class TestShutdown:

    def test_no_op_when_not_owned(self):
        """No state, nothing to clean. Idempotent under double-fire
        from the lifespan finally + atexit + signal handler."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            with patch("nexus.session.stop_t1_server") as mock_stop:
                core_mod._t1_chroma_shutdown()
            mock_stop.assert_not_called()

    def test_skip_on_nested(self):
        """Nested MCP server: the parent owns chroma; shutdown must
        not stop it."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            core_mod._OWNED_CHROMA["nested"] = True
            with patch("nexus.session.stop_t1_server") as mock_stop:
                core_mod._t1_chroma_shutdown()
            mock_stop.assert_not_called()
            assert core_mod._OWNED_CHROMA == {}

    def test_skip_on_reused(self):
        """FM-NEW-2 reuse: another MCP server in the same session
        owns chroma; shutdown must not stop it."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            core_mod._OWNED_CHROMA.update({
                "reused": True, "session_id": "x",
            })
            with patch("nexus.session.stop_t1_server") as mock_stop:
                core_mod._t1_chroma_shutdown()
            mock_stop.assert_not_called()
            assert core_mod._OWNED_CHROMA == {}

    def test_full_cleanup_when_owned(self, tmp_path):
        """Owned chroma: stop_t1_server is called, tmpdir is removed,
        session file is unlinked, state is cleared."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            tmpdir = tmp_path / "td"
            tmpdir.mkdir()
            (tmpdir / "chroma.sqlite3").write_bytes(b"x")
            session_file = tmp_path / "s.session"
            session_file.write_text("{}")

            core_mod._OWNED_CHROMA.update({
                "session_id": "y",
                "server_pid": 12345,
                "tmpdir": str(tmpdir),
                "session_file": str(session_file),
            })
            with patch("nexus.session.stop_t1_server") as mock_stop:
                core_mod._t1_chroma_shutdown()

            mock_stop.assert_called_once_with(12345)
            assert not tmpdir.exists()
            assert not session_file.exists()
            assert core_mod._OWNED_CHROMA == {}

    def test_idempotent_under_double_fire(self, tmp_path):
        """Lifespan finally + atexit + signal handler may all call
        shutdown. The first to fire performs the work; the rest are
        no-ops because _OWNED_CHROMA is cleared."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            session_file = tmp_path / "s.session"
            session_file.write_text("{}")
            core_mod._OWNED_CHROMA.update({
                "session_id": "y",
                "server_pid": 12345,
                "tmpdir": str(tmp_path / "td_unused"),
                "session_file": str(session_file),
            })
            with patch("nexus.session.stop_t1_server") as mock_stop:
                core_mod._t1_chroma_shutdown()
                core_mod._t1_chroma_shutdown()
                core_mod._t1_chroma_shutdown()
            assert mock_stop.call_count == 1


# ── _t1_chroma_lifespan async cm ────────────────────────────────────────────


class TestLifespan:
    """The lifespan context manager is the FastMCP entry point. Verify
    init runs on enter and shutdown runs on exit, with shutdown also
    firing if the body raises (so anyio cancellation propagation
    cleans up correctly)."""

    @pytest.mark.asyncio
    async def test_lifespan_runs_init_then_shutdown(self):
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            with patch.object(
                core_mod, "_t1_chroma_init_if_owner",
            ) as mock_init, patch.object(
                core_mod, "_t1_chroma_shutdown",
            ) as mock_shutdown:
                async with core_mod._t1_chroma_lifespan(MagicMock()):
                    mock_init.assert_called_once()
                    mock_shutdown.assert_not_called()
                mock_shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_runs_shutdown_on_exception(self):
        """anyio cancellation through the body must still trigger the
        finally block. Simulate by raising inside the async body."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            with patch.object(
                core_mod, "_t1_chroma_init_if_owner",
            ), patch.object(
                core_mod, "_t1_chroma_shutdown",
            ) as mock_shutdown:
                with pytest.raises(RuntimeError, match="cancellation"):
                    async with core_mod._t1_chroma_lifespan(MagicMock()):
                        raise RuntimeError("cancellation")
                mock_shutdown.assert_called_once()


# ── Feature flag wiring ─────────────────────────────────────────────────────


class TestFeatureFlag:

    def test_lifespan_only_attached_when_flag_set(self):
        """The lifespan kwarg passed to FastMCP() at module import depends
        on the env var. Verify the helper variable matches reality. (The
        env var is read once at module import, so this test is asserting
        the read happened correctly under the active env.)"""
        import os

        from nexus.mcp import core as core_mod

        flag = os.environ.get("NEXUS_MCP_OWNS_T1", "").strip().lower()
        expected = flag in ("1", "true", "yes")
        assert core_mod._MCP_OWNS_T1 is expected
