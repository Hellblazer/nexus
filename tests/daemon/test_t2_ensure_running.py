# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P6 follow-up (nexus-qnrvn): ``nx daemon t2 ensure-running``.

The command is idempotent: silent no-op if a daemon is already running
on the named config_dir, otherwise spawn a fresh one in the background
and poll the discovery file until the new daemon is reachable (or the
timeout expires).

Spawn is mocked — we exercise the discovery-file probe + the spawn-
argv shape + the timeout path without actually forking a daemon.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


def _discovery_path(config_dir: Path) -> Path:
    """Mirror ``nexus.daemon.t2_daemon.t2_discovery_path`` — the
    discovery file is keyed by the current UID, not a hardcoded
    501. macOS dev UIDs are usually 501; Linux GHA runner UIDs are
    1001. Hardcoding either fails on the other.
    """
    from nexus.daemon.t2_daemon import t2_discovery_path
    return t2_discovery_path(config_dir)


def _installed_conexus_version() -> str:
    from importlib.metadata import version as _v

    try:
        return _v("conexus")
    except Exception:
        return "0.0.0"


def _write_discovery(config_dir: Path, pid: int, version: str | None = None) -> None:
    """Pre-seed a discovery file shaped like the real daemon writes.

    ``version`` defaults to the installed conexus version so the
    "already running, current" path is exercised; pass an older string
    to simulate a stale daemon that ensure-running should cycle
    (nexus-5ldk1).
    """
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


