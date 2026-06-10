# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-141 P0 (nexus-cvaip): direct-call tests for ``_t2_ensure_running_inner``.

Tests assert exact T2EnsureOutcome enum values for every terminal path and
confirm that the inner function NEVER raises SystemExit (the whole point of
the P0 extraction — programmatic callers in mcp_infra must not have the
process killed under them).

Monkeypatching convention matches tests/daemon/test_t2_ensure_running.py:
patch subprocess.Popen, os.kill, and the discovery-file helper as needed.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from nexus.commands.daemon import T2EnsureOutcome, _t2_ensure_running_inner


# ---------------------------------------------------------------------------
# Shared helpers (mirror test_t2_ensure_running.py conventions)
# ---------------------------------------------------------------------------


def _discovery_path(config_dir: Path) -> Path:
    from nexus.daemon.t2_daemon import t2_discovery_path
    return t2_discovery_path(config_dir)


def _installed_conexus_version() -> str:
    from importlib.metadata import version as _v
    try:
        return _v("conexus")
    except Exception:
        return "0.0.0"


def _write_discovery(config_dir: Path, pid: int, version: str | None = None) -> None:
    payload = {
        "format_version": 1,
        "uds_path": str(config_dir / "sockets" / "t2.sock"),
        "tcp_host": "127.0.0.1",
        "tcp_port": 12345,
        "pid": pid,
        "daemon_version": version if version is not None else _installed_conexus_version(),
        "start_time": "2026-05-22T19:00:00+00:00",
    }
    dest = _discovery_path(config_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload))


def _seed_wal_db(path: Path) -> None:
    import sqlite3
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE _t (x INTEGER)")
    c.commit()
    c.close()


# ---------------------------------------------------------------------------
# REACHABLE path: already-running-current
# ---------------------------------------------------------------------------


