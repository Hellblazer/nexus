# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cause-agnostic event-loop spin guard (RDR-151, nexus-u2vmv).

The T2 daemon must be constitutionally unable to peg at ~100% CPU regardless of
client version mix or takeover races. The peg's signature is an asyncio event
loop whose ``selector.select()`` keeps returning a perpetually-ready fd on a
zero timeout — ``_run_once`` spins without ever blocking. We can't always
reproduce the specific trigger (it requires an old-version client), so instead
of chasing one path we make the loop spin-*proof*:

* :class:`SpinGuardSelector` counts zero-timeout ready-returns per fd — the
  precise spin signature (a healthy idle loop blocks in ``select``; a healthy
  busy loop is bounded by real work; only a spin polls ready thousands of
  times per second).
* :class:`SpinWatchdog` runs on a background thread (which still gets scheduled
  under a spinning loop — CPython releases the GIL on the switch interval) and,
  on a sustained over-threshold rate, invokes ``on_spin`` with a capture
  payload. The daemon wires ``on_spin`` to log the selector map + thread stacks
  (the ground-truth capture that has been missing) and then self-heal (SIGTERM →
  graceful stop → ``os._exit`` fallback), so a pegged daemon recovers instead of
  burning a core forever.
"""
from __future__ import annotations

import selectors
import threading
import time
from collections.abc import Callable
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: A ``select`` call that returns ready in under this many seconds is an
#: *immediate ready-return*. This is the spin shape and it is measured by
#: wall-time, NOT by the requested timeout: a perpetually-ready fd makes asyncio's
#: blocking ``select(timeout=None)`` return instantly (it never actually blocks),
#: which a timeout-based check would miss. A healthy idle loop blocks for real
#: (long duration); a healthy busy loop has a bounded immediate-return rate; only
#: a spin produces thousands of immediate ready-returns per second.
_IMMEDIATE_EPS: float = 0.0005


class SpinGuardSelector(selectors.DefaultSelector):  # type: ignore[misc]
    """``DefaultSelector`` that counts zero-timeout ready-returns per fd.

    Cheap: a couple of integer bumps per ``select`` call. The counters are read
    by :class:`SpinWatchdog` from another thread; plain int/dict reads and
    writes are atomic enough under the GIL for a rate estimate.
    """

    def __init__(self) -> None:
        super().__init__()
        self.zero_to_ready: int = 0
        self.ready_fd_hits: dict[int, int] = {}

    def select(self, timeout: float | None = None) -> list[Any]:  # type: ignore[override]
        t0 = time.monotonic()
        res = super().select(timeout)
        # Immediate ready-return (by wall-time) is the spin signature — see
        # _IMMEDIATE_EPS. Catches both select(0) polls and a blocking
        # select(None) that returns instantly on a perpetually-ready fd.
        if res and (time.monotonic() - t0) <= _IMMEDIATE_EPS:
            self.zero_to_ready += 1
            for key, _events in res:
                fd = key.fd
                self.ready_fd_hits[fd] = self.ready_fd_hits.get(fd, 0) + 1
        return res


class SpinWatchdog:
    """Detect a sustained event-loop spin and invoke ``on_spin``.

    Decision logic (:meth:`poll`) is separated from the threading (:meth:`start`)
    so it is deterministically testable with an injected clock. A spin is
    declared after ``consecutive`` polling windows whose zero-timeout
    ready-return *rate* meets or exceeds ``threshold_per_s``.
    """

    def __init__(
        self,
        counter: Any,
        *,
        threshold_per_s: float,
        window_s: float,
        consecutive: int,
        on_spin: Callable[[dict[str, Any]], None],
    ) -> None:
        self._counter = counter
        self._threshold = threshold_per_s
        self._window = window_s
        self._consecutive = consecutive
        self._on_spin = on_spin
        # Baseline at construction so the first poll() yields a real delta
        # (the deterministic tests rely on this); start() re-baselines to the
        # current monotonic clock for the live thread path.
        self._last_count: int = int(getattr(counter, "zero_to_ready", 0))
        self._last_time: float = 0.0
        self._streak: int = 0
        self._fired: bool = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def poll(self, now: float) -> bool:
        """Sample the counter at wall-clock *now*. Returns True iff a spin was
        declared this call (``on_spin`` already invoked)."""
        count = int(getattr(self._counter, "zero_to_ready", 0))
        dt = now - self._last_time
        delta = count - self._last_count
        self._last_count = count
        self._last_time = now
        if dt <= 0:
            return False
        rate = delta / dt
        if rate >= self._threshold:
            self._streak += 1
        else:
            self._streak = 0
        if self._streak >= self._consecutive and not self._fired:
            self._fired = True
            self._streak = 0
            self._on_spin(self._capture(rate))
            return True
        return False

    def _capture(self, rate: float) -> dict[str, Any]:
        hits: dict[int, int] = dict(getattr(self._counter, "ready_fd_hits", {}))
        hot_fd = max(hits, key=hits.get) if hits else None  # type: ignore[arg-type]
        return {
            "rate_per_s": rate,
            "hot_fd": hot_fd,
            "ready_fd_hits": hits,
            "threshold_per_s": self._threshold,
        }

    # ── live thread path ────────────────────────────────────────────────────

    def start(self, loop: Any = None) -> None:
        """Launch the watchdog on a daemon thread. ``loop`` is accepted for
        symmetry/future use; detection reads the selector counters directly."""
        import time

        self._last_time = time.monotonic()
        self._last_count = int(getattr(self._counter, "zero_to_ready", 0))

        def _run() -> None:
            while not self._stop.wait(self._window):
                try:
                    if self.poll(time.monotonic()):
                        return  # spun + handled; stop watching
                except Exception as exc:  # noqa: BLE001 — never crash the daemon
                    # But never die SILENTLY either: a silently-dead watchdog
                    # disarms the guard and the peg goes undetected. Log loud.
                    _log.warning("spin_watchdog_poll_error", error=repr(exc))

        self._thread = threading.Thread(
            target=_run, name="t2-spin-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
