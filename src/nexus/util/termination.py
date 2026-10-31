# SPDX-License-Identifier: AGPL-3.0-or-later
"""SIGTERM and SIGHUP as an exception for the length of one call (nexus-4vx0a, nexus-eq03c).

The default disposition of either signal ends the process at once: no ``finally``, no
``atexit``. A run that holds server-side state (the pipeline row of
``pipeline_stages.pipeline_index_pdf``) then leaves it to age out. SIGHUP is what a
closing terminal or a dropped ssh session sends. Inside :func:`sigterm_raises` either
signal raises :class:`TerminationRequested` in the main thread instead, so the run's own
exception handling gives the state back. When the block exits after one fired, the
previous disposition is restored and that signal is delivered again, so the process
still ends the way it would have (exit status 143 for SIGTERM and 129 for SIGHUP under
the default disposition, or the earlier handler runs).

SIGINT needs none of this: it already raises ``KeyboardInterrupt``.

:func:`interrupts_deferred` holds SIGINT, SIGTERM and SIGHUP back for a short call whose
answer the cleanup needs (nexus-euczk): the signal is acted on when the call returns.
"""
from __future__ import annotations

import contextlib
import os
import signal
import threading
from collections.abc import Iterator
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: The signals :func:`sigterm_raises` turns into :class:`TerminationRequested`.
TERMINATION_SIGNALS: tuple[signal.Signals, ...] = tuple(
    s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGHUP", None)) if s is not None
)


class TerminationRequested(BaseException):
    """A SIGTERM or SIGHUP arrived inside :func:`sigterm_raises`.

    A ``BaseException``, like ``KeyboardInterrupt``, so no ``except Exception``
    in the code under the block swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum

    @property
    def signal_name(self) -> str:
        """``SIGTERM``, ``SIGHUP``, or the number for a signal this platform does not name."""
        try:
            return signal.Signals(self.signum).name
        except ValueError:
            return str(self.signum)


def _install(handler: Any, signums: tuple[int, ...]) -> dict[int, Any]:
    """Install *handler* for each of *signums* that is not ignored; the previous handlers, by signal.

    Empty off the main thread (``signal.signal`` only works there)."""
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous: dict[int, Any] = {}
    for signum in signums:
        try:
            before = signal.getsignal(signum)
            if before is signal.SIG_IGN:
                continue  # nohup, or a parent that ignores it: leave that choice alone
            signal.signal(signum, handler)
        except (ValueError, OSError):
            continue
        previous[signum] = before
    return previous


@contextlib.contextmanager
def sigterm_raises() -> Iterator[None]:
    """Turn SIGTERM and SIGHUP into :class:`TerminationRequested` for the block.

    A no-op off the main thread, and for a signal that is ignored (``nohup``
    ignores SIGHUP, and that stays so). The first signal raises; a further one
    while the block is still cleaning up is ignored, so a supervisor that repeats
    the signal cannot end the process between the interrupt and the cleanup it
    exists for.

    Once a signal has fired, it is delivered again when the block exits,
    WHATEVER leaves it: the exception itself, another exception the code
    under the block chose to raise instead (a stage failure that was already
    propagating), or a normal return. Re-delivering only from
    ``except TerminationRequested`` would lose the signal in each of those
    cases, and the process would carry on as if never asked to stop
    (nexus-4vx0a review). Usable as a decorator."""
    fired: list[int] = []
    closing: list[bool] = []

    def _handler(signum: int, frame: Any) -> None:
        if fired:
            return
        fired.append(signum)
        if closing:
            # The block is already on its way out (the finally below); it re-delivers. Raising
            # here could leave this handler installed and every later signal ignored.
            return
        raise TerminationRequested(signum)

    previous = _install(_handler, TERMINATION_SIGNALS)
    try:
        yield
    finally:
        closing.append(True)
        _restore(_handler, previous)
        if fired:
            _redeliver(fired[0], previous.get(fired[0]))


@contextlib.contextmanager
def interrupts_deferred() -> Iterator[None]:
    """Hold SIGINT, SIGTERM and SIGHUP back until the block ends (nexus-euczk).

    For a call whose ANSWER the interrupt's cleanup needs: ``create_pipeline``
    makes the row ``running`` server-side, and an interrupt that lands while its
    HTTP call is in flight leaves the client without the ``pipeline_id`` and
    ``run_epoch`` that releasing the row takes. Inside the block the first such
    signal is recorded; when the block exits, the handler that was in place
    before it runs for that signal (``sigterm_raises``'s raises
    :class:`TerminationRequested`, Python's SIGINT handler raises
    ``KeyboardInterrupt``), or the signal is sent again under the default
    disposition. Keep the block to one bounded call: a second Ctrl-C inside it
    does not end the process. A no-op off the main thread."""
    fired: list[int] = []

    def _hold(signum: int, frame: Any) -> None:
        if not fired:
            fired.append(signum)

    signums = (signal.SIGINT, *TERMINATION_SIGNALS)
    previous = _install(_hold, signums)
    try:
        yield
    finally:
        _restore(_hold, previous)
        if fired:
            before = previous.get(fired[0])
            _log.info("interrupt_deferred_delivered", signum=fired[0])
            if callable(before):
                before(fired[0], None)
            else:
                os.kill(os.getpid(), fired[0])


def _restore(handler: Any, previous: dict[int, Any]) -> None:
    """Put each previous handler back, unless someone installed their own meanwhile."""
    for signum, before in previous.items():
        with contextlib.suppress(ValueError, OSError):
            if signal.getsignal(signum) is handler:
                signal.signal(signum, before if before is not None else signal.SIG_DFL)


def _redeliver(signum: int, previous: Any) -> None:
    """End the process the way the signal would have without the block."""
    _log.info("sigterm_redelivered", signum=signum)
    if callable(previous):
        previous(signum, None)
        return
    os.kill(os.getpid(), signum)
