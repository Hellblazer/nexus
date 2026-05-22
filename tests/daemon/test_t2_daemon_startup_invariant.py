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

    def test_second_start_succeeds_after_first_stops(
        self, config_dir: Path, db_path: Path,
    ) -> None:
        """The spawn lock is released on clean stop; a fresh start
        against the same path after the first daemon stops must
        succeed (the invariant is mutual-exclusion *while running*,
        not permanent exclusion).
        """
        from nexus.daemon.t2_daemon import T2Daemon

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

        # Spawn lock should be released — second start succeeds.
        second = T2Daemon(config_dir=config_dir, db_path=db_path)
        ready2 = threading.Event()
        stop2 = threading.Event()
        t2 = threading.Thread(
            target=_run_daemon_in_thread, args=(second, ready2, stop2),
        )
        t2.start()
        try:
            assert ready2.wait(timeout=10.0), (
                "second daemon failed to start after first stopped — "
                "spawn lock leak"
            )
        finally:
            stop2.set()
            t2.join(timeout=10.0)

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
