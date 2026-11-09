# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wait on named pending work instead of sleeping (nexus-3ceda.11).

``settle(pending, deadline)`` calls *pending* until it answers with nothing
pending, and on timeout fails with the last answer, so the failure says what
hung (queue depths, unacked tuples, held leases) and not only that a test was
slow. It returns as soon as the work is done, so the deadline can be generous:
it bounds a hang, it is not a guess at how long the work takes.

``FakeClock`` is a virtual clock for code that takes ``clock=`` and waits
through :mod:`nexus.clock`. Its time moves only when the test calls
:meth:`FakeClock.advance` or :meth:`FakeClock.step`, so a renewer with a 120 s
interval runs three rounds in a test that takes milliseconds, and the round
count is exact under any load.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable

import pytest

#: How often a FakeClock waiter rechecks its event. An ``Event.set()`` from
#: another thread does not notify the clock's condition, so waiters poll; the
#: interval affects latency only, never which way a wait ends.
_RECHECK_S: float = 0.005


def settle(
    pending: Callable[[], object],
    deadline: float = 10.0,
    *,
    interval: float = 0.01,
    what: str = "",
) -> None:
    """Call *pending* until its answer is empty or false; fail after *deadline*
    real seconds with that answer.

    *pending* returns what is still outstanding: a dict of counts, a list of
    ids, a string, or a bool. ``None``, ``False`` and any empty value mean
    settled. A probe that raises propagates at once. The probe always runs once
    more after the deadline passes, so work that finishes at the deadline is a
    pass.
    """
    end = time.monotonic() + deadline
    while True:
        answer = pending()
        if not answer:
            return
        if time.monotonic() >= end:
            label = f"{what} " if what else ""
            pytest.fail(f"{label}did not settle within {deadline} s; still pending: {answer!r}")
        time.sleep(interval)


class FakeClock:
    """A virtual clock: callable for the time, and the owner of waits.

    Pass it wherever code takes ``clock=``. ``wait`` and ``sleep`` block until
    the test advances virtual time past their timeout (or the event is set).
    With ``auto_advance=True`` they advance time themselves instead, for
    single-threaded code that would otherwise wait on itself.
    """

    def __init__(self, start: float = 0.0, *, auto_advance: bool = False) -> None:
        self._now = start
        self._auto = auto_advance
        self._cond = threading.Condition()
        self._waiters = 0
        self._dues: list[float] = []

    def __call__(self) -> float:
        with self._cond:
            return self._now

    def advance(self, seconds: float) -> None:
        """Move virtual time forward by *seconds* and wake every waiter."""
        if seconds < 0:
            raise ValueError(f"a clock does not go backwards ({seconds})")
        with self._cond:
            self._now += seconds
            self._cond.notify_all()

    def waiters(self) -> int:
        """How many threads are blocked in :meth:`wait` or :meth:`sleep` now."""
        with self._cond:
            return self._waiters

    def step(
        self, seconds: float, *, parked: int = 1, deadline: float = 10.0,
        exited: Callable[[], bool] | None = None,
    ) -> None:
        """One round of a loop that waits *seconds*: settle until *parked* threads
        wait on this clock, advance *seconds*, then :meth:`settle_waiters`. When
        this returns, the round's work is done. A loop that EXITS on this round
        never parks again: drive that round with :meth:`advance` and settle on
        the loop's own state, or pass *exited* (true once the loop has ended),
        which counts an ended loop as settled."""
        settle(
            lambda: not (exited and exited()) and self.waiters() < parked
            and f"waiters={self.waiters()}, want {parked}",
            deadline, what="the clock's waiters",
        )
        if seconds < 0:
            raise ValueError(f"a clock does not go backwards ({seconds})")
        if exited is not None and exited():
            self.advance(seconds)  # the loop has ended: nothing to release or wait for
            return
        with self._cond:
            self._now += seconds
            released = sum(1 for due in self._dues if due <= self._now)
            self._cond.notify_all()
        if released < parked:
            pytest.fail(
                f"a step of {seconds} s released {released} waiter(s), want {parked}: "
                "the step is shorter than the loop's remaining wait, so no round ran"
            )
        self.settle_waiters(parked=parked, deadline=deadline, exited=exited)

    def settle_waiters(
        self, *, parked: int = 1, deadline: float = 10.0,
        exited: Callable[[], bool] | None = None,
    ) -> None:
        """Settle until no thread is still waiting on a timeout the clock has
        already passed (each one released has run its round) and *parked*
        threads wait on a future time again, or *exited* says the loop ended."""
        settle(lambda: self._released_or_short(parked, exited), deadline, what="the clock's waiters")

    def _released_or_short(self, parked: int, exited: Callable[[], bool] | None = None) -> str:
        with self._cond:
            stale = sum(1 for due in self._dues if due <= self._now)
            waiting = self._waiters
        if stale:
            return f"{stale} released waiter(s) not yet run"
        if waiting >= parked or (exited is not None and exited()):
            return ""
        return f"{waiting} parked, want {parked}"

    def wait(self, event: threading.Event, timeout: float) -> bool:
        with self._cond:
            if event.is_set():
                return True
            if self._auto:
                self._now += timeout
                self._cond.notify_all()
                return event.is_set()
            due = self._now + timeout
            self._waiters += 1
            self._dues.append(due)
            try:
                while not event.is_set() and self._now < due:
                    self._cond.wait(_RECHECK_S)
                return event.is_set()
            finally:
                self._waiters -= 1
                self._dues.remove(due)

    def sleep(self, seconds: float) -> None:
        self.wait(threading.Event(), seconds)
