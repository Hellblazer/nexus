# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Send ``CTRL_BREAK`` to another process's console process group, from a
process that is not on that console (RDR-224, nexus-f9bgu.17), ctypes only.

``GenerateConsoleCtrlEvent`` reaches only a process group on the CALLER's
own console. A caller with no console fails with ``ERROR_INVALID_HANDLE``,
and a caller on a different console can get ``TRUE`` back and deliver
nothing (T2 ``nexus_rdr/224-research-20``). So the stopper detaches from its
console, attaches to the target's, sends, and re-attaches to its parent's:

    FreeConsole()
    AttachConsole(target pid)
    GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, target pid)
    FreeConsole()
    AttachConsole(ATTACH_PARENT_PROCESS)

The last two steps put the CLI's own console back. The second ``FreeConsole``
is required: ``AttachConsole`` is refused while the caller is still on the
target's console. The send is
process-global state (a process has one console at a time), so the sequence
runs under one lock.

A ``TRUE`` from the send is not proof of delivery. A caller confirms a stop
by the target's exit (``service_registry.pid_alive``), never by this module's
return value.

``AttachConsole`` fails with ``ERROR_ACCESS_DENIED`` when the target is in
another Windows session (an ssh logon is session 0, the desktop logon is
session 1; research-20). That is reported as ``refused`` with both session
ids, so a caller can say where the stop has to come from instead of killing
a process it was told it could not reach.

TESTABILITY: the Win32 calls are behind :class:`WinConsoleApi`, injected, so
the sequence runs under test on every host. The real binding
(:func:`ctypes_win_console_api`) exists only on Windows.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Protocol

import structlog

_log = structlog.get_logger(__name__)

#: ``CTRL_BREAK_EVENT`` for ``GenerateConsoleCtrlEvent`` (wincon.h).
CTRL_BREAK_EVENT: int = 1

#: ``AttachConsole(ATTACH_PARENT_PROCESS)``: attach to the parent's console.
ATTACH_PARENT_PROCESS: int = 0xFFFFFFFF

#: ``GetLastError`` values read here (winerror.h).
ERROR_ACCESS_DENIED: int = 5
ERROR_INVALID_HANDLE: int = 6

_MAX_WINDOWS_PID: int = 0xFFFFFFFF

#: A process has one console at a time, so two sends in one process must not
#: interleave their detach/attach steps.
_CONSOLE_LOCK = threading.Lock()


class WinConsoleApi(Protocol):
    """The kernel32 calls the send sequence makes. Each reports success and
    ``GetLastError`` where the sequence branches on it."""

    def free_console(self) -> bool:
        """``FreeConsole()``."""
        ...

    def attach_console(self, pid: int) -> tuple[bool, int]:
        """``AttachConsole(pid)``: ``(True, 0)`` or ``(False, GetLastError())``."""
        ...

    def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
        """``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)``."""
        ...

    def attach_parent_console(self) -> tuple[bool, int]:
        """``AttachConsole(ATTACH_PARENT_PROCESS)``."""
        ...

    def session_of(self, pid: int) -> int | None:
        """``ProcessIdToSessionId(pid)``, or ``None`` when it cannot be read."""
        ...


@dataclass(frozen=True)
class ConsoleBreakResult:
    """Outcome of one :func:`send_ctrl_break_via_console`.

    ``sent`` is ``GenerateConsoleCtrlEvent``'s return value and is NOT proof
    the target received anything. ``refused`` is True only for
    ``ERROR_ACCESS_DENIED`` from ``AttachConsole``. ``stage`` names where a
    failure happened: ``"invalid"``, ``"attach"``, ``"generate"`` or ``"ok"``.
    ``reattached`` says whether the caller's own console came back (False
    when the caller never had a parent console to return to).
    """

    sent: bool
    refused: bool = False
    stage: str = "ok"
    error: int | None = None
    target_session: int | None = None
    own_session: int | None = None
    reattached: bool = True


