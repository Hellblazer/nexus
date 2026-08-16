# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cy9u7: process-wide shared rate-limit pause.

All timing here is driven by an injected fake clock/sleep — never real
``time.sleep`` — per the deterministic-testing convention (fixed clocks,
no real sleeps in unit tests). The one exception is
``test_trip_releases_all_waiters_at_shared_deadline``, which proves actual
thread-safety with real OS threads; it still uses the fake clock (no
production-duration real sleeps), synchronized via a
``threading.Condition`` so the driver never busy-polls or guesses a real
wall-clock delay.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from nexus.rate_brake import RateLimitBrake, get_brake, parse_retry_after, reset_brake


class _FakeClock:
    """A self-advancing fake clock for single-threaded tests:
    ``sleep(seconds)`` immediately advances ``now`` by that amount and
    returns — no real time.sleep is ever invoked."""

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self._now

    @property
    def now(self) -> float:
        return self.time()

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


class _SyncFakeClock:
    """A shared virtual clock for the multi-thread test.

    ``sleep(seconds)`` BLOCKS the calling thread until ``advance()``
    (driven by the test) has moved ``now`` far enough — never self-
    advances — so concurrent callers genuinely share one timeline instead
    of each independently fast-forwarding it. No real time.sleep is ever
    invoked; blocking is via ``threading.Condition``, woken by
    ``advance()``."""

    def __init__(self) -> None:
        self._now = 0.0
        self._waiting = 0
        self._cond = threading.Condition()

    def time(self) -> float:
        with self._cond:
            return self._now

    @property
    def now(self) -> float:
        return self.time()

    def sleep(self, seconds: float) -> None:
        with self._cond:
            target = self._now + seconds
            self._waiting += 1
            self._cond.notify_all()
            self._cond.wait_for(lambda: self._now >= target)
            self._waiting -= 1

    def advance(self, seconds: float) -> None:
        with self._cond:
            self._now += seconds
            self._cond.notify_all()

    def wait_for_waiters(self, count: int, timeout: float = 5.0) -> None:
        with self._cond:
            ok = self._cond.wait_for(lambda: self._waiting >= count, timeout=timeout)
        assert ok, f"expected {count} waiters, only {self._waiting} arrived within {timeout}s"


@pytest.fixture(autouse=True)
def _isolate_default_brake():
    reset_brake()
    yield
    reset_brake()


# ── RateLimitBrake.wait() ────────────────────────────────────────────────


def test_wait_is_noop_when_never_tripped() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep)
    assert brake.wait() == 0.0
    assert fc.now == 0.0


def test_wait_is_noop_after_resume_point_passes() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    brake.trip(1.0, source="vector")
    fc.advance(2.0)  # already past the resume point
    assert brake.wait() == 0.0


def test_trip_with_retry_after_blocks_wait_for_that_long() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    brake.trip(7.0, source="vector")
    waited = brake.wait()
    assert waited == pytest.approx(7.0)
    assert fc.now == pytest.approx(7.0)


def test_wake_jitter_adds_extra_pause() -> None:
    fc = _FakeClock()
    # jitter() pinned to 1.0 (max) -> extra = last_delay * 1.0 * 0.2
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 1.0)
    brake.trip(5.0, source="vector")
    waited = brake.wait()
    assert waited == pytest.approx(5.0 + 5.0 * 0.2)


def test_trip_releases_all_waiters_at_shared_deadline() -> None:
    """Three independent callers, all blocked in wait(), are all released
    only once the SAME shared deadline passes — proving the resume point
    is genuinely shared state, not per-caller."""
    fc = _SyncFakeClock()
    # A slice larger than the trip delay -> each wait() does exactly one
    # sleep() call, so a single advance() resolves every thread at once.
    brake = RateLimitBrake(
        clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0, wait_slice_seconds=100.0,
    )
    brake.trip(3.0, source="vector")

    results: list[float] = []
    results_lock = threading.Lock()

    def worker() -> None:
        waited = brake.wait()
        with results_lock:
            results.append(waited)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()

    fc.wait_for_waiters(3)
    fc.advance(3.0)

    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive()

    assert len(results) == 3
    assert all(r == pytest.approx(3.0) for r in results)


