# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the T1 server watchdog (nexus-99jb Layer 1)."""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import structlog


# ── _is_alive ─────────────────────────────────────────────────────────────────


class TestIsAlive:

    def test_own_process_is_alive(self):
        from nexus.t1_watchdog import _is_alive
        assert _is_alive(os.getpid()) is True

    def test_zero_and_negative_pids_are_not_alive(self):
        from nexus.t1_watchdog import _is_alive
        assert _is_alive(0) is False
        assert _is_alive(-1) is False

    def test_process_lookup_error_means_dead(self):
        from nexus.t1_watchdog import _is_alive
        with patch("os.kill", side_effect=ProcessLookupError):
            assert _is_alive(12345) is False

    def test_permission_error_means_alive(self):
        """Process exists but owned by another uid — still alive."""
        from nexus.t1_watchdog import _is_alive
        with patch("os.kill", side_effect=PermissionError):
            assert _is_alive(1) is True


# ── _cleanup ──────────────────────────────────────────────────────────────────


class TestCleanup:

    def test_cleanup_removes_session_file_and_tmpdir(self, tmp_path):
        from nexus.t1_watchdog import _cleanup

        session_file = tmp_path / "test.session"
        session_file.write_text("{}")
        tmpdir = tmp_path / "nx_t1_xyz"
        tmpdir.mkdir()
        (tmpdir / "chroma.sqlite3").write_bytes(b"data")

        # Mock stop_t1_server so nothing real gets signalled.
        with patch("nexus.session.stop_t1_server") as mock_stop:
            _cleanup(chroma_pid=99999, session_file=session_file, tmpdir=tmpdir)

        mock_stop.assert_called_once_with(99999)
        assert not session_file.exists()
        assert not tmpdir.exists()

    def test_cleanup_swallows_session_file_oserror(self, tmp_path):
        """If session file can't be removed, cleanup still proceeds."""
        from nexus.t1_watchdog import _cleanup

        with patch("nexus.session.stop_t1_server"):
            # Nonexistent session file — unlink(missing_ok=True) succeeds,
            # but let's test an actively unwriteable path too.
            _cleanup(
                chroma_pid=1, session_file=tmp_path / "does-not-exist",
                tmpdir=None,
            )  # no raise

    def test_cleanup_swallows_stop_t1_server_failure(self, tmp_path):
        """Chroma stop failures don't prevent tmpdir cleanup."""
        from nexus.t1_watchdog import _cleanup

        tmpdir = tmp_path / "nx_t1_err"
        tmpdir.mkdir()
        with patch("nexus.session.stop_t1_server",
                   side_effect=RuntimeError("simulated")):
            _cleanup(chroma_pid=1, session_file=None, tmpdir=tmpdir)

        assert not tmpdir.exists(), "tmpdir cleanup must run even if stop fails"


# ── main() loop ───────────────────────────────────────────────────────────────


class TestMain:

    def test_exits_when_chroma_dies_without_cleanup(self, monkeypatch, tmp_path):
        """If chroma dies first, the watchdog exits cleanly without calling
        stop_t1_server — SessionEnd or sweep own that cleanup.
        """
        from nexus import t1_watchdog

        # Patch sleep to be instant + _is_alive sequence:
        #   Tick 1: chroma dead → exit(0) without cleanup.
        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)
        monkeypatch.setattr(
            t1_watchdog, "_is_alive",
            lambda pid: False,  # everything dead
        )

        session_file = tmp_path / "s.session"
        session_file.write_text("{}")
        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup:
            rc = t1_watchdog.main([
                "--claude-pid", "100",
                "--chroma-pid", "200",
                "--session-file", str(session_file),
                "--tmpdir", str(tmp_path / "tmpdir"),
            ])
        assert rc == 0
        mock_cleanup.assert_not_called()
        # Session file should still exist — the watchdog only cleans when
        # IT was responsible for the chroma teardown.
        assert session_file.exists()

    def test_triggers_cleanup_when_claude_pid_dies(self, monkeypatch, tmp_path):
        """Chroma alive + claude dead → cleanup fires and main returns."""
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _fake_alive(pid: int) -> bool:
            return pid == 200  # chroma alive, claude dead

        monkeypatch.setattr(t1_watchdog, "_is_alive", _fake_alive)

        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup:
            rc = t1_watchdog.main([
                "--claude-pid", "100",
                "--chroma-pid", "200",
                "--session-file", str(tmp_path / "s.session"),
                "--tmpdir", str(tmp_path / "tmpdir"),
            ])

        assert rc == 0
        mock_cleanup.assert_called_once()
        kwargs = mock_cleanup.call_args.kwargs
        assert kwargs["chroma_pid"] == 200
        assert str(kwargs["session_file"]).endswith("s.session")


