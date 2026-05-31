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


class TestDaemonSweepsSideOrphans:
    """RDR-129 A1 (nexus-exa2p): generalise the addr-file reap to a same-db
    SWEEP. A side-orphan daemon that started AFTER the canonical daemon (so it
    was never the addr-file pid) holds memory.db open but is invisible to the
    addr-file reap. The startup sweep enumerates all live t2 daemons holding
    THIS db open (open-fd probe) and reaps every non-self one, guaranteeing
    single occupancy rather than just the common takeover case.
    """

    @staticmethod
    def _write_discovery(config_dir: Path, pid: int) -> Path:
        import json

        from nexus.daemon.t2_daemon import t2_discovery_path

        p = t2_discovery_path(config_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"pid": pid, "tcp_port": 1234}))
        return p

    def test_sweeps_side_orphan_not_in_addr_file(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import signal

        from nexus.daemon import t2_daemon as td

        # addr file names the canonical predecessor; a side-orphan holds the
        # db open but is absent from the addr file.
        self._write_discovery(config_dir, 111111)
        monkeypatch.setattr(
            td, "_enumerate_t2_daemon_pids_for_db", lambda p: [222222],
        )
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        state = {111111: True, 222222: True}
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: state.get(pid, False))
        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))
            if sig == signal.SIGTERM:
                state[pid] = False

        monkeypatch.setattr(td.os, "kill", fake_kill)

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()

        assert (111111, signal.SIGTERM) in kills, "addr-file pid not reaped"
        assert (222222, signal.SIGTERM) in kills, "side-orphan not reaped by sweep"

    def test_sweep_excludes_self(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        import os

        from nexus.daemon import t2_daemon as td

        # No addr file; the open-fd probe surfaces only our own pid.
        monkeypatch.setattr(
            td, "_enumerate_t2_daemon_pids_for_db", lambda p: [os.getpid()],
        )
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: True)
        kills: list = []
        monkeypatch.setattr(td.os, "kill", lambda pid, sig: kills.append((pid, sig)))

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert kills == []

    def test_addr_and_sweep_dedup_single_reap(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """The addr-file pid that also surfaces in the open-fd sweep is reaped
        exactly once, not twice."""
        import signal

        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 333333)
        monkeypatch.setattr(
            td, "_enumerate_t2_daemon_pids_for_db", lambda p: [333333],
        )
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        state = {333333: True}
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: state.get(pid, False))
        kills: list[tuple[int, int]] = []

        def fake_kill(pid: int, sig: int) -> None:
            kills.append((pid, sig))
            if sig == signal.SIGTERM:
                state[pid] = False

        monkeypatch.setattr(td.os, "kill", fake_kill)

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert kills.count((333333, signal.SIGTERM)) == 1


