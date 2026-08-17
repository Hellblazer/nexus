# SPDX-License-Identifier: AGPL-3.0-or-later
import re
from unittest.mock import MagicMock, patch

import pytest
import voyageai.error as _ve

from nexus.retry import _is_retryable_voyage_error, _voyage_with_retry

#: structlog's ConsoleRenderer emits ANSI colour when FORCE_COLOR is set, which
#: interleaves escape codes inside `key=value` pairs. Log-content assertions
#: strip it so they measure the log's CONTENT in every environment.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


# Review remediation (Reviewer A/I-4): reset the retry accumulators on
# every test entry. The counters live in module state (`nexus.retry`), and
# a test that asserts `stats["total_count"] == N` without a paired reset
# will flake when a prior test leaves residue behind (e.g. an assertion
# failure that skips the trailing `reset_retry_stats()` at the end of the
# test body).
@pytest.fixture(autouse=True)
def _reset_retry_stats_on_entry():
    from nexus.retry import reset_retry_stats
    reset_retry_stats()
    yield
    reset_retry_stats()


# ── _is_retryable_voyage_error oracle ───────────────────────────────────────

@pytest.mark.parametrize("exc,expected", [
    # Transient — retried by our wrapper (nexus-vatx Gap 1 extended this set
    # when we set max_retries=0 on voyageai.Client to make retries visible).
    (_ve.APIConnectionError("connection reset"), True),
    (_ve.TryAgain("try again"), True),
    (_ve.Timeout("timed out"), True),
    (_ve.RateLimitError("rate limited"), True),
    (_ve.ServiceUnavailableError("unavailable"), True),
    (_ve.ServerError("server error"), True),
    # User/config errors — never retried.
    (_ve.AuthenticationError("bad key"), False),
    (_ve.InvalidRequestError("bad input"), False),
    (ValueError("random"), False),
])
def test_retryable_oracle(exc: Exception, expected: bool) -> None:
    assert _is_retryable_voyage_error(exc) is expected


# ── _voyage_with_retry wrapper ──────────────────────────────────────────────

def test_success_no_retry() -> None:
    fn = MagicMock(return_value="ok")
    assert _voyage_with_retry(fn, "arg") == "ok"
    fn.assert_called_once_with("arg")


def test_success_after_transient() -> None:
    fn = MagicMock(side_effect=[_ve.APIConnectionError("down"), "ok"])
    assert _voyage_with_retry(fn) == "ok"
    assert fn.call_count == 2


def test_exhausted_then_raises() -> None:
    fn = MagicMock(side_effect=_ve.APIConnectionError("persistent"))
    with patch("nexus.retry.time.sleep"), pytest.raises(_ve.APIConnectionError):
        _voyage_with_retry(fn, max_attempts=3)
    assert fn.call_count == 3


def test_non_retryable_raises_immediately() -> None:
    fn = MagicMock(side_effect=_ve.AuthenticationError("bad key"))
    with pytest.raises(_ve.AuthenticationError):
        _voyage_with_retry(fn)
    fn.assert_called_once()


def test_try_again_retries() -> None:
    fn = MagicMock(side_effect=[_ve.TryAgain("wait"), "result"])
    assert _voyage_with_retry(fn) == "result"


# ── Extended transient set (nexus-vatx Gap 1) ───────────────────────────────


@pytest.mark.parametrize("err_cls", [
    _ve.RateLimitError,
    _ve.ServiceUnavailableError,
    _ve.ServerError,
    _ve.Timeout,
])
def test_extended_transient_errors_retry(err_cls: type) -> None:
    """Every transient Voyage error class now retries (previously only
    APIConnectionError + TryAgain). This matters because the ingest-side
    spikes reported in nexus-vatx were driven by rate-limit backoff —
    formerly silent because voyageai.Client's internal tenacity swallowed
    them."""
    fn = MagicMock(side_effect=[err_cls("transient"), "ok"])
    with patch("nexus.retry.time.sleep"):
        assert _voyage_with_retry(fn) == "ok"
    assert fn.call_count == 2


# ── Retry accumulator (nexus-vatx Gap 4) ────────────────────────────────────


def test_retry_accumulator_tracks_voyage_backoff_seconds() -> None:
    """Every retry delay in ``_voyage_with_retry`` is recorded so the CLI
    can report how much of an indexing run was spent waiting on transient
    errors (nexus-vatx Gap 4). Uses patched ``time.sleep`` so the test
    stays fast while still exercising the real accumulator path."""
    from nexus.retry import get_retry_stats, reset_retry_stats
    reset_retry_stats()
    fn = MagicMock(side_effect=[
        _ve.RateLimitError("429"),
        _ve.ServiceUnavailableError("503"),
        "ok",
    ])
    # nexus-8g79.32: pin random.random()=0.5 so jitter factor = 1.0.
    with patch("nexus.retry.time.sleep"), patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _voyage_with_retry(fn) == "ok"
    stats = get_retry_stats()
    # 1s first backoff + 2s second backoff (exponential, capped at 10s)
    assert stats["voyage_count"] == 2
    assert stats["voyage_seconds"] == pytest.approx(1.0 + 2.0)
    assert stats["total_count"] == 2
    reset_retry_stats()


