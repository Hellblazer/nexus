# SPDX-License-Identifier: AGPL-3.0-or-later
"""Process-wide shared pause for a rate-limited upstream (nexus-cy9u7).

Leaf module — no ``nexus.*`` imports. Only stdlib + structlog.

The 2026-08-15 incident (conexus-ddh0 / nexus-99r7y): a parallel bulk
``nx index`` run exhausted Voyage's per-PROJECT RPM budget (4000 RPM for
``voyage-context-3``, shared by every worker thread AND every concurrent
session in this process — the client cannot see other processes). The
engine embeds server-side on write, so the client's "embed pressure" is
really its WRITE request rate: T3 vector writes and catalog manifest
writes. Each of ``nexus.retry``'s write-retry wrappers backs off
independently per WORKER on a transient failure — with N workers each
retrying on its own uncorrelated schedule, the aggregate request rate
never actually drops below the limit, so the retries keep re-firing it.

:class:`RateLimitBrake` gives every worker in this process a SINGLE shared
resume point instead: the first worker to see a retryable transient
failure trips the brake, every worker's next :meth:`RateLimitBrake.wait`
call blocks until that SAME deadline, and they wake together (each with a
small per-worker jitter so they don't all re-fire in the exact same
instant).

WHAT TRIPS IT (nexus-cy9u7 CRITICAL-2, 2026-08-16): every retryable
transient write failure — 429, 502, 503, 504, and retryable transport
errors (connect refused, read timeout, ...) — not only a narrow
429/503-with-Retry-After signal. The trigger for this widening: the
ACTUAL 2026-08-15 incident shape was the engine retrying Voyage
internally and the edge's own 30s timeout surfacing to the client as a
502/504 with NO Retry-After header at all — the original 429/503-only
scope would never have tripped on it. N workers hammering a struggling
upstream is the same class of problem regardless of which status code or
transport error reports it, so a shared pause engages on all of them:
Retry-After when the server supplies one (a 429, or a 503 that carries
one), the escalating default otherwise (2s, doubling per consecutive
process-wide trip, capped at 60s).

``nexus-99r7y`` (engine fail-fast with an explicit 429 + Retry-After
instead of a bare edge timeout) SHARPENS this signal — a real
server-characterised Retry-After is more informative than the escalating
default guess — but is NOT required for this brake to engage; the
escalating-default path covers every retryable failure shape whether or
not that engine-side fix has landed.

SCOPE, disclosed honestly:

- Per-PROCESS only. Not a substitute for server-side rate limiting, and
  not a global limiter across sessions — it coordinates workers WITHIN
  this process only, and cannot see or pace other processes/sessions
  sharing the same Voyage project.
- Attempt-bounded, not time-unbounded. Each write-retry wrapper in
  ``nexus.retry`` keeps its own attempt budget (widened only for a
  narrow 429/503+Retry-After signal — see ``_RATE_LIMIT_MAX_ATTEMPTS``);
  the brake floors each attempt's sleep, it never adds attempts on its
  own. Worst-case per-call bounds are documented on each wrapper
  function's docstring in ``nexus.retry`` (``_vector_with_retry``,
  ``_etl_with_retry``, ``_manifest_write_with_retry``).
- DELIBERATELY COUPLED, disclosed (nexus-gvuv0, nexus-cy9u7 round-3):
  ``_manifest_write_with_retry`` trips this SAME brake on a plain
  connectivity error (a bare ``httpx.TransportError``/``ConnectionError``/
  ``TimeoutError``, chained or direct — no HTTP status involved at all),
  not only on a rate-limit signal. That is a genuine widening beyond this
  brake's original Voyage-RPM framing above, and it is intentional, not an
  oversight: the catalog manifest write and the T3 vector write both land
  on the SAME local engine process (see ``nexus.retry``'s
  ``_manifest_write_with_retry`` docstring). A connectivity blip to that
  engine — a restart, a brief port-not-listening window — is evidence the
  ENGINE itself is struggling, which is relevant to every writer hitting
  it, not only the specific request that happened to see the blip first;
  pausing the shared brake for the same short, escalating-default window
  used elsewhere costs little (2s the first time, capped at 60s) and
  self-corrects immediately once a write succeeds (:meth:`release`). The
  alternative — scoping manifest-write's trip to its rate-limit signal
  only — was considered and rejected: it would silently reintroduce the
  independent-per-worker-backoff failure mode CRITICAL-2 fixed for the
  OTHER two wrappers, just for this one engine-connectivity case.
"""
from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import structlog

