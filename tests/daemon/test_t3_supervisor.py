# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-149 P3 (bead nexus-wab47): the long-lived T3 supervisor.

Unit coverage for ``T3Supervisor`` with the chroma subprocess spawn
injected, so the lease publish / heartbeat / version-cycle / stop wiring
is exercised deterministically without a real ``chroma run`` (the live
chroma path is an integration concern). Proves the supervisor delegates
to the shared ``ServiceRegistry`` / ``ServiceSupervisor`` exactly as the
conformance suite's T3 cells assume.

The injected ``clock`` fixture patches BOTH the supervisor's lease stamp
and the discovery reader's clock, so ``find_t3_daemon`` (the real read
path, which uses wall-clock TTL) agrees with the supervisor's
fixed-clock stamps.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.daemon import t3_daemon
from nexus.daemon.discovery import find_t3_daemon
from nexus.daemon.t3_daemon import T3Supervisor


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _FakeProc:
    """Stand-in for a chroma ``subprocess.Popen``: a controllable pid +
    poll() (None = running, int = exited)."""

    _next_pid = 880001

    def __init__(self) -> None:
        self.pid = _FakeProc._next_pid
        _FakeProc._next_pid += 1
        self._exited = False

    def poll(self) -> int | None:
        return 0 if self._exited else None

    def die(self) -> None:
        self._exited = True


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nexus.config.is_local_mode", lambda: True)


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cd = tmp_path / "cfg"
    cd.mkdir(parents=True, exist_ok=True, mode=0o700)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cd))
    return cd


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    c = _FakeClock()
    # The discovery reader checks lease freshness against wall-clock; pin
    # it to the same fixed clock the supervisor stamps with.
    monkeypatch.setattr("nexus.daemon.discovery.time.time", c)
    return c


@pytest.fixture
def fake_spawn(monkeypatch: pytest.MonkeyPatch) -> list[_FakeProc]:
    """Replace chroma spawn with a fake proc + a fixed port. Returns the
    list of spawned procs so a test can kill one to simulate chroma death."""
    spawned: list[_FakeProc] = []
    port = 54321

    def _spawn(self: T3Supervisor) -> tuple[_FakeProc, int]:
        proc = _FakeProc()
        spawned.append(proc)
        return proc, port

    monkeypatch.setattr(T3Supervisor, "_spawn_chroma", _spawn)
    # No real chroma is listening on the fake port; by default treat it as
    # reachable. The wedged-chroma test overrides this to False.
    monkeypatch.setattr(T3Supervisor, "_chroma_reachable", lambda self: True)
    return spawned


def _make(config_dir: Path, clock: _FakeClock) -> T3Supervisor:
    # supervised=True mirrors the real long-lived runner (run_t3_supervisor),
    # which records its pid as the lease supervisor_pid.
    return T3Supervisor(
        config_dir=config_dir, local_path=config_dir / "chroma",
        lease_clock=clock, supervised=True,
    )


class TestT3SupervisorPublish:
    def test_start_publishes_resolvable_lease(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock
    ) -> None:
        sup = _make(config_dir, clock)
        payload = sup.start()
        assert payload["pid"] == fake_spawn[0].pid  # chroma pid
        assert payload["tcp_port"] == 54321
        assert payload["generation"] == 1
        assert payload["supervisor_pid"] is not None
        resolved = find_t3_daemon(config_dir)
        assert resolved is not None
        assert resolved["pid"] == fake_spawn[0].pid
        sup.stop()

    def test_idempotent_second_start_returns_existing(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock
    ) -> None:
        sup = _make(config_dir, clock)
        sup.start()
        sup2 = _make(config_dir, clock)
        sup2.start()  # live lease present -> no second spawn
        assert len(fake_spawn) == 1
        assert not sup2.owns_process, (
            "the short-circuit branch must never assign self._proc"
        )
        sup.stop()


