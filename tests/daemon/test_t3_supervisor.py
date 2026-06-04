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


class TestT3SupervisorVersionCycle:
    def test_cycle_to_current_respawns_chroma_on_skew(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(t3_daemon, "_daemon_version", lambda: "0.9.0")
        sup = _make(config_dir, clock)
        sup.start()
        assert len(fake_spawn) == 1
        monkeypatch.setattr(t3_daemon, "_daemon_version", lambda: "1.0.0")
        assert sup.cycle_to_current() is True
        assert len(fake_spawn) == 2  # chroma respawned at the new version
        resolved = find_t3_daemon(config_dir)
        assert resolved is not None and resolved["version"] == "1.0.0"
        sup.stop()

    def test_cycle_to_current_noop_when_version_matches(
        self, config_dir: Path, fake_spawn: list[_FakeProc], clock: _FakeClock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(t3_daemon, "_daemon_version", lambda: "1.0.0")
        sup = _make(config_dir, clock)
        sup.start()
        assert sup.cycle_to_current() is False
        assert len(fake_spawn) == 1  # no respawn
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
