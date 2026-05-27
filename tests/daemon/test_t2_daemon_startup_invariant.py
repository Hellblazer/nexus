# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-120 P3b.B (nexus-0ax54): daemon-startup invariant test.

Recast of the previously-skipped ``nexus-9eaz`` cross-process
migration race test. The old framing asked "two processes race
``apply_pending``, exactly one wins"; that race surface was structural
to library mode and impossible to reproduce reliably on darwin GHA
runners.

P3b makes the T2 daemon the sole ``apply_pending`` caller (see
``nexus-e9x4l``). The cross-process race is gone by construction —
the daemon's ``_acquire_spawn_lock`` (fcntl ``LOCK_EX | LOCK_NB`` on
``<config_dir>/t2_spawn.lock``) is the mutual-exclusion mechanism.

This file pins the new invariant: **the daemon refuses a second start
against the same path while one is running, and fails loud with a
clear error message naming the spawn lock**. The companion concurrent-
``apply_pending`` tests in ``tests/test_migrations.py`` exercise the
in-process ``_upgrade_lock`` primitive that still guards intra-process
construction; this file covers the cross-process invariant.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
from pathlib import Path

import pytest


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """Short config_dir under /tmp; macOS AF_UNIX paths cap at 104
    chars and pytest's tmp_path already eats ~75 of those."""
    cd = Path(tempfile.mkdtemp(prefix="nxt2inv-", dir="/tmp"))
    yield cd
    shutil.rmtree(cd, ignore_errors=True)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "memory.db"


def _run_daemon_in_thread(daemon, ready, stop) -> None:
    async def _main() -> None:
        await daemon.start()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(0.05)
        await daemon.stop()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_main())
    finally:
        loop.close()