_log = structlog.get_logger(__name__)

#: Escalating-default backoff: starts here, doubles per consecutive trip
#: (only the trips that had no server-supplied Retry-After — see
#: :meth:`RateLimitBrake.trip`), capped at ``_MAX_DELAY_SECONDS``.
_BASE_DELAY_SECONDS: float = 2.0
_MAX_DELAY_SECONDS: float = 60.0
#: A trip more than this long after the previous one resets the escalation
#: counter — an isolated trip long after the last one is a fresh incident,
#: not a continuation of a sustained outage.
_ESCALATION_WINDOW_SECONDS: float = 120.0
#: wait() sleeps in slices this size (or less, on the final slice) so a
#: long pause stays interruptible/observable rather than one giant sleep.
_WAIT_SLICE_SECONDS: float = 1.0
#: Extra per-worker jitter added on wake, as a fraction of the delay that
#: was waited for — spreads workers out so they don't all re-fire in the
#: same instant once the shared deadline passes.
_WAKE_JITTER_FRACTION: float = 0.2
#: Retry-After is clamped to this ceiling — a malformed or absurd header
#: value must never stall a run indefinitely.
_RETRY_AFTER_CLAMP_MAX: float = 300.0


class RateLimitBrake:
    """A process-wide shared pause point. Thread-safe.

    Clock/sleep/jitter are constructor-injected so tests never sleep for
    real; production code uses the real-time defaults via :func:`get_brake`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        wait_slice_seconds: float = _WAIT_SLICE_SECONDS,
        base_delay_seconds: float = _BASE_DELAY_SECONDS,
        max_delay_seconds: float = _MAX_DELAY_SECONDS,
        escalation_window_seconds: float = _ESCALATION_WINDOW_SECONDS,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._wait_slice_seconds = wait_slice_seconds
        self._base_delay_seconds = base_delay_seconds
        self._max_delay_seconds = max_delay_seconds
        self._escalation_window_seconds = escalation_window_seconds
        self._lock = threading.Lock()
        self._resume_at: float = 0.0
        self._consecutive_trips: int = 0
        self._last_trip_clock: float | None = None
        self._last_delay: float = 0.0
        # Stats — mutated under _lock, read without it (observability only).
        self.trips: int = 0
        self.seconds_paused: float = 0.0
        self.last_retry_after: float | None = None
        self.last_source: str = ""

    #: nexus-cy9u7 round-3 CRITICAL C2 (decided + documented): what happens
    #: to escalation when a WRAPPER exhausts its own attempt budget and
    #: raises, without ever calling :meth:`release`? ``release()`` is
    #: success-only by design (see its own docstring) — it is deliberately
    #: NOT called on a final-attempt failure, so the escalation counter
    #: from that incident is still elevated when the call returns. This
    #: does NOT strand the brake escalated forever, by two independent
    #: paths to recovery, either of which is sufficient on its own:
    #:
    #: 1. The VERY NEXT call that succeeds — on the first attempt, with no
    #:    retry of its own — still reaches the wrapper's ``else`` branch and
    #:    calls :meth:`release`, which unconditionally resets
    #:    ``_consecutive_trips`` to 0. Recovery is therefore immediate once
    #:    the upstream is actually healthy again; it does not wait for the
    #:    escalation window to elapse.
    #: 2. Independently, :meth:`trip` itself decays: a trip arriving more
    #:    than ``escalation_window_seconds`` (120s default) after the
    #:    previous one resets the counter before computing its own delay
    #:    (see the ``now - self._last_trip_clock`` check below) — so even a
    #:    caller that keeps failing, just sparsely, never accumulates
    #:    escalation across unrelated incidents.
    #:
    #: See ``tests/test_rate_brake.py::test_release_resets_escalation`` and
    #: ``::test_stale_trip_resets_escalation_window`` for both paths.
    def trip(self, retry_after: float | None, *, source: str) -> float:
        """Record a rate-limit signal from *source* and extend the shared
        pause. Returns the delay (seconds) this trip contributed.

        ``retry_after`` (already parsed to seconds) is used directly when
        the server supplied one. Otherwise an escalating default is used:
        starts at ``base_delay_seconds`` (2s), doubles per consecutive
        trip THAT ALSO LACKED a Retry-After, capped at ``max_delay_seconds``
        (60s). A Retry-After-carrying trip does not itself advance the
        escalation counter — it is authoritative information from the
        server, not evidence of a sustained unknown-duration outage.
        """
        with self._lock:
            now = self._clock()
            if retry_after is not None:
                delay = max(0.0, retry_after)
            else:
                if (
                    self._last_trip_clock is not None
                    and (now - self._last_trip_clock) > self._escalation_window_seconds
                ):
                    self._consecutive_trips = 0
                delay = min(
                    self._base_delay_seconds * (2 ** self._consecutive_trips),
                    self._max_delay_seconds,
                )
                self._consecutive_trips += 1
            self._last_trip_clock = now
            self._last_delay = delay
            self._resume_at = max(self._resume_at, now + delay)
            self.trips += 1
            self.last_retry_after = retry_after
            self.last_source = source
        _log.warning(
            "rate_brake_tripped",
            source=source,
            delay=delay,
            retry_after_present=retry_after is not None,
        )
        return delay

    def release(self) -> None:
        """Call on a successful attempt: resets the escalation counter so
        the NEXT trip (if any) starts back at the base delay rather than
        continuing to escalate off an incident that just ended."""
        with self._lock:
            had_trips = self._consecutive_trips > 0
            self._consecutive_trips = 0
        if had_trips:
            _log.info("rate_brake_released")

    def wait(self) -> float:
        """Block until the shared resume point passes (a no-op — returns
        0.0 immediately — when the brake has never been tripped, or the
        resume point has already passed). Sleeps in small slices so a long
        pause stays interruptible/observable rather than one giant sleep.
        Returns the total seconds actually paused (including the wake
        jitter, if any).
        """
        waited = 0.0
        last_delay = 0.0
        while True:
            with self._lock:
                remaining = self._resume_at - self._clock()
                last_delay = self._last_delay
            if remaining <= 0:
                break
            slice_for = min(remaining, self._wait_slice_seconds)
            self._sleep(slice_for)
            waited += slice_for
        if waited > 0:
            # nexus-cy9u7: small per-worker jitter on wake so every worker
            # waiting on the same shared deadline doesn't re-fire the
            # upstream limit in the same instant.
            jitter_extra = last_delay * (self._jitter() * _WAKE_JITTER_FRACTION)
            if jitter_extra > 0:
                self._sleep(jitter_extra)
                waited += jitter_extra
            with self._lock:
                self.seconds_paused += waited
        return waited


_default_brake: RateLimitBrake | None = None
_default_brake_lock = threading.Lock()


def get_brake() -> RateLimitBrake:
    """Return the process-wide default brake, constructing it on first use."""
    global _default_brake
    with _default_brake_lock:
        if _default_brake is None:
            _default_brake = RateLimitBrake()
        return _default_brake


def reset_brake() -> None:
    """Replace the process-wide default brake with a fresh, untripped one.

    Called at the start of each ``nx index`` run (via
    :func:`nexus.retry.reset_retry_stats`) so a run's rate-limit state
    never leaks into the next one, and by tests for isolation.
    """
    global _default_brake
    with _default_brake_lock:
        _default_brake = RateLimitBrake()


def parse_retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Parse a ``Retry-After`` header value (from *headers*, any mapping —
    plain dict or ``httpx.Headers``) into seconds. Accepts an integer/float
    seconds count or an HTTP-date. Returns ``None`` when no header is
    present or the value cannot be parsed. Clamped to
    ``[0, _RETRY_AFTER_CLAMP_MAX]`` so a malformed or absurd value can
    never stall a run indefinitely.
    """
    if not headers:
        return None
    raw: str | None = None
    getter = getattr(headers, "get", None)
    if callable(getter):
        raw = getter("Retry-After")
    if raw is None:
        for key, value in headers.items():
            if key.lower() == "retry-after":
                raw = value
                break
    if raw is None:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN guard
        return None
    return max(0.0, min(seconds, _RETRY_AFTER_CLAMP_MAX))
