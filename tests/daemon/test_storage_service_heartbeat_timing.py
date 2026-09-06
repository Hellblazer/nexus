# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-59bah: a heartbeat tick that stalls past the lease TTL must log.

Observed 2026-09-06 (package-upgrade rehearsal, run.sh --artifacts): the
supervisor stayed alive, its lease stamp stopped for 17+ s during an
in-place venv reinstall, the lease was reaped, every client resolved
"endpoint not resolvable", and the supervisor log said nothing. The tick
now times its phases and emits ``storage_service_heartbeat_slow`` past
``_HEARTBEAT_SLOW_S`` and ``storage_service_heartbeat_missed_ttl`` (ERROR)
at or past the tier TTL. Driven with an injected monotonic clock, so the
stall is scripted, not slept.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import structlog.testing

import nexus.daemon.storage_service_daemon as ssd_mod
from nexus.daemon.service_registry import ttl_for_tier
from nexus.daemon.storage_service_daemon import HealthProbe

from tests.daemon.test_storage_service_daemon import (  # noqa: F401 — config_dir/clock are pytest fixtures re-exported into this module
    _FakeClock,
    _FakeProc,
    _make_supervisor,
    clock,
    config_dir,
)


class _ScriptedMonotonic:
    """A monotonic clock that jumps by a scripted amount on a chosen call."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.jump_on_next = 0.0

    def __call__(self) -> float:
        self.now += self.jump_on_next
        self.jump_on_next = 0.0
        return self.now


def _healthy_supervisor(config_dir: Path, clock: _FakeClock, mono: _ScriptedMonotonic):
    sup = _make_supervisor(config_dir, clock)
    sup._monotonic = mono
    sup._proc = _FakeProc(pid=42300)
    sup._service_port = 18090
    sup._publish(18090)
    return sup


@pytest.fixture
def mono() -> _ScriptedMonotonic:
    return _ScriptedMonotonic()


def _tick(sup, stall_in: str, stall_s: float, mono: _ScriptedMonotonic):
    """Run one tick with *stall_in* (health | stamp) taking *stall_s*."""
    def slow_health():
        mono.jump_on_next = stall_s if stall_in == "health" else 0.0
        return HealthProbe.OK

    def slow_stamp():
        mono.jump_on_next = stall_s if stall_in == "stamp" else 0.0

    with patch.object(sup, "_probe_service_health", side_effect=slow_health), \
         patch.object(sup, "_pg_reachable", return_value=True), \
         patch.object(sup._supervisor, "heartbeat_tick", side_effect=slow_stamp), \
         patch.object(ssd_mod, "_pid_is_alive", return_value=True), \
         structlog.testing.capture_logs() as logs:
        result = sup.heartbeat_once()
    return result, logs


def test_fast_tick_logs_nothing_about_timing(config_dir: Path, clock: _FakeClock, mono) -> None:
    sup = _healthy_supervisor(config_dir, clock, mono)
    result, logs = _tick(sup, "health", 0.0, mono)
    assert result == (True, True)
    assert not [e for e in logs if e["event"].startswith("storage_service_heartbeat_")]


def test_slow_tick_is_a_warning_naming_the_phase(config_dir: Path, clock: _FakeClock, mono) -> None:
    sup = _healthy_supervisor(config_dir, clock, mono)
    result, logs = _tick(sup, "health", ssd_mod._HEARTBEAT_SLOW_S + 0.5, mono)
    assert result == (True, True), "timing never changes the verdict"
    slow = [e for e in logs if e["event"] == "storage_service_heartbeat_slow"]
    assert len(slow) == 1 and slow[0]["log_level"] == "warning"
    assert slow[0]["phases_s"]["health"] >= ssd_mod._HEARTBEAT_SLOW_S
    assert not [e for e in logs if e["event"] == "storage_service_heartbeat_missed_ttl"]


def test_tick_at_or_past_ttl_is_an_error_naming_the_stalled_phase(
    config_dir: Path, clock: _FakeClock, mono
) -> None:
    """The 2026-09-06 shape: the stamp phase itself stalls (flock + os.replace
    on a saturated filesystem) for longer than the lease lives."""
    sup = _healthy_supervisor(config_dir, clock, mono)
    ttl = ttl_for_tier("storage_service")
    result, logs = _tick(sup, "stamp", ttl + 2.0, mono)
    assert result == (True, True)
    missed = [e for e in logs if e["event"] == "storage_service_heartbeat_missed_ttl"]
    assert len(missed) == 1 and missed[0]["log_level"] == "error"
    assert missed[0]["ttl_s"] == ttl
    assert missed[0]["elapsed_s"] >= ttl
    assert missed[0]["phases_s"]["stamp"] >= ttl, "the stalled phase is named"
    assert not [e for e in logs if e["event"] == "storage_service_heartbeat_slow"], \
        "a missed TTL is reported once, as the error, not also as slow"


def test_timing_is_reported_even_when_the_tick_returns_early(
    config_dir: Path, clock: _FakeClock, mono
) -> None:
    """An exited process returns before any probe; the wrapper still times it."""
    sup = _healthy_supervisor(config_dir, clock, mono)
    sup._proc = _FakeProc(pid=42301, returncode=137)
    ttl = ttl_for_tier("storage_service")

    def slow_poll():
        mono.jump_on_next = ttl + 1.0
        return 137

    with patch.object(sup._proc, "poll", side_effect=slow_poll), \
         structlog.testing.capture_logs() as logs:
        result = sup.heartbeat_once()
    assert result == (False, False)
    missed = [e for e in logs if e["event"] == "storage_service_heartbeat_missed_ttl"]
    assert len(missed) == 1 and missed[0]["phases_s"]["poll"] >= ttl


def test_scope_is_the_real_uid(config_dir: Path, clock: _FakeClock, mono) -> None:
    """Sanity: the fixture publishes under this uid, as production does."""
    sup = _healthy_supervisor(config_dir, clock, mono)
    assert sup._scope == str(os.getuid())
