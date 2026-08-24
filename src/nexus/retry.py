# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transient-error retry helpers for ChromaDB Cloud, Voyage AI, and the
migration ETLs.

Leaf module — no other nexus.* imports beyond ``nexus.rate_brake``, which
is itself a leaf module (stdlib + structlog only, no imports back into
this module or anything heavier). Otherwise: stdlib + httpx + structlog +
soft voyageai.error.
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

from nexus.rate_brake import get_brake, parse_retry_after, reset_brake

_log = structlog.get_logger(__name__)

#: nexus-cy9u7: a 429-with-Retry-After (or 503-with-Retry-After) can
#: legitimately need more attempts than a wrapper's default budget to
#: survive a sustained per-project rate-limit window — each attempt's own
#: sleep is now floored by the shared brake's delay (see
#: ``_rate_limit_signal`` below), so the default attempt count could
#: otherwise exhaust before the brake's pause elapses. Only applied when a
#: rate-limit signal is actually seen (a NARROW 429, or 503 with a
#: parseable Retry-After); every other retryable transient error keeps its
#: wrapper's own default attempt budget — CRITICAL-2 (2026-08-16) widened
#: BRAKE-TRIPPING to every retryable failure, but deliberately left this
#: attempt-budget widening narrowly scoped, so a genuinely dead endpoint
#: still fails in the same bounded number of attempts as before (each one
#: now also paying the shared brake's floor, per-wrapper docstrings for
#: the resulting worst-case numbers).
_RATE_LIMIT_MAX_ATTEMPTS: int = 8


def _find_chained_exc(
    exc: BaseException, types: tuple[type, ...],
) -> BaseException | None:
    """Direct match, then ``__context__``, then ``__cause__`` — the fixed
    one-level lookup order every status classifier in this module uses
    (nexus-cy9u7 CRITICAL-1: was ``_chained_http_status_error``, httpx-only;
    generalised so the same walk serves both HTTP-error families this
    codebase raises)."""
    if isinstance(exc, types):
        return exc
    ctx = exc.__context__
    if isinstance(ctx, types):
        return ctx
    cause = exc.__cause__
    if isinstance(cause, types):
        return cause
    return None


def _extract_status_and_retry_after(exc: BaseException) -> tuple[int, float | None] | None:
    """Authoritative ``(status_code, retry_after)`` for a chained/direct
    HTTP error of EITHER family this codebase raises, or ``None`` if *exc*
    carries no such signal.

    nexus-cy9u7 CRITICAL-1 fix: the classifier previously only recognised
    ``httpx.HTTPStatusError`` (the manifest/catalog write path,
    ``http_catalog_client.py``), so it never tripped for the production T3
    vector client (``http_vector_client.py``), which is urllib-based and
    surfaces every failure as ``VectorServiceError(msg, code=e.code) from
    e``. Recognises, in order:

    1. ``httpx.HTTPStatusError`` — direct or chained.
    2. ``urllib.error.HTTPError`` — direct or chained. ``VectorServiceError``
       is neither type itself, but ``raise VectorServiceError(...) from e``
       inside an ``except urllib.error.HTTPError as e:`` block sets BOTH
       ``__cause__`` and ``__context__`` to the original ``e`` (Python's
       implicit exception chaining applies regardless of the explicit
       ``from`` clause) — so the chain-walk finds the original HTTPError,
       headers included, without ``VectorServiceError`` needing to carry
       any duplicate state.
    3. A duck-typed ``.code`` int (mirrors ``_is_retryable_etl_error``'s
       existing precedent) — a fallback for a caller whose exception was
       constructed without an intact chain (e.g. a hand-built test double).
       This fallback cannot recover Retry-After (no headers available), so
       it only ever contributes ``retry_after=None``.
    """
    err = _find_chained_exc(exc, (httpx.HTTPStatusError, urllib.error.HTTPError))
    if err is not None:
        if isinstance(err, httpx.HTTPStatusError):
            return err.response.status_code, parse_retry_after(err.response.headers)
        return err.code, parse_retry_after(getattr(err, "headers", None))
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code, None
    return None


class _RateLimitSignal:
    """Normalised ``(code, retry_after)`` for a rate-limit-shaped failure.
    See :func:`_rate_limit_signal`."""

    __slots__ = ("code", "retry_after")

    def __init__(self, code: int, retry_after: float | None) -> None:
        self.code = code
        self.retry_after = retry_after


