# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transient-error retry helpers for ChromaDB Cloud, Voyage AI, and the
migration ETLs.

Leaf module — no nexus.* imports.  Only stdlib + httpx + structlog + soft voyageai.error.
"""
from __future__ import annotations

import random
import threading
import time
import urllib.error
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
_vector_retry_seconds: float = 0.0
_vector_retry_count: int = 0


def _add_voyage_retry(delay: float) -> None:
    global _voyage_retry_seconds, _voyage_retry_count
    with _retry_lock:
        _voyage_retry_seconds += delay
        _voyage_retry_count += 1


def _add_vector_retry(delay: float) -> None:
    global _vector_retry_seconds, _vector_retry_count
    with _retry_lock:
        _vector_retry_seconds += delay
        _vector_retry_count += 1


def get_retry_stats() -> dict[str, float | int]:
    """Return a snapshot of retry counters — voyage + vector, time + count.

    Returned keys: ``voyage_seconds``, ``voyage_count``, ``vector_seconds``
    (pre-P0d ``chroma_seconds``), ``vector_count``, ``etl_seconds``,
    ``etl_count``, ``total_seconds``, ``total_count``. Resetting the counters is the caller's responsibility via
    :func:`reset_retry_stats`.
    """
    with _retry_lock:
        return {
            "voyage_seconds": _voyage_retry_seconds,
            "voyage_count": _voyage_retry_count,
            "vector_seconds": _vector_retry_seconds,
            "vector_count": _vector_retry_count,
            "etl_seconds": _etl_retry_seconds,
            "etl_count": _etl_retry_count,
            "total_seconds": _voyage_retry_seconds + _vector_retry_seconds + _etl_retry_seconds,
            "total_count": _voyage_retry_count + _vector_retry_count + _etl_retry_count,
        }


def reset_retry_stats() -> None:
    """Zero the process-local retry counters. CLI callers invoke this at
    the start of an indexing run so the end-of-run summary reflects only
    that run's backoffs."""
    global _voyage_retry_seconds, _voyage_retry_count
    global _vector_retry_seconds, _vector_retry_count
    global _etl_retry_seconds, _etl_retry_count
    with _retry_lock:
        _voyage_retry_seconds = 0.0
        _voyage_retry_count = 0
        _vector_retry_seconds = 0.0
        _vector_retry_count = 0
        _etl_retry_seconds = 0.0
        _etl_retry_count = 0


# ── ChromaDB transient-error retry ───────────────────────────────────────────

_RETRYABLE_FRAGMENTS: frozenset[str] = frozenset({
    "502", "503", "504", "429",
    "bad gateway", "service unavailable", "gateway time-out", "too many requests",
})
_RETRYABLE_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})


def _is_retryable_vector_error(exc: BaseException) -> bool:
    """Return True if *exc* is a transient vector-store error worth retrying.

    Renamed from ``_is_retryable_chroma_error`` at RDR-155 P4b P0d — the
    classification is substrate-generic (httpx transport/status + message
    fragments) and serves the PG-backed HttpVectorClient path.

    Check order:
    1. sqlite3.OperationalError with 'locked' — the Chroma
       PersistentClient contention leg; dead code once the migration
       read legs delete at P2 (remove WITH them, not before).
    2. Transport-level errors (ConnectError, ReadTimeout, RemoteProtocolError) — always retry.
    3. Chained httpx.HTTPStatusError — authoritative integer status code check.
    4. String fallback — plain Exception message body (gateway HTML or service JSON).
    """
    # 1. Chroma PersistentClient concurrent write contention (dies at P2).
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


