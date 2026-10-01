# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-q81g7: the default ``RateLimitBrake`` must never wait on the real clock
inside the suite.

``RateLimitBrake.__init__`` binds ``clock=time.monotonic`` and ``sleep=time.sleep``
as default arguments at DEFINITION time (``src/nexus/rate_brake.py``), so a test
that patches ``nexus.retry.time.sleep`` reaches nothing: ``wait()`` loops until
the real monotonic clock passes the resume point. A 429 widens a write to 8
attempts and the brake schedule is 2+4+8+16+32+60+60 = 182 s, so
``test_rdr223_oversize_fallback.py::...[append-429]`` took 203 s and
``test_z0o2p12_note_write.py::TestSettlingFromEveryAttempt::
test_a_resend_that_fails_is_unknown`` took 138 s on every CI shard and every box.

``tests/conftest.py::_fresh_rate_limit_brake`` now builds the default brake on a
virtual clock whose ``sleep`` advances it. Brake-behaviour tests construct their
own ``RateLimitBrake`` with their own clock and are unaffected (the class they
import is the real one).
"""
from __future__ import annotations

import time

from nexus.rate_brake import RateLimitBrake, get_brake, reset_brake

#: Comfortably above any CPU noise on a loaded box, far below the delays tripped.
_REAL_SECONDS_BUDGET = 1.0


def test_the_default_brake_does_not_wait_on_the_real_clock() -> None:
    brake = get_brake()
    brake.trip(3.0, source="test")

    started = time.monotonic()
    waited = brake.wait()
    elapsed = time.monotonic() - started

    assert elapsed < _REAL_SECONDS_BUDGET, f"wait() blocked {elapsed:.2f}s of real time"
    # The wait still happened, on the virtual clock: it is not skipped.
    assert waited >= 3.0, waited
    assert brake.seconds_paused >= 3.0


def test_a_brake_reset_inside_a_test_is_virtual_too() -> None:
    """``reset_retry_stats()`` calls ``reset_brake()`` at the start of every
    ``nx index`` run, so a test driving that path replaces the fixture's brake
    with whatever ``reset_brake`` builds."""
    reset_brake()
    brake = get_brake()
    brake.trip(3.0, source="test")

    started = time.monotonic()
    brake.wait()
    assert time.monotonic() - started < _REAL_SECONDS_BUDGET


def test_an_escalating_trip_sequence_costs_no_real_time() -> None:
    """The 429 widening schedule that took 203 s: eight consecutive trips with
    no Retry-After escalate 2, 4, 8, ... 60 seconds."""
    brake = get_brake()
    started = time.monotonic()
    total_delay = 0.0
    for _ in range(8):
        total_delay += brake.trip(None, source="test")
        brake.wait()
    assert total_delay >= 182.0
    assert time.monotonic() - started < _REAL_SECONDS_BUDGET


def test_a_brake_given_its_own_clock_keeps_it() -> None:
    """The brake-behaviour contract: an explicit ``clock``/``sleep`` is honoured,
    not replaced by the suite's virtual default."""
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
