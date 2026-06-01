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