def _vector_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 5,
    **kwargs: Any,
) -> Any:
    """Call *fn* with exponential backoff on transient vector-store errors.

    Renamed from ``_chroma_with_retry`` at RDR-155 P4b P0d. Retries up to
    *max_attempts* times (default 5).  Backoff starts at 2 s, doubles each
    attempt, capped at 30 s.  Non-retryable errors raise immediately.
    """
    delay = 2.0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == max_attempts or not _is_retryable_vector_error(exc):
                raise
            _log.warning(
                "vector_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error=str(exc)[:120],
            )
            # nexus-8g79.32: jittered sleep so multiple concurrent
            # workers retrying after a shared rate-limit do not all wake
            # at the same instant and re-fire the limit. ±20% of delay.
            jittered = delay * (1.0 + (random.random() - 0.5) * 0.4)
            _add_vector_retry(jittered)
            time.sleep(jittered)
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
        import voyageai.error as _voyageai_error  # noqa: PLC0415  — optional/heavy dependency deferred (voyageai)
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
    to :func:`_is_retryable_vector_error`.
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
            # nexus-8g79.32: jittered sleep, see chroma path above.
            jittered = delay * (1.0 + (random.random() - 0.5) * 0.4)
            _add_voyage_retry(jittered)
            time.sleep(jittered)
            delay = min(delay * 2, 10.0)


# ── Migration-ETL transient-edge retry (RDR-176 Gap 6) ───────────────────────
#
# The managed migration round-trips many records over the nginx edge. A
# transient edge 403, a connection drop, or a read-timeout intermittently
# strands a leg (vectors) or records a whole batch failed (T2) — prod
# observed the vector leg succeeding only after two transient-403 retries.
# Idempotent upsert / ON CONFLICT makes a BOUNDED re-send safe.
#
# This is MIGRATION-SCOPED on purpose: it classifies a 403 as retryable, which
# would be wrong for a normal runtime store call (a real auth failure must
# fail fast). It is therefore applied at the ETL call sites, NOT in the shared
# HTTP-client `_post`. A genuinely-forbidden request still surfaces — it just
# exhausts the (small) attempt bound first, then raises with its remedy.

#: Transient edge statuses retried at the status level; 400/404/422 are real
#: client errors and fail fast. Connection drops / read-timeouts retry via the
#: transport-level checks below.
#:
#: RDR-178 Gap 3 (nexus-ob4vc, 2026-07-01 incident): this set used to be
#: ``{403}`` only. 429/502/503/504 — the CANONICAL transient class for an
#: overloaded ingress — fell straight through ``_is_retryable_etl_error`` as
#: "not retryable", so a batch that hit a 502 raised on the FIRST attempt
#: with zero backoff. The call sites (``chash_etl``, the ``catalog_etl``
#: table imports) already routed every batch through ``_etl_with_retry`` —
#: the bug was this classifier's scope, not a bypassed call site. See
#: ``EtlCircuitBreaker`` below for the companion fix (pacing a SUSTAINED
#: outage rather than burning through batches at import speed).
_RETRYABLE_ETL_HTTP_STATUSES: frozenset[int] = frozenset({403, 429, 502, 503, 504})

_etl_retry_seconds: float = 0.0
_etl_retry_count: int = 0


def _add_etl_retry(delay: float) -> None:
    global _etl_retry_seconds, _etl_retry_count
    with _retry_lock:
        _etl_retry_seconds += delay
        _etl_retry_count += 1


def _is_retryable_etl_error(exc: BaseException) -> bool:
    """Return True if *exc* is a transient migration-edge failure worth a bounded
    retry: a nginx edge 403, a connection drop, or a read-timeout.

    Real client errors (400/404/422), and any 401 (token rotation — handled by
    the vector client's own auto-restart), are NOT retried here.
    """
    # Transport-level httpx (ConnectError, ReadTimeout, RemoteProtocolError, …).
    if isinstance(exc, httpx.TransportError):
        return True
    # httpx response error — only the transient edge 403.
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_ETL_HTTP_STATUSES
    # urllib.error.HTTPError is a URLError subclass — check it FIRST so a 404
    # does not fall through to the blanket URLError (drop) branch below.
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _RETRYABLE_ETL_HTTP_STATUSES
    # urllib transport drop (no HTTP response).
    if isinstance(exc, urllib.error.URLError):
        return True
    # stdlib read-timeout / connection drop (socket.timeout aliases TimeoutError
    # on 3.10+; ConnectionResetError ⊂ ConnectionError).
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # VectorServiceError-like: an explicit integer ``.code`` (duck-typed so this
    # leaf module imports no nexus.*). A transient edge 403 wrapper carries
    # ``code=403`` and retries here.
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code in _RETRYABLE_ETL_HTTP_STATUSES
    # Transport drop wrapped with no code: the managed vector path reframes a
    # urllib/connection/timeout failure as ``VectorServiceError(code=None)`` via
    # ``raise … from e``. Classify by the chained cause so a managed-path drop /
    # read-timeout retries exactly like the local-path raw error would (the
    # code-review gap: code=None wrappers were silently not retried).
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    return False