class TestDaemonReapDiscrimination:
    """RDR-140 P3.1 (nexus-r9hq0): ownership/version-aware reap discrimination.

    THE invariant the P3 gate (nexus-x2k4b) checks: a healthy, current-version
    peer named in OUR addr-file token must be attached, NEVER SIGTERM'd. The
    RDR-128/129 single-writer flock backstop is NOT weakened: a stale-version or
    unreachable addr-file peer, AND every open-fd-only side-orphan, STILL get
    reaped.

    Discrimination scope (P3 design decision, 2026-05-31 — A2-faithful, single-
    writer-safe; see the rdr140 P3 gate notes): the spare/attach path applies
    ONLY to the addr-file pid, whose ``t2_addr`` token carries the
    ``daemon_version`` + socket needed to handshake it. Open-fd-only peers
    surfaced by the RDR-129 sweep have no token and no socket we can reach; they
    are, by construction, writers that ESCAPED the db-scoped spawn lock we now
    hold, so sparing one would re-admit a second writer. Therefore open-fd-only
    peers are ALWAYS reaped — they are exactly the orphan case the sweep exists
    to kill. (The literal "spare open-fd healthy peers too" reading was rejected:
    the db-scoped spawn lock already prevents two healthy current daemons
    coexisting, so a sparable open-fd peer cannot legitimately arise.)

    Seam contract these tests pin for the P3.2 implementation (nexus-7ffls):

    * A module-level ``_peer_handshake(pid, payload) -> (version, reachable)``
      yields the peer's reported daemon version and whether a health-ping
      reached it. The real impl reads ``daemon_version`` from the discovery
      payload and pings the token's socket; these tests stub it per-pid.
    * ``_reap_predecessor_daemon`` passes the addr-file token to
      ``_reap_one_daemon(pid, payload)`` for the addr pid, and ``payload=None``
      for open-fd-only pids.
    * Discrimination in the reap path: SPARE (no kill, attach) iff a token is
      present AND ``reachable and version == _daemon_version()``; otherwise
      reap (covers stale/unreachable addr peers and all open-fd-only peers).

    The healthy-current addr-peer-spared tests are RED against current code,
    which reaps every live t2-daemon pid unconditionally. The reap-the-orphan
    tests are GREEN now and MUST STAY green (the backstop).
    """

    _CUR = "9.9.9-current"
    _OLD = "1.0.0-stale"

    @staticmethod
    def _write_discovery(config_dir: Path, pid: int, daemon_version: str) -> Path:
        import json

        from nexus.daemon.t2_daemon import t2_discovery_path

        p = t2_discovery_path(config_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        # A2: the token already carries pid + daemon_version; no NEW persisted
        # state is introduced by P3.
        p.write_text(json.dumps(
            {"pid": pid, "tcp_port": 1234, "daemon_version": daemon_version},
        ))
        return p

    @staticmethod
    def _install_common(monkeypatch, td, handshakes: dict, kills: list) -> None:
        """Pin current version, liveness, cmdline; stub the per-pid handshake
        and capture kills. ``handshakes`` maps pid -> (version, reachable)."""
        import signal

        monkeypatch.setattr(td, "_daemon_version",
                            lambda: TestDaemonReapDiscrimination._CUR)
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: True)
        state = {pid: True for pid in handshakes}
        monkeypatch.setattr(td, "_pid_is_alive", lambda pid: state.get(pid, False))

        def _handshake(pid, payload=None):  # noqa: ANN001
            return handshakes.get(pid, (None, False))

        monkeypatch.setattr(td, "_peer_handshake", _handshake, raising=False)

        def fake_kill(pid, sig):  # noqa: ANN001
            kills.append((pid, sig))
            if sig == signal.SIGTERM:
                state[pid] = False

        monkeypatch.setattr(td, "_PREDECESSOR_REAP_TIMEOUT", 0.2)
        monkeypatch.setattr(td.os, "kill", fake_kill)

    def test_healthy_current_addr_peer_is_spared(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """RED against current code: a healthy, current-version predecessor
        named in the addr file must be attached, NEVER reaped."""
        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 700001, self._CUR)
        kills: list = []
        self._install_common(monkeypatch, td, {700001: (self._CUR, True)}, kills)

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert kills == []

    def test_stale_version_addr_peer_is_reaped(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """A reachable but stale-version peer IS reaped (ensure-a-current
        daemon); guard stays green through P3.2."""
        import signal

        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 700002, self._OLD)
        kills: list = []
        self._install_common(monkeypatch, td, {700002: (self._OLD, True)}, kills)

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert (700002, signal.SIGTERM) in kills

    def test_unreachable_current_peer_is_reaped(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """Orphan backstop (RDR-128): a live current-version pid whose
        health-ping FAILS is a wedged/orphaned writer and MUST be reaped."""
        import signal

        from nexus.daemon import t2_daemon as td

        self._write_discovery(config_dir, 700003, self._CUR)
        kills: list = []
        self._install_common(monkeypatch, td, {700003: (self._CUR, False)}, kills)

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert (700003, signal.SIGTERM) in kills

    def test_side_orphan_via_sweep_is_always_reaped(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """Single-writer backstop: an open-fd-only peer (surfaced by the sweep,
        absent from the addr file) is ALWAYS reaped — even if it would handshake
        as healthy + current. It escaped the db-scoped spawn lock we hold, so it
        cannot be a legitimately sparable peer; the spare path is addr-file-only.
        """
        import signal

        from nexus.daemon import t2_daemon as td

        # No addr file; the peer surfaces only through the open-fd probe. Its
        # handshake says healthy+current, but with no token it must still die.
        monkeypatch.setattr(
            td, "_enumerate_t2_daemon_pids_for_db", lambda p: [700004],
        )
        kills: list = []
        self._install_common(monkeypatch, td, {700004: (self._CUR, True)}, kills)

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert (700004, signal.SIGTERM) in kills

    def test_unreachable_side_orphan_via_sweep_is_reaped(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """Orphan backstop on the sweep path: an unreachable open-fd holder is
        reaped (single-writer preserved)."""
        import signal

        from nexus.daemon import t2_daemon as td

        monkeypatch.setattr(
            td, "_enumerate_t2_daemon_pids_for_db", lambda p: [700005],
        )
        kills: list = []
        self._install_common(monkeypatch, td, {700005: (self._CUR, False)}, kills)

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert (700005, signal.SIGTERM) in kills

    def test_mixed_targets_reap_only_the_stale_one(
        self, config_dir: Path, db_path: Path, monkeypatch,
    ) -> None:
        """Convergence (case c, deterministic): among multiple live peers, only
        the stale-version one is reaped; the healthy current-version peer is
        spared. Exactly one reap, the rest attach."""
        import signal

        from nexus.daemon import t2_daemon as td

        # addr-file peer is healthy+current (spare); a side-orphan is stale (reap).
        self._write_discovery(config_dir, 700006, self._CUR)
        monkeypatch.setattr(
            td, "_enumerate_t2_daemon_pids_for_db", lambda p: [700007],
        )
        kills: list = []
        self._install_common(
            monkeypatch, td,
            {700006: (self._CUR, True), 700007: (self._OLD, True)},
            kills,
        )

        td.T2Daemon(config_dir=config_dir, db_path=db_path)._reap_predecessor_daemon()
        assert (700007, signal.SIGTERM) in kills
        assert not any(pid == 700006 for pid, _sig in kills)
        assert {pid for pid, _sig in kills} == {700007}


class TestEnumerateT2DaemonPids:
    """RDR-129 A1 (nexus-exa2p): the open-fd enumeration helper used by both
    the startup sweep and the doctor multiplicity check."""

    def test_missing_db_short_circuits_empty(self, tmp_path, monkeypatch) -> None:
        from nexus.daemon import t2_daemon as td

        # A db file that does not exist can have no open-fd holders; the probe
        # must short-circuit (and never shell out to lsof / scan /proc).
        called = {"probe": False}

        def _boom(target):  # pragma: no cover — must not run
            called["probe"] = True
            return [1]

        monkeypatch.setattr(td, "_open_fd_holder_pids", _boom)
        assert td._enumerate_t2_daemon_pids_for_db(tmp_path / "absent.db") == []
        assert called["probe"] is False

    def test_filters_non_daemon_holders(self, tmp_path, monkeypatch) -> None:
        from nexus.daemon import t2_daemon as td

        db = tmp_path / "memory.db"
        db.write_text("x")  # only .exists() matters
        monkeypatch.setattr(td, "_open_fd_holder_pids", lambda target: [10, 20, 30])
        # 20 is some unrelated process holding the file; only daemons count.
        monkeypatch.setattr(td, "_is_t2_daemon_process", lambda pid: pid in (10, 30))
        assert td._enumerate_t2_daemon_pids_for_db(db) == [10, 30]
