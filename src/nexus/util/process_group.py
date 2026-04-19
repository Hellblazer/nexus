# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Safe process-group signalling.

Every site that spawns a subprocess with ``start_new_session=True`` and
later cleans it up with ``os.killpg(os.getpgid(pid), sig)`` must guard
against a specific class of mock-fixture hazard:

    >>> from unittest.mock import MagicMock
    >>> proc = MagicMock()
    >>> proc.pid                              # noqa -- a MagicMock attribute
    <MagicMock name='mock.pid' id=...>
    >>> import os
    >>> os.getpgid(proc.pid)                  # __index__ → 1
    1
    >>> os.killpg(1, signal.SIGKILL)          # signals pgid 1 — init / launchd

On macOS the signal is typically blocked with EPERM. On Linux
containers used by CI (GitHub Actions ubuntu-latest in particular) the
in-kernel signal-delivery path can stall deterministically even when
the caller lacks permission — the kernel's authorisation check
interacts badly with cgroup accounting for signals targeting init.
The observable symptom is a hung pytest step with no timeout.

``safe_killpg`` centralises the ``isinstance(pid, int)`` guard so mock
tests deterministically skip the kernel call and real subprocesses
continue to work unchanged.
"""
from __future__ import annotations

import os
import signal as _signal
from typing import Any

import structlog

_log = structlog.get_logger(__name__)


def safe_killpg(
    proc_or_pid: Any,
    sig: int = _signal.SIGKILL,
) -> bool:
    """Signal the process group of *proc_or_pid* with *sig*, safely.

    Accepts either a :class:`subprocess.Popen` / asyncio subprocess
    object (reads ``.pid``) **or** a raw integer pid. Returns ``True``
    when the signal was delivered, ``False`` on any non-delivery — mock
    fixture, absent process, or EPERM.

    Callers use this anywhere they would otherwise write::

        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)

    The safety properties the helper guarantees:

    1. ``isinstance(pid, int)`` guards ``MagicMock.pid`` — which
       ``__index__``-coerces to ``1`` and would otherwise signal
       ``pgid=1`` (init / launchd).
    2. ``ProcessLookupError`` (PID already reaped), ``PermissionError``
       (EPERM), and generic ``OSError`` are all swallowed and reported
       as a ``False`` return. No caller cares about the specific cause
       — the process is gone or unreachable either way.
    3. On a mock path, emits a ``safe_killpg_mock_guard`` debug log so
       production misuse (passing a mock proc by accident) is
       observable in structured logs.

    The helper is intentionally *not* async: every call site is already
    synchronous (a subprocess-cleanup branch inside an ``except`` or
    ``finally``) and adding awaitability would require every caller to
    thread an event loop through.
    """
    pid = proc_or_pid.pid if hasattr(proc_or_pid, "pid") else proc_or_pid
    if not isinstance(pid, int):
        _log.debug(
            "safe_killpg_mock_guard",
            pid_type=type(pid).__name__,
            msg="proc.pid is not an int — skipping killpg",
        )
        return False
    # pid <= 0 would route the signal to a wildcard target:
    #   pid == 0  → os.getpgid(0) returns the *caller's* pgid → kills `nx` itself
    #   pid == -1 → would be rejected by getpgid, but be explicit
    # A truncated or zero-byte pidfile that parses as 0 (e.g. from a mineru
    # crash before the child wrote its pid) must never self-terminate the CLI.
    if pid <= 0:
        _log.debug(
            "safe_killpg_nonpositive_pid_guard",
            pid=pid,
            msg="pid <= 0 would target the caller's own pgid — skipping killpg",
        )
        return False
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


__all__ = ["safe_killpg"]
