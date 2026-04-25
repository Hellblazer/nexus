# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transient-error retry helpers for ChromaDB Cloud and Voyage AI.

Leaf module — no nexus.* imports.  Only stdlib + httpx + structlog + soft voyageai.error.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

import sqlite3

import httpx
import structlog

_log = structlog.get_logger(__name__)


# ── Retry accumulator (nexus-vatx Gap 4) ─────────────────────────────────────

# Process-local counters so the CLI can report how much of an indexing run
# was spent waiting on transient-error backoffs. Both ChromaDB and Voyage
# retries contribute. Concurrent voyage calls in pipeline_stages can
# increment from worker threads — hence the lock.
#
# Semantics note (Reviewer A/S-2): ``_add_*_retry(delay)`` is called BEFORE
# ``time.sleep(delay)``, so the counters measure *intended* backoff
# (total sleep time committed to). If the process is killed mid-sleep, the
# counter will over-count. That's acceptable — under-counting would hide
# the cause of a hang, over-counting at most overstates a stall we did
# actually decide to enter.
_retry_lock = threading.Lock()
_voyage_retry_seconds: float = 0.0
_voyage_retry_count: int = 0
_chroma_retry_seconds: float = 0.0
_chroma_retry_count: int = 0


def _add_voyage_retry(delay: float) -> None:
    global _voyage_retry_seconds, _voyage_retry_count
    with _retry_lock:
        _voyage_retry_seconds += delay
        _voyage_retry_count += 1


def _add_chroma_retry(delay: float) -> None:
    global _chroma_retry_seconds, _chroma_retry_count
    with _retry_lock:
        _chroma_retry_seconds += delay
        _chroma_retry_count += 1


def get_retry_stats() -> dict[str, float | int]:
    """Return a snapshot of retry counters — voyage + chroma, time + count.

    Returned keys: ``voyage_seconds``, ``voyage_count``, ``chroma_seconds``,
    ``chroma_count``, ``total_seconds``, ``total_count``. Resetting the
    counters is the caller's responsibility via :func:`reset_retry_stats`.
    """
    with _retry_lock:
        return {
            "voyage_seconds": _voyage_retry_seconds,
            "voyage_count": _voyage_retry_count,
            "chroma_seconds": _chroma_retry_seconds,
            "chroma_count": _chroma_retry_count,
            "total_seconds": _voyage_retry_seconds + _chroma_retry_seconds,
            "total_count": _voyage_retry_count + _chroma_retry_count,
        }


def reset_retry_stats() -> None:
    """Zero the process-local retry counters. CLI callers invoke this at
    the start of an indexing run so the end-of-run summary reflects only
    that run's backoffs."""
    global _voyage_retry_seconds, _voyage_retry_count
    global _chroma_retry_seconds, _chroma_retry_count
    with _retry_lock:
        _voyage_retry_seconds = 0.0
        _voyage_retry_count = 0
        _chroma_retry_seconds = 0.0
        _chroma_retry_count = 0


# ── ChromaDB transient-error retry ───────────────────────────────────────────

_RETRYABLE_FRAGMENTS: frozenset[str] = frozenset({
    "502", "503", "504", "429",
    "bad gateway", "service unavailable", "gateway time-out", "too many requests",
})
_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})


def _is_retryable_chroma_error(exc: BaseException) -> bool:
    """Return True if *exc* represents a transient ChromaDB error worth retrying.

    Check order:
    1. sqlite3.OperationalError with 'locked' — PersistentClient concurrent access.
    2. Transport-level errors (ConnectError, ReadTimeout, RemoteProtocolError) — always retry.
    3. Chained httpx.HTTPStatusError — authoritative integer status code check.
    4. String fallback — plain Exception message body (gateway HTML or chroma JSON).
    """
    # 1. PersistentClient concurrent write contention.
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
        return True
    # 2. Transport-level errors — no HTTP response, but clearly transient.
    if isinstance(exc, httpx.TransportError):
        return True
    # 3. ChromaDB wraps HTTPStatusError as Exception(resp.text); original is __context__.
    ctx = exc.__context__
    if isinstance(ctx, httpx.HTTPStatusError):
        return ctx.response.status_code in _RETRYABLE_HTTP_STATUSES
    # 4. Fallback: scan the message body for retryable status tokens.
    msg = str(exc).lower()
    return any(fragment in msg for fragment in _RETRYABLE_FRAGMENTS)


