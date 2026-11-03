# SPDX-License-Identifier: AGPL-3.0-or-later
"""A per-process request gate: a minimum interval between HTTP requests, and a count of them (nexus-ldf2s).

``nx catalog backfill-meta`` sweeps a whole catalog and must not saturate a shared service. Counting its
logical calls cannot bound that: one call pages inside the client, a gateway retry sends the same request
again, and a failed ``update_many`` page falls back to one request per document. So the limit sits where
every request leaves the process, at the transports' send points: the pooled urllib transport
(:mod:`nexus.db.pooled_http`, T3 and everything on it), the refreshable httpx store mixin (the catalog),
and the two stray one-shots (the managed ``/version`` probe, the data-token mint). Each calls
:func:`before_request` immediately before it puts a request on the wire, once per attempt, so a retry, a
stale-connection replay and a fallback are each paced and counted.

The gate is a leaf: it imports nothing from ``nexus``. Nothing is installed by default, and
:func:`before_request` is then a single attribute read. A command installs one with :func:`paced` for the
length of its run.
"""
from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator


class RequestGate:
    """At most *max_rps* requests a second: :meth:`acquire` hands out send slots at least ``1 / max_rps``
    seconds apart and blocks until its slot. ``max_rps=None`` paces nothing and still counts.

    The slots are reserved under a lock and slept for outside it, so concurrent callers queue in order and
    no two are granted closer than the interval, whatever the thread. The first request waits one interval
    as well, so N requests take at least ``N / max_rps`` seconds from the first call: the rate over a whole
    run, counted from its start, never exceeds the limit either."""

    def __init__(
        self, max_rps: float | None = None, *,
        clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_rps is not None and not max_rps > 0:
            raise ValueError(f"max_rps must be above 0, got {max_rps}")
        self.max_rps = max_rps
        self.interval = 1.0 / max_rps if max_rps is not None else 0.0
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_slot: float | None = None
        self.count = 0
        self.stamps: list[float] = []   # the clock reading of each grant, for tests and diagnostics

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            if self._next_slot is None:
                self._next_slot = now + self.interval   # the first request waits one interval too
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
            self.count += 1
            self.stamps.append(slot)
        wait = slot - now
        if wait > 0:
            self._sleep(wait)


_active: RequestGate | None = None


def before_request() -> None:
    """Called by a transport right before each request attempt goes on the wire. Free when no gate is installed."""
    gate = _active
    if gate is not None:
        gate.acquire()


def requests_so_far() -> int:
    """The installed gate's request count, 0 when none is installed."""
    gate = _active
    return gate.count if gate is not None else 0


@contextlib.contextmanager
def paced(gate: RequestGate) -> Iterator[RequestGate]:
    """Install *gate* for the length of the block, then put back what was there."""
    global _active  # noqa: PLW0603 — the one process-wide hook the transports consult
    previous = _active
    _active = gate
    try:
        yield gate
    finally:
        _active = previous
