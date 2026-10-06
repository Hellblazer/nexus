# SPDX-License-Identifier: AGPL-3.0-or-later
"""Move the MCP stdio protocol off fd 0 on Windows (nexus-jg99b).

The MCP stdio transport keeps a worker thread blocked in a synchronous
``readline`` on the stdin pipe for the life of the server. Windows serialises
I/O on a synchronous-mode handle, so while that read is pending,
``PeekNamedPipe``, ``GetFileInformationByHandle``, ``GetFileSizeEx`` and the C
runtimes' ``_fstat64(0)`` on the same handle wait for it. Loading the
gfortran-built OpenBLAS DLL that numpy and scipy ship makes one of those calls
under the loader lock, so the first ``import numpy`` in a tool call hung
forever (and blocked every new thread in the process with it). Measured on the
RDR-224 guest; T2 nexus_rdr/224-mcp-search-hang-diagnosis.

The cure removes the shared handle rather than any one importer: the pipe is
duplicated to a private descriptor, ``sys.stdin`` is rebound to it (the
transport reads ``sys.stdin.buffer`` when it starts, a contract pinned by
tests/test_mcp_win_stdin.py), and fd 0 plus
``STD_INPUT_HANDLE`` are pointed at ``NUL``. A DLL that inspects stdin then sees
NUL, and a child process a tool spawns inherits NUL instead of the protocol
pipe. POSIX is untouched.

The steps run in an order that keeps a working ``sys.stdin`` whatever fails:
the rebind to the private descriptor happens before fd 0 is replaced.

It never logs. It runs before ``configure_logging``, when structlog's default
writes to stdout, and stdout is the MCP protocol pipe: a log line there reaches
the client as a malformed JSON-RPC message (measured on the guest). The caller
logs the returned result once logging is configured.
"""

from __future__ import annotations

import io
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass

STD_INPUT_HANDLE: int = -10


@dataclass(frozen=True)
class IsolationResult:
    """What happened, for the caller to log after logging is configured."""

    isolated: bool
    step: str = ""
    error: str = ""


@dataclass(frozen=True)
class StdinOps:
    """The OS touchpoints, injectable so every host can test the sequence."""

    dup: Callable[[int], int]
    rebind: Callable[[int], None]
    open_nul: Callable[[], int]
    dup2: Callable[[int, int], object]
    close: Callable[[int], None]
    set_std_input: Callable[[int], bool]


def _rebind_stdin(fd: int) -> None:
    sys.stdin = io.TextIOWrapper(
        io.BufferedReader(io.FileIO(fd, "rb", closefd=True)),
        encoding="utf-8",
        errors="replace",
    )


def _set_std_input_from_fd(fd: int) -> bool:
    import ctypes  # noqa: PLC0415 — Windows-only path
    import msvcrt  # noqa: PLC0415 — Windows-only module
    from ctypes import wintypes  # noqa: PLC0415 — Windows-only path

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.SetStdHandle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
    kernel32.SetStdHandle.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    return bool(kernel32.SetStdHandle(wintypes.DWORD(STD_INPUT_HANDLE & 0xFFFFFFFF), handle))


def windows_ops() -> StdinOps:
    """The real operations. Only meaningful on Windows."""
    return StdinOps(
        dup=os.dup,
        rebind=_rebind_stdin,
        open_nul=lambda: os.open(os.devnull, os.O_RDONLY),
        dup2=lambda src, dst: os.dup2(src, dst),
        close=os.close,
        set_std_input=_set_std_input_from_fd,
    )


def isolate_stdin(*, platform: str | None = None, ops: StdinOps | None = None) -> IsolationResult:
    """On Windows, move the stdio protocol to a private fd and give fd 0 NUL.

    ``isolated`` is True only when fd 0 and STD_INPUT_HANDLE both refer to
    NUL. Never raises and never logs: a failure is reported in the result and
    leaves a working ``sys.stdin`` (the protocol is never lost). A failed
    SetStdHandle counts as a failure, because subprocess takes a child's
    default stdin from that handle.
    """
    if (platform if platform is not None else sys.platform) != "win32":
        return IsolationResult(False, step="not-windows")
    ops = ops if ops is not None else windows_ops()
    try:
        private = ops.dup(0)
    except OSError as exc:
        return IsolationResult(False, step="dup", error=str(exc))
    try:
        ops.rebind(private)
    except Exception as exc:  # noqa: BLE001 — the server must still start on the original stdin
        try:
            ops.close(private)
        except OSError:
            pass
        return IsolationResult(False, step="rebind", error=str(exc))
    try:
        nul = ops.open_nul()
        try:
            ops.dup2(nul, 0)
        finally:
            ops.close(nul)
        std_ok = ops.set_std_input(0)
    except Exception as exc:  # noqa: BLE001 — sys.stdin already reads the private fd, so the protocol is intact
        return IsolationResult(False, step="nul", error=str(exc))
    if not std_ok:
        return IsolationResult(False, step="std-handle", error="SetStdHandle(STD_INPUT_HANDLE) failed")
    return IsolationResult(True, step="done")
