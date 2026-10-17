# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The overall ceiling for the detached ``nx upgrade --auto`` child (nexus-dcbof).

The SessionStart hook starts ``nx upgrade --auto`` detached and does not wait
for it (nexus-wozn6), so nothing bounds how long the child runs. A kill from
outside would land partway through a ladder rung, which is worse than letting
it finish. The ceiling is therefore cooperative: the ladder runner asks
:meth:`Deadline.expired` BEFORE it starts each rung and stops starting new ones
once it is true. A rung that is running is never interrupted. The next
rung is reported as deferred, the ledger holds only verified completions, and
the walk is level-triggered, so the next session's ``--auto`` run re-detects
and resumes from the rung that was skipped.

Only the ``--auto`` path builds a :class:`Deadline`; an interactive ``nx
upgrade`` passes none and has no ceiling.
"""
from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Mapping

import structlog

_log = structlog.get_logger(__name__)

#: Env var that overrides the ceiling, in seconds. Positive number.
AUTO_DEADLINE_ENV = "NX_UPGRADE_AUTO_DEADLINE_SECONDS"

#: 15 minutes: about 17 times the measured 51.5 s RDR-192 census over 98
#: collections (nexus-wozn6), and far past any healthy ``--auto`` run.
DEFAULT_AUTO_DEADLINE_S = 900.0


class Deadline:
    """A wall-clock ceiling started at construction. The clock is injectable
    (monotonic seconds) so tests use a fixed clock and never sleep."""

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


def auto_deadline_seconds(environ: Mapping[str, str] | None = None) -> float:
    """The ceiling in seconds: :data:`AUTO_DEADLINE_ENV` when it holds a
    positive finite number, else :data:`DEFAULT_AUTO_DEADLINE_S`. An invalid
    value falls back to the default with one ``upgrade_auto_deadline_env_invalid``
    warning; it never fails the upgrade."""
    env = os.environ if environ is None else environ
    raw = env.get(AUTO_DEADLINE_ENV)
    if raw is None or raw.strip() == "":
        return DEFAULT_AUTO_DEADLINE_S
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        _log.warning(
            "upgrade_auto_deadline_env_invalid",
            env=AUTO_DEADLINE_ENV,
            value=raw,
            using_seconds=DEFAULT_AUTO_DEADLINE_S,
        )
        return DEFAULT_AUTO_DEADLINE_S
    return value
