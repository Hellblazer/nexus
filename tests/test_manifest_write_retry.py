# SPDX-License-Identifier: AGPL-3.0-or-later
"""GH #1371: transient-connection retry for the catalog manifest write.

``_manifest_write_with_retry`` is scoped to CONNECTION-class failures only
(no HTTP-status classification, unlike the migration-scoped
``_etl_with_retry``) — a real 4xx from the catalog service must fail on the
first attempt so a genuine data problem surfaces immediately.
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from nexus.retry import (
    _is_retryable_manifest_connection_error,
    _manifest_write_with_retry,
)


# ── _is_retryable_manifest_connection_error ─────────────────────────────────

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
    assert _is_retryable_manifest_connection_error(exc) is expected


def test_retryable_via_chained_cause() -> None:
    # A wrapper exception (e.g. a VectorServiceError-shaped raise ... from e)
    # chains the original connection failure as __cause__/__context__.
    wrapper = RuntimeError("manifest write failed")
    wrapper.__cause__ = httpx.ConnectError("Connection refused")
    assert _is_retryable_manifest_connection_error(wrapper) is True


def test_non_retryable_status_error_not_classified_as_connection() -> None:
    request = httpx.Request("POST", "http://127.0.0.1:8765/v1/catalog/manifest/write")
    response = httpx.Response(status_code=400, request=request)
    exc = httpx.HTTPStatusError("Bad Request", request=request, response=response)
    assert _is_retryable_manifest_connection_error(exc) is False


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
    delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert delays == [0.5, 1.0, 2.0]


def test_passes_through_args_and_kwargs() -> None:
    def fn(a: int, *, b: int) -> int:
        return a + b

    with patch("nexus.retry.time.sleep"):
        assert _manifest_write_with_retry(fn, 1, b=2) == 3
