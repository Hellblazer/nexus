# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Windows process identity: creation time, image path, command line, parent.

RDR-224 (nexus-f9bgu.21). Every site that asks ``ps`` or ``/proc`` who a
process is, how old it is, or what it is running has a Windows branch that
lands here. The POSIX branches return a pid's age in seconds, its argv
joined by single spaces, the basename of its executable and its parent pid;
these functions return exactly those shapes, so a caller's matcher
(``command.startswith(engine_path + " ")``, ``"daemon service start" in
command``, ``name.startswith("claude")``) works unchanged.

WHY THIS FILE SITS IN ``_install/`` AND IMPORTS NOTHING FROM NEXUS

``census_core.py`` runs with nexus absent (installer and GC dispatch into it
as a script) and needs the Windows process table, so the logic lives in a
stdlib-only module it loads through its ``_sibling`` accessor. The nexus
callers (``session``, ``service_registry``, ``aspect_worker_daemon``,
``doctor``) import it normally. One implementation, so there is nothing to
keep in step. Pinned by ``tests/test_winproc_core_is_bootstrap_safe.py``.

WHAT EACH ANSWER COMES FROM

* creation time: ``GetProcessTimes`` (FILETIME, 100 ns ticks since 1601).
* image path: ``QueryFullProcessImageNameW``.
* command line: ``NtQueryInformationProcess(ProcessCommandLineInformation)``,
  information class 60, Windows 8.1 and later. It needs only
  ``PROCESS_QUERY_LIMITED_INFORMATION``, reads the kernel's own copy (no
  remote PEB read, no ``PROCESS_VM_READ``), and costs one call. Windows has
  no documented cheap API for this; the alternatives are WMI/CIM (spawns a
  host, hundreds of ms per query) and a PEB walk (needs VM_READ and a wow64
  case). The callers match on argv tokens, so the command line is not
  optional: the engine is matched by argv[0] and the supervisor by
  ``daemon service start`` plus a ``--config-dir`` token.
* parent pid: ``NtQueryInformationProcess(ProcessBasicInformation)``.
* enumeration: ``CreateToolhelp32Snapshot`` + ``Process32FirstW/NextW``.

THE COMMAND LINE IS RE-RENDERED, NOT PASSED THROUGH. Windows keeps one raw
string, quoted per ``CommandLineToArgvW`` rules, where POSIX keeps an argv
vector and the callers see its space-joined form. :func:`render_command_line`
splits the raw string by the same rules and joins with single spaces, so
``"C:\\Program Files\\nx.exe" daemon`` becomes ``C:\\Program Files\\nx.exe
daemon``, the shape every matcher was written against. When the command line
is unreadable (a protected or elevated process, a pre-8.1 kernel) the image
path stands in for it, in BOTH enumeration and the single-pid read, so the
recycle re-check that compares them sees the same text.

A WINDOWS PARENT PID IS NOT REPARENTED ON EXIT. The child keeps naming a
parent that has exited, and the number can be handed to an unrelated new
process. :func:`parent_pid` therefore refuses a parent created AFTER its
child: that cannot be a parent, it is a recycled number.

TESTABILITY. Every function takes the :class:`WinProcessInfoApi` it uses, so
the Windows branches run under test on any host with a scripted fake; only
:func:`ctypes_win_info_api` touches kernel32 and it is built lazily.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Protocol

__all__ = [
    "WinProcessInfoApi",
    "ctypes_win_info_api",
    "executable_stem",
    "split_command_line",
    "render_command_line",
    "process_age_seconds",
    "process_image_path",
    "process_command_line",
    "process_command_name",
    "parent_pid",
    "enumerate_processes",
]