def _etl_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    max_attempts: int = 3,
    **kwargs: Any,
) -> Any:
    """Call *fn* with bounded backoff on transient migration-edge errors.

    Retries up to *max_attempts* (default 3). Backoff 1→2 s between the attempts
    (max_attempts=3 sleeps twice: ~1s + ~2s ≈ 3s of added latency before the
    final raise), capped at 10 s, ±20% jitter. Non-transient errors (and the
    final attempt) raise immediately. Each retry emits a WARN line
    (``etl_transient_error_retry``) so a stalled migration leg is visible.

    Two caveats: (1) a PERSISTENT failure (e.g. a real auth 403) is retried as
    "transient" — the WARN carries ``persistent_if_all_fail=True`` to flag that
    triage should treat repeated lines as a real failure, and the final raised
    exception still carries its remedy message. (2) a genuine server STALL (the
    request never returns) is bounded by the caller's per-call timeout, NOT by
    this helper; retrying multiplies the worst-case stall by up to
    *max_attempts* (e.g. 3× the vector leg's 600 s upsert timeout). The retry
    only shortens recovery from errors that RAISE; it does not add a timeout.

    Safe because every migration write is idempotent (upsert / ON CONFLICT), so
    re-sending a batch that may have partially landed is a no-op on the dupes.
    """
    delay = 1.0
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if attempt == max_attempts or not _is_retryable_etl_error(exc):
                raise
            _log.warning(
                "etl_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error_type=type(exc).__name__,
                error=str(exc)[:120],
                persistent_if_all_fail=True,
            )
            jittered = delay * (1.0 + (random.random() - 0.5) * 0.4)
            _add_etl_retry(jittered)
            time.sleep(jittered)
            delay = min(delay * 2, 10.0)


# ── ETL circuit breaker (RDR-178 Gap 3, nexus-ob4vc) ─────────────────────────
#
# 2026-07-01 incident: two concurrent chash-import legs overloaded the
# ingress; nginx answered 502 for ~10s. Every batch in flight during that
# window failed PERMANENTLY at ~3 batches/second with zero backoff
# (structlog ``chash_etl_batch_error``), and 270 catalog manifest
# (``document_chunks``) rows were lost in the same window. Root cause was
# TWO bugs, both fixed here:
#
#   1. ``_is_retryable_etl_error`` scoped the retryable HTTP-status set to
#      ``{403}`` only, so a 502/503/504/429 raised on the first attempt with
#      no backoff at all even though the call site DID route through
#      ``_etl_with_retry`` (see ``_RETRYABLE_ETL_HTTP_STATUSES`` above).
#   2. The ``document_chunks`` manifest write in ``catalog_etl.py`` called
#      ``client._post(...)`` DIRECTLY — it never routed through
#      ``_etl_with_retry`` at all (a genuine bypassed call site, unlike the
#      chash leg). Fixed at the call site, not here.
#
# ``EtlCircuitBreaker`` is the pacing half of the fix: bug (1) alone means a
# SUSTAINED outage (longer than one bounded ``_etl_with_retry`` cycle: up to
# ~3s of backoff across 3 attempts) still burns through every batch in the
# leg at import speed, each one permanently failed. The breaker instead
# retries the SAME batch (every migration write is idempotent) and, after
# ``trip_threshold`` consecutive exhausted cycles, pauses ``pause_seconds``
# before resuming — "idempotent re-runs recovered everything" (the incident
# post-mortem) is what this automates inline instead of requiring an
# operator to notice the failed report and re-run migrate-all by hand. A
# genuinely non-retryable error (a real 400/404/422/401) still raises
# immediately on the first attempt — same fail-fast semantics as
# ``_etl_with_retry`` alone; the breaker never intercepts those.

