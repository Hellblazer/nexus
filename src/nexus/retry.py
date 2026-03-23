# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transient-error retry helpers for ChromaDB Cloud and Voyage AI.

Leaf module — no nexus.* imports.  Only stdlib + httpx + structlog + soft voyageai.error.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import sqlite3

import httpx
import structlog

_log = structlog.get_logger(__name__)


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
            time.sleep(delay)
            delay = min(delay * 2, 30.0)


# ── Voyage AI transient-error retry ──────────────────────────────────────────

try:
    import voyageai.error as _voyageai_error
    _VOYAGE_ERROR_TYPES: tuple[type, ...] | None = (
        _voyageai_error.APIConnectionError,
        _voyageai_error.TryAgain,
    )
except Exception:  # ImportError or Pydantic v1 ValueError on Python ≥ 3.14
    _VOYAGE_ERROR_TYPES = None


def _is_retryable_voyage_error(exc: BaseException) -> bool:
    """Return True if *exc* is a transient Voyage AI error worth retrying.

    Only APIConnectionError and TryAgain are retried here.  Timeout,
    RateLimitError, and ServiceUnavailableError are handled by the built-in
    ``max_retries`` on ``voyageai.Client`` (tenacity-based).  The two error
    spaces are disjoint; do not add Voyage AI types to _is_retryable_chroma_error.
    """
    return bool(_VOYAGE_ERROR_TYPES and isinstance(exc, _VOYAGE_ERROR_TYPES))


def _voyage_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    **kwargs: Any,
) -> Any:
    """Call *fn* with backoff on transient Voyage AI errors (APIConnectionError, TryAgain).

    Retries up to *max_attempts* times (default 3).  Backoff starts at 1 s,
    doubles each attempt, capped at 10 s.  Non-retryable errors raise immediately.
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
                error=str(exc)[:120],
            )
            time.sleep(delay)
            delay = min(delay * 2, 10.0)
