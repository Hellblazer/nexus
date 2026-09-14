# SPDX-License-Identifier: AGPL-3.0-or-later
"""Instance-held slots for process-lifetime shared service clients
(nexus-w1ip Phases 1-3).

Generalises the CAS-narrowed, refcounted-eviction singleton shape that
``catalog/factory.py`` (nexus-53x7s / nexus-u2u0n / nexus-0dpli /
nexus-jb4pp) and ``mcp_infra.py`` (nexus-ldab2, the T2 mirror) each grew
independently as MODULE GLOBALS. :class:`SharedClientSlot` holds the exact
same state (current client, resolution lock, refcounts, pending-close set,
per-op timing stats) on an INSTANCE instead, so a caller — production code
or a test — can construct a fresh slot and inject it rather than reaching
into module globals through an autouse reset fixture.

This also closes nexus-w1ip's actual defect, not just its test-isolation
symptom: an optional ``endpoint_key`` callable lets a slot notice when the
identity it should be talking to (e.g. ``(base_url, token)``) has changed
— a rotated per-test tenant token, or a live credential rotation — and
rebuild instead of silently continuing to answer under the OLD identity.
Pre-nexus-w1ip, the two module-global singletons this replaces had no
notion of which endpoint they were built against, so a token rotation
(an env var change with no code touching the cached client) was invisible
to them.

Threading contract (identical to the module-global implementations this
replaces):

- The slot's internal lock is held ONLY to resolve (get-or-build) the
  client and to acquire/release an in-flight reference — never across a
  caller's own forwarded call (the CAS-narrowing, nexus-u2u0n).
- :meth:`SharedClientSlot.resolve_and_acquire` + :meth:`SharedClientSlot.release`
  bracket a call that may fail; pass ``evict=True`` to ``release`` only
  for a genuine connectivity failure — the caller decides what counts,
  this module has no opinion on error taxonomy.
- :meth:`SharedClientSlot.resolve_and_apply` is for a caller with no round
  trip at all (e.g. a plain, non-callable attribute read) that wants its
  access folded into the SAME locked section as the resolve, matching the
  pre-narrowing convoy-serialization semantics for that path.
- An eviction (error-triggered, or a stale-key swap at resolve time) never
  closes a client out from under an in-flight sibling (nexus-0dpli): every
  acquired reference is tracked, and the physical ``close()`` is deferred
  to whichever caller's release drains the refcount to zero.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Generic, TypeVar

T = TypeVar("T")


class SharedClientSlot(Generic[T]):
    """Process-lifetime shared-client slot: CAS-narrowed, refcounted.

    Args:
        build: Constructs a fresh client on demand.
        close: Tears one down (assumed cheap/local — see the module
            docstring's "never across a round trip" contract; this is
            called for a client with zero in-flight callers, never for
            one still mid-call).
        endpoint_key: Optional. Re-evaluated on every resolve; when its
            return value differs from the key the CURRENTLY held client
            was built under, that client is evicted (closed once its
            last in-flight caller releases it) and a fresh one is built
            against the new key. Omitting this reproduces the historical
            module-global behaviour exactly: build once, keep forever,
            evict only via an explicit :meth:`release` with
            ``evict=True``.
    """

    def __init__(
        self,
        build: Callable[[], T],
        close: Callable[[T], None],
        *,
        endpoint_key: Callable[[], Any] | None = None,
    ) -> None:
        self._build = build
        self._close = close
        self._endpoint_key = endpoint_key
        self._lock = threading.Lock()
        self._client: T | None = None
        self._client_key: Any = None
        self._refcounts: dict[int, int] = {}
        self._pending_close: set[int] = set()
        self._op_stats: dict[str, list[float]] = {}
        self._stats_lock = threading.Lock()

    # ── resolution (internal; caller must hold self._lock) ──────────────

    def _resolve_locked(self, desired: Any) -> tuple[T, T | None]:
        """Get-or-build the current client against the ALREADY-COMPUTED
        *desired* key (computed by the caller BEFORE acquiring
        ``self._lock`` — see :meth:`resolve_and_apply` /
        :meth:`resolve_and_acquire`; ``endpoint_key()`` itself never runs
        under this lock, review round nexus-w1ip finding (b): an
        I/O-bound key resolver run under the lock would serialize every
        concurrent resolver in the process behind that I/O). Returns
        ``(client, stale_client_or_None)`` — a non-None second element is
        a client evicted by a stale-key swap with ZERO in-flight callers,
        which the caller must close AFTER releasing ``self._lock`` (never
        inside it — this mirrors the deferred-close discipline
        :meth:`release` uses for an error-triggered eviction, so a
        resolve-time swap never holds the lock across a ``close()``
        call either)."""
        to_close: T | None = None
        if self._client is not None and self._endpoint_key is not None and desired != self._client_key:
            old = self._client
            self._client = None
            self._client_key = None
            key = id(old)
            if self._refcounts.get(key, 0) > 0:
                self._pending_close.add(key)
            else:
                self._refcounts.pop(key, None)
                self._pending_close.discard(key)
                to_close = old
        if self._client is None:
            self._client = self._build()
            self._client_key = desired
        return self._client, to_close

    def _acquire_ref_locked(self, client: T) -> None:
        key = id(client)
        self._refcounts[key] = self._refcounts.get(key, 0) + 1

    def _release_ref_locked(self, client: T, *, evict: bool) -> bool:
        """Returns True exactly when the CALLER must run ``close()``
        itself (after releasing the lock)."""
        if evict and self._client is client:
            self._client = None
            self._client_key = None
        key = id(client)
        remaining = self._refcounts.get(key, 1) - 1
        if remaining > 0:
            self._refcounts[key] = remaining
            if evict:
                self._pending_close.add(key)
            return False
        self._refcounts.pop(key, None)
        was_pending = key in self._pending_close
        self._pending_close.discard(key)
        return evict or was_pending

    # ── public entry points ──────────────────────────────────────────────

    def resolve_and_apply(self, fn: Callable[[T], Any]) -> tuple[Any, float]:
        """Resolve under the lock and apply *fn* to the client WITHOUT
        releasing the lock first — for a caller with no round trip (e.g.
        a plain attribute read). Returns ``(fn(client), wait_seconds)``.

        ``endpoint_key()`` is called BEFORE ``self._lock`` is acquired
        (nexus-w1ip review round, finding (b)) — a possibly I/O-bound key
        resolver must never run while holding a lock every other resolver
        in the process blocks on; only the CHEAP comparison against the
        already-computed key happens under the lock.
        """
        desired = self._endpoint_key() if self._endpoint_key is not None else None
        _w0 = time.monotonic()
        stale: T | None = None
        with self._lock:
            wait = time.monotonic() - _w0
            client, stale = self._resolve_locked(desired)
            result = fn(client)
        if stale is not None:
            self._close(stale)
        return result, wait

    def resolve_and_acquire(self) -> tuple[T, float]:
        """Resolve the client and acquire an in-flight reference on it,
        atomically. The lock is released before this returns — the
        caller's own round trip never runs under it. Pair with
        :meth:`release`. Returns ``(client, wait_seconds)``.

        ``endpoint_key()`` is called BEFORE ``self._lock`` is acquired —
        see :meth:`resolve_and_apply`'s docstring for why.
        """
        desired = self._endpoint_key() if self._endpoint_key is not None else None
        _w0 = time.monotonic()
        with self._lock:
            wait = time.monotonic() - _w0
            client, stale = self._resolve_locked(desired)
            self._acquire_ref_locked(client)
        if stale is not None:
            self._close(stale)
        return client, wait

    def release(self, client: T, *, evict: bool) -> None:
        """Release this caller's in-flight reference to *client*, closing
        it if this was the last reference AND it is evicted (by this
        call, or a prior stale-key swap / connectivity eviction whose
        close was deferred pending this caller's drain)."""
        with self._lock:
            close_now = self._release_ref_locked(client, evict=evict)
        if close_now:
            self._close(client)

    # ── per-op timing stats (nexus-jb4pp / nexus-ldab2) ──────────────────

    def record_op(self, name: str, wait_s: float, call_s: float, *, calls: int = 1) -> None:
        """Accumulate one op's timings. *calls* is 0 for the non-callable
        attribute path — that access still queued for the lock, so its
        wait is real and must be counted, but it is not a round trip and
        must not inflate the call count."""
        with self._stats_lock:
            row = self._op_stats.setdefault(name, [0.0, 0.0, 0.0])
            row[0] += calls
            row[1] += wait_s
            row[2] += call_s

    def op_stats(self) -> dict[str, dict[str, float]]:
        """Snapshot of ``{op: {"calls": n, "lock_wait_s": s, "call_s": s}}``.
        Cumulative across threads, so both may exceed wall clock."""
        with self._stats_lock:
            return {
                op: {"calls": v[0], "lock_wait_s": v[1], "call_s": v[2]}
                for op, v in self._op_stats.items()
            }

    def reset_op_stats(self) -> None:
        """Zero the per-op counters."""
        with self._stats_lock:
            self._op_stats.clear()

    # ── test support ─────────────────────────────────────────────────────

    def reset_for_tests(self) -> None:
        """Close and clear the held client (tests only)."""
        with self._lock:
            if self._client is not None:
                self._close(self._client)
            self._client = None
            self._client_key = None
            # nexus-0dpli: a test that left in-flight refcount bookkeeping
            # behind (e.g. a barrier test that closed early) must not leak
            # into the next test's assertions.
            self._refcounts.clear()
            self._pending_close.clear()


#: Default TTL for :func:`cached_endpoint_key`'s memoized (slow, I/O-bound)
#: path (nexus-w1ip review round, finding (b)). A rotation of the thing an
#: ``endpoint_key`` resolves — a supervisor restart onto a new port, a
#: credential remint — is never sub-second: nothing in this codebase
#: republishes a lease or mints a fresh token on a timescale anywhere near
#: 1 second, so memoizing the RESOLVED key for this long trades an
#: unmeasurable staleness window for eliminating a disk read (a lease-file
#: read + JSON parse) on nearly every ``SharedClientSlot`` resolve — a path
#: ``catalog/factory.py``'s own docstring calls HOT (manifest write_many,
#: the RUNFENCE begin_index_run_many, every catalog/T2 op). The window
#: resets naturally: the next call after it elapses re-reads and picks up
#: any real rotation, so a genuine change is never masked for longer than
#: this constant.
DEFAULT_ENDPOINT_KEY_CACHE_TTL_S: float = 1.0


def cached_endpoint_key(
    resolve: Callable[[], Any],
    *,
    is_fresh_required: Callable[[], bool] | None = None,
    ttl_s: float = DEFAULT_ENDPOINT_KEY_CACHE_TTL_S,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[], Any]:
    """Wrap *resolve* (an endpoint-key resolver that MAY do blocking I/O,
    e.g. a lease-file read) with a short TTL memo cache, thread-safe.

    The returned callable is what a :class:`SharedClientSlot` should
    receive as its ``endpoint_key`` — it is always called OUTSIDE the
    slot's own lock (see :meth:`SharedClientSlot.resolve_and_apply` /
    :meth:`SharedClientSlot.resolve_and_acquire`), so caching here bounds
    call FREQUENCY (the actual I/O cost), while the slot's own restructure
    bounds LOCK CONTENTION (no resolver ever blocks another resolver's I/O)
    — two independent fixes for the same finding.

    *is_fresh_required*, when given, is consulted on every call (cheap by
    construction — never itself the I/O-bound part): when it returns
    ``True`` the cache is bypassed entirely and *resolve* runs live. Use
    this for a resolver whose CHEAP path (e.g. an env var is set) must
    never be masked by a stale cache — memoizing an already-cheap value
    would trade zero benefit for a real staleness window (a rotated
    per-test tenant token must be caught on its very next call, not after
    up to *ttl_s* of delay). Only the resolver's genuinely I/O-bound
    fallback path benefits from — and needs — the cache.
    """
    _cache_lock = threading.Lock()
    _state: dict[str, Any] = {"value": None, "at": -(ttl_s + 1.0)}

    def _get() -> Any:
        if is_fresh_required is not None and is_fresh_required():
            return resolve()
        now = clock()
        with _cache_lock:
            if now - _state["at"] < ttl_s:
                return _state["value"]
        # Resolved OUTSIDE the memo lock -- this may be the I/O-bound
        # path; never hold a lock across it.
        value = resolve()
        with _cache_lock:
            _state["value"] = value
            _state["at"] = now
        return value

    return _get