#: Consecutive exhausted-retry cycles before the breaker pauses the loop.
_ETL_BREAKER_TRIP_THRESHOLD: int = 3

#: Pause duration (seconds) once the breaker trips.
_ETL_BREAKER_PAUSE_SECONDS: float = 30.0

#: Outer sanity ceiling on trips per batch — after this many pauses (~100
#: minutes of pause time at the default 30s) a batch gives up and raises, so
#: a genuinely DEAD (not transient) endpoint cannot hang an unattended
#: migration forever. The caller's existing per-batch except/record path
#: then attributes the failure — never silently swallowed.
_ETL_BREAKER_MAX_TRIPS: int = 20


class EtlCircuitBreaker:
    """Per-ETL-run state: consecutive exhausted-retry cycles + trip count.

    Share ONE instance across every batch in a single ETL leg/table (pass it
    into :func:`_etl_batch_with_breaker` at each call) so "N consecutive"
    reflects the whole leg's health, not just one batch's retries.
    Not thread-safe — construct one per sequential ETL run; the migration
    ETLs are single-threaded batch loops.
    """

    def __init__(
        self,
        *,
        trip_threshold: int = _ETL_BREAKER_TRIP_THRESHOLD,
        pause_seconds: float = _ETL_BREAKER_PAUSE_SECONDS,
        max_trips: int = _ETL_BREAKER_MAX_TRIPS,
    ) -> None:
        self.trip_threshold = trip_threshold
        self.pause_seconds = pause_seconds
        self.max_trips = max_trips
        self.consecutive_failures = 0
        self.trip_count = 0


def _etl_batch_with_breaker(
    fn: Callable[..., Any],
    *args: Any,
    breaker: EtlCircuitBreaker,
    max_attempts: int = 3,
    **kwargs: Any,
) -> Any:
    """Call *fn* through :func:`_etl_with_retry`, pausing the batch loop
    instead of permanently dropping a batch when a SUSTAINED outage outlasts
    one bounded retry cycle (RDR-178 Gap 3).

    On a retryable-but-exhausted failure the SAME call is retried (every
    migration write is idempotent) after recording the failure against
    *breaker*. Every ``breaker.trip_threshold``-th consecutive exhaustion
    pauses ``breaker.pause_seconds`` (loud WARN structlog events on trip and
    on resume) before continuing. A non-retryable error (a real
    400/404/422/401) raises immediately, identical to :func:`_etl_with_retry`
    alone — the breaker never intercepts those. After
    ``breaker.max_trips`` pauses the call gives up and re-raises so a
    genuinely dead endpoint cannot hang forever; the caller's existing
    per-batch except/record path then attributes the failure.
    """
    while True:
        try:
            result = _etl_with_retry(fn, *args, max_attempts=max_attempts, **kwargs)
        except Exception as exc:
            if not _is_retryable_etl_error(exc):
                raise
            breaker.consecutive_failures += 1
            _log.error(
                "etl_batch_exhausted_retry",
                consecutive=breaker.consecutive_failures,
                trip_threshold=breaker.trip_threshold,
                error_type=type(exc).__name__,
                error=str(exc)[:160],
            )
            if breaker.consecutive_failures < breaker.trip_threshold:
                continue
            if breaker.trip_count >= breaker.max_trips:
                _log.error(
                    "etl_circuit_breaker_giving_up",
                    trip_count=breaker.trip_count,
                    max_trips=breaker.max_trips,
                )
                raise
            breaker.trip_count += 1
            _log.warning(
                "etl_circuit_breaker_tripped",
                consecutive=breaker.consecutive_failures,
                pause_seconds=breaker.pause_seconds,
                trip_count=breaker.trip_count,
            )
            time.sleep(breaker.pause_seconds)
            breaker.consecutive_failures = 0
            _log.warning("etl_circuit_breaker_resumed", trip_count=breaker.trip_count)
            continue
        else:
            breaker.consecutive_failures = 0
            return result


