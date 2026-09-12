# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-59bah: the heartbeat's election flock is bounded below the lease TTL.

2026-09-06 shape: a live supervisor's heartbeat tick blocked on the per-scope
election flock for longer than the lease lived, the lease was reaped, and the
loop stayed wedged inside one tick. ``ServiceRegistry.heartbeat`` now takes
the flock with a budget of one third of the TTL (LOCK_NB polling against an
injected monotonic clock); a busy election raises ``ElectionBusyError`` and
``ServiceSupervisor.heartbeat_tick`` turns that into a logged skipped stamp,
never a blocked loop. ``publish`` stays blocking: a first claim must wait its
turn so concurrent siblings serialize into strictly increasing generations.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from pathlib import Path

import pytest
import structlog.testing

from nexus.daemon.service_registry import (
    ElectionBusyError,
    LeaseRecord,
    ServiceRegistry,
    ServiceSupervisor,
    StaleOwnerError,
)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _FakeMonotonic:
    """Advances by ``step`` per call so a LOCK_NB poll loop runs out of budget
    deterministically instead of sleeping the wall clock."""

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        self.now += self.step
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def mono() -> _FakeMonotonic:
    return _FakeMonotonic(step=0.25)


@pytest.fixture
def registry(tmp_path: Path, clock: _FakeClock, mono: _FakeMonotonic) -> ServiceRegistry:
    return ServiceRegistry(
        dir=tmp_path,
        tier="storage_service",
        clock=clock,
        ttl=15.0,
        heartbeat_interval=1.0,
        monotonic=mono,
        sleep=mono.sleep,
    )


def _endpoint() -> dict:
    return {"host": "127.0.0.1", "port": 5000}


@pytest.fixture
def held_flock(registry: ServiceRegistry):
    """Hold the scope's election flock from a foreign fd for the test's life."""
    path = registry._election_path("42")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield fd
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_election_budget_is_a_third_of_the_ttl(registry: ServiceRegistry) -> None:
    assert registry.heartbeat_election_budget == pytest.approx(5.0)


def test_heartbeat_gives_up_within_budget_when_election_is_held(
    registry: ServiceRegistry, mono: _FakeMonotonic, held_flock
) -> None:
    # publish would block on the held flock; build the record directly instead.
    rec = LeaseRecord(
        scope_key="42", generation=1, owner_token="a", heartbeat_epoch=1000.0,
        ttl=15.0, endpoint=_endpoint(), version="1",
    )
    registry._write_record_atomic(rec)
    before = mono.now
    with pytest.raises(ElectionBusyError):
        registry.heartbeat(rec)
    waited = mono.now - before
    # nexus-wo6sc: assert the WAIT, not total clock advance. This fake
    # monotonic advances by ``step`` on every read, so any instrumentation
    # that reads the clock inflates ``mono.now - before`` without changing a
    # single thing about the budget. ``elect_flock`` is the quantity the
    # budget actually governs.
    # The two-step slack is the measuring instrument itself: ``_timed`` reads
    # this clock once before the wait and once after, and each read advances
    # it by ``step``. On a real monotonic those two reads are nanoseconds.
    assert registry.last_heartbeat_phases["elect_flock"] <= (
        registry.heartbeat_election_budget + 2 * mono.step
    ), "the tick must return inside its budget, never block to the TTL"
    assert waited < registry._ttl, "and it must never block out to the TTL"
    assert mono.sleeps, "a busy election is polled with LOCK_NB, not spun hot"
    assert registry._read_record("42").heartbeat_epoch == 1000.0, "nothing written"


def test_heartbeat_takes_the_flock_when_free(
    registry: ServiceRegistry, clock: _FakeClock, mono: _FakeMonotonic
) -> None:
    rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="a")
    clock.t += 1.0
    out = registry.heartbeat(rec)
    assert out.heartbeat_epoch == 1001.0
    assert not mono.sleeps


def test_fencing_still_wins_over_a_free_election(registry: ServiceRegistry) -> None:
    rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="a")
    registry.publish("42", endpoint=_endpoint(), version="2", owner_token="b")
    with pytest.raises(StaleOwnerError):
        registry.heartbeat(rec)


def test_supervisor_tick_logs_and_skips_a_busy_election(
    registry: ServiceRegistry, held_flock
) -> None:
    sup = ServiceSupervisor(registry, "42", version="1", endpoint_provider=_endpoint)
    rec = LeaseRecord(
        scope_key="42", generation=1, owner_token=sup.owner_token,
        heartbeat_epoch=1000.0, ttl=15.0, endpoint=_endpoint(), version="1",
    )
    registry._write_record_atomic(rec)
    sup._record = rec
    with structlog.testing.capture_logs() as logs:
        sup.heartbeat_tick()  # must return, not block
    busy = [e for e in logs if e["event"] == "service_supervisor_heartbeat_election_busy"]
    assert len(busy) == 1 and busy[0]["log_level"] == "warning"
    assert busy[0]["scope"] == "42"
    assert sup.fenced is False, "a busy election is not a fence"
    assert sup.record is rec, "the lease we hold is unchanged; the next tick retries"


def test_publish_still_blocks_on_a_held_election(
    registry: ServiceRegistry, mono: _FakeMonotonic, held_flock
) -> None:
    """publish is the first claim and has no lease to age; it waits its turn.
    Proven by releasing the foreign flock from a thread and observing publish
    complete only afterwards."""
    released = threading.Event()

    def release_later() -> None:
        time.sleep(0.2)
        fcntl.flock(held_flock, fcntl.LOCK_UN)
        released.set()

    threading.Thread(target=release_later, daemon=True).start()
    rec = registry.publish("42", endpoint=_endpoint(), version="1", owner_token="a")
    assert released.is_set(), "publish returned before the foreign holder released"
    assert rec.generation == 1
    assert not mono.sleeps, "publish uses the blocking flock, not the polled one"
