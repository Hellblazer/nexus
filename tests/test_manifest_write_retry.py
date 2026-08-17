# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1371: transient-connection retry for the catalog manifest write.

``_manifest_write_with_retry`` is scoped to CONNECTION-class failures only
(no HTTP-status classification, unlike the migration-scoped
``_etl_with_retry``) — a real 4xx from the catalog service must fail on the
first attempt so a genuine data problem surfaces immediately.

nexus-cy9u7 addendum: a rate-limit signal IS now retried too (routed
through the shared ``RateLimitBrake``) — see the "shared brake wiring"
section below. CRITICAL-2 fix (2026-08-16): the brake now ALSO trips on a
plain connectivity error, not only a rate-limit signal — see
``test_connect_error_also_trips_brake_with_escalating_default`` below.
That remains the one deliberate exception to "connection errors only" for
RETRY eligibility: a genuine 4xx (400/404/422) still fails fast, unchanged.
This file uses REAL httpx exceptions throughout — the manifest write path
genuinely is httpx-based (``http_catalog_client.py``), unlike the T3
vector client (see ``tests/test_vector_retry.py``'s urllib-shaped tests).
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest

import nexus.retry as retry_mod
from nexus.rate_brake import RateLimitBrake, reset_brake
from nexus.retry import (
    _is_connectivity_error,
    _manifest_write_with_retry,
)


class _FakeClock:
    """Self-advancing fake clock — no real time.sleep, deterministic."""

    def __init__(self) -> None:
        self._now = 0.0
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds


@pytest.fixture(autouse=True)
def _isolate_default_brake(monkeypatch):
    """nexus-cy9u7 CRITICAL-2: the brake now trips on EVERY retried
    failure here (connectivity errors included, not only a rate-limit
    signal) — see the identical fixture / rationale in
    tests/test_vector_retry.py. Installs a fast fake-clocked brake as the
    default for every test in this module."""
    fc = _FakeClock()
    fake_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: fake_brake)
    yield fake_brake
    reset_brake()


# ── _is_connectivity_error ─────────────────────────────────

@pytest.mark.parametrize("exc,expected", [
    (httpx.ConnectError("Connection refused"), True),
    (httpx.ConnectTimeout("timed out connecting"), True),
    (httpx.ReadTimeout("Read timed out"), True),
    (ConnectionError("connection reset"), True),
    (ConnectionRefusedError("refused"), True),
    (TimeoutError("timed out"), True),
    (ValueError("bad payload"), False),
    (RuntimeError("HTTP 400: validation failed"), False),
], ids=[
    "connect-error", "connect-timeout", "read-timeout", "connection-error",
    "connection-refused", "timeout-error", "value-error", "http-400-runtime",
])
def test_retryable_classification(exc: Exception, expected: bool) -> None:
    assert _is_connectivity_error(exc) is expected


def test_retryable_via_chained_cause() -> None:
    # A wrapper exception (e.g. a VectorServiceError-shaped raise ... from e)
    # chains the original connection failure as __cause__/__context__.
    wrapper = RuntimeError("manifest write failed")
    wrapper.__cause__ = httpx.ConnectError("Connection refused")
    assert _is_connectivity_error(wrapper) is True


def test_non_retryable_status_error_not_classified_as_connection() -> None:
    request = httpx.Request("POST", "http://127.0.0.1:8765/v1/catalog/manifest/write")
    response = httpx.Response(status_code=400, request=request)
    exc = httpx.HTTPStatusError("Bad Request", request=request, response=response)
    assert _is_connectivity_error(exc) is False


# ── _manifest_write_with_retry ──────────────────────────────────────────────

def test_succeeds_after_transient_connect_errors() -> None:
    call_count = 0

    def flaky_fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("transient connect failure")
        return "ok"

    with patch("nexus.retry.time.sleep"):
        result = _manifest_write_with_retry(flaky_fn)
    assert result == "ok"
    assert call_count == 3


def test_raises_immediately_on_non_connection_error() -> None:
    call_count = 0

    def failing_fn() -> None:
        nonlocal call_count
        call_count += 1
        raise ValueError("bad payload")

    with patch("nexus.retry.time.sleep") as mock_sleep:
        with pytest.raises(ValueError):
            _manifest_write_with_retry(failing_fn)
    assert call_count == 1
    mock_sleep.assert_not_called()


