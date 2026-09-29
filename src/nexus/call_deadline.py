# SPDX-License-Identifier: AGPL-3.0-or-later
"""A wall-clock deadline that follows a call into its worker thread.

nexus-5ezgn. ``nx_answer`` cuts a retrieval step that outlives its budget by
abandoning the wait on it, but a thread cannot be killed, so the abandoned
search kept making round trips to the engine for its full duration with its
result discarded. The plan runner sets this deadline around the dispatch; the
worker runs in a copy of the caller's context, so long-running retrieval code
can call :func:`check` between stages and stop at the next boundary instead.

A process with no deadline set pays one ContextVar read per check.
"""
from __future__ import annotations

import time
from contextvars import ContextVar, Token

__all__ = ["DeadlineExceeded", "check", "get", "reset", "set_deadline"]

_DEADLINE: ContextVar[float | None] = ContextVar("nexus_call_deadline", default=None)


class DeadlineExceeded(Exception):
    """The call's deadline passed; raised by :func:`check` at a stage boundary."""


def set_deadline(deadline: float | None) -> Token:
    """Set a ``time.monotonic()`` deadline for this context; returns the reset token."""
    return _DEADLINE.set(deadline)


def reset(token: Token) -> None:
    _DEADLINE.reset(token)


def get() -> float | None:
    return _DEADLINE.get()


def check(stage: str) -> None:
    """Raise :class:`DeadlineExceeded` if this context's deadline has passed."""
    deadline = _DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise DeadlineExceeded(stage)
