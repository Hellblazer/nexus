# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The soft ceiling for the detached ``nx upgrade --auto`` child (nexus-dcbof).

The SessionStart hook starts ``nx upgrade --auto`` detached and does not wait
for it (nexus-wozn6), so nothing bounds how long the child runs. A kill from
outside would land partway through a ladder rung, which is worse than letting
it finish. The ceiling is therefore cooperative: after N seconds the child
stops STARTING new work at safe points, and a safe point is a boundary where
nothing is half-written:

* between ladder rungs (the runner asks :meth:`Deadline.expired` before it
  starts each rung that has work to do), and
* inside the rung, between census calls and between backfill collections
  (the runner binds the deadline for the rung's run; the rung reads it back
  through :func:`active_deadline`).

A collection's backfill is either completed or not started. The next rung, or
the rest of the rung, is reported DEFERRED, the ledger holds only verified
completions, and the walk is level-triggered, so the next session's ``--auto``
run re-detects and resumes. Backfill progress persists in the database, so a
resumed run's census skips what is already done, and census progress persists
too (nexus-y67n5, :mod:`nexus.upgrade_ladder.census_progress`): each
collection's reading is kept, age bounded, so a slow engine's sessions together
finish the census instead of each starting it again. Consecutive cuts that
made no progress are counted and ``nx doctor`` shows them.

What the ceiling does NOT bound: (a) a census call already in flight, up to
its own HTTP timeout, and (b) one collection's entire backfill once it has
started (``backfill_manifest_for_collection`` makes many calls in a
per-document loop and can run well past the limit on a large collection); and
also the "finish" pass the root CLI
group runs before ``upgrade()`` starts (the clock starts in ``upgrade()``), and
the service stop/start cycle that runs in ``upgrade()``'s ``finally`` after the
ladder (about 60 s each).

Only the ``--auto`` path builds a :class:`Deadline`; an interactive ``nx
upgrade`` passes none and has no ceiling.
"""
from __future__ import annotations

import contextlib
import contextvars
import math
import os
import time
from collections.abc import Callable, Iterator, Mapping

import structlog

_log = structlog.get_logger(__name__)

#: Env var that overrides the ceiling, in seconds. At least 60.
AUTO_DEADLINE_ENV = "NX_UPGRADE_AUTO_DEADLINE_SECONDS"

#: 15 minutes: about 17 times the measured 51.5 s RDR-192 census over 98
#: collections (nexus-wozn6), and far past any healthy ``--auto`` run.
DEFAULT_AUTO_DEADLINE_S = 900.0

#: A value under this falls back to the default. A tiny ceiling would defer the
#: rung at its first safe point in every session and the upgrade would never
#: finish (a census alone is about 51 s).
MIN_AUTO_DEADLINE_S = 60.0


class Deadline:
    """A ceiling on elapsed process-awake time, started at construction (the
    default clock is ``time.monotonic``, which does not advance while the
    machine sleeps). The clock is injectable (seconds) so tests use a fixed
    clock and never sleep."""

    def __init__(
        self, seconds: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._seconds = float(seconds)
        self._clock = clock
        self._start = clock()

    @property
    def seconds(self) -> float:
        return self._seconds

    def elapsed(self) -> float:
        return self._clock() - self._start

    def expired(self) -> bool:
        return self.elapsed() >= self._seconds


#: The deadline bound for the rung that is running, if any. The runner binds it
#: around each rung it walks, so a rung's own safe points can read it without a
#: change to the Rung protocol.
_ACTIVE: contextvars.ContextVar[Deadline | None] = contextvars.ContextVar(
    "nexus_upgrade_active_deadline", default=None
)


def active_deadline() -> Deadline | None:
    """The deadline bound by the runner for the running rung, else ``None``."""
    return _ACTIVE.get()


def active_expired() -> bool:
    """True when a deadline is bound and has passed. Rungs call this at their
    safe points: between calls, never inside one."""
    deadline = _ACTIVE.get()
    return deadline is not None and deadline.expired()


@contextlib.contextmanager
def bound(deadline: Deadline | None) -> Iterator[None]:
    """Bind *deadline* (or none) as the active one for the enclosed run."""
    token = _ACTIVE.set(deadline)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def auto_deadline_seconds(environ: Mapping[str, str] | None = None) -> float:
    """The ceiling in seconds: :data:`AUTO_DEADLINE_ENV` when it holds a
    finite number of at least :data:`MIN_AUTO_DEADLINE_S`, else
    :data:`DEFAULT_AUTO_DEADLINE_S`. An invalid or too-small value falls back to
    the default with one ``upgrade_auto_deadline_env_invalid`` warning; it never
    fails the upgrade."""
    env = os.environ if environ is None else environ
    raw = env.get(AUTO_DEADLINE_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_AUTO_DEADLINE_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < MIN_AUTO_DEADLINE_S:
        _log.warning(
            "upgrade_auto_deadline_env_invalid",
            env=AUTO_DEADLINE_ENV,
            value=raw,
            using_seconds=DEFAULT_AUTO_DEADLINE_S,
        )
        return DEFAULT_AUTO_DEADLINE_S
    return value