class TestT3SupervisorHeartbeat:
    def test_heartbeat_keeps_lease_fresh_past_ttl(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock
    ) -> None:
        sup = _make(config_dir, clock)
        sup.start()
        clock.advance(2.0)
        assert sup.heartbeat_once() is True
        clock.advance(2.0)
        assert find_t3_daemon(config_dir) is not None  # heartbeat held it fresh
        sup.stop()

    def test_heartbeat_self_heals_deleted_record(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock
    ) -> None:
        sup = _make(config_dir, clock)
        sup.start()
        t3_daemon.t3_discovery_path(config_dir).unlink()
        assert find_t3_daemon(config_dir) is None
        assert sup.heartbeat_once() is True
        assert find_t3_daemon(config_dir) is not None  # re-asserted
        sup.stop()

    def test_heartbeat_stops_when_chroma_dies(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock
    ) -> None:
        sup = _make(config_dir, clock)
        sup.start()
        fake_spawn[0].die()  # chroma exits
        assert sup.heartbeat_once() is False  # supervisor stops heartbeating
        sup.stop()

    def test_wedged_chroma_lets_lease_expire(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # RF-4: a chroma whose pid is alive but is NOT serving (wedged) must
        # NOT keep the lease fresh — heartbeat_once keeps supervising (True)
        # but skips the re-stamp, so the lease ages out and clients see down.
        sup = _make(config_dir, clock)
        sup.start()
        monkeypatch.setattr(T3Supervisor, "_chroma_reachable", lambda self: False)
        assert sup.heartbeat_once() is True  # still supervising
        clock.advance(3.1)  # past TTL with no re-stamp
        assert find_t3_daemon(config_dir) is None  # lease expired
        sup.stop()


class TestT3SupervisorStop:
    def test_stop_relinquishes_lease(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock
    ) -> None:
        sup = _make(config_dir, clock)
        sup.start()
        assert find_t3_daemon(config_dir) is not None
        sup.stop()
        assert find_t3_daemon(config_dir) is None  # record removed


class TestT3SupervisorCoexistence:
    """GH #1369 (T3 analog of the StorageServiceSupervisor fix): a second
    supervisor started under run_t3_supervisor while another already holds
    a live lease must exit 0, not enter the heartbeat loop. Before this fix
    run_t3_supervisor called sup.heartbeat_once() unconditionally after
    start(); heartbeat_once() reads self._proc is None (never assigned by
    the short-circuit branch) as "chroma died" and forced exit(3) against a
    perfectly healthy chroma owned by another supervisor — under launchd
    KeepAlive / systemd Restart this drove an unbounded respawn loop."""

    def test_second_supervisor_under_live_lease_exits_zero(
        self,
        config_dir: Path,
        fake_spawn: list[_FakeProc],
        clock: _FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First supervisor holds a live lease (models the owning unit/session).
        first = _make(config_dir, clock)
        first.start()
        assert len(fake_spawn) == 1

        # run_t3_supervisor's internal T3Supervisor must short-circuit on the
        # live lease rather than spawn a second chroma. If it ever tried to
        # spawn, fake_spawn would grow past 1, which we assert below.
        monkeypatch.setattr(t3_daemon, "DEFAULT_HEARTBEAT_INTERVAL", 0.0)
        code = t3_daemon.run_t3_supervisor(
            config_dir=config_dir, local_path=config_dir / "chroma",
        )

        assert code == 0, (
            "a supervisor that finds the lease already held owns no chroma "
            "process and must exit 0 without entering the heartbeat loop"
        )
        assert len(fake_spawn) == 1, "coexisting supervisor must not double-spawn"
        first.stop()

    def test_not_owning_process_never_calls_heartbeat_once(
        self, monkeypatch: pytest.MonkeyPatch, config_dir: Path,
    ) -> None:
        """Precise-mechanism pin (mirrors the storage-side
        test_not_owning_process_exits_0_without_entering_heartbeat_loop): a
        supervisor with owns_process=False must never call heartbeat_once(),
        not merely happen to exit 0. A scripted double whose heartbeat_once()
        raises makes an accidental call fail loudly instead of passing by
        coincidence."""

        class _UnownedDouble:
            owns_process = False

            def __init__(self, **_kwargs: object) -> None:
                self.stopped = False

            def start(self) -> None:
                pass

            def heartbeat_once(self) -> bool:
                raise AssertionError(
                    "a supervisor with nothing to own must never call "
                    "heartbeat_once()"
                )

            def stop(self) -> None:
                self.stopped = True

        monkeypatch.setattr(t3_daemon, "T3Supervisor", _UnownedDouble)
        monkeypatch.setattr(t3_daemon, "DEFAULT_HEARTBEAT_INTERVAL", 0.0)

        code = t3_daemon.run_t3_supervisor(
            config_dir=config_dir, local_path=config_dir / "chroma",
        )

        assert code == 0
