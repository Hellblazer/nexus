# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-wo6sc: the stamp phase is four things, so time them separately.

Measured 2026-09-12 (release battery, --max-parallel 4): two heartbeat ticks
took 19.098 s and 31.622 s against a 15 s TTL with ``poll``, ``health`` and
``pg`` all at ~0, so the whole stall sat in ``stamp``. Two sessions then
argued about the cause from that one number and neither could settle it,
because ``stamp`` names a phase that is really four: the election open, the
flock wait, the record read, and the atomic write. Wall clock around the
whole group cannot separate "blocked in a syscall" from "this thread was not
scheduled on a CPU", and process CPU time cannot either -- both read as ~0.

So the tick reports sub-phases plus an ``unaccounted`` term, and that term is
the discriminator:

  one slow SYSCALL sub-phase        -> a filesystem stall (H1)
  fast syscalls, large unaccounted  -> descheduling (H2)

These tests drive both shapes on an injected monotonic clock. No sleeps: the
stall is scripted.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import nexus.daemon.service_registry as sr_mod
from nexus.daemon.service_registry import ServiceRegistry


class _ScriptedMonotonic:
    """Monotonic clock that adds ``charge`` to the next reading, once."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.charge = 0.0

    def __call__(self) -> float:
        self.now += self.charge
        self.charge = 0.0
        return self.now


@pytest.fixture
def mono() -> _ScriptedMonotonic:
    return _ScriptedMonotonic()


def _registry(tmp_path: Path, mono: _ScriptedMonotonic) -> ServiceRegistry:
    return ServiceRegistry(
        dir=tmp_path / "cfg",
        tier="storage_service",
        clock=lambda: 5000.0,
        monotonic=mono,
    )


def _published(reg: ServiceRegistry):
    return reg.publish(
        "1000",
        endpoint={"host": "127.0.0.1", "port": 38237},
        version="7.43.0",
        owner_token="tok-a",
    )


# -- the sub-phases exist at all ------------------------------------------------

def test_heartbeat_reports_every_stamp_subphase(tmp_path: Path, mono) -> None:
    """A plain heartbeat names each syscall group it spent time in."""
    reg = _registry(tmp_path, mono)
    record = _published(reg)
    reg.heartbeat(record)

    phases = reg.last_heartbeat_phases
    assert set(phases) >= {
        "elect_open",
        "elect_flock",
        "read",
        "write_open",
        "write_body",
        "write_replace",
    }, f"stamp is still reported as one opaque phase: {sorted(phases)}"
    assert all(v >= 0.0 for v in phases.values())


def test_phases_are_fresh_per_call_not_accumulated(tmp_path: Path, mono) -> None:
    """Two ticks must not sum into one another; the next reading is this tick."""
    reg = _registry(tmp_path, mono)
    record = _published(reg)

    def charge_replace(src, dst, _real=os.replace):
        mono.charge = 7.0
        return _real(src, dst)

    reg.heartbeat(record)
    first = dict(reg.last_heartbeat_phases)
    assert first["write_replace"] < 1.0

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sr_mod.os, "replace", charge_replace)
    try:
        reg.heartbeat(record)
    finally:
        monkey.undo()
    second = dict(reg.last_heartbeat_phases)
    assert second["write_replace"] >= 7.0
    assert first["write_replace"] < 1.0, "the earlier reading was mutated in place"


# -- H1: a filesystem stall lands in one syscall --------------------------------

def test_a_stalled_replace_is_named_as_the_replace(tmp_path: Path, mono) -> None:
    """H1 shape. os.replace blocks; the time must be attributed to it, and the
    unaccounted term must stay small -- otherwise the discriminator lies in the
    direction that would send the next reader after a scheduler ghost."""
    reg = _registry(tmp_path, mono)
    record = _published(reg)

    def charge_replace(src, dst, _real=os.replace):
        mono.charge = 19.0
        return _real(src, dst)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sr_mod.os, "replace", charge_replace)
    try:
        reg.heartbeat(record)
    finally:
        monkey.undo()

    phases = reg.last_heartbeat_phases
    assert phases["write_replace"] >= 19.0
    assert reg.last_heartbeat_unaccounted < 1.0, (
        "a syscall stall must not leak into the descheduling term"
    )


def test_a_stalled_flock_is_named_as_the_flock(tmp_path: Path, mono) -> None:
    """The other H1 surface: contention on the election lock, still bounded."""
    reg = _registry(tmp_path, mono)
    record = _published(reg)

    # Patches the shared advisory-lock primitive rather than ``fcntl.flock``
    # directly: the election acquire routes through nexus._locking now, so
    # the module has no ``fcntl`` attribute left to patch (Windows-
    # portability port). Same shape as before — a module-object attribute
    # patch, undone in the finally — and only ACQUIRES are charged, because
    # releases go through ``unlock_fd`` and never reach this callable.
    real_lock_fd = sr_mod._locking.lock_fd

    def charge_lock_fd(fd, *, blocking, _real=real_lock_fd):
        mono.charge = 2.0
        return _real(fd, blocking=blocking)

    monkey = pytest.MonkeyPatch()
    monkey.setattr(sr_mod._locking, "lock_fd", charge_lock_fd)
    try:
        reg.heartbeat(record)
    finally:
        monkey.undo()

    assert reg.last_heartbeat_phases["elect_flock"] >= 2.0


# -- H2: descheduling lands in the unaccounted term -----------------------------

def test_time_outside_every_syscall_is_reported_as_unaccounted(
    tmp_path: Path, mono
) -> None:
    """H2 shape. The thread loses the CPU between the record read and the
    write, where no syscall is running. Every syscall sub-phase stays fast and
    the gap surfaces as ``unaccounted`` -- which is the only signal that
    distinguishes a starved thread from a stalled disk."""
    charged = {"done": False}

    def clock_that_loses_the_cpu() -> float:
        # self._clock() is called between the read and the write, in the
        # untimed region -- exactly where a descheduled thread resumes.
        if not charged["done"]:
            charged["done"] = True
            mono.charge = 22.0
        return 5000.0

    reg = ServiceRegistry(
        dir=tmp_path / "cfg",
        tier="storage_service",
        clock=clock_that_loses_the_cpu,
        monotonic=mono,
    )
    record = reg.publish(
        "1000",
        endpoint={"host": "127.0.0.1", "port": 38237},
        version="7.43.0",
        owner_token="tok-a",
    )
    charged["done"] = False
    reg.heartbeat(record)

    phases = reg.last_heartbeat_phases
    assert reg.last_heartbeat_unaccounted >= 22.0, (
        "a stall with no syscall to blame must be visible as unaccounted, "
        f"got phases={dict(phases)}"
    )
    assert all(v < 1.0 for v in phases.values()), (
        f"no syscall should be blamed for a scheduling gap: {dict(phases)}"
    )


def test_the_two_hypotheses_produce_different_signatures(tmp_path: Path) -> None:
    """The point of the whole exercise: H1 and H2 must not look alike. If this
    fails the instrumentation is decoration, not a discriminator."""
    def run_h1() -> tuple[float, float]:
        mono = _ScriptedMonotonic()
        reg = _registry(tmp_path / "h1", mono)
        record = _published(reg)

        def charge_replace(src, dst, _real=os.replace):
            mono.charge = 19.0
            return _real(src, dst)

        monkey = pytest.MonkeyPatch()
        monkey.setattr(sr_mod.os, "replace", charge_replace)
        try:
            reg.heartbeat(record)
        finally:
            monkey.undo()
        return reg.last_heartbeat_phases["write_replace"], reg.last_heartbeat_unaccounted

    def run_h2() -> tuple[float, float]:
        mono = _ScriptedMonotonic()
        charged = {"done": True}

        def clock_that_loses_the_cpu() -> float:
            if not charged["done"]:
                charged["done"] = True
                mono.charge = 19.0
            return 5000.0

        reg = ServiceRegistry(
            dir=tmp_path / "h2" / "cfg",
            tier="storage_service",
            clock=clock_that_loses_the_cpu,
            monotonic=mono,
        )
        record = reg.publish(
            "1000",
            endpoint={"host": "127.0.0.1", "port": 38237},
            version="7.43.0",
            owner_token="tok-a",
        )
        charged["done"] = False
        reg.heartbeat(record)
        return reg.last_heartbeat_phases["write_replace"], reg.last_heartbeat_unaccounted

    h1_replace, h1_unaccounted = run_h1()
    h2_replace, h2_unaccounted = run_h2()

    assert h1_replace >= 19.0 and h1_unaccounted < 1.0
    assert h2_unaccounted >= 19.0 and h2_replace < 1.0


# -- the sub-phases must reach the log a real incident is read from ------------

def test_missed_ttl_log_carries_the_stamp_subphases(tmp_path: Path, monkeypatch) -> None:
    """The whole point is the field artifact. nexus-19's 4-wide battery re-run
    reads ``storage_service_heartbeat_missed_ttl``, so sub-phases that exist
    only on the registry object would be invisible exactly when they matter.
    Drives a REAL stamp (no patched heartbeat_tick) with os.replace stalled."""
    import structlog.testing

    import nexus.daemon.storage_service_daemon as ssd_mod
    from nexus.daemon.service_registry import ttl_for_tier
    from tests.daemon.test_storage_service_daemon import (
        _FakeClock,
        _FakeProc,
        _make_supervisor,
    )

    mono = _ScriptedMonotonic()
    sup = _make_supervisor(tmp_path / "cfg", _FakeClock())
    sup._monotonic = mono
    sup._proc = _FakeProc(pid=42300)
    sup._service_port = 18090
    sup._publish(18090)          # builds the ServiceSupervisor + registry
    sup._supervisor._registry._monotonic = mono

    ttl = ttl_for_tier("storage_service")

    def charge_replace(src, dst, _real=os.replace):
        mono.charge = ttl + 4.0
        return _real(src, dst)

    monkeypatch.setattr(sr_mod.os, "replace", charge_replace)
    with monkeypatch.context() as m:
        m.setattr(sup, "_probe_service_health", lambda: ssd_mod.HealthProbe.OK)
        m.setattr(sup, "_pg_reachable", lambda: True)
        m.setattr(ssd_mod, "_pid_is_alive", lambda _pid: True)
        with structlog.testing.capture_logs() as logs:
            sup.heartbeat_once()

    missed = [e for e in logs if e["event"] == "storage_service_heartbeat_missed_ttl"]
    assert len(missed) == 1, f"expected one missed-TTL error, got {logs}"
    phases = missed[0]["phases_s"]
    assert phases["stamp"] >= ttl
    assert phases["stamp.write_replace"] >= ttl, (
        "the log names 'stamp' but not which of its four parts stalled: "
        f"{phases}"
    )
    assert "stamp.unaccounted" in phases, (
        "the descheduling term must be in the log line, not only on the object"
    )
    assert phases["stamp.unaccounted"] < 1.0
