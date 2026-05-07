# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the MCP-owned T1 chroma lifecycle (RDR-094 Phase 4).

Covers the unconditional MCP-owns-chroma path (the 4.12.0-era
``NEXUS_MCP_OWNS_T1`` opt-out gate was removed in Phase F / 4.13.0):

  * ``_t1_chroma_init_if_owner`` spawn / reuse / nested-skip branches.
  * ``_t1_chroma_shutdown`` idempotency + skip-on-reuse / skip-on-nested.
  * ``_tcp_probe_alive`` happy path + connection-refused path.
  * The lifespan async context manager wires init + shutdown.

All tests mock the subprocess + filesystem boundaries so the suite runs
fast and deterministically. Live-I/O coverage lands in the RDR-094
spike harness.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest


@contextmanager
def _clean_owned_chroma():
    """Reset _OWNED_CHROMA + _SHUTDOWN_IN_FLIGHT before and after each
    test so module-scope state from one test does not leak into the
    next. Both are sticky module globals; tests that exercise
    _t1_chroma_shutdown must start from a clean slate."""
    from nexus.mcp import core as core_mod

    saved = dict(core_mod._OWNED_CHROMA)
    saved_in_flight = core_mod._SHUTDOWN_IN_FLIGHT
    core_mod._OWNED_CHROMA.clear()
    core_mod._SHUTDOWN_IN_FLIGHT = False
    try:
        yield
    finally:
        core_mod._OWNED_CHROMA.clear()
        core_mod._OWNED_CHROMA.update(saved)
        core_mod._SHUTDOWN_IN_FLIGHT = saved_in_flight


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

    def test_subprocess_with_nx_session_id_never_spawns_when_probe_fails(
        self, monkeypatch,
    ):
        """GH #576 Phase D — invariant I1+I5: subprocess MCPs (those
        with NX_SESSION_ID set) must NEVER spawn their own chroma,
        even when the parent's probe fails. Pre-fix the function fell
        through to the spawn path (line 178) and overwrote
        ``sessions/<inherited>.session`` with the subprocess's own
        server_pid — the deep-analyst's "fifth bug" (silent
        spawn-and-overwrite under inherited UUID).

        Two failure modes feed the spawn fall-through:
          1. ``find_session_by_id`` returns None (parent's record was
             swept by an earlier subprocess SessionStart).
          2. TCP probe to parent times out under load.

        Phase D's hard read-only gate kills both modes at the same
        check: when NX_SESSION_ID is set, never spawn. Period.
        """
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.setenv("NX_SESSION_ID", "parent-uuid")
            monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)
            # Parent record gone (sweep raced) → find returns None.
            with patch(
                "nexus.session.find_session_by_id", return_value=None,
            ), patch(
                "nexus.session.start_t1_server",
            ) as mock_start, patch(
                "nexus.session.write_session_record_by_id",
            ) as mock_write, patch(
                "nexus.session.spawn_t1_watchdog",
            ) as mock_spawn:
                core_mod._t1_chroma_init_if_owner()

            mock_start.assert_not_called()
            mock_write.assert_not_called()
            mock_spawn.assert_not_called()
            # _OWNED_CHROMA stays empty: subprocess T1Database falls
            # back to its NEXUS_SKIP_T1 / EphemeralClient or raises
            # T1ServerNotFoundError. No silent record overwrite.
            assert core_mod._OWNED_CHROMA == {}

    def test_subprocess_with_nx_session_id_never_spawns_when_probe_succeeds_no_record(
        self, monkeypatch,
    ):
        """Variant of the above: probe succeeds (parent chroma is
        reachable in some other guise) but find_session_by_id returns
        None — still no spawn, no record write."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.setenv("NX_SESSION_ID", "parent-uuid")
            monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)
            with patch(
                "nexus.session.find_session_by_id", return_value=None,
            ), patch.object(
                core_mod, "_tcp_probe_alive", return_value=True,
            ), patch(
                "nexus.session.start_t1_server",
            ) as mock_start:
                core_mod._t1_chroma_init_if_owner()
            mock_start.assert_not_called()
            assert core_mod._OWNED_CHROMA.get("nested") is not True

    def test_subprocess_with_nexus_skip_t1_never_spawns(
        self, monkeypatch,
    ):
        """Phase D: NEXUS_SKIP_T1=1 (operator-subprocess opt-in from
        ``claude_dispatch``) is also a hard read-only gate. The conftest
        sets it autouse; this test relies on that default rather than
        opting out."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.delenv("NX_SESSION_ID", raising=False)
            # NEXUS_SKIP_T1 is set autouse by conftest._isolate_t1_sessions.
            assert os.environ.get("NEXUS_SKIP_T1") == "1"
            with patch(
                "nexus.session.start_t1_server",
            ) as mock_start, patch(
                "nexus.session.write_session_record_by_id",
            ) as mock_write:
                core_mod._t1_chroma_init_if_owner()
            mock_start.assert_not_called()
            mock_write.assert_not_called()

    def test_reuse_path_when_existing_record_reachable(self, monkeypatch):
        """FM-NEW-2: existing record for own session_id is reachable,
        so reuse instead of spawning. _OWNED_CHROMA is marked reused
        so the shutdown path skips cleanup."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            monkeypatch.delenv("NX_SESSION_ID", raising=False)
            # Phase D: NEXUS_SKIP_T1=1 short-circuits to read-only
            # subprocess mode. The conftest sets it autouse for ALL
            # tests; top-level-MCP scenarios must opt out explicitly.
            monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)
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
            monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)
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
            monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)
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

    def test_in_flight_flag_blocks_reentrant_call(self):
        """Regression sentinel for the production stdin-EOF + SIGTERM
        race that produced spurious mcp_server_crashed events on every
        clean shutdown post-4.12.0. When the lifespan finally is in
        the middle of running stop_t1_server (Python is paused inside
        ``time.sleep``), a SIGTERM-driven re-entrant call to
        _t1_chroma_shutdown must short-circuit instead of running
        cleanup again from the signal handler frame."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            core_mod._OWNED_CHROMA.update({
                "session_id": "y", "server_pid": 12345,
                "tmpdir": "", "session_file": "",
            })
            # Simulate "lifespan finally is in flight": the flag is
            # set but _OWNED_CHROMA has not yet been cleared.
            core_mod._SHUTDOWN_IN_FLIGHT = True
            with patch("nexus.session.stop_t1_server") as mock_stop:
                core_mod._t1_chroma_shutdown()
            mock_stop.assert_not_called(), (
                "Re-entrant call must short-circuit while shutdown "
                "is in flight to avoid double-execute / SystemExit "
                "race through anyio TaskGroup."
            )