# ── Dual-watch (RDR-094 FM-NEW-1) ───────────────────────────────────────────


class TestDualWatch:
    """RDR-094 FM-NEW-1: when --mcp-pid is set, the watchdog OR-triggers
    on either MCP server death (clean chroma directly) or Claude Code
    death (signal SIGTERM to mcp_pid then clean chroma)."""

    def test_mcp_pid_death_triggers_cleanup_directly(self, monkeypatch, tmp_path):
        """Dual-watch: chroma alive + claude alive + mcp dead → cleanup
        fires immediately (lifespan/atexit did not run on the dead MCP
        server, so the watchdog cleans chroma)."""
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _fake_alive(pid: int) -> bool:
            if pid == 200:  # chroma
                return True
            if pid == 100:  # claude
                return True
            if pid == 300:  # mcp_pid
                return False
            return False

        monkeypatch.setattr(t1_watchdog, "_is_alive", _fake_alive)

        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup, \
             patch.object(t1_watchdog, "_signal_then_kill") as mock_signal:
            rc = t1_watchdog.main([
                "--claude-pid", "100",
                "--chroma-pid", "200",
                "--mcp-pid", "300",
                "--session-file", str(tmp_path / "s.session"),
                "--tmpdir", str(tmp_path / "tmpdir"),
            ])

        assert rc == 0
        mock_cleanup.assert_called_once()
        # MCP death path does NOT signal mcp_pid (it's already dead).
        mock_signal.assert_not_called()

    def test_claude_pid_death_signals_mcp_then_cleans(self, monkeypatch, tmp_path):
        """Dual-watch: chroma alive + mcp alive + claude dead → SIGTERM
        the orphaned MCP server (giving its lifespan finally a chance to
        run), then fall through to cleanup as belt-and-braces."""
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _fake_alive(pid: int) -> bool:
            if pid == 200:  # chroma alive
                return True
            if pid == 100:  # claude DEAD
                return False
            if pid == 300:  # mcp alive
                return True
            return False

        monkeypatch.setattr(t1_watchdog, "_is_alive", _fake_alive)

        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup, \
             patch.object(t1_watchdog, "_signal_then_kill") as mock_signal:
            rc = t1_watchdog.main([
                "--claude-pid", "100",
                "--chroma-pid", "200",
                "--mcp-pid", "300",
                "--session-file", str(tmp_path / "s.session"),
                "--tmpdir", str(tmp_path / "tmpdir"),
            ])

        assert rc == 0
        mock_signal.assert_called_once_with(300)
        mock_cleanup.assert_called_once()

    def test_single_watch_mode_unchanged_when_no_mcp_pid(self, monkeypatch, tmp_path):
        """Backwards compat: omitting --mcp-pid preserves the old
        single-watch claude-only behaviour for hook-spawned watchdogs."""
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _fake_alive(pid: int) -> bool:
            return pid == 200  # only chroma alive

        monkeypatch.setattr(t1_watchdog, "_is_alive", _fake_alive)

        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup, \
             patch.object(t1_watchdog, "_signal_then_kill") as mock_signal:
            rc = t1_watchdog.main([
                "--claude-pid", "100",
                "--chroma-pid", "200",
                "--session-file", str(tmp_path / "s.session"),
                "--tmpdir", str(tmp_path / "tmpdir"),
            ])

        assert rc == 0
        mock_cleanup.assert_called_once()
        mock_signal.assert_not_called()

    def test_signal_then_kill_sigterm_first(self, monkeypatch):
        """_signal_then_kill sends SIGTERM, sleeps grace, SIGKILL
        only if still alive afterwards."""
        import signal as _signal

        from nexus import t1_watchdog

        sleeps: list[float] = []
        kills: list[tuple[int, int]] = []

        def _fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))
            if sig == 0:  # liveness check inside _is_alive
                return

        def _fake_sleep(s: float) -> None:
            sleeps.append(s)

        monkeypatch.setattr(t1_watchdog.os, "kill", _fake_kill)
        monkeypatch.setattr(t1_watchdog.time, "sleep", _fake_sleep)
        monkeypatch.setattr(t1_watchdog, "_is_alive", lambda pid: False)

        t1_watchdog._signal_then_kill(300)

        assert kills[0] == (300, _signal.SIGTERM)
        assert sleeps == [t1_watchdog.MCP_GRACE_SECS]
        # Process is dead by the time we re-check, so no SIGKILL.
        assert _signal.SIGKILL not in {s for _, s in kills}

    def test_signal_then_kill_falls_through_to_sigkill(self, monkeypatch):
        """If process is still alive after grace, SIGKILL fires."""
        import signal as _signal

        from nexus import t1_watchdog

        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(
            t1_watchdog.os, "kill",
            lambda pid, sig: kills.append((pid, sig)),
        )
        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)
        monkeypatch.setattr(t1_watchdog, "_is_alive", lambda pid: True)

        t1_watchdog._signal_then_kill(300)

        # Both SIGTERM and SIGKILL should have been sent.
        sigs = {s for _, s in kills}
        assert _signal.SIGTERM in sigs
        assert _signal.SIGKILL in sigs


