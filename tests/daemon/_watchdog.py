# SPDX-License-Identifier: AGPL-3.0-or-later
"""A per-test watchdog for the stop-channel and lifecycle-conformance files
(RDR-224, nexus-f9bgu.33, code review: falsifiability).

``pytest-timeout`` is not a dependency, and two mutations of the Windows stop
path could not fail: a sharing-violation retry made unbounded and a spawn lock
that ignores a stop request both made the scoped run HANG (one burned ten CPU
minutes), so a regression showed as a stuck CI job and no red test. This turns a
hang into a failure.

* Where ``SIGALRM`` exists (every POSIX host) the watchdog is an interval timer
  whose handler raises :class:`WatchdogTimeout` in the main thread, so the test
  FAILS with a traceback at the line it was stuck on and the next test runs.
* On Windows there is no ``SIGALRM``; the watchdog is ``faulthandler``'s
  ``dump_traceback_later(exit=True)``, which writes every thread's traceback and
  ends the process. That is a red job with the stuck stack in its log, bounded
  in time, instead of a hang, though it cannot fail one test and go on.

The limit is per test and generous (the slowest real test here, a child process
that must be hard-killed after a grace, takes a few seconds). It only has to be
finite.
"""
from __future__ import annotations

import contextlib
import faulthandler
import signal
import sys
from collections.abc import Iterator

#: Seconds one watched test may run before it is failed.
PER_TEST_TIMEOUT_S: float = 120.0

#: Test modules the autouse fixture in ``conftest.py`` watches.
WATCHED_MODULES: frozenset[str] = frozenset({
    "test_storage_service_stop_channel",
    "test_storage_service_stop_cli",
    "test_rdr149_lifecycle_conformance",
    "test_windows_stop_marker",
    "test_supervisor_stop_grace",
    "test_hard_kill_and_posix_wait",
    "test_windows_upgrade_replace",
})


class WatchdogTimeout(AssertionError):  # noqa: N818 — an AssertionError so pytest reports it as a test failure
    """A watched test ran past :data:`PER_TEST_TIMEOUT_S`."""


def mechanism(platform: str | None = None) -> str:
    """``"alarm"`` where ``SIGALRM`` is the tool, ``"faulthandler"`` on Windows."""
    return "faulthandler" if (platform if platform is not None else sys.platform) == "win32" else "alarm"


@contextlib.contextmanager
def watchdog(seconds: float, label: str, *, platform: str | None = None) -> Iterator[None]:
    """Fail (or, on Windows, end the process with every stack) if the body outlives *seconds*."""
    if mechanism(platform) == "faulthandler":
        faulthandler.dump_traceback_later(seconds, exit=True)
        try:
            yield
        finally:
            faulthandler.cancel_dump_traceback_later()
        return

    def on_alarm(_signum: int, _frame: object) -> None:
        raise WatchdogTimeout(f"{label} ran longer than {seconds:g}s: a wait or retry with no bound")

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