def _rate_limit_signal(exc: BaseException) -> _RateLimitSignal | None:
    """Return a normalised rate-limit signal if *exc* carries a Retry-After
    worth THREADING THROUGH to the shared
    :class:`~nexus.rate_brake.RateLimitBrake` for: a 429, or a 503 that
    carries a (parseable) Retry-After header. A 503 with no resolvable
    Retry-After is still retried (see the wrapper functions below, which
    now trip the brake on every retryable failure regardless of this
    function's verdict — nexus-cy9u7 CRITICAL-2), it just contributes no
    server-authoritative delay, so the brake falls back to its own
    escalating default."""
    found = _extract_status_and_retry_after(exc)
    if found is None:
        return None
    code, retry_after = found
    if code == 429:
        return _RateLimitSignal(code, retry_after)
    if code == 503 and retry_after is not None:
        return _RateLimitSignal(code, retry_after)
    return None


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
    ``etl_count``, ``total_seconds``, ``total_count``, plus (nexus-cy9u7)
    ``brake_trips`` and ``brake_seconds`` — the process-wide shared
    rate-limit brake's cumulative trip count and seconds paused (see
    ``nexus.rate_brake``). Brake pauses are NOT folded into
    ``total_seconds``/``total_count``: those two remain the pre-existing
    per-wrapper backoff sums, and the brake counters are a distinct signal
    ("how much of this was a SHARED pause vs. per-attempt backoff").
    Resetting the counters is the caller's responsibility via
    :func:`reset_retry_stats`.
    """
    brake = get_brake()
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
            "brake_trips": brake.trips,
            "brake_seconds": brake.seconds_paused,
        }


def reset_retry_stats() -> None:
    """Zero the process-local retry counters. CLI callers invoke this at
    the start of an indexing run so the end-of-run summary reflects only
    that run's backoffs.

    nexus-cy9u7: also resets the shared :class:`~nexus.rate_brake.RateLimitBrake`
    (via :func:`nexus.rate_brake.reset_brake`) so a prior run's rate-limit
    escalation/pause state never leaks into the next one.
    """
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
    reset_brake()


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
    3. Chained httpx.HTTPStatusError OR urllib.error.HTTPError (direct,
       chained, or wrapped in VectorServiceError) — authoritative integer
       status code check via :func:`_extract_status_and_retry_after`
       (nexus-cy9u7 CRITICAL-1: this used to be httpx-only, so the
       production T3 vector client's VectorServiceError failures fell
       through to the string-fallback below, which happened to work only
       because the status digits appear literally in the error message —
       fragile, and silently wrong for a status like 500 that shares no
       digits with a retryable one).
    3b. Bare urllib connectivity errors — no HTTP response at all (nexus-cy9u7
        round-3 CRITICAL C1): step 2 above only recognises httpx's transport
        errors, but the production HttpVectorClient path is urllib-based
        (``http_vector_client._post``) and a connectivity blip surfaces as a
        raw ``urllib.error.URLError``/``TimeoutError``/``ConnectionError``
        (the local/lease topology re-raises these untouched — see ``_post``'s
        ``except (urllib.error.URLError, ConnectionError, TimeoutError)``
        branch), or as ``VectorServiceError(code=None)`` chained ``from e``
        (the managed-endpoint topology, when ``_managed_remedy()`` reframes
        it). Neither shape was covered before this fix — step 2 doesn't see
        urllib types, and step 3 finds no status code to check (``code`` is
        ``None``), so a connectivity blip fell through to the fragment
        fallback in step 4, which never matches (no status digits in a
        socket-level error message). Placed AFTER step 3 deliberately:
        ``urllib.error.HTTPError`` IS a ``URLError`` subclass, so this check
        must never see a real HTTPError — step 3 already matched (and
        returned) on any HTTPError, direct or chained, so a URLError
        instance reaching this line is genuinely connectivity, not a real
        HTTP status. Mirrors ``_is_retryable_etl_error``'s already-correct
        equivalent logic below.
    4. String fallback — plain Exception message body (gateway HTML or service JSON).
    """
    # 1. Chroma PersistentClient concurrent write contention (dies at P2).
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower():
        return True
    # 2. Transport-level errors — no HTTP response, but clearly transient.
    if isinstance(exc, httpx.TransportError):
        return True
    # 3. Authoritative status-code check across both HTTP-error families.
    found = _extract_status_and_retry_after(exc)
    if found is not None:
        return found[0] in _RETRYABLE_HTTP_STATUSES
    # 3b. Bare urllib connectivity errors, direct or chained (see docstring).
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
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

    nexus-cy9u7: every attempt first calls the process-wide
    :class:`~nexus.rate_brake.RateLimitBrake`'s ``wait()`` — a no-op unless
    some OTHER worker already tripped it — so concurrent callers converge
    on one shared resume point instead of each backing off independently
    and re-firing Voyage's per-project RPM limit.

    CRITICAL-2 fix (2026-08-16): EVERY retryable failure now trips the
    brake, not just a narrow 429/503+Retry-After signal — N workers
    hammering a struggling upstream is the same class of problem
    regardless of which transient status (429/502/503/504) or transport
    error (connect refused, read timeout, ...) is reported; a 502 from an
    overloaded edge with no Retry-After is exactly the 2026-08-15 incident
    shape this brake exists for (the engine internally retrying Voyage,
    the edge's own 30s timeout surfacing as a 502/504 with no
    Retry-After — see ``nexus-99r7y``: that engine-side fix SHARPENS this
    signal but is NOT required for the brake to engage). A real
    Retry-After (429, or 503 that carries one) floors the pause at that
    value; every other retryable failure floors it at the brake's own
    escalating default (2s, doubling per consecutive process-wide trip,
    capped at 60s — see ``nexus.rate_brake``). Sleeps
    ``max(local_backoff, brake_delay)`` so a worker never retries before
    the shared resume time. Only a narrow 429/503+Retry-After signal
    widens the effective attempt budget to ``_RATE_LIMIT_MAX_ATTEMPTS``
    (8) — a SERVER-CHARACTERISED rate-limit window can legitimately
    outlast *max_attempts*; a generic transient error keeps its original
    attempt budget (this wrapper's default: 5) so a genuinely dead
    upstream still fails in bounded time, just with each of those
    attempts now paying the shared brake's floor too. A successful call
    releases the brake's escalation state.

    Worst-case per call (S2, corrected — nexus-cy9u7 round-3 SIGNIFICANT):
    non-rate-limited retryable errors stay at *max_attempts* (default 5, 4
    sleeps) each floored by the brake's CURRENT escalation level — which is
    process-GLOBAL, so a call arriving mid-incident can see the 60s cap on
    its very first attempt. The prior number here (4 x 60s = 240s) omitted
    ``http_vector_client._request``'s OWN inner gateway retry
    (``_GATEWAY_RETRY_SLEEPS``: 2s + 5s + 10s = 17s), which fires for the
    SAME statuses (502/503/504) at a layer BELOW this wrapper, before this
    wrapper's caller (``_post``) ever raises — so each of this wrapper's 4
    attempts can itself already have paid up to 17s there: 4 x (60s + 17s)
    = 308s, call it about 5 minutes. A 429/503+Retry-After signal widens to
    8 attempts (7 sleeps), each floored at either the escalating default
    (cap 60s, worst case 7 x 60s = 420s, or 7 x 77s = 539s including the
    inner gateway retry when the widening status is 503) or the server's
    own Retry-After (clamped to 300s, worst case 7 x 300s = 2100s if every
    attempt reports a large one — the inner gateway retry never applies to
    a 429, which is not in ``_GATEWAY_RETRY_CODES``).
    """
    brake = get_brake()
    delay = 2.0
    attempt = 1
    while True:
        brake.wait()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if not _is_retryable_vector_error(exc):
                raise
            rate_limit_err = _rate_limit_signal(exc)
            effective_max_attempts = (
                max(max_attempts, _RATE_LIMIT_MAX_ATTEMPTS)
                if rate_limit_err is not None else max_attempts
            )
            if attempt >= effective_max_attempts:
                raise
            # nexus-8g79.32: jittered sleep so multiple concurrent
            # workers retrying after a shared rate-limit do not all wake
            # at the same instant and re-fire the limit. ±20% of delay.
            jittered = delay * (1.0 + (random.random() - 0.5) * 0.4)
            retry_after = rate_limit_err.retry_after if rate_limit_err is not None else None
            # nexus-cy9u7 CRITICAL-2: unconditional — every retryable
            # failure trips the shared brake, not only a narrow signal.
            brake_delay = brake.trip(retry_after, source="vector")
            sleep_for = max(jittered, brake_delay)
            _log.warning(
                "vector_transient_error_retry",
                attempt=attempt,
                delay=sleep_for,
                error=str(exc)[:120],
                rate_limited=rate_limit_err is not None,
            )
            _add_vector_retry(sleep_for)
            time.sleep(sleep_for)
            delay = min(delay * 2, 30.0)
            attempt += 1
        else:
            brake.release()
            return result


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
#: nexus-1jtob (2026-08-23): 403 REMOVED. It was here on a "transient edge
#: 403" premise asserted in the comments below and never measured. conexus
#: swept 3,675,603 edge application-log records over 2026-08-19..08-23 and
#: found ZERO 403s on this path — strong evidence of absence over that window,
#: though not proof for all history.
#:
#: The two 403 populations are real, and this set had them exactly backwards
#: by path. On the VECTOR path 403s are frequent (151 in four days) and are
#: DETERMINISTIC AWS WAF refusals keyed on request-body content; that path
#: already excluded 403 correctly. On THIS path, which included it, no 403 of
#: any kind was observed. So the set was armed for a mode with no observed
#: instances while the mode that actually occurs is unpassable — retrying it
#: 5-8 times with escalating brake amplified load against our own edge for a
#: request that could never succeed.
#:
#: Neither real 403 population is transient: a WAF refusal is deterministic in
#: the body, and a control-plane 401/403 is an authz verdict. 5xx from the ALB
#: (``server: awselb/2.0`` with no healthy target) IS transient and stays
#: retryable via 502/503/504 — the edge is not blanket-untrustworthy, only its
#: 4xx refusals are deterministic.
_RETRYABLE_ETL_HTTP_STATUSES: frozenset[int] = frozenset({429, 502, 503, 504})

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
    # httpx response error — transient statuses only; see the frozenset above
    # for why 403 is NOT among them (nexus-1jtob).
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
    # leaf module imports no nexus.*). nexus-1jtob: a ``code=403`` wrapper no
    # longer retries here — see the frozenset above. Such a wrapper also carries
    # ``edge_refusal=True`` when the ALB/WAF generated it, checked first below
    # so an edge 4xx can never be retried even if a status is re-added later.
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        # nexus-1jtob: an EDGE-generated 4xx is deterministic in the request
        # body. Belt-and-braces against a future status re-addition.
        if getattr(exc, "edge_refusal", False) and 400 <= code < 500:
            return False
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

    nexus-cy9u7: same shared-brake wiring as ``_vector_with_retry`` — every
    attempt calls the process-wide brake's ``wait()`` first. CRITICAL-2 fix
    (2026-08-16): EVERY retryable failure now trips the brake (403,
    connection drops, plain 502/503/504 included — not only a narrow
    429/503+Retry-After signal), floored at either the server's
    Retry-After (when a 429/503 supplies one) or the brake's own
    escalating default otherwise. Only the narrow 429/503+Retry-After
    signal widens the effective attempt budget to
    ``_RATE_LIMIT_MAX_ATTEMPTS``; every other retryable failure keeps this
    wrapper's own default (3, 2 sleeps) so a genuinely dead endpoint still
    fails in bounded time. A successful call releases the brake's
    escalation state. Worst case per call: 2 sleeps at the brake's current
    escalation level (process-global, cap 60s) = 120s without a
    Retry-After signal; widened to 7 sleeps (rate-limited) at up to the
    300s Retry-After clamp = 2100s worst case.
    """
    brake = get_brake()
    delay = 1.0
    attempt = 1
    while True:
        brake.wait()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            if not _is_retryable_etl_error(exc):
                raise
            rate_limit_err = _rate_limit_signal(exc)
            effective_max_attempts = (
                max(max_attempts, _RATE_LIMIT_MAX_ATTEMPTS)
                if rate_limit_err is not None else max_attempts
            )
            if attempt >= effective_max_attempts:
                raise
            _log.warning(
                "etl_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error_type=type(exc).__name__,
                error=str(exc)[:120],
                persistent_if_all_fail=True,
                rate_limited=rate_limit_err is not None,
            )
            jittered = delay * (1.0 + (random.random() - 0.5) * 0.4)
            retry_after = rate_limit_err.retry_after if rate_limit_err is not None else None
            # nexus-cy9u7 CRITICAL-2: unconditional trip.
            brake_delay = brake.trip(retry_after, source="etl")
            sleep_for = max(jittered, brake_delay)
            _add_etl_retry(sleep_for)
            time.sleep(sleep_for)
            delay = min(delay * 2, 10.0)
            attempt += 1
        else:
            brake.release()
            return result


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


def _is_connectivity_error(exc: BaseException) -> bool:
    """Return True if *exc* is a transient connection-level failure — the
    network/transport layer, not a business-domain response.

    Checks ``httpx.TransportError`` (covers ``ConnectError``,
    ``ConnectTimeout``, ``ReadTimeout``, etc.), the stdlib
    ``ConnectionError``/``TimeoutError``, and — since the vector/catalog
    HTTP clients sometimes reframe a transport drop as an application
    error via ``raise ... from e`` — the chained ``__cause__``/
    ``__context__``. Does NOT inspect HTTP status codes: an
    ``httpx.HTTPStatusError`` (a real 4xx/5xx response, including a
    documented business refusal like ``IndexRunVerifyRefused``'s 409) is
    never classified as connectivity here, unlike the migration-scoped
    ``_is_retryable_etl_error``.

    RENAMED from ``_is_retryable_manifest_connection_error`` (nexus-0dpli):
    originally scoped to catalog manifest-write retries
    (:func:`_manifest_write_with_retry`, below), now also reused as the
    EVICTION TRIGGER for the process-lifetime shared-client singletons in
    ``catalog/factory.py`` and ``mcp_infra.py`` — deciding whether a
    call's failure means the singleton's underlying connections are
    broken badly enough to warrant closing and rebuilding the whole
    instance, versus a routine domain outcome (a 409 refusal, a
    validation error) that must propagate without touching the shared
    client at all. Authentication / lease-rotation failures do NOT need a
    separate branch here: ``RefreshableHttpStoreMixin``
    (``db/t2/_refreshable_client.py``) already retries a 401 and
    re-resolves the endpoint INSIDE each store instance before an
    exception would ever reach a caller of this function — by the time
    any caller sees an exception at all, the mixin's own internal
    recovery has already been exhausted for that failure class, so a
    singleton-level eviction is neither necessary nor sufficient for it.
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

    nexus-cy9u7 addendum: a 429 (or 503 with a Retry-After header) is ALSO
    retried now, in addition to the connection-level errors above — the
    manifest write lands on the same engine that embeds server-side, so it
    can be paced by the identical per-project rate limit. This is the one
    exception to "connection errors only": a rate-limit signal is not a
    data-correctness failure, so retrying it does not risk masking a real
    4xx (a genuine 400/404/422 is still classified as neither connectivity
    nor rate-limited, and still raises on the first attempt).

    CRITICAL-2 fix (2026-08-16): every attempt first calls the shared
    :class:`~nexus.rate_brake.RateLimitBrake`'s ``wait()``, and EVERY
    retried failure (connectivity OR rate-limit) now trips the brake too —
    not only a narrow 429/503+Retry-After signal — floored at either the
    server's Retry-After or the brake's own escalating default. Only the
    narrow rate-limit signal widens the effective attempt budget to
    ``_RATE_LIMIT_MAX_ATTEMPTS`` (a real rate-limit window can outlast the
    connectivity-only 4-attempt default's 0.5+1+2=3.5s); a plain
    connectivity error keeps the 4-attempt default (3 sleeps), each now
    also floored by the brake's current escalation level. Worst case per
    call: 3 sleeps at the brake's cap (60s, process-global — a call
    arriving mid-incident can see it on its first attempt) = 180s without
    a Retry-After signal; widened to 7 sleeps at up to the 300s
    Retry-After clamp = 2100s worst case when rate-limited. A successful
    call releases the brake's escalation state.
    """
    brake = get_brake()
    max_connectivity_attempts = len(_MANIFEST_WRITE_RETRY_DELAYS) + 1
    attempt = 1
    while True:
        brake.wait()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            rate_limit_err = _rate_limit_signal(exc)
            is_connectivity = _is_connectivity_error(exc)
            if rate_limit_err is None and not is_connectivity:
                raise
            effective_max_attempts = (
                max(max_connectivity_attempts, _RATE_LIMIT_MAX_ATTEMPTS)
                if rate_limit_err is not None else max_connectivity_attempts
            )
            if attempt >= effective_max_attempts:
                raise
            base_delay = (
                _MANIFEST_WRITE_RETRY_DELAYS[attempt - 1]
                if attempt <= len(_MANIFEST_WRITE_RETRY_DELAYS)
                else _MANIFEST_WRITE_RETRY_DELAYS[-1]
            )
            retry_after = rate_limit_err.retry_after if rate_limit_err is not None else None
            # nexus-cy9u7 CRITICAL-2: unconditional trip (connectivity too).
            brake_delay = brake.trip(retry_after, source="manifest")
            delay = max(base_delay, brake_delay)
            _log.warning(
                "manifest_write_transient_error_retry",
                attempt=attempt,
                delay=delay,
                error_type=type(exc).__name__,
                error=str(exc)[:120],
                rate_limited=rate_limit_err is not None,
            )
            time.sleep(delay)
            attempt += 1
        else:
            brake.release()
            return result
