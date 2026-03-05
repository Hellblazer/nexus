"""Tests for Voyage AI retry helpers and scoring.py singleton reset.

Covers:
  - _is_retryable_voyage_error oracle (nexus-k8dv)
  - _voyage_with_retry wrapper (nexus-k8dv)
  - _reset_voyage_client singleton reset (nexus-b1m0)
"""

from unittest.mock import MagicMock, patch

import pytest
import voyageai.error as _ve

from nexus.db.t3 import _is_retryable_voyage_error, _voyage_with_retry


# ── _is_retryable_voyage_error oracle ─────────────────────────────────────────

def test_retryable_voyage_api_connection_error() -> None:
    assert _is_retryable_voyage_error(_ve.APIConnectionError("connection reset")) is True


def test_retryable_voyage_try_again() -> None:
    assert _is_retryable_voyage_error(_ve.TryAgain("try again")) is True


def test_not_retryable_voyage_timeout() -> None:
    """Timeout handled by voyageai.Client built-in max_retries — not by outer wrapper."""
    assert _is_retryable_voyage_error(_ve.Timeout("timed out")) is False


def test_not_retryable_voyage_rate_limit() -> None:
    """RateLimitError handled by built-in max_retries."""
    assert _is_retryable_voyage_error(_ve.RateLimitError("rate limited")) is False


def test_not_retryable_voyage_service_unavailable() -> None:
    """ServiceUnavailableError handled by built-in max_retries."""
    assert _is_retryable_voyage_error(_ve.ServiceUnavailableError("unavailable")) is False


def test_not_retryable_voyage_auth_error() -> None:
    assert _is_retryable_voyage_error(_ve.AuthenticationError("bad key")) is False


def test_not_retryable_voyage_invalid_request() -> None:
    assert _is_retryable_voyage_error(_ve.InvalidRequestError("bad input")) is False


def test_not_retryable_plain_exception() -> None:
    assert _is_retryable_voyage_error(ValueError("random")) is False


# ── _voyage_with_retry wrapper ─────────────────────────────────────────────────

def test_voyage_with_retry_success_no_retry() -> None:
    fn = MagicMock(return_value="ok")
    result = _voyage_with_retry(fn, "arg")
    assert result == "ok"
    fn.assert_called_once_with("arg")


def test_voyage_with_retry_success_after_transient() -> None:
    """Succeeds on second attempt after one APIConnectionError."""
    fn = MagicMock(side_effect=[_ve.APIConnectionError("down"), "ok"])
    result = _voyage_with_retry(fn)
    assert result == "ok"
    assert fn.call_count == 2


def test_voyage_with_retry_fires_3_times_then_raises() -> None:
    """Retries max_attempts=3 times on APIConnectionError then re-raises."""
    fn = MagicMock(side_effect=_ve.APIConnectionError("persistent"))
    with pytest.raises(_ve.APIConnectionError):
        _voyage_with_retry(fn, max_attempts=3)
    assert fn.call_count == 3


def test_voyage_with_retry_non_retryable_raises_immediately() -> None:
    """AuthenticationError (non-retryable) raises on first attempt."""
    fn = MagicMock(side_effect=_ve.AuthenticationError("bad key"))
    with pytest.raises(_ve.AuthenticationError):
        _voyage_with_retry(fn)
    fn.assert_called_once()


def test_voyage_with_retry_try_again_retries() -> None:
    fn = MagicMock(side_effect=[_ve.TryAgain("wait"), "result"])
    result = _voyage_with_retry(fn)
    assert result == "result"
    assert fn.call_count == 2


# ── _reset_voyage_client singleton reset ──────────────────────────────────────

def test_reset_voyage_client_clears_singleton() -> None:
    """_reset_voyage_client() sets _voyage_instance = None; next call re-creates."""
    import nexus.scoring as scoring
    # Always get _reset_voyage_client from the CURRENT module object (not cached via 'from')
    # test_no_circular_imports reloads nexus.scoring, detaching cached imports.
    _reset_voyage_client = scoring._reset_voyage_client

    # Ensure clean slate (idempotent)
    _reset_voyage_client()
    assert scoring._voyage_instance is None

    # Inject a fake singleton
    sentinel = MagicMock()
    scoring._voyage_instance = sentinel
    assert scoring._voyage_instance is sentinel

    # Reset clears it
    _reset_voyage_client()
    assert scoring._voyage_instance is None

