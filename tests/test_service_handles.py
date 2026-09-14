# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nexus.service_handles: SharedClientSlot's lock/refcount
contract and the cached_endpoint_key TTL wrapper (nexus-w1ip Phases 1-3,
review round finding (b)).

These exercise SharedClientSlot directly rather than through a consumer
(catalog/factory.py, mcp_infra.py) — the generic instance-held slot this
module owns has no dedicated unit coverage otherwise.
"""
from __future__ import annotations

import threading

from nexus.service_handles import SharedClientSlot, cached_endpoint_key


def _slot(*, endpoint_key=None) -> tuple[SharedClientSlot, list]:
    constructed: list = []

    class _Client:
        def __init__(self) -> None:
            constructed.append(self)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    slot: SharedClientSlot = SharedClientSlot(
        _Client, lambda c: c.close(), endpoint_key=endpoint_key,
    )
    return slot, constructed


# ── finding (b): endpoint_key must never run under the slot's lock ─────────


def test_resolve_and_apply_calls_endpoint_key_outside_the_lock() -> None:
    """A key resolver that records the calling thread and checks
    ``self._lock.locked()`` while running must see it UNLOCKED — proving
    endpoint_key() executes before the slot's lock is acquired, not
    inside the locked critical section. Before the nexus-w1ip review-round
    fix, endpoint_key() ran inside ``_resolve_locked`` under the lock,
    which would serialize every concurrent resolver in the process behind
    one I/O-bound key resolution (review finding (b))."""
    slot, _ = _slot()
    observations: list[tuple[str, bool]] = []

    def _key() -> str:
        observations.append((threading.current_thread().name, slot._lock.locked()))
        return "k"

    slot._endpoint_key = _key  # inject after construction; simplest seam
    slot.resolve_and_apply(lambda c: None)

    assert observations, "endpoint_key() was never called"
    for thread_name, was_locked in observations:
        assert not was_locked, (
            f"endpoint_key() (called from thread {thread_name!r}) observed "
            f"the slot's lock as HELD — it ran inside the critical section "
            f"instead of before it"
        )


def test_resolve_and_acquire_calls_endpoint_key_outside_the_lock() -> None:
    slot, _ = _slot()
    observations: list[tuple[str, bool]] = []

    def _key() -> str:
        observations.append((threading.current_thread().name, slot._lock.locked()))
        return "k"

    slot._endpoint_key = _key
    client, _wait = slot.resolve_and_acquire()
    slot.release(client, evict=False)

    assert observations, "endpoint_key() was never called"
    for thread_name, was_locked in observations:
        assert not was_locked, (
            f"endpoint_key() (called from thread {thread_name!r}) observed "
            f"the slot's lock as HELD"
        )


# ── cached_endpoint_key: TTL memoization ────────────────────────────────────


def test_cached_endpoint_key_memoizes_within_ttl() -> None:
    calls: list[int] = []
    clock_value = [0.0]

    def _resolve() -> int:
        calls.append(1)
        return len(calls)

    key = cached_endpoint_key(
        _resolve, ttl_s=1.0, clock=lambda: clock_value[0],
    )

    assert key() == 1
    clock_value[0] = 0.5  # still within the 1.0s window
    assert key() == 1, "a call inside the TTL window must reuse the cached value"
    assert len(calls) == 1


def test_cached_endpoint_key_recomputes_after_ttl_elapses() -> None:
    calls: list[int] = []
    clock_value = [0.0]

    def _resolve() -> int:
        calls.append(1)
        return len(calls)

    key = cached_endpoint_key(
        _resolve, ttl_s=1.0, clock=lambda: clock_value[0],
    )

    assert key() == 1
    clock_value[0] = 1.5  # past the 1.0s window
    assert key() == 2, "a call past the TTL window must re-resolve"
    assert len(calls) == 2


def test_cached_endpoint_key_bypasses_cache_when_fresh_required() -> None:
    """A resolver whose cheap (env-pinned) path must stay live: when
    ``is_fresh_required`` returns True, the cache is skipped entirely —
    memoizing an already-cheap value would only add staleness with no
    benefit (a rotated per-test tenant token must be caught on the very
    next call, not delayed up to ttl_s)."""
    calls: list[int] = []
    clock_value = [0.0]

    def _resolve() -> int:
        calls.append(1)
        return len(calls)

    key = cached_endpoint_key(
        _resolve, ttl_s=1000.0, clock=lambda: clock_value[0],
        is_fresh_required=lambda: True,
    )

    assert key() == 1
    assert key() == 2, "fresh_required=True must never consult the cache"
    assert key() == 3
    assert len(calls) == 3


def test_cached_endpoint_key_is_thread_safe_under_concurrent_misses() -> None:
    """Concurrent callers racing a cold cache must not corrupt the
    stored (value, timestamp) pair -- every caller gets SOME resolved
    value, and the cache converges to a consistent state afterward."""
    calls: list[int] = []
    lock = threading.Lock()

    def _resolve() -> int:
        with lock:
            calls.append(1)
            return len(calls)

    key = cached_endpoint_key(_resolve, ttl_s=5.0)

    results: list[int] = []
    results_lock = threading.Lock()

    def _call() -> None:
        v = key()
        with results_lock:
            results.append(v)

    threads = [threading.Thread(target=_call) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(results) == 8
    assert all(isinstance(v, int) for v in results)
    # A second call now must be served from cache (no crash, consistent).
    assert isinstance(key(), int)