def _chroma_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 5,
    **kwargs: Any,
) -> Any:
    """Call *fn* with exponential backoff on transient ChromaDB Cloud errors.

    Retries up to *max_attempts* times (default 5).  Backoff starts at 2 s,
    doubles each attempt, capped at 30 s.  Non-retryable errors raise immediately.
    """
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == max_attempts or not _is_retryable_chroma_error(exc):
                raise
            _log.warning(
                "chroma_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error=str(exc)[:120],
            )
            _add_chroma_retry(delay)
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


# ── Voyage AI transient-error retry ──────────────────────────────────────────
#
# voyageai.error is imported lazily by ``_get_voyage_error_types()`` rather
# than at module load. The eager import was pulling
# voyageai -> langchain_text_splitters -> transformers -> torch into every
# CLI invocation that touches retry.py (which is the entire indexer / scoring
# / pipeline_stages graph). Lazy-init keeps ``nx <subcommand>`` cold-start
# free of torch.

_VOYAGE_ERROR_TYPES: tuple[type, ...] | None = None


def _get_voyage_error_types() -> tuple[type, ...]:
    """Return the voyage-error tuple, importing voyageai.error on first use.

    All transient classes are listed; ``voyageai.Client`` is constructed
    with ``max_retries=0`` at every nexus call site, so this wrapper is
    the sole retry authority and every retry decision surfaces through
    ``_log.warning`` (nexus-vatx Gap 1).

    Excluded: ``AuthenticationError``, ``InvalidRequestError``,
    ``MalformedRequestError`` (user/config errors, never transient).
    """
    global _VOYAGE_ERROR_TYPES
    if _VOYAGE_ERROR_TYPES is None:
        import voyageai.error as _voyageai_error  # noqa: PLC0415
        _VOYAGE_ERROR_TYPES = (
            _voyageai_error.APIConnectionError,
            _voyageai_error.TryAgain,
            _voyageai_error.RateLimitError,
            _voyageai_error.ServiceUnavailableError,
            _voyageai_error.ServerError,
            _voyageai_error.Timeout,
        )
    return _VOYAGE_ERROR_TYPES


def _is_retryable_voyage_error(exc: BaseException) -> bool:
    """Return True if *exc* is a transient Voyage AI error worth retrying.

    APIConnectionError, TryAgain, RateLimitError, ServiceUnavailableError,
    ServerError, and Timeout are retried — every attempt logs a WARN line so
    operators can tell "slow file" from "being rate-limited" from "network
    stalled." The two error spaces are disjoint; do not add Voyage AI types
    to :func:`_is_retryable_chroma_error`.
    """
    return isinstance(exc, _get_voyage_error_types())


def _voyage_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    **kwargs: Any,
) -> Any:
    """Call *fn* with backoff on transient Voyage AI errors.

    Retries up to *max_attempts* times (default 3). Backoff starts at 1 s,
    doubles each attempt, capped at 10 s. Non-retryable errors raise
    immediately. Each retry decision emits a WARN structlog line
    (``voyage_transient_error_retry``) so ingest-side observability reports
    rate-limit stalls instead of looking like silent multi-minute hangs
    (nexus-vatx Gap 1).
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == max_attempts or not _is_retryable_voyage_error(exc):
                raise
            _log.warning(
                "voyage_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )
            _add_voyage_retry(delay)
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
