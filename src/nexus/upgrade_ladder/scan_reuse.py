# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Hand a data rung's clean converge scan to its verify (nexus-mjmjl).

A data rung's converge walks every document; when that walk found nothing to
fill it wrote nothing, so it is the same reading a dry run would take. verify
runs moments later and used to take that reading again. :class:`ScanReuse`
holds the clean reading for one :meth:`take`, within :data:`VERIFY_REUSE_S`,
the way the RDR-192 backfill reuses its clean census. A walk that filled rows
is never held: verify reads afresh to confirm the fill.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Generic, TypeVar

#: The longest a held reading stays usable, in seconds.
VERIFY_REUSE_S: float = 60.0

T = TypeVar("T")


class ScanReuse(Generic[T]):
    """One held reading, taken at most once. *clock* is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._held: tuple[T, float] | None = None

    def keep(self, reading: T) -> None:
        self._held = (reading, self._clock())

    def clear(self) -> None:
        self._held = None

    def take(self) -> T | None:
        """The held reading if it is fresh, else ``None``; either way nothing is held after."""
        held, self._held = self._held, None
        if held is None or self._clock() - held[1] > VERIFY_REUSE_S:
            return None
        return held[0]
