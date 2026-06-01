# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-n8sbw: T2 daemon observability + stale-state detection.

The T2 daemon historically ran with stdout/stderr -> DEVNULL and no
structlog file sink, so a crash or signal-kill left no record (the
``daemon.log`` was a 0-byte file and the death cause was
undiagnosable). These tests pin two fixes:

1. ``run_t2_daemon`` routes the daemon's structlog events to a rotating
   file at ``<config_dir>/logs/t2_daemon.log`` (via ``configure_logging``),
   so the daemon's lifecycle is recorded regardless of how it was
   launched. A SIGTERM that begins a graceful stop now leaves a
   ``t2_daemon_stop_requested`` breadcrumb; a death without
   ``t2_daemon_stopped`` is therefore diagnosable.

2. ``nx daemon t2 status`` probes the recorded pid for liveness and
   reports a stale discovery file (dead pid) as such, instead of
   printing it as if the daemon were alive.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from click.testing import CliRunner

from nexus.cli import main
from nexus.daemon.t2_daemon import t2_discovery_path


def _write_discovery(config_dir: Path, pid: int) -> Path:
    payload = {
        "format_version": 1,
        "uds_path": str(config_dir / "sockets" / "t2.sock"),
        "tcp_host": "127.0.0.1",
        "tcp_port": 12345,
        "pid": pid,
        "daemon_version": "5.0.2",
        "start_time": "2026-05-25T00:00:00+00:00",
    }
    dest = t2_discovery_path(config_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload))
    return dest