class TestInnerReachable:
    def test_already_running_current_returns_reachable(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Live daemon whose version == installed -> REACHABLE."""
        _write_discovery(tmp_path, pid=os.getpid())
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not spawn when already running"),
        )
        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=5.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE

    def test_already_running_current_quiet_returns_reachable(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Already-running-current path with --quiet still returns REACHABLE."""
        _write_discovery(tmp_path, pid=os.getpid())
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not spawn"),
        )
        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=5.0, quiet=False)
        assert outcome == T2EnsureOutcome.REACHABLE

    def test_cold_spawn_becomes_reachable_returns_reachable(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Cold-spawn path: daemon becomes reachable mid-wait -> REACHABLE."""
        import time as _t

        class _AlivePopen:
            def __init__(self, argv, **_kw):
                pass

            def poll(self):
                return None  # alive throughout

        monkeypatch.setattr(subprocess, "Popen", _AlivePopen)

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE


# ---------------------------------------------------------------------------
# DEFERRED_WRITE_LOCK path: stale daemon alive, WAL write-lock held
# ---------------------------------------------------------------------------


class TestInnerDeferredWriteLock:
    def test_stale_daemon_with_held_lock_returns_deferred_write_lock(
        self, tmp_path, monkeypatch,
    ) -> None:
        import sqlite3
        import threading

        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )

        db = tmp_path / "memory.db"
        _seed_wal_db(db)
        locked = threading.Event()
        release = threading.Event()

        def _holder():
            h = sqlite3.connect(str(db))
            h.execute("PRAGMA busy_timeout=15000")
            h.execute("BEGIN IMMEDIATE")
            h.execute("INSERT INTO _t VALUES (1)")
            locked.set()
            release.wait(timeout=20)
            h.rollback()
            h.close()

        holder = threading.Thread(target=_holder)
        holder.start()
        assert locked.wait(timeout=5)

        monkeypatch.setattr(_daemon, "_T2_CYCLE_DB_PROBE_TIMEOUT_MS", 200)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # daemon "alive"
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not spawn when cycle deferred"),
        )

        try:
            outcome = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        finally:
            release.set()
            holder.join()

        assert outcome == T2EnsureOutcome.DEFERRED_WRITE_LOCK

    def test_deferred_write_lock_does_not_raise_system_exit(
        self, tmp_path, monkeypatch,
    ) -> None:
        """DEFERRED_WRITE_LOCK must return enum, never raise SystemExit."""
        import sqlite3
        import threading

        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )

        db = tmp_path / "memory.db"
        _seed_wal_db(db)
        locked = threading.Event()
        release = threading.Event()

        def _holder():
            h = sqlite3.connect(str(db))
            h.execute("PRAGMA busy_timeout=15000")
            h.execute("BEGIN IMMEDIATE")
            locked.set()
            release.wait(timeout=20)
            h.rollback()
            h.close()

        holder = threading.Thread(target=_holder)
        holder.start()
        assert locked.wait(timeout=5)

        monkeypatch.setattr(_daemon, "_T2_CYCLE_DB_PROBE_TIMEOUT_MS", 200)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

        try:
            result = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        except SystemExit as exc:
            pytest.fail(f"_t2_ensure_running_inner raised SystemExit({exc.code})")
        finally:
            release.set()
            holder.join()

        assert result == T2EnsureOutcome.DEFERRED_WRITE_LOCK


# ---------------------------------------------------------------------------
# DEFERRED_SIGTERM path: stale daemon alive, SIGTERM'd but did not exit
# ---------------------------------------------------------------------------


class TestInnerDeferredSigterm:
    def test_predecessor_outlives_window_returns_deferred_sigterm(
        self, tmp_path, monkeypatch,
    ) -> None:
        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        _seed_wal_db(tmp_path / "memory.db")  # unlocked — probe passes
        monkeypatch.setattr(_daemon, "_T2_CYCLE_EXIT_TIMEOUT", 0.3)

        def _fake_kill(pid, sig):
            if pid != 424242:
                raise ProcessLookupError
            # pid 424242 never dies: sig 0 succeeds forever

        monkeypatch.setattr(os, "kill", _fake_kill)
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._is_t2_daemon_process", lambda pid: True
        )
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not spawn while predecessor alive"),
        )

        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        assert outcome == T2EnsureOutcome.DEFERRED_SIGTERM

    def test_deferred_sigterm_does_not_raise_system_exit(
        self, tmp_path, monkeypatch,
    ) -> None:
        """DEFERRED_SIGTERM must return enum, never raise SystemExit."""
        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        _seed_wal_db(tmp_path / "memory.db")
        monkeypatch.setattr(_daemon, "_T2_CYCLE_EXIT_TIMEOUT", 0.3)
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # never exits
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._is_t2_daemon_process", lambda pid: True
        )
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

        try:
            result = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        except SystemExit as exc:
            pytest.fail(f"_t2_ensure_running_inner raised SystemExit({exc.code})")

        assert result == T2EnsureOutcome.DEFERRED_SIGTERM


# ---------------------------------------------------------------------------
# CRASHLOOP_SUPPRESSED path: crash-loop guard tripped
# ---------------------------------------------------------------------------


class TestInnerCrashloopSuppressed:
    def _trip_crashloop(self, config_dir: Path) -> None:
        """Pre-seed crash-loop sentinel above the cap so the guard fires."""
        import nexus.commands.daemon as _daemon
        import time

        now = time.time()
        for _ in range(_daemon._CRASHLOOP_MAX_RESTARTS):
            _daemon._record_restart(config_dir, now=now)

    def test_crashloop_tripped_returns_crashloop_suppressed(
        self, tmp_path, monkeypatch,
    ) -> None:
        # No live daemon.
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

        self._trip_crashloop(tmp_path)
        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        assert outcome == T2EnsureOutcome.CRASHLOOP_SUPPRESSED

    def test_crashloop_suppressed_does_not_raise_system_exit(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The whole point of P0: crash-loop path must RETURN, not sys.exit."""
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: None)

        self._trip_crashloop(tmp_path)

        try:
            result = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        except SystemExit as exc:
            pytest.fail(
                f"_t2_ensure_running_inner raised SystemExit({exc.code}) "
                f"on the crash-loop path — this is the defect P0 was filed to fix."
            )

        assert result == T2EnsureOutcome.CRASHLOOP_SUPPRESSED

    def test_version_skew_predecessor_dead_then_crashloop_suppressed(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Version-skew path: stale D_old is SIGTERM'd and CONFIRMED DEAD
        (predecessor-exit poll sees it gone), THEN the crash-loop guard fires.

        Validates the invariant the P2 fallback-safety reasoning depends on:
        when CRASHLOOP_SUPPRESSED is returned on the version-skew path, the
        incumbent was reaped before the guard — there is no live writer. The
        sibling tests only exercise the cold-start (no-D_old) route to this
        outcome; this pins the version-skew-then-dead route.
        """
        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        _seed_wal_db(tmp_path / "memory.db")  # unlocked — write-lock probe passes
        monkeypatch.setattr(_daemon, "_T2_CYCLE_EXIT_TIMEOUT", 0.3)

        # os.kill is stateful: pid 424242 is ALIVE during discovery (so the
        # version-skew block is entered), then DEAD after SIGTERM is delivered
        # (so the predecessor-exit poll confirms the reap and falls through).
        state = {"sigtermed": False}

        def _fake_kill(pid, sig):
            if pid != 424242:
                raise ProcessLookupError
            if sig == 0:
                if state["sigtermed"]:
                    raise ProcessLookupError  # confirmed dead post-SIGTERM
                return  # alive during discovery
            state["sigtermed"] = True  # SIGTERM delivered

        monkeypatch.setattr(os, "kill", _fake_kill)
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._is_t2_daemon_process", lambda pid: True
        )

        # Guard pre-tripped: after confirming D_old dead, refuse the respawn.
        self._trip_crashloop(tmp_path)
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not spawn when crash-loop tripped"),
        )

        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        assert outcome == T2EnsureOutcome.CRASHLOOP_SUPPRESSED
        assert state["sigtermed"] is True, "D_old must have been SIGTERM'd (reaped) before the guard fired"


# ---------------------------------------------------------------------------
# SPAWN_FAILED path: cold-spawn process died / never became reachable
# ---------------------------------------------------------------------------


class TestInnerSpawnFailed:
    def test_spawned_process_dies_returns_spawn_failed(
        self, tmp_path, monkeypatch,
    ) -> None:
        import time as _t

        class _DeadPopen:
            returncode = 1

            def __init__(self, argv, **_kw):
                pass

            def poll(self):
                return 1  # already exited

        monkeypatch.setattr(subprocess, "Popen", _DeadPopen)
        monkeypatch.setattr(_t, "sleep", lambda _s: None)

        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.SPAWN_FAILED

    def test_timeout_waiting_for_daemon_returns_spawn_failed(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Daemon spawned, stays alive, but never writes discovery file
        within the timeout -> SPAWN_FAILED."""

        class _AlivePopen:
            def __init__(self, argv, **_kw):
                pass

            def poll(self):
                return None  # alive throughout — never becomes reachable

        monkeypatch.setattr(subprocess, "Popen", _AlivePopen)

        outcome = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        assert outcome == T2EnsureOutcome.SPAWN_FAILED

    def test_spawn_failed_does_not_raise_system_exit_process_died(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Process-died SPAWN_FAILED path must return enum, not sys.exit."""
        import time as _t

        class _DeadPopen:
            returncode = 1

            def __init__(self, argv, **_kw):
                pass

            def poll(self):
                return 1

        monkeypatch.setattr(subprocess, "Popen", _DeadPopen)
        monkeypatch.setattr(_t, "sleep", lambda _s: None)

        try:
            result = _t2_ensure_running_inner(str(tmp_path), timeout=30.0, quiet=True)
        except SystemExit as exc:
            pytest.fail(
                f"_t2_ensure_running_inner raised SystemExit({exc.code}) "
                f"on the spawn-failed (process-died) path."
            )

        assert result == T2EnsureOutcome.SPAWN_FAILED

    def test_spawn_failed_does_not_raise_system_exit_timeout(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Timeout SPAWN_FAILED path must return enum, not sys.exit."""

        class _AlivePopen:
            def __init__(self, argv, **_kw):
                pass

            def poll(self):
                return None

        monkeypatch.setattr(subprocess, "Popen", _AlivePopen)

        try:
            result = _t2_ensure_running_inner(str(tmp_path), timeout=0.2, quiet=True)
        except SystemExit as exc:
            pytest.fail(
                f"_t2_ensure_running_inner raised SystemExit({exc.code}) "
                f"on the spawn-failed (timeout) path."
            )

        assert result == T2EnsureOutcome.SPAWN_FAILED


# ---------------------------------------------------------------------------
# nexus-uybp6: OS-supervisor single-owner routing for cold-spawn
# ---------------------------------------------------------------------------


class TestOsSupervisorRouting:
    """Tests for _autostart_unit_installed() + supervisor-routed cold-spawn.

    All subprocess invocations are recorded via monkeypatch.  No real
    launchctl/systemctl is ever invoked.  Deterministic: tmp_path config dirs.
    """

    # -- test 1: darwin unit installed, kickstart succeeds -> no Popen ----------

    def test_darwin_unit_installed_kickstart_succeeds_no_popen(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Unit installed (darwin), supervisor kickstart succeeds -> REACHABLE, Popen NOT called."""
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        unit_dir = tmp_path / "LaunchAgents"
        unit_dir.mkdir(parents=True)
        (unit_dir / _daemon._T2_PLIST_NAME).write_text("<plist/>")

        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "darwin")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        recorded_runs: list[list[str]] = []

        def _fake_run(argv, **kw):
            recorded_runs.append(list(argv))

            class _Res:
                returncode = 0

            return _Res()

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(
            _subprocess, "Popen",
            lambda *a, **kw: pytest.fail("Popen must NOT be called when supervisor route succeeds"),
        )

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE

        uid = _os.getuid()
        kickstart_args = [a for a in recorded_runs if "kickstart" in a]
        assert kickstart_args, f"expected launchctl kickstart call; got {recorded_runs}"
        assert any(
            f"gui/{uid}/com.nexus.t2" in " ".join(a) for a in kickstart_args
        ), f"kickstart target missing gui/{uid}/com.nexus.t2; recorded: {recorded_runs}"

    # -- test 2: linux unit installed, systemctl start -> no Popen --------------

    def test_linux_unit_installed_systemctl_start_no_popen(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Unit installed (linux), systemctl --user start succeeds -> REACHABLE, Popen NOT called."""
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        unit_dir = tmp_path / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        (unit_dir / _daemon._T2_SERVICE_NAME).write_text("[Unit]")

        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "linux")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        recorded_runs: list[list[str]] = []

        def _fake_run(argv, **kw):
            recorded_runs.append(list(argv))

            class _Res:
                returncode = 0

            return _Res()

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(
            _subprocess, "Popen",
            lambda *a, **kw: pytest.fail("Popen must NOT be called when supervisor route succeeds"),
        )

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE

        systemctl_calls = [a for a in recorded_runs if "systemctl" in a]
        assert systemctl_calls, f"expected systemctl call; got {recorded_runs}"
        assert any(
            "--user" in a and "start" in a and _daemon._T2_SERVICE_NAME in " ".join(a)
            for a in systemctl_calls
        ), f"systemctl --user start nexus-t2.service not found; recorded: {recorded_runs}"

    # -- test 3a: unit installed, supervisor returns non-zero -> Popen fallback --

    def test_unit_installed_supervisor_nonzero_popen_fallback(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Unit installed (darwin), kickstart returns non-zero -> Popen fallback -> REACHABLE."""
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        unit_dir = tmp_path / "LaunchAgents"
        unit_dir.mkdir(parents=True)
        (unit_dir / _daemon._T2_PLIST_NAME).write_text("<plist/>")

        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "darwin")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        recorded_runs: list[list[str]] = []

        def _fake_run(argv, **kw):
            recorded_runs.append(list(argv))

            class _Res:
                returncode = 1  # every supervisor cmd fails

            return _Res()

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        popen_called = {"called": False}

        class _AlivePopen:
            def __init__(self, argv, **_kw):
                popen_called["called"] = True

            def poll(self):
                return None

        monkeypatch.setattr(_subprocess, "Popen", _AlivePopen)

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE
        # Non-vacuous direction (critic SIG-1): the supervisor must have been
        # ATTEMPTED before the fallback — pre-change code went straight to
        # Popen and would pass the two assertions below it.
        assert any("kickstart" in a for a in recorded_runs), (
            f"supervisor must be attempted before Popen fallback; got {recorded_runs}"
        )
        assert popen_called["called"], "Popen fallback must be invoked when supervisor returns non-zero"

    # -- test 3b: unit installed, supervisor raises -> Popen fallback -----------

    def test_unit_installed_supervisor_raises_popen_fallback(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Unit installed (darwin), launchctl not found (raises) -> Popen fallback."""
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        unit_dir = tmp_path / "LaunchAgents"
        unit_dir.mkdir(parents=True)
        (unit_dir / _daemon._T2_PLIST_NAME).write_text("<plist/>")

        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "darwin")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        recorded_runs: list[list[str]] = []

        def _fake_run(argv, **kw):
            recorded_runs.append(list(argv))
            raise FileNotFoundError("launchctl not found")

        monkeypatch.setattr(_subprocess, "run", _fake_run)

        popen_called = {"called": False}

        class _AlivePopen:
            def __init__(self, argv, **_kw):
                popen_called["called"] = True

            def poll(self):
                return None

        monkeypatch.setattr(_subprocess, "Popen", _AlivePopen)

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE
        assert any("kickstart" in a for a in recorded_runs), (
            f"supervisor must be attempted before Popen fallback; got {recorded_runs}"
        )
        assert popen_called["called"], "Popen fallback must be invoked when launchctl raises"

    # -- test 4: unit NOT installed -> Popen path unchanged (regression) --------

    def test_no_unit_installed_uses_popen(
        self, tmp_path, monkeypatch,
    ) -> None:
        """No unit file present -> straight Popen path (regression: unchanged)."""
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        unit_dir = tmp_path / "LaunchAgents"
        unit_dir.mkdir(parents=True)
        # Do NOT create the plist file.

        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "darwin")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        def _fail_run(argv, **kw):
            pytest.fail("subprocess.run (supervisor) must NOT be called when unit absent")

        monkeypatch.setattr(_subprocess, "run", _fail_run)

        popen_called = {"called": False}

        class _AlivePopen:
            def __init__(self, argv, **_kw):
                popen_called["called"] = True

            def poll(self):
                return None

        monkeypatch.setattr(_subprocess, "Popen", _AlivePopen)

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE
        assert popen_called["called"], "Popen must be used when unit not installed"

    # -- test 5: version-skew + unit installed -> supervisor route after SIGTERM -

    def test_version_skew_with_unit_installed_uses_supervisor(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Version-skew path: stale daemon SIGTERM'd+dead, then cold-spawn via supervisor."""
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        _seed_wal_db(tmp_path / "memory.db")
        monkeypatch.setattr(_daemon, "_T2_CYCLE_EXIT_TIMEOUT", 5.0)

        unit_dir = tmp_path / "LaunchAgents"
        unit_dir.mkdir(parents=True)
        (unit_dir / _daemon._T2_PLIST_NAME).write_text("<plist/>")
        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "darwin")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        # os.kill: pid 424242 alive until SIGTERM, then dead.
        # Any other pid (including the test process's real pid used in the
        # discovery file written by _fake_sleep) must be treated as alive.
        state = {"sigtermed": False}

        def _fake_kill(pid, sig):
            if pid == 424242:
                if sig == 0:
                    if state["sigtermed"]:
                        raise ProcessLookupError  # dead post-SIGTERM
                    return
                state["sigtermed"] = True
                return
            # Any other pid (real process in test): alive for kill(0) probe.
            if sig == 0:
                return  # alive
            # pass other signals through silently

        monkeypatch.setattr("os.kill", _fake_kill)
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._is_t2_daemon_process", lambda pid: True
        )

        recorded_runs: list[list[str]] = []

        def _fake_run(argv, **kw):
            recorded_runs.append(list(argv))

            class _Res:
                returncode = 0

            return _Res()

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(
            _subprocess, "Popen",
            lambda *a, **kw: pytest.fail("Popen must NOT be called when supervisor route succeeds"),
        )

        sleep_state = {"n": 0}

        def _fake_sleep(_s):
            sleep_state["n"] += 1
            if sleep_state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE
        assert state["sigtermed"], "stale daemon must have been SIGTERM'd"
        uid = _os.getuid()
        kickstart_args = [a for a in recorded_runs if "kickstart" in a]
        assert kickstart_args, f"expected launchctl kickstart; got {recorded_runs}"
        assert any(f"gui/{uid}/com.nexus.t2" in " ".join(a) for a in kickstart_args)

    # -- test 6 (audit advisory): darwin not-loaded -> bootstrap then kickstart --

    def test_darwin_not_loaded_bootstrap_then_kickstart(
        self, tmp_path, monkeypatch,
    ) -> None:
        """darwin: kickstart returns non-zero (unit not bootstrapped) -> fallback to Popen.

        The audit advisory notes this exercises the Popen-fallback clause for
        the residual failure mode: if bootstrap-then-kickstart inside the
        supervisor path also fails, we fall back to Popen rather than leaving
        zero daemons.  The implementation must NOT require a successful
        bootstrap+kickstart; it must fall back to Popen on any failure.
        """
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        unit_dir = tmp_path / "LaunchAgents"
        unit_dir.mkdir(parents=True)
        (unit_dir / _daemon._T2_PLIST_NAME).write_text("<plist/>")

        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "darwin")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        # All supervisor attempts fail (bootstrap + kickstart both non-zero)
        recorded_runs: list[list[str]] = []

        def _all_fail(argv, **kw):
            recorded_runs.append(list(argv))

            class _Res:
                returncode = 1

            return _Res()

        monkeypatch.setattr(_subprocess, "run", _all_fail)

        popen_called = {"called": False}

        class _AlivePopen:
            def __init__(self, argv, **_kw):
                popen_called["called"] = True

            def poll(self):
                return None

        monkeypatch.setattr(_subprocess, "Popen", _AlivePopen)

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE
        # The full not-loaded sequence must have been attempted before the
        # fallback: kickstart -> bootstrap -> (kickstart retry suppressed by
        # bootstrap failure) -> Popen (critic SIG-1).
        assert any("kickstart" in a for a in recorded_runs), (
            f"kickstart must be attempted; got {recorded_runs}"
        )
        assert any("bootstrap" in a for a in recorded_runs), (
            f"bootstrap must be attempted after failed kickstart; got {recorded_runs}"
        )
        assert popen_called["called"], "Popen fallback must fire when all supervisor cmds fail"

    # -- test 7 (review LOW-1): bootstrap-then-kickstart POSITIVE path -----------

    def test_darwin_bootstrap_then_kickstart_succeeds_no_popen(
        self, tmp_path, monkeypatch,
    ) -> None:
        """darwin: first kickstart non-zero, bootstrap OK, retry kickstart OK -> no Popen."""
        import time as _t
        import subprocess as _subprocess
        import os as _os
        import nexus.commands.daemon as _daemon

        unit_dir = tmp_path / "LaunchAgents"
        unit_dir.mkdir(parents=True)
        (unit_dir / _daemon._T2_PLIST_NAME).write_text("<plist/>")

        monkeypatch.setattr(_daemon, "_autostart_platform", lambda: "darwin")
        monkeypatch.setattr(_daemon, "_autostart_install_dir", lambda: unit_dir)
        # Supervisor routing is gated on the UNQUALIFIED default config
        # dir (no flag, no env): clear the env override and make the
        # default resolve to the test tmp_path so the gate passes.
        monkeypatch.delenv("NEXUS_CONFIG_DIR", raising=False)
        monkeypatch.setattr(_daemon, "nexus_config_dir", lambda: tmp_path)

        recorded_runs: list[list[str]] = []

        def _fake_run(argv, **kw):
            recorded_runs.append(list(argv))

            class _Res:
                # First kickstart fails (unit not loaded); bootstrap and the
                # kickstart retry succeed.
                returncode = 1 if len(recorded_runs) == 1 else 0

            return _Res()

        monkeypatch.setattr(_subprocess, "run", _fake_run)
        monkeypatch.setattr(
            _subprocess, "Popen",
            lambda *a, **kw: pytest.fail(
                "Popen must NOT be called when bootstrap-then-kickstart succeeds"
            ),
        )

        state = {"n": 0}

        def _fake_sleep(_s):
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, _os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        outcome = _t2_ensure_running_inner(None, timeout=30.0, quiet=True)
        assert outcome == T2EnsureOutcome.REACHABLE
        flat = [" ".join(a) for a in recorded_runs]
        assert len(recorded_runs) == 3, f"expected kickstart, bootstrap, kickstart; got {flat}"
        assert "kickstart" in flat[0]
        assert "bootstrap" in flat[1]
        assert "kickstart" in flat[2]