#: winnt.h / winerror.h values the Windows branch reads. Module-level so
#: tests script the same numbers.
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000
ERROR_ACCESS_DENIED: int = 5
ERROR_INVALID_PARAMETER: int = 87
ERROR_INSUFFICIENT_BUFFER: int = 122
TH32CS_SNAPPROCESS: int = 0x2
ERROR_NO_MORE_FILES: int = 18
PROCESS_BASIC_INFORMATION_CLASS: int = 0
PROCESS_COMMAND_LINE_INFORMATION_CLASS: int = 60
STATUS_INFO_LENGTH_MISMATCH: int = 0xC0000004
STATUS_BUFFER_OVERFLOW: int = 0x80000005
STATUS_BUFFER_TOO_SMALL: int = 0xC0000023
_MAX_WINDOWS_PID: int = 0xFFFFFFFF

#: 100 ns ticks between 1601-01-01 (FILETIME epoch) and 1970-01-01.
_FILETIME_UNIX_EPOCH: int = 116_444_736_000_000_000
_FILETIME_TICKS_PER_SECOND: int = 10_000_000


class WinProcessInfoApi(Protocol):
    """The Win32 and ntdll calls the identity reads make.

    One handle per probe: open once, read several attributes, close. Injected
    so the Windows branch runs under test on any host; the real binding is
    :func:`ctypes_win_info_api`.
    """

    def open_process(self, pid: int) -> tuple[int | None, int]:
        """``OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)``:
        ``(handle, 0)`` on success, ``(None, GetLastError())`` on failure."""
        ...

    def close(self, handle: int) -> None:
        """``CloseHandle(handle)``."""
        ...

    def creation_filetime(self, handle: int) -> int | None:
        """``GetProcessTimes`` creation time as one 64-bit FILETIME, or None."""
        ...

    def image_path(self, handle: int) -> str | None:
        """``QueryFullProcessImageNameW``, or None."""
        ...

    def command_line(self, handle: int) -> str | None:
        """``NtQueryInformationProcess`` class 60, or None (unreadable)."""
        ...

    def parent_pid(self, handle: int) -> int | None:
        """``NtQueryInformationProcess`` class 0, InheritedFromUniqueProcessId,
        or None."""
        ...

    def snapshot(self) -> list[tuple[int, int]] | None:
        """``[(pid, parent_pid)]`` from a Toolhelp32 snapshot, or None."""
        ...


# ── Pure helpers ────────────────────────────────────────────────────────────

_EXE_SUFFIX = re.compile(r"\.exe$", re.IGNORECASE)


def executable_stem(token: str) -> str:
    """Last path component of *token*, split on either separator, with a
    trailing ``.exe`` removed (case-insensitive).

    POSIX ``comm`` and ``argv[0]`` basenames carry no extension; a Windows
    image is ``nx-mcp.exe``. The matchers were written against the bare name.
    The split is on both separators so the same call answers for a Windows
    path rendered on any host.
    """
    base = re.split(r"[\\/]", token)[-1]
    return _EXE_SUFFIX.sub("", base)