def _dead_pid() -> int:
    """Return a pid that is reliably not running: fork a trivial child
    and reap it. Reuse within the test window is vanishingly unlikely."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ---------------------------------------------------------------------------
# Fix 1: daemon logging (the death is no longer invisible)
# ---------------------------------------------------------------------------


class TestDaemonLogging:
    def test_run_t2_daemon_writes_lifecycle_log_to_config_dir(
        self, tmp_path: Path,
    ) -> None:
        """A real daemon subprocess records start + stop-request
        breadcrumbs to ``<config_dir>/logs/t2_daemon.log``."""
        import shutil
        import tempfile

        # Short config_dir: macOS caps AF_UNIX socket paths at 104 chars.
        config_dir = Path(tempfile.mkdtemp(prefix="nxt2obs-", dir="/tmp"))
        db_path = config_dir / "memory.db"
        log_path = config_dir / "logs" / "t2_daemon.log"
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                [
                    sys.executable, "-m", "nexus.cli",
                    "daemon", "t2", "start",
                    "--config-dir", str(config_dir),
                    "--db-path", str(db_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            disc = t2_discovery_path(config_dir)
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if disc.exists():
                    break
                if proc.poll() is not None:
                    raise AssertionError(
                        f"daemon exited early (code {proc.returncode})"
                    )
                time.sleep(0.1)
            assert disc.exists(), "daemon did not write discovery file in 15s"

            # The log file must exist and record the start event; the
            # whole point: the daemon is no longer silent.
            assert log_path.exists(), (
                "daemon produced no log file; it is still silent"
            )
            assert "t2_daemon_started" in log_path.read_text()

            # Graceful stop leaves a breadcrumb. Its presence (and the
            # later absence of t2_daemon_stopped on a hard kill) is the
            # diagnostic signal nexus-n8sbw was about.
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15.0)
            stop_deadline = time.monotonic() + 5.0
            text = ""
            while time.monotonic() < stop_deadline:
                text = log_path.read_text()
                if "t2_daemon_stop_requested" in text:
                    break
                time.sleep(0.1)
            assert "t2_daemon_stop_requested" in text
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10.0)
            shutil.rmtree(config_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fix 3: status reports stale discovery (dead pid) instead of "running"
# ---------------------------------------------------------------------------


class TestStatusLiveness:
    def test_status_reports_stale_when_pid_dead(self, tmp_path: Path) -> None:
        _write_discovery(tmp_path, _dead_pid())
        result = CliRunner().invoke(
            main, ["daemon", "t2", "status", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code != 0, result.output
        assert "stale" in result.output.lower()

    def test_status_reports_running_when_pid_alive(self, tmp_path: Path) -> None:
        _write_discovery(tmp_path, os.getpid())
        result = CliRunner().invoke(
            main, ["daemon", "t2", "status", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert str(os.getpid()) in result.output

    def test_status_absent_discovery_unchanged(self, tmp_path: Path) -> None:
        """No discovery file still exits non-zero with the existing message."""
        result = CliRunner().invoke(
            main, ["daemon", "t2", "status", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code != 0
        assert "no t2 daemon discovery file" in result.output.lower()


class TestStopBreadcrumbDurability:
    """nexus-61539: the t2_daemon_stop_requested breadcrumb must be flushed
    to disk immediately after it is written and BEFORE stop() (which can
    stall on a hung close). Under CI load the daemon could otherwise exit
    before the RotatingFileHandler flushed the line, losing the diagnostic
    in production as well as flaking the observability test."""

    def test_run_until_signal_flushes_after_breadcrumb(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import asyncio
        import nexus.daemon.t2_daemon as t2d
        import nexus.logging_setup as ls

        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        daemon = t2d.T2Daemon(
            config_dir=config_dir, db_path=tmp_path / "memory.db",
        )

        order: list[str] = []

        class _FakeLog:
            def info(self, event, **kw):
                if event == "t2_daemon_stop_requested":
                    order.append("breadcrumb")

            def __getattr__(self, _name):
                return lambda *a, **k: None

        monkeypatch.setattr(t2d, "_log", _FakeLog())
        # run_until_signal does `from nexus.logging_setup import flush_logging`
        # at call time, so patching the module attr is picked up.
        monkeypatch.setattr(ls, "flush_logging", lambda: order.append("flush"))

        async def _drive() -> None:
            daemon._stop_event = asyncio.Event()
            daemon._stop_event.set()  # signal already arrived
            await daemon.run_until_signal()

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_drive())
        finally:
            loop.close()

        assert order == ["breadcrumb", "flush"], (
            f"breadcrumb must be written THEN flushed; saw {order}"
        )


# ---------------------------------------------------------------------------
# RDR-140 P4.1 (nexus-0gyhe): Gap 5 — status surface + crash-loop guard
# ---------------------------------------------------------------------------


class TestStatusSurface:
    """`nx daemon t2 status` must explicitly surface owner pid, socket
    liveness, daemon version, AND a restart-count-in-interval. The first three
    already render (the discovery dict is dumped); the restart count is NEW and
    is RED until P4.2 (nexus-hrrpz) sources it from the crash-loop guard."""

    def test_status_surfaces_owner_pid_version_and_liveness(
        self, tmp_path: Path,
    ) -> None:
        _write_discovery(tmp_path, os.getpid())
        result = CliRunner().invoke(
            main, ["daemon", "t2", "status", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        out = result.output
        assert str(os.getpid()) in out          # owner pid
        assert "5.0.2" in out                    # daemon version (token)
        assert "running" in out.lower()          # socket/pid liveness

    def test_status_surfaces_restart_count_field(self, tmp_path: Path) -> None:
        """RED until P4.2: status reports how many restarts occurred in the
        crash-loop window (sourced from the guard sentinel; 0 when none).

        Asserts the explicit ``restarts_in_window`` label, NOT a bare
        "restart" substring — the pytest tmp_path is derived from this test's
        name and contains "restart", which would match the path vacuously."""
        _write_discovery(tmp_path, os.getpid())
        result = CliRunner().invoke(
            main, ["daemon", "t2", "status", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "restarts_in_window" in result.output


class TestCrashLoopGuard:
    """Bounded crash-loop guard (Gap 5). Seam contract pinned for P4.2
    (nexus-hrrpz), all in ``nexus.commands.daemon``:

    * constants ``_CRASHLOOP_WINDOW_S: float`` and ``_CRASHLOOP_MAX_RESTARTS: int``.
    * ``_record_restart(config_dir, *, now) -> int`` — append a wall-clock
      timestamp to the sentinel, prune entries older than the window, return
      the count within the window.
    * ``_restart_count(config_dir, *, now) -> int`` — read-only count within
      the window (no write); used by status.
    * ``_crashloop_tripped(config_dir, *, now) -> bool`` — count >= cap.
    * ``_reset_crashloop(config_dir)`` — clear the sentinel (healthy converge).

    The clock is injected (``now`` is a ``time.time()``-style float) so the
    window logic is deterministic — no real wall clock, no sleeps. RED
    (AttributeError) until P4.2 adds the seam.
    """

    def test_records_and_counts_within_window(self, tmp_path: Path) -> None:
        from nexus.commands import daemon as dm

        now = 1_000_000.0
        assert dm._record_restart(tmp_path, now=now) == 1
        assert dm._record_restart(tmp_path, now=now + 1) == 2
        assert dm._record_restart(tmp_path, now=now + 2) == 3
        assert dm._restart_count(tmp_path, now=now + 2) == 3

    def test_count_excludes_entries_outside_window(self, tmp_path: Path) -> None:
        from nexus.commands import daemon as dm

        now = 1_000_000.0
        dm._record_restart(tmp_path, now=now)
        # A probe a full window + 1s later: the old entry has aged out.
        later = now + dm._CRASHLOOP_WINDOW_S + 1.0
        assert dm._restart_count(tmp_path, now=later) == 0

    def test_tripped_after_cap(self, tmp_path: Path) -> None:
        from nexus.commands import daemon as dm

        now = 1_000_000.0
        for i in range(dm._CRASHLOOP_MAX_RESTARTS):
            dm._record_restart(tmp_path, now=now + i)
            # Not tripped until the cap is reached.
            expected = (i + 1) >= dm._CRASHLOOP_MAX_RESTARTS
            assert dm._crashloop_tripped(tmp_path, now=now + i) is expected

    def test_reset_clears_count(self, tmp_path: Path) -> None:
        from nexus.commands import daemon as dm

        now = 1_000_000.0
        for i in range(dm._CRASHLOOP_MAX_RESTARTS):
            dm._record_restart(tmp_path, now=now + i)
        assert dm._crashloop_tripped(tmp_path, now=now) is True
        dm._reset_crashloop(tmp_path)
        assert dm._restart_count(tmp_path, now=now) == 0
        assert dm._crashloop_tripped(tmp_path, now=now) is False


class TestCrashLoopRespawnRefusal:
    """A tripped crash-loop guard must make ensure-running log ONCE at error
    and STOP respawning — no Nth+1 spawn, no traceback. RED until P4.2 wires
    the guard into ``t2_ensure_running_cmd``."""

    def test_tripped_guard_refuses_respawn_and_logs_once(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        import nexus.commands.daemon as dm

        # Pre-trip the guard: cap restarts inside the window.
        now = 1_000_000.0
        for i in range(dm._CRASHLOOP_MAX_RESTARTS):
            dm._record_restart(tmp_path, now=now + i)
        monkeypatch.setattr(dm.time, "time", lambda: now + dm._CRASHLOOP_MAX_RESTARTS)

        spawns: list = []
        monkeypatch.setattr(
            dm.subprocess, "Popen",
            lambda *a, **k: spawns.append(a) or _Unreachable(),
        )
        errors: list = []

        class _FakeLog:
            def error(self, event, **kw):
                errors.append(event)

            def __getattr__(self, _n):
                return lambda *a, **k: None

        monkeypatch.setattr(dm, "_log", _FakeLog(), raising=False)

        # No live daemon (no discovery file) -> would normally cold-spawn.
        result = CliRunner().invoke(
            main, ["daemon", "t2", "ensure-running",
                   "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert spawns == [], "tripped guard must NOT spawn"
        assert result.exit_code != 0
        assert len(errors) == 1, f"crash-loop must log exactly once, saw {errors}"


class _Unreachable:
    """A Popen stand-in that is alive but never becomes reachable."""

    def poll(self):
        return None
