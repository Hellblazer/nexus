# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-q81g7: the default ``RateLimitBrake`` resolves ``time.monotonic`` and
``time.sleep`` at CALL time, so the seam the retry tests patch is the one it uses.

``RateLimitBrake.__init__`` used to bind ``clock=time.monotonic`` and
``sleep=time.sleep`` as default arguments at DEFINITION time. A test that patched
``nexus.retry.time.sleep`` (the same ``time`` module object) therefore reached
nothing: ``wait()`` slept on the real functions until the real monotonic clock
passed the resume point. A 429 widens a write to 8 attempts and the brake
schedule is 2+4+8+16+32+60+60 = 182 s, so
``test_rdr223_oversize_fallback.py::...[append-429]`` took 203 s and
``test_z0o2p12_note_write.py::TestSettlingFromEveryAttempt::
test_a_resend_that_fails_is_unknown`` took 138 s on every CI shard and box.

Fixed at the source (``None`` defaults, looked up inside) and in ``wait()``
(a sleep that returned counts as elapsed even if the clock did not move, so a
patched no-op ``sleep`` cannot make the loop spin on the real clock).

These tests use the REAL class with its REAL defaults. None goes through the
conftest fixture's brake, so each one fails if the defaults are bound early again.
"""
from __future__ import annotations

import inspect
import time

import pytest

from nexus.rate_brake import RateLimitBrake
from tests._module_seam import module_time

#: Comfortably above CPU noise on a loaded box, far below the delays tripped.
_REAL_SECONDS_BUDGET = 1.0


def test_the_production_defaults_are_late_bound() -> None:
    """The pin the fixture used to make impossible: nothing runs the default brake
    on a real clock any more, so assert on the signature itself. ``None`` is the
    'resolve ``time.monotonic`` / ``time.sleep`` when used' marker."""
    params = inspect.signature(RateLimitBrake.__init__).parameters
    assert params["clock"].default is None
    assert params["sleep"].default is None
    assert params["clock"].default is not time.monotonic
    assert params["sleep"].default is not time.sleep


def test_the_default_brake_sleeps_through_the_patched_time_module(monkeypatch) -> None:
    """The seam the slow tests patch: ``module_time(monkeypatch, "nexus.retry").sleep``,
    whose proxy ``tests/_time_seam.py`` shares with ``nexus.rate_brake`` (nexus-hkafl).
    A no-op there must make the brake's wait cost no real time, and the wait must
    still be REQUESTED (the brake asked to sleep ~3 s, it was not skipped)."""
    slept: list[float] = []
    module_time(monkeypatch, "nexus.retry").sleep = lambda seconds: slept.append(seconds)
    brake = RateLimitBrake()
    brake.trip(3.0, source="test")

    started = time.monotonic()
    waited = brake.wait()
    elapsed = time.monotonic() - started

    assert elapsed < _REAL_SECONDS_BUDGET, f"wait() blocked {elapsed:.2f}s of real time"
    assert sum(slept) >= 3.0, slept
    assert waited >= 3.0, waited


def test_the_default_brake_reads_the_patched_monotonic_clock(monkeypatch) -> None:
    """The clock half: with ``time.monotonic`` and ``time.sleep`` replaced by a
    fake pair, the default brake runs entirely on them."""
    now = [1000.0]
    module_time(monkeypatch, "nexus.rate_brake").monotonic = lambda: now[0]

    def fake_sleep(seconds: float) -> None:
        now[0] += seconds

    module_time(monkeypatch, "nexus.rate_brake").sleep = fake_sleep
    brake = RateLimitBrake(jitter=lambda: 0.0)
    assert brake.trip(5.0, source="test") == 5.0
    assert brake.wait() == pytest.approx(5.0)
    assert now[0] == pytest.approx(1005.0)


def test_an_escalating_trip_sequence_costs_no_real_time(monkeypatch) -> None:
    """The widened-429 schedule that took 203 s: eight consecutive trips with no
    Retry-After escalate 2, 4, 8 ... 60 seconds."""
    module_time(monkeypatch, "nexus.retry").sleep = lambda _s: None
    brake = RateLimitBrake()
    started = time.monotonic()
    total_delay = 0.0
    for _ in range(8):
        total_delay += brake.trip(None, source="test")
        brake.wait()
    assert total_delay >= 182.0
    assert time.monotonic() - started < _REAL_SECONDS_BUDGET


def test_a_brake_given_its_own_clock_ignores_the_time_module(monkeypatch) -> None:
    """The brake-behaviour contract. If an explicit ``clock`` / ``sleep`` were
    ignored in favour of the late-bound defaults, ``time.sleep`` (made to explode
    here) would be called and this test would fail."""
    def boom(*_a, **_k):
        raise AssertionError("an explicit clock/sleep must not fall back to time.*")

    module_time(monkeypatch, "nexus.rate_brake").sleep = boom
    module_time(monkeypatch, "nexus.rate_brake").monotonic = boom

    now = [100.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    brake = RateLimitBrake(
        clock=lambda: now[0], sleep=sleep, jitter=lambda: 0.0, wait_slice_seconds=100.0,
    )
    brake.trip(5.0, source="test")
    assert brake.wait() == 5.0
    assert slept == [5.0]
    assert now[0] == 105.0
