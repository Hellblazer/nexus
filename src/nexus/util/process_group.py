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

WINDOWS (nexus-34f7r). ``os.killpg``, ``os.getpgid`` and ``signal.SIGKILL``
do not exist there, and ``start_new_session=True`` is silently ignored at
spawn, so there is never a group to signal. Before this module handled that,
the ``SIGKILL`` default arguments below raised ``AttributeError`` at IMPORT,
and every cleanup branch that reached for this helper threw from inside its
own ``except``/``finally``, masking the original error. :data:`KILL_SIGNAL`
is the platform's hard kill, :func:`safe_killpg` degrades to signalling the
one process (the same weaker reach ``nexus.bounded_subprocess`` reports),
and :func:`safe_killpg_group` refuses, because a recorded pid whose owner
may already have exited is not safe to kill by number alone.
"""
from __future__ import annotations

import os
import signal as _signal
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: The platform's hard kill: ``SIGKILL`` on POSIX. Windows has no
#: ``SIGKILL``; there ``os.kill(pid, SIGTERM)`` calls TerminateProcess, which
#: is the same uncatchable stop. Use this instead of ``signal.SIGKILL`` on any
#: path a Windows client can reach (``tests/test_process_group_safety.py``
#: holds the census).
KILL_SIGNAL: int = getattr(_signal, "SIGKILL", _signal.SIGTERM)


def safe_killpg(
    proc_or_pid: Any,
    sig: int = KILL_SIGNAL,
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
    4. Where ``os.killpg`` does not exist (Windows), signals the process
       itself with ``os.kill``. The reach is weaker, one process instead of
       its tree, and ``True`` then means that process was signalled.

    ON WINDOWS THERE IS NO GRACEFUL SIGNAL. ``os.kill`` with anything but a
    console CTRL event is TerminateProcess, so a caller's ``signal.SIGTERM``
    meant as "stop, then escalate" is the hard kill and its grace window is
    gone. That is still better than the alternative: refusing would return
    ``False``, which callers read as "already gone". A real graceful stop
    there needs CTRL_BREAK_EVENT to a child spawned with
    CREATE_NEW_PROCESS_GROUP, which is nexus-6y4e0's design question.

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
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        # Windows: one process, and any sig is TerminateProcess (docstring).
        try:
            os.kill(pid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False
    try:
        pgid = os.getpgid(pid)
        killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def safe_killpg_group(pgid: Any, sig: int = KILL_SIGNAL) -> bool:
    """Signal process group *pgid* directly, safely (nexus-5ny9r).

    :func:`safe_killpg` resolves the group from a LIVE pid. After the
    group leader has exited and been reaped, ``os.getpgid(pid)`` raises
    and the sweep silently does nothing — which is how a MinerU worker's
    ``os._exit`` left its multiprocessing pool child and resource tracker
    reparented to init on every page (324 of them on one box). A leader
    spawned with ``start_new_session=True`` has ``pgid == pid`` for its
    whole life, so the caller records that number at spawn and sweeps by
    it afterwards; the group id stays reserved while any member lives.

    Same guards and same swallow contract as :func:`safe_killpg`: a
    non-int or ``pgid <= 1`` is refused (``1`` is init's group), and
    ``ESRCH`` (nothing left in the group) / ``EPERM`` return ``False``.

    Returns ``False`` without signalling anything where ``os.killpg`` does
    not exist (Windows). There is no group there, and the number is the
    LEADER's pid, recorded because the leader may already be gone, so
    ``os.kill`` on it could reach an unrelated process that reused the pid.
    """
    if not isinstance(pgid, int) or isinstance(pgid, bool):
        _log.debug("safe_killpg_group_type_guard", pgid_type=type(pgid).__name__)
        return False
    if pgid <= 1:
        _log.debug("safe_killpg_group_pgid_guard", pgid=pgid)
        return False
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        _log.debug("safe_killpg_group_no_process_groups", pgid=pgid)
        return False
    try:
        killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


__all__ = ["KILL_SIGNAL", "safe_killpg", "safe_killpg_group"]
