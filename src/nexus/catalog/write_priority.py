# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interactive-vs-batch fairness primitives for catalog writes (RDR-146 P2).

The daemon is the single ``.catalog.db`` writer (RDR-146 Phase 1). Phase 2
closes the residual scheduling unfairness: a foreground ``nx dt index``
catalog write must not wait behind a sustained background ``nx index repo``
write burst (GH #1046, inverted).

The chosen lever is producer back-pressure, NOT a daemon priority queue
(see the PC-2 design spike): an interactive write tags its RPC frame
``priority="interactive"``, which opens a short in-memory deadline window
on the daemon; the background indexer polls that window before each
catalog write and yields. This module holds the two pure, side-effect-free
pieces of that mechanism so they are unit-testable with fixed clocks:

- :func:`resolve_write_priority` — how a call site decides its priority.
- :func:`await_fair_window` — the bounded escalating-backoff yield loop the
  background indexer runs (mirrors the RDR-129 B2 dispatch-retry shape:
  finite attempts, escalating sleeps, defined terminal).
"""
from __future__ import annotations

import os
import sys
import time
from typing import Callable

#: Daemon-side interactive window (seconds). After an interactive catalog
#: write, the probe reports "pending" for this long, refreshed per write so
#: a multi-op interactive burst (``nx dt index`` does register -> link ->
#: aspects as several ops) keeps the window open across the whole burst.
INTERACTIVE_WINDOW_S: float = 2.0

#: Bounded escalating yield budget for the background indexer (~1.75s worst
#: case). Finite by construction so the batch is never permanently starved.
INDEXER_YIELD_SLEEPS: tuple[float, ...] = (0.25, 0.5, 1.0)

#: Explicit override env var. Lets a hook force ``batch`` and a script force
#: ``interactive`` regardless of tty detection.
WRITE_PRIORITY_ENV: str = "NX_WRITE_PRIORITY"

_VALID_PRIORITIES: frozenset[str] = frozenset({"interactive", "batch"})


def resolve_write_priority(explicit: str | None = None) -> str:
    """Resolve the write priority for a catalog-write call site.

    Resolution order (cheapest, most explicit first):

    1. ``NX_WRITE_PRIORITY`` env override (``interactive`` | ``batch``).
    2. The *explicit* per-command intent argument, when a command threads
       one (``nx dt index`` / ``capture`` pass ``"interactive"``).
    3. ``sys.stdout.isatty()`` fallback, matching the existing convention:
       a tty is interactive, non-tty (the hook-spawned ``nx index repo``)
       is batch.

    Always returns one of ``"interactive"`` / ``"batch"``.
    """
    env = os.environ.get(WRITE_PRIORITY_ENV, "").strip().lower()
    if env in _VALID_PRIORITIES:
        return env
    if explicit in _VALID_PRIORITIES:
        return explicit  # type: ignore[return-value]
    try:
        return "interactive" if sys.stdout.isatty() else "batch"
    except (AttributeError, ValueError):
        # Detached / closed stdout: default to the safe non-disruptive batch.
        return "batch"


def await_fair_window(
    probe: Callable[[], bool],
    on_locked: str,
    *,
    sleeps: tuple[float, ...] = INDEXER_YIELD_SLEEPS,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    """Bounded yield loop a background (batch) producer runs before a write.

    Polls *probe* (the daemon's ``is_interactive_write_pending`` flag);
    while an interactive write is pending, backs off over the escalating
    *sleeps* budget. Returns:

    - ``"proceed"`` as soon as no interactive write is pending (the common
      case returns immediately with zero sleeps), OR after the bounded
      budget is exhausted with ``on_locked == "wait"`` (never permanently
      starve the batch: the RDR-129 "never trade a working state for none"
      discipline).
    - ``"skip"`` when the budget is exhausted and ``on_locked == "skip"``
      (defer this write to the next idempotent index pass).

    *sleep_fn* is injectable so tests drive the loop with a fake clock.
    """
    for delay in sleeps:
        if not probe():
            return "proceed"
        sleep_fn(delay)
    # Final probe after the last sleep: the window may have cleared during it,
    # in which case proceed rather than defer a write for no live reason.
    if not probe():
        return "proceed"
    return "skip" if on_locked == "skip" else "proceed"
