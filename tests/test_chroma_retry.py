# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for ChromaDB transient-error retry helpers.

TDD RED phase: these tests import _is_retryable_chroma_error and
_chroma_with_retry from nexus.db.t3, which do not exist yet.  All
tests are expected to fail with ImportError until T2 (TDD GREEN) is
implemented.

RDR: docs/rdr/rdr-019-chromadb-transient-retry.md
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import httpx
import pytest

from nexus.db.t3 import _chroma_with_retry, _is_retryable_chroma_error


# ── _is_retryable_chroma_error ────────────────────────────────────────────────


def test_retryable_504_string_fallback() -> None:
    """Exception whose message contains '504' gateway text returns True (string fallback path)."""
    exc = Exception("504 Gateway Time-out HTML")
    assert _is_retryable_chroma_error(exc) is True


def test_non_retryable_400_string() -> None:
    """Exception whose message is a 400 Bad Request returns False (non-retryable)."""
    exc = Exception("400 Bad Request: invalid payload")
    assert _is_retryable_chroma_error(exc) is False


def _make_chained_exc(status_code: int) -> Exception:
    """Build a plain Exception with an httpx.HTTPStatusError as __context__."""
    request = httpx.Request("GET", "https://api.trychroma.com/")
    response = httpx.Response(status_code=status_code, request=request)
    http_err = httpx.HTTPStatusError(
        f"Server error '{status_code}'", request=request, response=response
    )
    plain_exc = Exception(f"<html>Gateway error {status_code}</html>")
    plain_exc.__context__ = http_err
    return plain_exc


def test_retryable_429_via_chained_httpx_status() -> None:
    """Exception with chained httpx.HTTPStatusError(429) returns True (integer check path)."""
    exc = _make_chained_exc(429)
    assert _is_retryable_chroma_error(exc) is True


def test_non_retryable_404_via_chained_httpx_status() -> None:
    """Exception with chained httpx.HTTPStatusError(404) returns False (integer check path)."""
    exc = _make_chained_exc(404)
    assert _is_retryable_chroma_error(exc) is False


def test_retryable_connect_error_transport() -> None:
    """httpx.ConnectError returns True (transport isinstance path)."""
    exc = httpx.ConnectError("Connection refused")
    assert _is_retryable_chroma_error(exc) is True


def test_retryable_read_timeout_transport() -> None:
    """httpx.ReadTimeout returns True (transport isinstance path)."""
    exc = httpx.ReadTimeout("Read timed out")
    assert _is_retryable_chroma_error(exc) is True


def test_retryable_remote_protocol_error_transport() -> None:
    """httpx.RemoteProtocolError returns True (transport isinstance path)."""
    exc = httpx.RemoteProtocolError("Server disconnected without response")
    assert _is_retryable_chroma_error(exc) is True


# ── _chroma_with_retry ────────────────────────────────────────────────────────


def test_retry_connect_error_twice_then_success() -> None:
    """fn raises ConnectError on attempts 1 and 2, succeeds on attempt 3.

    Verifies: fn called 3 times, time.sleep called with 2.0 then 4.0.
    """
    call_count = 0

    def flaky_fn() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.ConnectError("transient connect failure")
        return "ok"

    with patch("nexus.db.t3.time") as mock_time:
        result = _chroma_with_retry(flaky_fn)

    assert result == "ok"
    assert call_count == 3
    assert mock_time.sleep.call_args_list == [call(2.0), call(4.0)]


def test_all_attempts_exhausted_on_persistent_504() -> None:
    """fn raises 504 on every attempt; _chroma_with_retry raises after 5 attempts."""
    fn = MagicMock(side_effect=Exception("504 Gateway Time-out"))

    with patch("nexus.db.t3.time"):
        with pytest.raises(Exception, match="504"):
            _chroma_with_retry(fn, max_attempts=5)

    assert fn.call_count == 5


def test_non_retryable_400_raises_immediately() -> None:
    """fn raises 400 on first attempt; _chroma_with_retry re-raises immediately without sleeping."""
    fn = MagicMock(side_effect=Exception("400 Bad Request: invalid collection name"))

    with patch("nexus.db.t3.time") as mock_time:
        with pytest.raises(Exception, match="400"):
            _chroma_with_retry(fn)

    fn.assert_called_once()
    mock_time.sleep.assert_not_called()


def test_backoff_curve_2_4_8_16() -> None:
    """Exponential backoff: sleep args follow 2.0, 4.0, 8.0, 16.0 over 4 retries before 5th attempt."""
    call_count = 0

    def fn_succeeds_on_5th() -> str:
        nonlocal call_count
        call_count += 1
        if call_count < 5:
            raise Exception("503 Service Unavailable")
        return "done"

    with patch("nexus.db.t3.time") as mock_time:
        result = _chroma_with_retry(fn_succeeds_on_5th, max_attempts=5)

    assert result == "done"
    assert mock_time.sleep.call_args_list == [
        call(2.0),
        call(4.0),
        call(8.0),
        call(16.0),
    ]