class TestEnsureRunning:
    def test_already_running_is_idempotent_silent_no_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Seed the discovery file with the current process's PID — the
        # probe checks via os.kill(pid, 0) which succeeds for any
        # running process the caller can signal.
        _write_discovery(tmp_path, os.getpid())

        spawn_calls: list[list[str]] = []

        def _no_spawn(argv, **_kw):  # noqa: ANN001
            spawn_calls.append(argv)
            raise AssertionError("ensure-running must not spawn when daemon is alive")

        monkeypatch.setattr(subprocess, "Popen", _no_spawn)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "already running" in result.output
        assert spawn_calls == []

    def test_stale_version_daemon_is_cycled_then_respawned(
        self, tmp_path, monkeypatch,
    ) -> None:
        """nexus-5ldk1: a LIVE daemon whose version != installed tool is
        stale (froze old code at start). ensure-running must SIGTERM it
        and respawn a current one, rather than leaving the stale daemon."""
        import signal as _signal

        # Live daemon at an older version than the installed tool.
        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )

        # Stateful os.kill: pid is alive until it receives SIGTERM, then
        # dead. Guards the test process: we never signal a real pid.
        state = {"terminated": False}

        def _fake_kill(pid, sig):  # noqa: ANN001
            if pid != 424242:
                raise ProcessLookupError
            if sig == 0:
                if state["terminated"]:
                    raise ProcessLookupError
                return
            if sig == _signal.SIGTERM:
                state["terminated"] = True
                return

        monkeypatch.setattr(os, "kill", _fake_kill)

        spawn_calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawn_calls.append(argv)

            def poll(self):  # real Popen exposes poll(); None = alive/migrating
                return None

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        # Stale daemon was SIGTERM'd and a respawn was attempted.
        assert state["terminated"] is True, "stale daemon was not cycled"
        assert len(spawn_calls) == 1, "no respawn after cycling stale daemon"
        assert "stale" in result.output.lower()

    def test_current_version_daemon_not_cycled(
        self, tmp_path, monkeypatch,
    ) -> None:
        """A live daemon whose version == installed tool is left alone."""
        _write_discovery(tmp_path, pid=424242, version="9.9.9-installed")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # pid "alive"
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not cycle a current daemon"),
        )
        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running", "--config-dir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output
        assert "already running" in result.output

    def test_already_running_quiet_suppresses_output(
        self, tmp_path, monkeypatch,
    ) -> None:
        _write_discovery(tmp_path, os.getpid())
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not spawn"),
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--quiet"],
        )
        assert result.exit_code == 0
        assert result.output == ""

    def test_stale_discovery_pid_dead_triggers_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        """PID 1 is init (always alive). Use PID 2**31 - 1 which can't
        be a real PID on any supported platform — os.kill(pid, 0) raises
        ProcessLookupError, and the probe treats that as 'daemon dead'."""
        _write_discovery(tmp_path, 2**31 - 1)

        spawn_calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawn_calls.append(argv)

            def poll(self):  # real Popen exposes poll(); None = alive/migrating
                return None

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        runner = CliRunner()
        # timeout=0.2 — we expect the spawn to fire but the new daemon
        # won't actually start (Popen is mocked), so the timeout path
        # exits 1.
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert result.exit_code == 1
        assert len(spawn_calls) == 1
        argv = spawn_calls[0]
        # The spawn invokes the nx CLI's ``daemon t2 start`` subcommand
        # with the same --config-dir the operator passed in. The first
        # element is the resolved nx binary (or python -m fallback) so
        # we tail-match on the well-known suffix.
        assert argv[-4:] == ["daemon", "t2", "start", "--config-dir"] or \
               argv[-5:] == ["daemon", "t2", "start", "--config-dir", str(tmp_path)]

    def test_missing_discovery_file_triggers_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        # No discovery file pre-seeded; ensure-running must spawn.
        spawn_calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawn_calls.append(argv)

            def poll(self):  # real Popen exposes poll(); None = alive/migrating
                return None

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        # Spawn fired; mock didn't produce a discovery file so timeout=1.
        assert result.exit_code == 1
        assert len(spawn_calls) == 1
        assert "did not become reachable" in result.output

    def test_corrupt_discovery_file_triggers_spawn(
        self, tmp_path, monkeypatch,
    ) -> None:
        # Discovery file present but not valid JSON — probe treats as dead.
        dest = _discovery_path(tmp_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("not json {{{")

        spawn_calls: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda argv, **kw: spawn_calls.append(argv) or type(
                "P", (), {"__init__": lambda self: None, "poll": lambda self: None}
            )(),
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert result.exit_code == 1
        assert len(spawn_calls) == 1

    def test_timeout_message_names_log_paths(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The timeout warning must point the operator at the launchd /
        systemd log so they can self-diagnose without spelunking."""
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda argv, **kw: type("P", (), {"__init__": lambda self: None, "poll": lambda self: None})(),
        )
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert result.exit_code == 1
        # Both platform-specific log hints appear (the command doesn't
        # know which platform the operator is on, so it names both).
        assert "nexus-t2.err" in result.output
        assert "journalctl --user -u nexus-t2.service" in result.output

    def test_spawn_reachable_after_migration_delay_no_warning(
        self, tmp_path, monkeypatch,
    ) -> None:
        """nexus-u3mfr: a cold-start daemon binds only AFTER its multi-second
        startup migration. The wait must keep polling while the spawned
        child is alive and succeed (no spurious warning / exit-1) once the
        daemon becomes reachable, even though reachability lagged the spawn.
        """
        import time as _t

        # Spawned child stays alive (poll() == None) throughout.
        class _AlivePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                pass

            def poll(self):
                return None

        monkeypatch.setattr(subprocess, "Popen", _AlivePopen)

        # Simulate the daemon binding mid-wait: on the 2nd poll interval the
        # discovery file appears (migration finished, socket bound). Patch
        # sleep so the test doesn't actually wait.
        state = {"n": 0}
        real_sleep = _t.sleep

        def _fake_sleep(_s):  # noqa: ANN001
            state["n"] += 1
            if state["n"] == 2:
                _write_discovery(tmp_path, os.getpid())

        monkeypatch.setattr(_t, "sleep", _fake_sleep)

        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "30"],
        )
        assert result.exit_code == 0, result.output
        assert "reachable" in result.output
        assert "did not become reachable" not in result.output
        assert "exited" not in result.output

    def test_spawn_child_dies_fails_fast(
        self, tmp_path, monkeypatch,
    ) -> None:
        """nexus-u3mfr: if the spawned daemon process EXITS without becoming
        reachable (e.g. a failed migration), ensure-running reports the exit
        code and fails fast — it does NOT wait out the full --timeout budget
        on a corpse, and the message names the exit, not 'still alive'."""
        import time as _t

        class _DeadPopen:
            returncode = 1

            def __init__(self, argv, **_kw):  # noqa: ANN001
                pass

            def poll(self):
                return 1  # exited

        monkeypatch.setattr(subprocess, "Popen", _DeadPopen)
        # No-op sleep so a hypothetical budget-wait would be fast too; the
        # point is fail-fast returns on the first poll, before the budget.
        monkeypatch.setattr(_t, "sleep", lambda _s: None)

        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "30"],
        )
        assert result.exit_code == 1
        assert "exited (code 1)" in result.output
        assert "did not become reachable" not in result.output


# ---------------------------------------------------------------------------
# RDR-128 P0b (RF-4): pre-cycle DB-acquirability interlock
# ---------------------------------------------------------------------------


def _seed_wal_db(path) -> None:
    """Create a WAL-mode memory.db so a competing writer lock is meaningful."""
    import sqlite3

    c = sqlite3.connect(str(path))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE _t (x INTEGER)")
    c.commit()
    c.close()


class TestDbProbe:
    """The bounded ``_t2_db_write_lock_acquirable`` probe."""

    def test_missing_file_is_acquirable(self, tmp_path) -> None:
        from nexus.commands.daemon import _t2_db_write_lock_acquirable

        assert _t2_db_write_lock_acquirable(
            tmp_path / "absent.db", timeout_ms=200
        ) is True

    def test_free_lock_is_acquirable(self, tmp_path) -> None:
        from nexus.commands.daemon import _t2_db_write_lock_acquirable

        db = tmp_path / "memory.db"
        _seed_wal_db(db)
        assert _t2_db_write_lock_acquirable(db, timeout_ms=200) is True

    def test_held_writer_lock_is_not_acquirable_and_is_bounded(
        self, tmp_path,
    ) -> None:
        """A competing ``BEGIN IMMEDIATE`` makes the probe return False
        within ~timeout_ms — it waits the bounded window, never forever."""
        import sqlite3
        import threading
        import time

        from nexus.commands.daemon import _t2_db_write_lock_acquirable

        db = tmp_path / "memory.db"
        _seed_wal_db(db)

        locked = threading.Event()
        release = threading.Event()

        def _holder() -> None:
            h = sqlite3.connect(str(db))
            h.execute("PRAGMA busy_timeout=10000")
            h.execute("BEGIN IMMEDIATE")
            h.execute("INSERT INTO _t VALUES (1)")
            locked.set()
            release.wait(timeout=15)
            h.rollback()
            h.close()

        holder = threading.Thread(target=_holder)
        holder.start()
        assert locked.wait(timeout=5), "holder failed to take the writer lock"

        start = time.monotonic()
        acquirable = _t2_db_write_lock_acquirable(db, timeout_ms=200)
        elapsed = time.monotonic() - start

        release.set()
        holder.join()

        assert acquirable is False
        assert elapsed < 5.0, "probe must be bounded by busy_timeout, not hang"


class TestCycleInterlock:
    """ensure-running must defer the version-cycle when memory.db is locked,
    and proceed when it is free."""

    def test_stale_daemon_not_cycled_when_db_lock_held(
        self, tmp_path, monkeypatch,
    ) -> None:
        import signal as _signal
        import sqlite3
        import threading

        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )

        # Real, locked memory.db at the config dir.
        db = tmp_path / "memory.db"
        _seed_wal_db(db)
        locked = threading.Event()
        release = threading.Event()

        def _holder() -> None:
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

        # Fast probe so the deferral path returns quickly.
        monkeypatch.setattr(_daemon, "_T2_CYCLE_DB_PROBE_TIMEOUT_MS", 200)

        signals: list[tuple[int, int]] = []

        def _fake_kill(pid, sig):  # noqa: ANN001
            if sig == 0:
                if pid == 424242:
                    return  # alive
                raise ProcessLookupError
            signals.append((pid, sig))

        monkeypatch.setattr(os, "kill", _fake_kill)
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail("must not respawn when cycle deferred"),
        )

        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )

        release.set()
        holder.join()

        # The healthy-but-stale daemon was LEFT UP (never SIGTERM'd).
        assert not any(sig == _signal.SIGTERM for _, sig in signals), (
            "stale daemon must NOT be cycled while memory.db is locked"
        )
        assert "cycle deferred" in result.output.lower()
        assert result.exit_code == 0

    def test_cycle_aborts_when_predecessor_outlives_window(
        self, tmp_path, monkeypatch,
    ) -> None:
        """RDR-129 A2 (nexus-kwqhd): if the SIGTERM'd stale daemon does not
        exit within the cycle window, ensure-running ABORTS — it does not
        cold-spawn a replacement (which would EAGAIN on the still-held spawn
        lock and leave zero daemons). The stale-but-working daemon is left up
        (RF-4)."""
        import signal as _signal

        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        _seed_wal_db(tmp_path / "memory.db")  # exists, unlocked → probe passes
        monkeypatch.setattr(_daemon, "_T2_CYCLE_EXIT_TIMEOUT", 0.3)

        sigterms: list[int] = []

        def _fake_kill(pid, sig):  # noqa: ANN001
            if pid != 424242:
                raise ProcessLookupError
            if sig == _signal.SIGTERM:
                sigterms.append(pid)
                return
            if sig == 0:
                return  # predecessor never exits

        monkeypatch.setattr(os, "kill", _fake_kill)
        # The still-alive pid genuinely looks like a t2 daemon (PID-reuse guard).
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._is_t2_daemon_process", lambda pid: True
        )
        monkeypatch.setattr(
            subprocess, "Popen",
            lambda *a, **kw: pytest.fail(
                "must NOT spawn while the predecessor is still alive"
            ),
        )

        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert sigterms == [424242], "predecessor should have been SIGTERM'd"
        assert "cycle aborted" in result.output.lower()
        assert result.exit_code == 0  # never trade a working daemon for none

    def test_cycle_waits_for_pid_exit_then_spawns(
        self, tmp_path, monkeypatch,
    ) -> None:
        """The cycle wait polls PID liveness, not the discovery file: even
        after stop() unlinks the discovery file early, ensure-running waits
        for the predecessor to actually exit and then cold-spawns exactly one
        replacement (never zero)."""
        import signal as _signal

        import nexus.commands.daemon as _daemon

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        _seed_wal_db(tmp_path / "memory.db")
        monkeypatch.setattr(_daemon, "_T2_CYCLE_EXIT_TIMEOUT", 5.0)

        disc = _discovery_path(tmp_path)
        state = {"polls": 0}

        def _fake_kill(pid, sig):  # noqa: ANN001
            if pid != 424242:
                raise ProcessLookupError
            if sig == _signal.SIGTERM:
                # stop() unlinks the discovery file BEFORE the process exits.
                disc.unlink(missing_ok=True)
                return
            if sig == 0:
                state["polls"] += 1
                if state["polls"] >= 2:
                    raise ProcessLookupError  # predecessor finally exits
                return

        monkeypatch.setattr(os, "kill", _fake_kill)
        monkeypatch.setattr(
            "nexus.daemon.t2_daemon._is_t2_daemon_process", lambda pid: True
        )

        spawned: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawned.append(argv)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)

        CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )
        assert state["polls"] >= 2, (
            "should have polled PID liveness past the unlinked discovery file"
        )
        assert len(spawned) == 1, (
            "exactly one replacement spawned after the predecessor exited"
        )

    def test_stale_daemon_cycled_when_db_lock_free(
        self, tmp_path, monkeypatch,
    ) -> None:
        """Mirror of the deferral test: an existing but UNLOCKED memory.db
        lets the probe pass, so the stale daemon is cycled as before."""
        import signal as _signal

        _write_discovery(tmp_path, pid=424242, version="0.0.1-stale")
        monkeypatch.setattr(
            "importlib.metadata.version", lambda _name: "9.9.9-installed"
        )
        _seed_wal_db(tmp_path / "memory.db")  # exists, not locked

        state = {"terminated": False}

        def _fake_kill(pid, sig):  # noqa: ANN001
            if pid != 424242:
                raise ProcessLookupError
            if sig == 0:
                if state["terminated"]:
                    raise ProcessLookupError
                return
            if sig == _signal.SIGTERM:
                state["terminated"] = True

        monkeypatch.setattr(os, "kill", _fake_kill)

        spawned: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawned.append(argv)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)

        result = CliRunner().invoke(
            main,
            ["daemon", "t2", "ensure-running",
             "--config-dir", str(tmp_path), "--timeout", "0.2"],
        )

        assert state["terminated"] is True, "free lock should allow the cycle"
        assert len(spawned) == 1
        assert "cycling to current" in result.output.lower()


class TestEnsureRunningElectionLock:
    """RDR-140 P2.2 (nexus-fkhe2): single-flight election-lock helpers and the
    on-timeout graceful-degradation path."""

    def test_acquire_election_lock_times_out_when_held(self, tmp_path) -> None:
        import fcntl

        from nexus.commands.daemon import (
            _acquire_election_lock,
            _election_lock_path_for_db,
        )

        db = tmp_path / "memory.db"
        lock_path = _election_lock_path_for_db(db)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            # A second exclusive lock on the held file must not be granted; the
            # bounded wait returns None rather than blocking forever.
            assert _acquire_election_lock(db, 0.2) is None
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

    def test_acquire_release_roundtrip_and_reacquire(self, tmp_path) -> None:
        from nexus.commands.daemon import (
            _acquire_election_lock,
            _release_election_lock,
        )

        db = tmp_path / "memory.db"
        fd = _acquire_election_lock(db, 1.0)
        assert isinstance(fd, int)
        _release_election_lock(fd)
        # Re-acquirable once released.
        fd2 = _acquire_election_lock(db, 1.0)
        assert isinstance(fd2, int)
        _release_election_lock(fd2)
        # Releasing None is a no-op (does not raise).
        _release_election_lock(None)

    def test_election_wait_covers_worst_case_hold(self) -> None:
        from nexus.commands.daemon import (
            _T2_CYCLE_DB_PROBE_TIMEOUT_MS,
            _T2_CYCLE_EXIT_TIMEOUT,
            _election_wait_for,
        )

        # The waiter budget must exceed the holder's worst-case hold (stale
        # write-lock probe + predecessor-exit poll + reachability poll), or the
        # herd reappears on timeout.
        timeout = 15.0
        hold = _T2_CYCLE_DB_PROBE_TIMEOUT_MS / 1000.0 + _T2_CYCLE_EXIT_TIMEOUT + timeout
        assert _election_wait_for(timeout) > hold

    def test_election_timeout_proceeds_to_spawn_unguarded(
        self, tmp_path, monkeypatch,
    ) -> None:
        """When the election lock is held by another stack and the wait
        elapses, ensure-running degrades to an unguarded spawn (the daemon
        spawn lock remains the backstop) and warns — never deadlocks."""
        import fcntl

        from nexus.commands import daemon as daemon_mod

        # No discovery file pre-seeded => no live daemon to attach to.
        db = tmp_path / "memory.db"
        lock_path = daemon_mod._election_lock_path_for_db(db)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)

        # Shrink the election wait so the timeout path fires fast.
        monkeypatch.setattr(daemon_mod, "_election_wait_for", lambda _t: 0.2)

        spawn_calls: list[list[str]] = []

        class _FakePopen:
            def __init__(self, argv, **_kw):  # noqa: ANN001
                spawn_calls.append(argv)

            def poll(self):
                return None  # alive/migrating; mocked daemon never reachable

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        try:
            result = CliRunner().invoke(
                main,
                ["daemon", "t2", "ensure-running",
                 "--config-dir", str(tmp_path), "--timeout", "0.2"],
            )
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)

        # Proceeded unguarded (one spawn), did not deadlock, warned.
        assert len(spawn_calls) == 1
        assert result.exit_code == 1  # mocked daemon never becomes reachable
        err = result.stderr_bytes.decode() if result.stderr_bytes else ""
        assert "election-lock wait timed out" in (result.output + err)
