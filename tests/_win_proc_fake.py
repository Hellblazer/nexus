# SPDX-License-Identifier: AGPL-3.0-or-later
"""A scripted Windows process table for the identity tests (nexus-f9bgu.21).

Implements :class:`nexus._install.winproc_core.WinProcessInfoApi` over a dict,
so every Windows branch runs on macOS and Linux. It records each handle it
opens and closes, so a test can assert no handle leaks, and it can be told a
process is unreadable (access denied) or has no readable command line.
"""
from __future__ import annotations

from dataclasses import dataclass

ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
_FILETIME_UNIX_EPOCH = 116_444_736_000_000_000


def filetime(unix_seconds: float) -> int:
    """The FILETIME value for a Unix timestamp."""
    return int(unix_seconds * 10_000_000) + _FILETIME_UNIX_EPOCH


@dataclass
class FakeProc:
    ppid: int
    created: float  # unix seconds
    image: str
    cmdline: str | None  # raw Windows command line; None = unreadable
    denied: bool = False  # OpenProcess fails with access denied


class FakeWinInfoApi:
    def __init__(self, procs: dict[int, FakeProc], *, snapshot_ok: bool = True) -> None:
        self.procs = procs
        self.snapshot_ok = snapshot_ok
        self.opened: list[int] = []
        self.closed: list[int] = []
        self._handles: dict[int, int] = {}

    # --- WinProcessInfoApi -------------------------------------------------
    def open_process(self, pid: int) -> tuple[int | None, int]:
        self.opened.append(pid)
        proc = self.procs.get(pid)
        if proc is None:
            return None, ERROR_INVALID_PARAMETER
        if proc.denied:
            return None, ERROR_ACCESS_DENIED
        handle = 1000 + len(self.opened)
        self._handles[handle] = pid
        return handle, 0

    def close(self, handle: int) -> None:
        self.closed.append(handle)

    def _proc(self, handle: int) -> FakeProc:
        return self.procs[self._handles[handle]]

    def creation_filetime(self, handle: int) -> int | None:
        return filetime(self._proc(handle).created)

    def image_path(self, handle: int) -> str | None:
        return self._proc(handle).image

    def command_line(self, handle: int) -> str | None:
        return self._proc(handle).cmdline

    def parent_pid(self, handle: int) -> int | None:
        return self._proc(handle).ppid

    def snapshot(self) -> list[tuple[int, int]] | None:
        if not self.snapshot_ok:
            return None
        return [(pid, p.ppid) for pid, p in self.procs.items()]

    # --- assertions --------------------------------------------------------
    def assert_no_leaked_handles(self) -> None:
        opened_ok = [h for h in self._handles]
        assert sorted(self.closed) == sorted(opened_ok), (
            f"opened {opened_ok}, closed {self.closed}"
        )