# ── _sigterm_handler (4.12.1 race fix) ──────────────────────────────────────


class TestSigtermHandler:
    """Pin the production race fix: stdin-EOF + SIGTERM must NOT log
    mcp_server_crashed when the lifespan finally is already running
    cleanup."""

    def test_returns_silently_when_shutdown_in_flight(self):
        """Lifespan finally has entered _t1_chroma_shutdown; signal
        handler must return without sys.exit so the in-flight teardown
        completes without SystemExit propagating through anyio."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            core_mod._SHUTDOWN_IN_FLIGHT = True
            with (
                patch.object(core_mod, "_t1_chroma_shutdown") as mock_shutdown,
                patch("os._exit") as mock_exit,
            ):
                core_mod._sigterm_handler(15, None)  # SIGTERM
            mock_shutdown.assert_not_called(), (
                "In-flight handler must not re-call shutdown"
            )
            mock_exit.assert_not_called(), (
                "In-flight handler must not os._exit -- lifespan "
                "owns the exit path"
            )

    def test_drives_shutdown_and_os_exit_when_first_signal(self):
        """SIGTERM-only path (no prior stdin EOF): handler runs the
        shutdown then os._exit(0). Critically uses os._exit rather
        than sys.exit -- the latter raises SystemExit which anyio's
        TaskGroup logs as mcp_server_crashed."""
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            with (
                patch.object(core_mod, "_t1_chroma_shutdown") as mock_shutdown,
                patch("os._exit") as mock_exit,
            ):
                core_mod._sigterm_handler(15, None)
            mock_shutdown.assert_called_once()
            mock_exit.assert_called_once_with(0)

    def test_does_not_use_sys_exit(self):
        """Regression sentinel: sys.exit raises SystemExit which
        propagates through anyio's TaskGroup as 'unhandled error',
        logged as mcp_server_crashed. The handler must use os._exit
        to exit the process without raising."""
        import sys
        from nexus.mcp import core as core_mod

        with _clean_owned_chroma():
            with (
                patch.object(core_mod, "_t1_chroma_shutdown"),
                patch("os._exit"),
                patch.object(sys, "exit") as mock_sys_exit,
            ):
                core_mod._sigterm_handler(15, None)
            mock_sys_exit.assert_not_called(), (
                "sys.exit raises SystemExit through anyio TaskGroup; "
                "must use os._exit instead"
            )


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


# ── Lifespan wiring (Phase F / 4.13.0: unconditional) ──────────────────────


class TestLifespanWiring:
    """The 4.12.0-era ``NEXUS_MCP_OWNS_T1`` opt-out gate was removed
    in Phase F / 4.13.0. The lifespan is now unconditionally attached
    to the FastMCP instance; there is no env-var path to disable it.
    """

    def test_lifespan_unconditionally_attached(self):
        """``mcp.run`` always uses ``_t1_chroma_lifespan`` -- there is
        no flag-off path that constructs FastMCP with ``lifespan=None``.
        """
        from nexus.mcp import core as core_mod

        # FastMCP's ``settings`` carries the lifespan it was constructed
        # with. Verify it points at our async cm, not None.
        # The ``_lifespan_cm`` attribute is the canonical store for the
        # callable across FastMCP versions; fall back to ``settings``
        # for older minor releases.
        lifespan = (
            getattr(core_mod.mcp, "_lifespan", None)
            or getattr(core_mod.mcp, "_lifespan_cm", None)
            or getattr(getattr(core_mod.mcp, "settings", None), "lifespan", None)
        )
        assert lifespan is not None, (
            "FastMCP must have a lifespan attached after Phase F"
        )
        # Whichever attribute carried it, the function name must match.
        # The lifespan stored may be the function itself or a wrapper;
        # checking the qualname covers both shapes.
        qualname = getattr(lifespan, "__qualname__", repr(lifespan))
        assert "_t1_chroma_lifespan" in qualname, (
            f"Expected _t1_chroma_lifespan; got {qualname}"
        )

    def test_no_mcp_owns_t1_module_attr(self):
        """Regression sentinel: the ``_MCP_OWNS_T1`` module attribute
        was removed in Phase F. Any reference to it in production
        code or tests is a stale gate from 4.12.x."""
        from nexus.mcp import core as core_mod

        assert not hasattr(core_mod, "_MCP_OWNS_T1"), (
            "_MCP_OWNS_T1 was removed in Phase F (RDR-094 / nexus-2lm0)"
        )
        assert not hasattr(core_mod, "_flag_enabled"), (
            "_flag_enabled was removed with the gate"
        )


# ── GH #567 follow-up: lifespan-vs-SessionStart race ─────────────────────────


def test_t1_chroma_init_self_mints_session_id_when_pointer_missing(
    tmp_path, monkeypatch,
) -> None:
    """GH #567 follow-up: lifespan boots before SessionStart writes
    current_session pointer (race observed locally on 4.26.4, mcp.log
    boot at 14:29:40 had no chroma_init log because the pointer was
    empty). Pre-fix _t1_chroma_init_if_owner returned silently on
    'no own_id'; chroma never spawned; .session never written. Post-
    fix it self-mints a UUID and writes current_session so the lazy
    chroma comes up regardless of hook order.

    Mocks start_t1_server so the test does not actually spawn chroma.
    """
    from unittest.mock import patch

    from nexus.mcp.core import _t1_chroma_init_if_owner, _OWNED_CHROMA

    # Sandbox config dir so we don't touch the operator's real
    # current_session.
    pointer = tmp_path / "current_session"
    monkeypatch.setattr("nexus.session.CLAUDE_SESSION_FILE", pointer)

    # Phase D: NEXUS_SKIP_T1=1 short-circuits to read-only subprocess
    # mode. The conftest sets it autouse for ALL tests; top-level-MCP
    # scenarios (this self-mint test) must opt out explicitly.
    monkeypatch.delenv("NEXUS_SKIP_T1", raising=False)
    monkeypatch.delenv("NX_SESSION_ID", raising=False)

    # Confirm pointer is absent at start.
    assert not pointer.exists()

    # Reset _OWNED_CHROMA singleton between tests.
    _OWNED_CHROMA.clear()

    fake_record_writes: list[tuple] = []

    def _fake_start():
        return ("127.0.0.1", 54321, 99999, str(tmp_path / "fake-tmpdir"))

    def _fake_write_record(sessions_dir, session_id, host, port, server_pid, tmpdir, **kwargs):
        fake_record_writes.append((session_id, host, port))

    with patch("nexus.session.start_t1_server", side_effect=_fake_start), \
         patch("nexus.session.write_session_record_by_id", side_effect=_fake_write_record), \
         patch("nexus.session.find_claude_root_pid", return_value=None), \
         patch("nexus.session.spawn_t1_watchdog", return_value=0):
        _t1_chroma_init_if_owner()

    # Self-minted UUID written to current_session.
    assert pointer.exists(), "current_session pointer must be written"
    minted_uuid = pointer.read_text().strip()
    assert len(minted_uuid) == 36, f"expected UUID, got {minted_uuid!r}"

    # Chroma spawn fired with that UUID.
    assert fake_record_writes, "no record write fired"
    session_id, host, port = fake_record_writes[0]
    assert session_id == minted_uuid, (
        f"record session_id {session_id!r} must match minted UUID {minted_uuid!r}"
    )
    assert host == "127.0.0.1"

    # _OWNED_CHROMA reflects the spawn.
    assert _OWNED_CHROMA.get("session_id") == minted_uuid
    _OWNED_CHROMA.clear()  # cleanup


# ── GH #572: post-spawn pointer reconciliation ──────────────────────────────


class TestReconcileOwnedChroma:
    """GH #572: SessionStart can fire AFTER the lifespan spawn.
    ``reconcile_owned_chroma`` renames the session record file when
    the canonical pointer drifts post-spawn so discovery via
    ``find_session_by_id`` succeeds.
    """

    def test_reconcile_renames_when_pointer_drifts_after_spawn(
        self, tmp_path, monkeypatch,
    ):
        from nexus.mcp import core as core_mod

        # Set up a synthetic _OWNED_CHROMA + session record on disk.
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        old_id = "old-uuid-aaaa-bbbb-cccc-dddddddddddd"
        new_id = "new-uuid-aaaa-bbbb-cccc-dddddddddddd"
        old_record = sessions_dir / f"{old_id}.session"
        old_record.write_text(
            '{"session_id": "' + old_id + '", "server_pid": 99999, '
            '"server_host": "127.0.0.1", "server_port": 12345}'
        )

        pointer = tmp_path / "current_session"
        pointer.write_text(new_id)

        monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr("nexus.session.CLAUDE_SESSION_FILE", pointer)

        # Reset module state.
        core_mod._OWNED_CHROMA.clear()
        core_mod._OWNED_CHROMA.update({
            "session_id": old_id,
            "server_pid": 99999,
            "tmpdir": str(tmp_path / "tmp"),
            "session_file": str(old_record),
        })

        # Drive the reconcile.
        renamed = core_mod.reconcile_owned_chroma()

        assert renamed, "expected reconcile to fire"
        assert not old_record.exists(), "old record must be moved"
        new_record = sessions_dir / f"{new_id}.session"
        assert new_record.exists(), "new record must be present"
        assert core_mod._OWNED_CHROMA["session_id"] == new_id
        assert core_mod._OWNED_CHROMA["session_file"] == str(new_record)

        # GH #576 Phase B: JSON content must be rewritten too — the
        # filename rename was historically NOT followed by a content
        # rewrite, leaving the JSON's ``session_id`` field stale. That
        # stale field then triggered ``sweep_stale_sessions.uuid_stale``
        # in any subsequent SessionStart fire (including the plan-runner
        # subprocess SessionStart from claude_dispatch), which unlinked
        # the canonical record and caused the silent T1 data loss
        # reported in #576.
        import json
        content = json.loads(new_record.read_text())
        assert content["session_id"] == new_id, (
            f"JSON content session_id must be rewritten to canonical; "
            f"got {content.get('session_id')!r}"
        )
        # Other fields preserved.
        assert content["server_pid"] == 99999
        assert content["server_host"] == "127.0.0.1"

        core_mod._OWNED_CHROMA.clear()

    def test_reconcile_noop_when_pointer_matches(
        self, tmp_path, monkeypatch,
    ):
        """Steady-state: pointer already matches our record. No-op."""
        from nexus.mcp import core as core_mod

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        sid = "stable-uuid-xxxx"
        record = sessions_dir / f"{sid}.session"
        record.write_text("{}")
        pointer = tmp_path / "current_session"
        pointer.write_text(sid)

        monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr("nexus.session.CLAUDE_SESSION_FILE", pointer)

        core_mod._OWNED_CHROMA.clear()
        core_mod._OWNED_CHROMA.update({
            "session_id": sid,
            "server_pid": 99999,
            "tmpdir": str(tmp_path / "tmp"),
            "session_file": str(record),
        })

        assert core_mod.reconcile_owned_chroma() is False
        assert record.exists()  # unchanged

        core_mod._OWNED_CHROMA.clear()

    def test_reconcile_noop_for_subagent_path(
        self, tmp_path, monkeypatch,
    ):
        """Subagent (NX_SESSION_ID set) MUST NOT rename the parent's
        session record. The subagent inherits the parent's record key.
        """
        from nexus.mcp import core as core_mod

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        parent_id = "parent-uuid"
        record = sessions_dir / f"{parent_id}.session"
        record.write_text("{}")
        pointer = tmp_path / "current_session"
        pointer.write_text("different-uuid")  # would normally trigger

        monkeypatch.setattr("nexus.db.t1.SESSIONS_DIR", sessions_dir)
        monkeypatch.setattr("nexus.session.CLAUDE_SESSION_FILE", pointer)
        monkeypatch.setenv("NX_SESSION_ID", parent_id)

        core_mod._OWNED_CHROMA.clear()
        core_mod._OWNED_CHROMA.update({
            "session_id": parent_id,
            "session_file": str(record),
        })

        assert core_mod.reconcile_owned_chroma() is False
        assert record.exists(), "subagent must not rename parent record"

        core_mod._OWNED_CHROMA.clear()