def send_ctrl_break_via_console(
    pid: int, api: WinConsoleApi, *, own_pid: int | None = None,
) -> ConsoleBreakResult:
    """Send ``CTRL_BREAK`` to the console process group rooted at *pid*, from
    a process on any console (or none). Never raises."""
    if pid <= 0 or pid > _MAX_WINDOWS_PID:
        return ConsoleBreakResult(sent=False, stage="invalid")
    with _CONSOLE_LOCK:
        api.free_console()
        attached, attach_error = api.attach_console(pid)
        if not attached:
            reattached, _ = api.attach_parent_console()
            refused = attach_error == ERROR_ACCESS_DENIED
            _log.info(
                "win_console_attach_failed",
                pid=pid,
                error=attach_error,
                refused=refused,
            )
            return ConsoleBreakResult(
                sent=False,
                refused=refused,
                stage="attach",
                error=attach_error,
                target_session=api.session_of(pid) if refused else None,
                own_session=(
                    api.session_of(own_pid if own_pid is not None else os.getpid())
                    if refused
                    else None
                ),
                reattached=reattached,
            )
        try:
            sent, send_error = api.generate_ctrl_break(pid)
        finally:
            # AttachConsole fails with ERROR_ACCESS_DENIED while the caller is
            # still attached to a console, and here it is attached to the
            # TARGET's. Without this FreeConsole the re-attach silently fails
            # and the caller stays on the supervisor's hidden console
            # (measured on Windows 11, nexus-f9bgu.17: reattached=False).
            api.free_console()
            reattached, _ = api.attach_parent_console()
        if not sent:
            _log.info("win_console_send_failed", pid=pid, error=send_error)
        return ConsoleBreakResult(
            sent=sent,
            stage="ok" if sent else "generate",
            error=None if sent else send_error,
            reattached=reattached,
        )


class _CtypesWinConsoleApi:
    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415 — Windows-only binding, built on first Windows send
        from ctypes import wintypes  # noqa: PLC0415

        self._ctypes = ctypes
        self._wintypes = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k32.FreeConsole.argtypes = ()
        k32.FreeConsole.restype = wintypes.BOOL
        k32.AttachConsole.argtypes = (wintypes.DWORD,)
        k32.AttachConsole.restype = wintypes.BOOL
        k32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
        k32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
        k32.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
        k32.ProcessIdToSessionId.restype = wintypes.BOOL
        self._k32 = k32

    def _last_error(self) -> int:
        return self._ctypes.get_last_error()  # type: ignore[attr-defined,no-any-return]

    def free_console(self) -> bool:
        return bool(self._k32.FreeConsole())

    def attach_console(self, pid: int) -> tuple[bool, int]:
        if self._k32.AttachConsole(pid):
            return True, 0
        return False, self._last_error()

    def generate_ctrl_break(self, pid: int) -> tuple[bool, int]:
        if self._k32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid):
            return True, 0
        return False, self._last_error()

    def attach_parent_console(self) -> tuple[bool, int]:
        if self._k32.AttachConsole(ATTACH_PARENT_PROCESS):
            return True, 0
        return False, self._last_error()

    def session_of(self, pid: int) -> int | None:
        out = self._wintypes.DWORD(0)
        if self._k32.ProcessIdToSessionId(pid, self._ctypes.byref(out)):
            return int(out.value)
        return None


_api_cache: list[WinConsoleApi] = []


def ctypes_win_console_api() -> WinConsoleApi:
    """The real kernel32 binding, built once per process. Windows only."""
    if not _api_cache:
        _api_cache.append(_CtypesWinConsoleApi())
    return _api_cache[0]


__all__ = [
    "ATTACH_PARENT_PROCESS",
    "CTRL_BREAK_EVENT",
    "ERROR_ACCESS_DENIED",
    "ERROR_INVALID_HANDLE",
    "ConsoleBreakResult",
    "WinConsoleApi",
    "ctypes_win_console_api",
    "send_ctrl_break_via_console",
]