def split_command_line(cmdline: str) -> list[str]:
    """Split a raw Windows command line into argv by the
    ``CommandLineToArgvW`` / MSVCRT rules.

    argv[0] is special: no backslash escaping, quotes only group (so
    ``"C:\\Program Files\\x.exe"`` and ``C:\\x.exe`` both work). From argv[1]:
    ``2n`` backslashes then a quote is ``n`` backslashes and a toggle,
    ``2n+1`` backslashes then a quote is ``n`` backslashes and a literal
    quote, and backslashes not followed by a quote are literal. An empty or
    all-space line yields ``[]``.
    """
    s = cmdline.lstrip(" \t")
    if not s:
        return []
    argv: list[str] = []
    i, n = 0, len(s)

    # argv[0]
    if s[0] == '"':
        end = s.find('"', 1)
        if end == -1:
            argv.append(s[1:])
            return argv
        argv.append(s[1:end])
        i = end + 1
    else:
        j = i
        while j < n and s[j] not in " \t":
            j += 1
        argv.append(s[i:j])
        i = j

    while True:
        while i < n and s[i] in " \t":
            i += 1
        if i >= n:
            break
        arg: list[str] = []
        in_quotes = False
        while i < n:
            c = s[i]
            if c == "\\":
                k = i
                while k < n and s[k] == "\\":
                    k += 1
                slashes = k - i
                if k < n and s[k] == '"':
                    arg.append("\\" * (slashes // 2))
                    if slashes % 2:
                        arg.append('"')
                        i = k + 1
                    else:
                        # even: the quote is a delimiter, handled next pass
                        i = k
                else:
                    arg.append("\\" * slashes)
                    i = k
            elif c == '"':
                in_quotes = not in_quotes
                i += 1
            elif c in " \t" and not in_quotes:
                break
            else:
                arg.append(c)
                i += 1
        argv.append("".join(arg))
    return argv


def render_command_line(cmdline: str) -> str:
    """*cmdline* as the space-joined argv every POSIX matcher expects."""
    return " ".join(split_command_line(cmdline))


def _unix_from_filetime(filetime: int) -> float:
    return (filetime - _FILETIME_UNIX_EPOCH) / _FILETIME_TICKS_PER_SECOND


# ── Reads over an injected API ──────────────────────────────────────────────


def _open(api: WinProcessInfoApi, pid: int) -> int | None:
    if pid <= 0 or pid > _MAX_WINDOWS_PID:
        return None
    handle, _err = api.open_process(pid)
    return handle


def process_age_seconds(
    pid: int,
    api: WinProcessInfoApi,
    *,
    now: Callable[[], float] = time.time,
) -> float | None:
    """Seconds since *pid* was created, or None when it is gone or unreadable.

    ``ps etime`` equivalent. Clamped at zero (a clock step must not yield a
    negative age).
    """
    handle = _open(api, pid)
    if handle is None:
        return None
    try:
        created = api.creation_filetime(handle)
    finally:
        api.close(handle)
    if created is None:
        return None
    return max(0.0, now() - _unix_from_filetime(created))


def process_image_path(pid: int, api: WinProcessInfoApi) -> str:
    """Full image path of *pid*, or ``""`` when gone or unreadable."""
    handle = _open(api, pid)
    if handle is None:
        return ""
    try:
        return api.image_path(handle) or ""
    finally:
        api.close(handle)


def _command_of(handle: int, api: WinProcessInfoApi) -> str:
    raw = api.command_line(handle)
    if raw:
        rendered = render_command_line(raw)
        if rendered:
            return rendered
    # Unreadable command line: the image path stands in, so the single-pid
    # read and the enumeration agree on the text they return.
    return api.image_path(handle) or ""


def process_command_line(pid: int, api: WinProcessInfoApi) -> str:
    """The space-joined argv of *pid*, falling back to its image path when the
    command line cannot be read; ``""`` when the process is gone."""
    handle = _open(api, pid)
    if handle is None:
        return ""
    try:
        return _command_of(handle, api)
    finally:
        api.close(handle)


def process_command_name(pid: int, api: WinProcessInfoApi) -> str:
    """``comm`` equivalent: the executable's name without directory or
    ``.exe``, or ``""`` when unknown."""
    path = process_image_path(pid, api)
    return executable_stem(path) if path else ""


def parent_pid(pid: int, api: WinProcessInfoApi) -> int | None:
    """Parent pid of *pid*, or None when the process or its parent is gone.

    Same contract as ``session._ppid_of``: a parent of 0 or 1 is None.
    A parent created after its child is a recycled pid number, not a parent,
    and reads as gone. A parent that cannot be opened for lack of access is
    still a parent (it exists, we cannot date it).
    """
    handle = _open(api, pid)
    if handle is None:
        return None
    try:
        ppid = api.parent_pid(handle)
        child_created = api.creation_filetime(handle)
    finally:
        api.close(handle)
    if ppid is None or ppid <= 1:
        return None
    parent_handle, err = api.open_process(ppid)
    if parent_handle is None:
        return ppid if err == ERROR_ACCESS_DENIED else None
    try:
        parent_created = api.creation_filetime(parent_handle)
    finally:
        api.close(parent_handle)
    if (
        parent_created is not None
        and child_created is not None
        and parent_created > child_created
    ):
        return None
    return ppid


def enumerate_processes(
    api: WinProcessInfoApi,
    *,
    only_ppid: int | None = None,
    now: Callable[[], float] = time.time,
) -> list[tuple[int, int, float, str]]:
    """``[(pid, ppid, age_s, command)]`` for every readable process.

    A process that exited between the snapshot and the open, or that cannot
    be opened (System, protected, another user's elevated process), is
    skipped, never guessed at, the way the ``/proc`` walk skips a vanished
    entry. *only_ppid* narrows by the snapshot's parent field BEFORE any
    process is opened: a caller that wants one parent's children pays for
    those children only.

    Raises ``RuntimeError`` when the snapshot itself cannot be taken, so a
    caller never reads "zero processes" off a failed read.
    """
    table = api.snapshot()
    if table is None:
        raise RuntimeError("CreateToolhelp32Snapshot failed: process table unreadable")
    t_now = now()
    out: list[tuple[int, int, float, str]] = []
    for pid, ppid in table:
        if pid <= 0 or (only_ppid is not None and ppid != only_ppid):
            continue
        handle = _open(api, pid)
        if handle is None:
            continue
        try:
            command = _command_of(handle, api)
            created = api.creation_filetime(handle)
        finally:
            api.close(handle)
        if not command:
            continue
        age = (
            0.0 if created is None
            else max(0.0, t_now - _unix_from_filetime(created))
        )
        out.append((pid, ppid, age, command))
    return out


# ── The real binding (Windows only, built lazily) ───────────────────────────

_api_cache: list[WinProcessInfoApi] = []


def ctypes_win_info_api() -> WinProcessInfoApi:
    """The real kernel32/ntdll binding, built once per process. Windows only."""
    if not _api_cache:
        _api_cache.append(_CtypesWinInfoApi())
    return _api_cache[0]


class _CtypesWinInfoApi:
    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415 -- Windows-only binding, built on first use
        from ctypes import wintypes  # noqa: PLC0415

        self._ct = ctypes
        w = wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        nt = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]

        k32.OpenProcess.argtypes = (w.DWORD, w.BOOL, w.DWORD)
        k32.OpenProcess.restype = w.HANDLE
        k32.CloseHandle.argtypes = (w.HANDLE,)
        k32.CloseHandle.restype = w.BOOL
        k32.GetProcessTimes.argtypes = (
            w.HANDLE, ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME),
        )
        k32.GetProcessTimes.restype = w.BOOL
        k32.QueryFullProcessImageNameW.argtypes = (
            w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD),
        )
        k32.QueryFullProcessImageNameW.restype = w.BOOL
        nt.NtQueryInformationProcess.argtypes = (
            w.HANDLE, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        )
        nt.NtQueryInformationProcess.restype = ctypes.c_long
        k32.CreateToolhelp32Snapshot.argtypes = (w.DWORD, w.DWORD)
        k32.CreateToolhelp32Snapshot.restype = w.HANDLE

        class _ProcessEntry32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", w.DWORD),
                ("cntUsage", w.DWORD),
                ("th32ProcessID", w.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", w.DWORD),
                ("cntThreads", w.DWORD),
                ("th32ParentProcessID", w.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", w.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        class _ProcessBasicInformation(ctypes.Structure):
            # winternl.h: Reserved1, PebBaseAddress, Reserved2[2],
            # UniqueProcessId, Reserved3 (= InheritedFromUniqueProcessId).
            _fields_ = [
                ("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_size_t),
                ("InheritedFromUniqueProcessId", ctypes.c_size_t),
            ]

        class _UnicodeString(ctypes.Structure):
            _fields_ = [
                ("Length", ctypes.c_ushort),
                ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p),
            ]

        k32.Process32FirstW.argtypes = (w.HANDLE, ctypes.POINTER(_ProcessEntry32W))
        k32.Process32FirstW.restype = w.BOOL
        k32.Process32NextW.argtypes = (w.HANDLE, ctypes.POINTER(_ProcessEntry32W))
        k32.Process32NextW.restype = w.BOOL

        self._k32, self._nt, self._w = k32, nt, w
        self._Entry, self._Pbi, self._Ustr = (
            _ProcessEntry32W, _ProcessBasicInformation, _UnicodeString,
        )

    def open_process(self, pid: int) -> tuple[int | None, int]:
        handle = self._k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None, self._ct.get_last_error()  # type: ignore[attr-defined]
        return handle, 0

    def close(self, handle: int) -> None:
        self._k32.CloseHandle(handle)

    def creation_filetime(self, handle: int) -> int | None:
        w = self._w
        created, exited, kernel, user = (w.FILETIME(), w.FILETIME(), w.FILETIME(), w.FILETIME())
        ct = self._ct
        if not self._k32.GetProcessTimes(
            handle, ct.byref(created), ct.byref(exited), ct.byref(kernel), ct.byref(user),
        ):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime

    def image_path(self, handle: int) -> str | None:
        ct, w = self._ct, self._w
        for size in (1024, 32768):
            buf = ct.create_unicode_buffer(size)
            length = w.DWORD(size)
            if self._k32.QueryFullProcessImageNameW(handle, 0, buf, ct.byref(length)):
                return buf.value
            if ct.get_last_error() != ERROR_INSUFFICIENT_BUFFER:  # type: ignore[attr-defined]
                return None
        return None

    def command_line(self, handle: int) -> str | None:
        ct = self._ct
        needed = ct.c_ulong(0)
        status = self._nt.NtQueryInformationProcess(
            handle, PROCESS_COMMAND_LINE_INFORMATION_CLASS, None, 0, ct.byref(needed),
        ) & 0xFFFFFFFF
        if status not in (
            STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_OVERFLOW, STATUS_BUFFER_TOO_SMALL,
        ) or needed.value < ct.sizeof(self._Ustr):
            return None
        # The kernel can grow the line between the sizing call and the read;
        # retry with the larger size once or twice rather than report None.
        for _ in range(3):
            buf = ct.create_string_buffer(needed.value)
            status = self._nt.NtQueryInformationProcess(
                handle, PROCESS_COMMAND_LINE_INFORMATION_CLASS, buf, needed.value,
                ct.byref(needed),
            ) & 0xFFFFFFFF
            if status == 0:
                ustr = self._Ustr.from_buffer(buf)
                if not ustr.Buffer or not ustr.Length:
                    return ""
                # Buffer points INTO buf, after the UNICODE_STRING header.
                return ct.wstring_at(ustr.Buffer, ustr.Length // 2)
            if status not in (
                STATUS_INFO_LENGTH_MISMATCH, STATUS_BUFFER_OVERFLOW, STATUS_BUFFER_TOO_SMALL,
            ):
                return None
        return None

    def parent_pid(self, handle: int) -> int | None:
        ct = self._ct
        info = self._Pbi()
        got = ct.c_ulong(0)
        status = self._nt.NtQueryInformationProcess(
            handle, PROCESS_BASIC_INFORMATION_CLASS, ct.byref(info),
            ct.sizeof(info), ct.byref(got),
        ) & 0xFFFFFFFF
        if status != 0:
            return None
        return int(info.InheritedFromUniqueProcessId)

    def snapshot(self) -> list[tuple[int, int]] | None:
        ct = self._ct
        invalid = ct.c_void_p(-1).value
        snap = self._k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == invalid:
            return None
        try:
            entry = self._Entry()
            entry.dwSize = ct.sizeof(entry)
            rows: list[tuple[int, int]] = []
            ok = self._k32.Process32FirstW(snap, ct.byref(entry))
            while ok:
                rows.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                ok = self._k32.Process32NextW(snap, ct.byref(entry))
            if ct.get_last_error() not in (0, ERROR_NO_MORE_FILES) and not rows:  # type: ignore[attr-defined]
                return None
            return rows
        finally:
            self._k32.CloseHandle(snap)