def test_retry_accumulator_tracks_chroma_backoff_seconds() -> None:
    """Same contract for the chroma wrapper — its delays (2 → 4 s) also
    roll into the total so the summary captures both backoff paths."""
    from nexus.retry import (
        _vector_with_retry,
        get_retry_stats,
        reset_retry_stats,
    )
    reset_retry_stats()
    fn = MagicMock(side_effect=[Exception("503"), Exception("503"), "ok"])
    # nexus-8g79.32: pin random.random()=0.5 so jitter factor = 1.0.
    with patch("nexus.retry.time.sleep"), patch(
        "nexus.retry.random.random", return_value=0.5,
    ):
        assert _vector_with_retry(fn, max_attempts=3) == "ok"
    stats = get_retry_stats()
    assert stats["vector_count"] == 2
    # Chroma backoff is 2 → 4 s (exponential, capped at 30 s)
    assert stats["vector_seconds"] == pytest.approx(2.0 + 4.0)
    reset_retry_stats()


def test_retry_accumulator_reset_zeros_all_counters() -> None:
    from nexus.retry import (
        _add_vector_retry,
        _add_voyage_retry,
        get_retry_stats,
        reset_retry_stats,
    )
    _add_voyage_retry(5.0)
    _add_vector_retry(3.0)
    pre = get_retry_stats()
    assert pre["total_seconds"] == pytest.approx(8.0)
    reset_retry_stats()
    post = get_retry_stats()
    assert post == {
        "voyage_seconds": 0.0, "voyage_count": 0,
        "vector_seconds": 0.0, "vector_count": 0,
        "etl_seconds": 0.0, "etl_count": 0,
        "total_seconds": 0.0, "total_count": 0,
        # nexus-cy9u7: reset_retry_stats() also resets the shared
        # RateLimitBrake (nexus.rate_brake.reset_brake).
        "brake_trips": 0, "brake_seconds": 0.0,
    }


def test_retry_warn_log_fires_on_backoff(capsys) -> None:
    """Each retry decision must emit a WARN-level ``voyage_transient_error_retry``
    line carrying attempt + delay + error_type — nexus-vatx Gap 1 operator
    observability. Default structlog routes to stdout via ``PrintLoggerFactory``,
    so we capture stdout rather than caplog."""
    fn = MagicMock(side_effect=[
        _ve.RateLimitError("429"),
        _ve.ServiceUnavailableError("503"),
        "ok",
    ])
    with patch("nexus.retry.time.sleep"):
        assert _voyage_with_retry(fn) == "ok"
    captured = capsys.readouterr()
    # Strip ANSI first: structlog's ConsoleRenderer colorizes whenever
    # FORCE_COLOR is set (independent of isatty, so capsys does not disable it),
    # which splits `attempt=1` into `\x1b[36mattempt\x1b[0m=\x1b[35m1\x1b[0m`.
    # CI has no FORCE_COLOR, so this pin was green there and red in any
    # developer terminal that sets one.
    plain = _ANSI_RE.sub("", captured.out)
    warn_lines = [
        ln for ln in plain.splitlines()
        if "voyage_transient_error_retry" in ln and "warning" in ln
    ]
    assert len(warn_lines) == 2, (
        f"expected 2 WARN retry lines, got {len(warn_lines)}: {plain!r}"
    )
    # Each carries attempt, delay, and error_type so operators can tell
    # rate-limit from unavailable from connection drop.
    assert any("RateLimitError" in ln for ln in warn_lines)
    assert any("ServiceUnavailableError" in ln for ln in warn_lines)
    assert all("attempt=" in ln and "delay=" in ln for ln in warn_lines)


# nexus-sghyo (2026-08-06): every test in this section (T3Database
# Voyage-client-timeout, _embed_with_fallback retry-at-4-sites,
# _index_code_file embed retries, CCE/standard exhaustion propagation,
# config-timeout construction) directly exercised deleted client-side
# call sites — T3Database's voyage_api_key/voyageai.Client construction
# and doc_indexer._embed_with_fallback are retired outright (Hal
# determination 2026-07-28: "we do no embedding on the client"). No
# surviving subject: the generic _voyage_with_retry / _is_retryable_
# voyage_error / accumulator tests above this point are UNAFFECTED
# (they exercise the retry wrapper directly against a plain mock
# function, never a Voyage client) and remain the coverage for the
# retry machinery itself.

