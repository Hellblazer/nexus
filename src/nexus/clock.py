# SPDX-License-Identifier: AGPL-3.0-or-later
"""The clock that time-dependent code waits on (nexus-3ceda.11).

Code here already takes ``clock: Callable[[], float]`` for reading the time.
What a test could not control was the WAITING: ``Event.wait(interval)`` and
``time.sleep`` run on real time, so a test of a renewer or a heartbeat loop
had to sleep for real and count rounds, and under load it counted wrong.

A clock is still any callable returning seconds. A clock MAY also carry
``wait(event, timeout) -> bool`` and ``sleep(seconds)``; :func:`wait` and
:func:`sleep` below use them when present and fall back to real time when not.
Existing callers that pass ``time.monotonic`` are unchanged, and a test that
passes a virtual clock (``tests/_settle.py`` ``FakeClock``) as the same
``clock=`` argument makes the loop's waits virtual too.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

__all__ = ["SYSTEM", "Clock", "sleep", "wait"]

#: A clock: a callable returning seconds.
Clock = Callable[[], float]


#: Real monotonic time.
SYSTEM: Clock = time.monotonic


def wait(clock: Clock, event: threading.Event, timeout: float) -> bool:
    """``event.wait(timeout)`` measured on *clock*: True when the event was set,
    False when *timeout* seconds of *clock* time passed first."""
    owner = getattr(clock, "wait", None)
    if callable(owner):
        return bool(owner(event, timeout))
    return event.wait(timeout)


def sleep(clock: Clock, seconds: float) -> None:
    """Sleep *seconds* of *clock* time."""
    owner = getattr(clock, "sleep", None)
    if callable(owner):
        owner(seconds)
        return
    time.sleep(seconds)