# ── Lifecycle logging (RDR-094 Phase G / nexus-aqna) ─────────────────────────


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every structlog event emitted by t1_watchdog.

    Routes ``structlog.get_logger("nexus.t1_watchdog")`` to a recording
    bound logger so tests can assert on event names + kwargs without
    touching the real RotatingFileHandler. The ``configure_logging``
    call inside ``main()`` is also stubbed so it does not reset the
    capture wrapper.
    """
    events: list[dict] = []

    def _capture_processor(logger, method_name, event_dict):
        events.append({"_method": method_name, **event_dict})
        raise structlog.DropEvent

    structlog.configure(
        processors=[_capture_processor],
        wrapper_class=structlog.make_filtering_bound_logger(0),  # DEBUG
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    from nexus import t1_watchdog

    monkeypatch.setattr(t1_watchdog, "configure_logging", lambda _mode: None)
    return events


class TestLifecycleLogging:
    """The watchdog must emit structlog events at every lifecycle
    transition so Spike B (Phase D) and the Phase E canary can attribute
    chroma cleanups to the right trigger."""

    def test_emits_watchdog_started_with_pid_args(
        self, monkeypatch, tmp_path, captured_events,
    ):
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)
        monkeypatch.setattr(t1_watchdog, "_is_alive", lambda pid: False)

        t1_watchdog.main([
            "--claude-pid", "100",
            "--chroma-pid", "200",
            "--mcp-pid", "300",
            "--session-file", str(tmp_path / "s.session"),
            "--tmpdir", str(tmp_path / "tmpdir"),
        ])

        started = next(
            e for e in captured_events if e.get("event") == "watchdog_started"
        )
        assert started["claude_pid"] == 100
        assert started["chroma_pid"] == 200
        assert started["mcp_pid"] == 300
        assert started["dual_watch"] is True

    def test_chroma_dies_externally_path_emits_exit_event(
        self, monkeypatch, tmp_path, captured_events,
    ):
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)
        monkeypatch.setattr(t1_watchdog, "_is_alive", lambda pid: False)

        with patch.object(t1_watchdog, "_cleanup") as mock_cleanup:
            t1_watchdog.main([
                "--claude-pid", "100", "--chroma-pid", "200",
                "--session-file", str(tmp_path / "s"),
                "--tmpdir", str(tmp_path / "td"),
            ])

        mock_cleanup.assert_not_called()
        exit_events = [
            e for e in captured_events if e.get("event") == "watchdog_exiting"
        ]
        assert len(exit_events) == 1
        assert exit_events[0]["reason"] == "chroma_died_externally"

    def test_mcp_pid_disappeared_path_logs_full_sequence(
        self, monkeypatch, tmp_path, captured_events,
    ):
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _alive(pid: int) -> bool:
            return pid != 300  # mcp dead, others alive

        monkeypatch.setattr(t1_watchdog, "_is_alive", _alive)
        with patch.object(t1_watchdog, "_cleanup"):
            t1_watchdog.main([
                "--claude-pid", "100", "--chroma-pid", "200",
                "--mcp-pid", "300",
                "--session-file", str(tmp_path / "s"),
                "--tmpdir", str(tmp_path / "td"),
            ])

        names = [e.get("event") for e in captured_events]
        assert "watchdog_started" in names
        assert "mcp_pid_disappeared" in names
        assert "chroma_cleanup_started" in names
        assert "chroma_cleanup_complete" in names
        assert names.count("watchdog_exiting") == 1
        # Ordering invariant: started → mcp_disappeared → cleanup_started
        # → cleanup_complete → exiting.
        assert names.index("mcp_pid_disappeared") < names.index(
            "chroma_cleanup_started",
        )
        assert names.index("chroma_cleanup_started") < names.index(
            "chroma_cleanup_complete",
        )

    def test_claude_pid_disappeared_path_logs_signal_then_cleanup(
        self, monkeypatch, tmp_path, captured_events,
    ):
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _alive(pid: int) -> bool:
            return pid != 100  # claude dead, mcp + chroma alive

        monkeypatch.setattr(t1_watchdog, "_is_alive", _alive)
        with patch.object(t1_watchdog, "_cleanup"), \
             patch.object(t1_watchdog, "_signal_then_kill"):
            t1_watchdog.main([
                "--claude-pid", "100", "--chroma-pid", "200",
                "--mcp-pid", "300",
                "--session-file", str(tmp_path / "s"),
                "--tmpdir", str(tmp_path / "td"),
            ])

        names = [e.get("event") for e in captured_events]
        assert "claude_pid_disappeared" in names
        assert "signalling_mcp_pid" in names
        assert "chroma_cleanup_complete" in names

    def test_single_watch_mode_does_not_emit_signalling_event(
        self, monkeypatch, tmp_path, captured_events,
    ):
        """With no --mcp-pid, claude-death must skip the signalling step."""
        from nexus import t1_watchdog

        monkeypatch.setattr(t1_watchdog.time, "sleep", lambda _s: None)

        def _alive(pid: int) -> bool:
            return pid == 200  # only chroma alive

        monkeypatch.setattr(t1_watchdog, "_is_alive", _alive)
        with patch.object(t1_watchdog, "_cleanup"):
            t1_watchdog.main([
                "--claude-pid", "100", "--chroma-pid", "200",
                "--session-file", str(tmp_path / "s"),
                "--tmpdir", str(tmp_path / "td"),
            ])

        names = [e.get("event") for e in captured_events]
        assert "claude_pid_disappeared" in names
        assert "signalling_mcp_pid" not in names


class TestWatchdogLogConfiguration:
    """``configure_logging('watchdog')`` must produce a real rotating
    file handler at ``<config>/logs/watchdog.log``. We only check the
    file appears + structlog events flow through; rotation is covered
    by the stdlib RotatingFileHandler tests upstream."""

    def test_watchdog_log_file_created_under_nexus_config_dir(
        self, monkeypatch, tmp_path,
    ):
        cfg_dir = tmp_path / "nexus_config"
        monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg_dir))

        from nexus.logging_setup import configure_logging

        configure_logging("watchdog")
        log = structlog.get_logger("nexus.t1_watchdog")
        log.info("watchdog_started", claude_pid=1, chroma_pid=2, mcp_pid=0)

        # Force the handler to flush.
        import logging
        for h in logging.getLogger().handlers:
            h.flush()

        log_file = cfg_dir / "logs" / "watchdog.log"
        assert log_file.exists()
        body = log_file.read_text()
        assert "watchdog_started" in body
        assert "claude_pid=1" in body