# ── Catalog manifest-write transient-connection retry (GH #1371) ────────────
#
# The catalog manifest-write hook (mcp_infra._manifest_write_loop) is
# best-effort by contract (nexus-zq79): any failure is swallowed into a
# WARNING log and must never propagate to the indexing caller. Prior to
# this fix, a transient connection blip to the catalog engine-service
# (``httpx.ConnectError`` while the service was briefly restarting) was
# treated identically to a permanent failure — the manifest write was lost
# with zero retry, silently leaving ``catalog_document_chunks`` empty for
# that document (17 of 24 audited entries in the reported incident).
#
# Deliberately narrower than ``_is_retryable_etl_error``: this classifies
# CONNECTION-level failures only, never by HTTP status code. A real 4xx
# from the catalog service (a bad payload, an FK violation) must still fail
# on the first attempt — that is a genuine data problem, not a transient
# network blip, and retrying it would only delay the WARNING that makes it
# discoverable.

#: 1 initial attempt + 3 retries, backing off 0.5s -> 1s -> 2s (~3.5s of
#: added latency in the worst case). The catalog engine-service is a
#: local-to-local connection that is usually just slow to start, not down
#: for minutes — this is a short bounded wait, not the ETL breaker's
#: sustained-outage pacing.
_MANIFEST_WRITE_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)


def _is_retryable_manifest_connection_error(exc: BaseException) -> bool:
    """Return True if *exc* is a transient connection-level failure worth
    retrying a catalog manifest write.

    Checks ``httpx.TransportError`` (covers ``ConnectError``,
    ``ConnectTimeout``, ``ReadTimeout``, etc.), the stdlib
    ``ConnectionError``/``TimeoutError``, and — since the vector/catalog
    HTTP clients sometimes reframe a transport drop as an application
    error via ``raise ... from e`` — the chained ``__cause__``/
    ``__context__``. Does NOT inspect HTTP status codes: an
    ``httpx.HTTPStatusError`` (a real 4xx/5xx response) is never retried
    here, unlike the migration-scoped ``_is_retryable_etl_error``.
    """
    if isinstance(exc, (httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, (httpx.TransportError, ConnectionError, TimeoutError)):
        return True
    return False


def _manifest_write_with_retry(
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call *fn* with a short bounded backoff on transient connection errors.

    4 attempts total (1 initial + 3 retries per
    :data:`_MANIFEST_WRITE_RETRY_DELAYS`). Non-connection errors (a real
    4xx, an application-level ``ValueError``) raise immediately on the
    first attempt — this helper only buys time against a flapping
    connection, never against a genuine data-correctness failure. Every
    retry emits a WARN structlog line (``manifest_write_transient_error_
    retry``) so a flapping catalog connection is visible in production
    logs instead of surfacing only as the hook's swallowed WARNING.
    """
    for attempt in range(1, len(_MANIFEST_WRITE_RETRY_DELAYS) + 2):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if (
                attempt > len(_MANIFEST_WRITE_RETRY_DELAYS)
                or not _is_retryable_manifest_connection_error(exc)
            ):
                raise
            delay = _MANIFEST_WRITE_RETRY_DELAYS[attempt - 1]
            _log.warning(
                "manifest_write_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error_type=type(exc).__name__,
                error=str(exc)[:120],
            )
            time.sleep(delay)
