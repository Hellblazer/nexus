# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Windows Job Object process-tree containment (nexus-6y4e0), ctypes-only.

``start_new_session=True`` makes a POSIX child a session/group leader, so
the whole tree it spawns can be reached later with one ``killpg``. Windows
has no equivalent primitive: the kwarg is accepted and silently ignored
(``bounded_subprocess`` and ``util.process_group`` both document this), so
a subprocess timeout there kills only the direct child and every
grandchild survives.

The Windows analogue of a killable process group is a **Job Object**:
create one with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, assign the child to
it right after spawn, and every descendant that does not explicitly break
away joins the same job automatically. Closing the job's last handle then
terminates every process still assigned to it — the tree-kill this module
exists to provide.

STANDARD LIBRARY ONLY. No ``pywin32``: this is a Windows client-side
runtime dependency this project does not otherwise carry, and the whole
surface needed here (``CreateJobObjectW``, ``SetInformationJobObject``,
``OpenProcess``, ``AssignProcessToJobObject``, ``CloseHandle``,
``GenerateConsoleCtrlEvent``) is five ``kernel32`` exports reachable
directly through :mod:`ctypes`.

NOT WINDOWS: every public function here degrades to a no-op return
(``None``/``False``) rather than raising. ``IS_WINDOWS`` gates every kernel32
touch, so importing this module on macOS/Linux never references
``ctypes.WinDLL`` (which does not exist there — the exact AttributeError
class nexus-34f7r found in :mod:`nexus.util.process_group`). ``ctypes`` and
``ctypes.wintypes`` themselves import cleanly on every platform (the latter
is pure-Python type aliases); only the ``WinDLL`` binding at the bottom of
this module is platform-gated.

TESTABILITY: ``_kernel32`` and ``IS_WINDOWS`` are read as module globals
inside every function (never captured into a local at import time), so
``tests/test_win_job.py`` exercises the Windows branch on macOS/Linux by
monkeypatching both — ``win_job.IS_WINDOWS = True`` and
``win_job._kernel32 = <fake with the same five methods>`` — without a real
Windows box. That is a shape test, not a behavioral one: it proves this
module calls the five APIs with the arguments the design calls for, not
that the real kernel32 behaves as documented. The qwentescence run in
nexus-6y4e0's closing report is what confirms the latter.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import sys
from typing import Any

import structlog

_log = structlog.get_logger(__name__)

#: True only on native Windows. Every public function below is a no-op
#: (returns None/False) when this is False, so a caller can dial this
#: module unconditionally from cross-platform code.
IS_WINDOWS: bool = sys.platform == "win32"

#: ``CREATE_NEW_PROCESS_GROUP`` — a Windows ``Popen(creationflags=...)``
#: value, not a job-object constant. It does not by itself contain a
#: process tree (that is what the job object is for); it makes the child
#: the root of its own console process group, which is what a later
#: ``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)`` graceful stop needs
#: to target the child without also signalling the caller. Exported here
#: so a spawn site can add it to ``creationflags`` alongside the job-object
#: containment this module provides — see
#: :func:`nexus.util.process_group.isolation_popen_kwargs`.
CREATE_NEW_PROCESS_GROUP: int = 0x00000200

#: Console control event for a graceful stop of a process spawned with
#: ``CREATE_NEW_PROCESS_GROUP`` (see :func:`send_ctrl_break`). The storage
#: supervisor sends it to its engine from the same console; the stopper
#: reaches the supervisor through ``nexus.util.win_console``.
CTRL_BREAK_EVENT: int = 1

#: ``JOBOBJECT_LIMIT_KILL_ON_JOB_CLOSE`` — the job-object limit flag that
#: makes closing the job's last handle terminate every process still
#: assigned to it. This is the tree-kill mechanism.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: int = 0x00002000

#: ``JobObjectExtendedLimitInformation`` — the ``JOBOBJECTINFOCLASS`` value
#: identifying the extended-limit-information struct in
#: ``SetInformationJobObject``.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: int = 9

