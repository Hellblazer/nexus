# SPDX-License-Identifier: AGPL-3.0-or-later
"""SIGTERM as an exception for the length of one call (nexus-4vx0a).

SIGTERM's default disposition ends the process at once: no ``finally``, no
``atexit``. A run that holds server-side state (the pipeline row of
``pipeline_stages.pipeline_index_pdf``) then leaves it to age out. Inside
:func:`sigterm_raises` a SIGTERM raises :class:`TerminationRequested` in the
main thread instead, so the run's own exception handling gives the state back.
When the block exits after a SIGTERM, the previous disposition is restored and
the signal is delivered again, so the process still ends the way it would have
(exit status 143 under the default disposition, or the earlier handler runs).

SIGINT needs none of this: it already raises ``KeyboardInterrupt``.
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


class TerminationRequested(BaseException):
    """A SIGTERM arrived inside :func:`sigterm_raises`.

    A ``BaseException``, like ``KeyboardInterrupt``, so no ``except Exception``
    in the code under the block swallows it."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"terminated by signal {signum}")
        self.signum = signum


@contextlib.contextmanager
def sigterm_raises() -> Iterator[None]:
    """Turn SIGTERM into :class:`TerminationRequested` for the block.

    A no-op off the main thread (``signal.signal`` only works there) and when
    SIGTERM is ignored. The first SIGTERM raises; a further one while the block
    is still cleaning up is ignored, so a supervisor that repeats the signal
    cannot end the process between the interrupt and the cleanup it exists for.

    Once a SIGTERM has fired, it is delivered again when the block exits,
    WHATEVER leaves it: the exception itself, another exception the code
    under the block chose to raise instead (a stage failure that was already
    propagating), or a normal return. Re-delivering only from
    ``except TerminationRequested`` would lose the signal in each of those
    cases, and the process would carry on as if never asked to stop
    (nexus-4vx0a review). Usable as a decorator."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous: Any = signal.getsignal(signal.SIGTERM)
    except (ValueError, OSError):
        yield
        return
    if previous is signal.SIG_IGN:
        yield
        return
    fired: list[int] = []
    closing: list[bool] = []

    def _handler(signum: int, frame: Any) -> None:
        if fired:
            return
        fired.append(signum)
        if closing:
            # The block is already on its way out (the finally below); it re-delivers. Raising
            # here could leave this handler installed and every later SIGTERM ignored.
            return
        raise TerminationRequested(signum)

    try:
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, OSError):
        yield
        return
    try:
        yield
    finally:
        closing.append(True)
        _restore(_handler, previous)
        if fired:
            _redeliver(fired[0], previous)


def _restore(handler: Any, previous: Any) -> None:
    """Put *previous* back, unless someone installed their own handler meanwhile."""
    with contextlib.suppress(ValueError, OSError):
        if signal.getsignal(signal.SIGTERM) is handler:
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


def _redeliver(signum: int, previous: Any) -> None:
    """End the process the way the signal would have without the block."""
    _log.info("sigterm_redelivered", signum=signum)
    if callable(previous):
        previous(signum, None)
        return
    os.kill(os.getpid(), signum)