# ── RateLimitBrake.trip() escalation ─────────────────────────────────────


def test_trip_without_retry_after_escalates_and_caps() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    delays = [brake.trip(None, source="vector") for _ in range(8)]
    assert delays[:5] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert delays[5:] == [60.0, 60.0, 60.0]


def test_release_resets_escalation() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    assert brake.trip(None, source="vector") == 2.0
    assert brake.trip(None, source="vector") == 4.0
    brake.release()
    assert brake.trip(None, source="vector") == 2.0


def test_retry_after_trip_does_not_advance_escalation_counter() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    assert brake.trip(None, source="vector") == 2.0
    assert brake.trip(9.0, source="vector") == 9.0  # explicit, doesn't escalate
    assert brake.trip(None, source="vector") == 4.0  # continues from before the explicit trip


def test_stale_trip_resets_escalation_window() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(
        clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0, escalation_window_seconds=10.0,
    )
    assert brake.trip(None, source="vector") == 2.0
    fc.advance(11.0)  # past the escalation window
    assert brake.trip(None, source="vector") == 2.0  # fresh incident, not 4.0


# ── Stats ─────────────────────────────────────────────────────────────────


def test_stats_track_trips_and_pause_time_and_last_source() -> None:
    fc = _FakeClock()
    brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    assert brake.trips == 0
    assert brake.seconds_paused == 0.0
    assert brake.last_retry_after is None
    assert brake.last_source == ""

    brake.trip(2.0, source="vector")
    assert brake.trips == 1
    assert brake.last_retry_after == 2.0
    assert brake.last_source == "vector"

    waited = brake.wait()
    assert waited == 2.0
    assert brake.seconds_paused == pytest.approx(2.0)

    brake.trip(None, source="manifest")
    assert brake.trips == 2
    assert brake.last_retry_after is None
    assert brake.last_source == "manifest"


# ── get_brake() / reset_brake() ──────────────────────────────────────────


def test_get_brake_is_a_singleton() -> None:
    assert get_brake() is get_brake()


def test_reset_brake_replaces_the_singleton() -> None:
    b1 = get_brake()
    reset_brake()
    b2 = get_brake()
    assert b1 is not b2


# ── parse_retry_after ─────────────────────────────────────────────────────


def test_parse_retry_after_integer_seconds() -> None:
    assert parse_retry_after({"Retry-After": "120"}) == 120.0


def test_parse_retry_after_case_insensitive_plain_dict() -> None:
    assert parse_retry_after({"retry-after": "5"}) == 5.0


def test_parse_retry_after_http_date() -> None:
    target = datetime.now(timezone.utc) + timedelta(seconds=10)
    header = {"Retry-After": format_datetime(target, usegmt=True)}
    result = parse_retry_after(header)
    assert result is not None
    assert result == pytest.approx(10.0, abs=2.0)


def test_parse_retry_after_httpx_headers() -> None:
    request = httpx.Request("POST", "https://api.voyageai.com/v1/embeddings")
    response = httpx.Response(429, request=request, headers={"Retry-After": "3"})
    assert parse_retry_after(response.headers) == 3.0


def test_parse_retry_after_garbage_returns_none() -> None:
    assert parse_retry_after({"Retry-After": "banana"}) is None


def test_parse_retry_after_clamps_to_max() -> None:
    assert parse_retry_after({"Retry-After": "10000"}) == 300.0


def test_parse_retry_after_clamps_negative_to_zero() -> None:
    assert parse_retry_after({"Retry-After": "-5"}) == 0.0


def test_parse_retry_after_missing_header_returns_none() -> None:
    assert parse_retry_after({}) is None
    assert parse_retry_after(None) is None