#: Process access rights ``AssignProcessToJobObject`` needs from
#: ``OpenProcess``: it must be able to both query and (as the job's
#: eventual kill) terminate the target.
_PROCESS_SET_QUOTA: int = 0x0100
_PROCESS_TERMINATE: int = 0x0001


class _IoCounters(ctypes.Structure):
    """``IO_COUNTERS`` — embedded in the extended-limit struct below.

    Never read by this module; it exists only because
    ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` embeds it by value and
    ``SetInformationJobObject`` validates the struct's total size against
    what Windows expects, so the layout must match exactly.
    """

    _fields_ = (
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    )


class _JobObjectBasicLimitInformation(ctypes.Structure):
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION``. Only ``LimitFlags`` is set;
    every other field is left zeroed (no limit)."""

    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` — the struct
    ``SetInformationJobObject`` takes for ``JobObjectExtendedLimitInformation``.
    Only ``BasicLimitInformation.LimitFlags`` is populated; the memory-limit
    and I/O-counter fields are left zeroed (unused/unlimited)."""

    _fields_ = (
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


def _load_kernel32() -> Any | None:
    """Bind the five kernel32 exports this module needs, or ``None`` off
    Windows. Isolated in its own function so tests can monkeypatch the
    module-level ``_kernel32`` result directly without touching import
    machinery."""
    if not IS_WINDOWS:
        return None
    dll = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    dll.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    dll.CreateJobObjectW.restype = wintypes.HANDLE
    dll.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    dll.SetInformationJobObject.restype = wintypes.BOOL
    dll.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    dll.OpenProcess.restype = wintypes.HANDLE
    dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    dll.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    dll.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    return dll


#: Bound once at import time; read as a module global by every function
#: below (never captured into a local), so tests can replace it wholesale.
_kernel32: Any | None = _load_kernel32()


def _last_error() -> int | None:
    """``ctypes.get_last_error()`` where it exists, else ``None``.

    ``get_last_error``/``set_last_error`` are present in the ``ctypes``
    module only on a Windows build of CPython — this file imports on
    macOS/Linux too (see the module docstring), so every debug-log call
    site below reads through this rather than the bare attribute.
    """
    fn = getattr(ctypes, "get_last_error", None)
    return fn() if fn is not None else None


def create_job() -> int | None:
    """Create a job object configured to kill every assigned process when
    its last handle closes. Returns the handle as a plain ``int``, or
    ``None`` off Windows or on any API failure (never raises).

    The returned handle is a real Windows kernel resource: callers own it
    and must eventually pass it to :func:`close_job` exactly once, or it
    leaks for the life of the process.
    """
    if not IS_WINDOWS or _kernel32 is None:
        return None
    handle = None
    try:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            _log.debug("win_job_create_failed", error=_last_error())
            return None
        info = _JobObjectExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        # ctypes.pointer(), not the lighter ctypes.byref(): the latter
        # produces a call-only reference object that a real WinDLL call
        # accepts but a Python test double cannot introspect. A pointer is
        # equally valid here (this is not a hot path) and lets
        # tests/test_win_job.py's fake kernel32 read ``.contents`` back to
        # assert the LimitFlags this module actually set.
        ok = _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.pointer(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _log.debug("win_job_set_limit_failed", error=_last_error())
            _kernel32.CloseHandle(handle)
            return None
        return int(handle)
    except (OSError, ctypes.ArgumentError) as exc:
        # The module docstring promises every public function here
        # degrades to None/False rather than raising; nothing enforced
        # that until this review (nexus-6y4e0). Logs the exception CLASS
        # only -- a ctypes marshalling failure's message can embed raw
        # struct/pointer values, which do not belong in a log line.
        _log.debug("win_job_create_exception", exc_class=type(exc).__name__)
        if handle:
            try:
                _kernel32.CloseHandle(handle)
            except (OSError, ctypes.ArgumentError):
                pass
        return None


def assign_process(job: int | None, pid: int) -> bool:
    """Assign process *pid* to job object *job* (from :func:`create_job`).

    Returns ``False`` (never raises) off Windows, when *job* is falsy, when
    the process cannot be opened (already gone, or a permission mismatch —
    e.g. an elevated child), or when the assignment itself is refused (a
    process already in another job that forbids nesting on this Windows
    version).

    Every child spawned by *pid* after this call joins the same job
    automatically unless it explicitly breaks away
    (``CREATE_BREAKAWAY_FROM_JOB``), which is what makes this containment
    rather than a single-process reach: it need not be called again for
    grandchildren.
    """
    if not IS_WINDOWS or _kernel32 is None or not job:
        return False
    hproc = None
    try:
        hproc = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid,
        )
        if not hproc:
            _log.debug(
                "win_job_open_process_failed", pid=pid, error=_last_error(),
            )
            return False
        ok = bool(_kernel32.AssignProcessToJobObject(job, hproc))
        if not ok:
            _log.debug(
                "win_job_assign_failed", pid=pid, error=_last_error(),
            )
        return ok
    except (OSError, ctypes.ArgumentError) as exc:
        _log.debug(
            "win_job_assign_exception", pid=pid, exc_class=type(exc).__name__,
        )
        return False
    finally:
        if hproc:
            try:
                _kernel32.CloseHandle(hproc)
            except (OSError, ctypes.ArgumentError):
                pass


def close_job(job: int | None) -> bool:
    """Close *job*'s handle, killing every process still assigned to it.

    This IS the tree-kill operation: the job was created with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, so the last handle closing
    terminates every member, contained grandchildren included. Safe to
    call on a job whose processes already exited on their own — there is
    nothing left to kill and the close still succeeds.

    Returns ``False`` (never raises) off Windows or when *job* is falsy
    (already closed, or containment was never established for this
    spawn — the caller's degraded-reach path).
    """
    if not IS_WINDOWS or _kernel32 is None or not job:
        return False
    try:
        ok = bool(_kernel32.CloseHandle(job))
    except (OSError, ctypes.ArgumentError) as exc:
        _log.debug("win_job_close_exception", exc_class=type(exc).__name__)
        return False
    if not ok:
        _log.debug("win_job_close_failed", error=_last_error())
    return ok


def send_ctrl_break(pid: int) -> bool:
    """Send ``CTRL_BREAK_EVENT`` to the console process group rooted at
    *pid* — the graceful-stop counterpart of a POSIX ``SIGTERM`` to a
    process group, for a child spawned with :data:`CREATE_NEW_PROCESS_GROUP`.

    The caller must already be on the target's console. That holds for the
    storage-service supervisor stopping its own engine (the engine inherits
    the supervisor's console), which is the caller today
    (``StorageServiceSupervisor._stop_service`` and
    ``_kill_after_readiness_failure``, RDR-224, nexus-f9bgu.17). A caller on
    a DIFFERENT console, such as the ``nx`` CLI stopping the supervisor,
    must attach first: :func:`nexus.util.win_console.send_ctrl_break_via_console`.
    From the wrong console this can return ``True`` and deliver nothing, so
    the stop is confirmed by the target's exit and never by this return value.

    The mineru stop sites are still ``TerminateProcess`` on Windows; they do
    not use this.

    Returns ``False`` (never raises) off Windows or on any API failure.
    """
    if not IS_WINDOWS or _kernel32 is None:
        return False
    try:
        ok = bool(_kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid))
    except (OSError, ctypes.ArgumentError) as exc:
        _log.debug("win_job_ctrl_break_exception", exc_class=type(exc).__name__)
        return False
    if not ok:
        _log.debug(
            "win_job_ctrl_break_failed", pid=pid, error=_last_error(),
        )
    return ok


__all__ = [
    "CREATE_NEW_PROCESS_GROUP",
    "CTRL_BREAK_EVENT",
    "IS_WINDOWS",
    "assign_process",
    "close_job",
    "create_job",
    "send_ctrl_break",
]