def test_raises_after_exhausting_retries_on_persistent_connection_error() -> None:
    call_count = 0

    def always_down() -> None:
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("connection refused")

    with patch("nexus.retry.time.sleep") as mock_sleep:
        with pytest.raises(httpx.ConnectError):
            _manifest_write_with_retry(always_down)
    # 1 initial + 3 retries = 4 attempts total; 3 sleeps between them.
    assert call_count == 4
    assert mock_sleep.call_count == 3
    # nexus-cy9u7 CRITICAL-2: the brake now trips (and floors) every one of
    # these too — escalating default 2.0 -> 4.0 -> 8.0 across three
    # consecutive process-wide trips, above the connectivity-only local
    # curve (0.5 -> 1.0 -> 2.0) that applied pre-fix.
    delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert delays == [2.0, 4.0, 8.0]


def test_passes_through_args_and_kwargs() -> None:
    def fn(a: int, *, b: int) -> int:
        return a + b

    with patch("nexus.retry.time.sleep"):
        assert _manifest_write_with_retry(fn, 1, b=2) == 3


def test_400_status_error_still_raises_immediately() -> None:
    """A genuine 4xx is neither a connectivity error nor a rate-limit
    signal — the nexus-cy9u7 addendum below must not weaken this."""
    request = httpx.Request("POST", "http://127.0.0.1:8765/v1/catalog/manifest/write")
    response = httpx.Response(400, request=request)
    err = httpx.HTTPStatusError("Bad Request", request=request, response=response)
    fn = MagicMock(side_effect=err)
    with patch("nexus.retry.time.sleep") as mock_sleep, pytest.raises(httpx.HTTPStatusError):
        _manifest_write_with_retry(fn)
    fn.assert_called_once()
    mock_sleep.assert_not_called()


# ── nexus-cy9u7: shared RateLimitBrake wiring ────────────────────────────────


def _make_429_exc(retry_after: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1:8765/v1/catalog/manifest/write")
    response = httpx.Response(429, request=request, headers={"Retry-After": retry_after})
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


def test_429_is_retried_and_trips_shared_brake(monkeypatch) -> None:
    fc = _FakeClock()
    test_brake = RateLimitBrake(clock=fc.time, sleep=fc.sleep, jitter=lambda: 0.0)
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise _make_429_exc(retry_after="4")
        return "ok"

    # base connectivity delay (0.5) < brake delay (4.0) -> floor wins.
    with patch("nexus.retry.time.sleep") as mock_sleep:
        assert _manifest_write_with_retry(flaky) == "ok"
    assert call_count == 2
    assert test_brake.trips == 1
    assert test_brake.last_source == "manifest"
    mock_sleep.assert_called_once_with(4.0)


def test_success_releases_brake() -> None:
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    with patch.object(retry_mod, "get_brake", return_value=test_brake):
        fn = MagicMock(return_value="ok")
        with patch("nexus.retry.time.sleep"):
            assert _manifest_write_with_retry(fn) == "ok"
    test_brake.wait.assert_called_once()
    test_brake.release.assert_called_once()


def test_connect_error_also_trips_brake_with_escalating_default(monkeypatch) -> None:
    """nexus-cy9u7 CRITICAL-2 fix (2026-08-16): renamed from
    ``test_connect_error_does_not_trip_brake`` — that was the OLD, narrow
    behaviour. A plain connectivity error is a retryable transport failure
    (the 2026-08-15 incident's exact class, generalised beyond rate-limit
    statuses), so it now trips the shared brake too, floored at the
    escalating default since it carries no server-authoritative delay."""
    test_brake = MagicMock()
    test_brake.wait.return_value = 0.0
    test_brake.trip.return_value = 2.0  # escalating default, first trip
    monkeypatch.setattr(retry_mod, "get_brake", lambda: test_brake)

    call_count = 0

    def flaky() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 2:
            raise httpx.ConnectError("transient connect failure")
        return "ok"

    with patch("nexus.retry.time.sleep") as mock_sleep:
        assert _manifest_write_with_retry(flaky) == "ok"
    test_brake.trip.assert_called_once_with(None, source="manifest")
    test_brake.release.assert_called_once()
    mock_sleep.assert_called_once_with(2.0)  # max(local 0.5, brake 2.0)