class TestDaemonRefusesSecondStartAgainstSamePath:
    """The nexus-9eaz invariant in its P3b form."""

    def test_second_start_same_config_dir_same_db_path_fails_loud(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        """Two daemons with the SAME config_dir AND SAME db_path.
        The second start must raise T2DaemonError; the error message
        must name the spawn lock so an operator can diagnose without
        reading code.
        """
        from nexus.daemon.t2_daemon import T2Daemon, T2DaemonError

        first = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready = threading.Event()
        stop = threading.Event()
        thread = threading.Thread(
            target=_run_daemon_in_thread, args=(first, ready, stop),
        )
        thread.start()
        try:
            assert ready.wait(timeout=10.0), "first daemon did not start"

            second = T2Daemon(config_dir=config_dir, db_path=db_path)
            with pytest.raises(T2DaemonError) as excinfo:
                asyncio.run(second.start())
            msg = str(excinfo.value)
            assert "spawn lock" in msg, (
                f"expected error to name the spawn lock; got {msg!r}"
            )
            assert "refusing to start a second instance" in msg
        finally:
            stop.set()
            thread.join(timeout=10.0)
            assert not thread.is_alive(), "first daemon did not stop"

    def test_spawn_lock_held_until_release_not_just_stop(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        """RDR-129 A2 (nexus-kwqhd): ``stop()`` no longer releases the spawn
        lock — the lock is held for the process lifetime and dropped by the OS
        on exit. This closes the released-but-alive window where a respawn
        could acquire the freed lock while the predecessor was still draining.

        In-process (thread-based) the OS never drops the lock, so a second
        start on the same path after ``stop()`` must FAIL; only an explicit
        ``_release_spawn_lock()`` (the process-exit equivalent) frees the next
        start. This is the inverse of the prior contract, which released on
        ``stop()``.
        """
        from nexus.daemon.t2_daemon import T2Daemon, T2DaemonError

        first = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready1 = threading.Event()
        stop1 = threading.Event()
        t1 = threading.Thread(
            target=_run_daemon_in_thread, args=(first, ready1, stop1),
        )
        t1.start()
        assert ready1.wait(timeout=10.0)
        stop1.set()
        t1.join(timeout=10.0)
        assert not t1.is_alive()

        # stop() ran but the lock is still held by this process — a
        # same-process restart on the same path must fail loud.
        second = T2Daemon(config_dir=config_dir, db_path=db_path)
        with pytest.raises(T2DaemonError) as excinfo:
            asyncio.run(second.start())
        assert "spawn lock" in str(excinfo.value)

        # Explicit release (what the OS does on real process exit) frees the
        # lock; a fresh start then succeeds.
        first._release_spawn_lock()
        third = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready3 = threading.Event()
        stop3 = threading.Event()
        t3 = threading.Thread(
            target=_run_daemon_in_thread, args=(third, ready3, stop3),
        )
        t3.start()
        try:
            assert ready3.wait(timeout=10.0), (
                "start after explicit lock release should succeed"
            )
        finally:
            stop3.set()
            t3.join(timeout=10.0)
            third._release_spawn_lock()  # tidy: drop the lock fd for this pid

    def test_second_start_different_config_dir_same_db_path_fails_loud(
        self, db_path: Path,
    ) -> None:
        """Cross-config_dir collision on the same data file: the
        db_path-scoped spawn lock (RDR-120 P3b code-review item 2)
        must prevent two daemons against the same db_path from
        running concurrently even when started with different
        config_dirs.
        """
        import shutil
        import tempfile

        from nexus.daemon.t2_daemon import T2Daemon, T2DaemonError

        cd1 = Path(tempfile.mkdtemp(prefix="nxt2inv-a-", dir="/tmp"))
        cd2 = Path(tempfile.mkdtemp(prefix="nxt2inv-b-", dir="/tmp"))
        try:
            first = T2Daemon(config_dir=cd1, db_path=db_path)
            ready = threading.Event()
            stop = threading.Event()
            thread = threading.Thread(
                target=_run_daemon_in_thread, args=(first, ready, stop),
            )
            thread.start()
            try:
                assert ready.wait(timeout=10.0), "first daemon did not start"

                second = T2Daemon(config_dir=cd2, db_path=db_path)
                with pytest.raises(T2DaemonError) as excinfo:
                    asyncio.run(second.start())
                msg = str(excinfo.value)
                assert "db_path spawn lock" in msg, (
                    f"expected db_path-scoped lock error; got {msg!r}"
                )
                assert "same data file" in msg
            finally:
                stop.set()
                thread.join(timeout=10.0)
        finally:
            shutil.rmtree(cd1, ignore_errors=True)
            shutil.rmtree(cd2, ignore_errors=True)

    def test_spawn_lock_error_includes_lock_path(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        """Operator-debuggability: the failure message must include
        the spawn-lock file path so the diagnostic is self-contained.
        """
        from nexus.daemon.t2_daemon import (
            T2Daemon, T2DaemonError, _SPAWN_LOCK_FILE,
        )

        first = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready = threading.Event()
        stop = threading.Event()
        thread = threading.Thread(
            target=_run_daemon_in_thread, args=(first, ready, stop),
        )
        thread.start()
        try:
            assert ready.wait(timeout=10.0)

            second = T2Daemon(config_dir=config_dir, db_path=db_path)
            with pytest.raises(T2DaemonError) as excinfo:
                asyncio.run(second.start())
            expected_path = str(config_dir / _SPAWN_LOCK_FILE)
            assert expected_path in str(excinfo.value), (
                f"expected {expected_path!r} in error message; "
                f"got {str(excinfo.value)!r}"
            )
        finally:
            stop.set()
            thread.join(timeout=10.0)


class TestDaemonReapsPredecessor:
    """RDR-128 single-writer backstop (nexus-070e2).

    The fcntl spawn lock normally prevents a second daemon, but a
    predecessor can survive a version transition (or a released-but-alive
    window) WITHOUT holding the lock. When a new daemon then acquires the
    lock it is the legitimate single writer, and must reap that lingering
    predecessor named in the addr file rather than coexist with it (the
    two-daemons / WAL-contention class seen in the 5.1.1->5.1.4 upgrade).

    These pin ``_reap_predecessor_daemon`` directly: the real two-process
    race is the same one the nexus-9eaz framing could not reproduce on
    GHA, so the primitives are monkeypatched for determinism.
    """

    @staticmethod
    def _write_discovery(config_dir: Path, pid: int) -> Path:
        import json

        from nexus.daemon.t2_daemon import t2_discovery_path

        p = t2_discovery_path(config_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": pid, "tcp_port": 1234}))
        return p

    def test_reaps_live_predecessor_with_sigterm(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import signal

        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 999999)
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        state = {"alive": True}
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: state["alive"])
        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))
            if sig == signal.SIGTERM:
                state["alive"] = False  # graceful predecessor exits

        monkeypatch.setattr(td.os, "kill", fake_kill)

        d = td.T2Daemon(config_dir=config_dir, db_path=db_path)
        d._reap_predecessor_daemon()

        assert (999999, signal.SIGTERM) in kills
        assert (999999, signal.SIGKILL) not in kills

    def test_escalates_to_sigkill_when_sigterm_ignored(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import signal

        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 999998)
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: True)  # never dies
        monkeypatch.setattr(td, "_PREDECESSOR_REAP_TIMEOUT", 0.2)
        kills: list[tuple[int, int]] = []
        monkeypatch.setattr(td.os, "kill", lambda pid, sig: kills.append((pid, sig)))

        d = td.T2Daemon(config_dir=config_dir, db_path=db_path)
        d._reap_predecessor_daemon()

        assert (999998, signal.SIGTERM) in kills
        assert (999998, signal.SIGKILL) in kills

    def test_no_reap_when_predecessor_dead(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 999997)
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: False)
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        kills: list = []
        monkeypatch.setattr(td.os, "kill", lambda pid, sig: kills.append((pid, sig)))

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert kills == []

    def test_no_reap_when_pid_is_self(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import os

        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, os.getpid())
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        kills: list = []
        monkeypatch.setattr(td.os, "kill", lambda pid, sig: kills.append((pid, sig)))

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert kills == []

    def test_no_reap_when_pid_not_a_t2_daemon(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """PID-reuse guard: a live pid whose cmdline is NOT a t2 daemon must
        not be killed (the addr-file pid may have been recycled)."""
        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 999996)
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: True)
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: False)
        kills: list = []
        monkeypatch.setattr(td.os, "kill", lambda pid, sig: kills.append((pid, sig)))

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert kills == []

    def test_no_discovery_file_is_noop(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        from nexus.daemon import t2_daemon as td

        kills: list = []
        monkeypatch.setattr(td.os, "kill", lambda pid, sig: kills.append((pid, sig)))
        # no discovery file written
        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert kills == []
