# SPDX-License-Identifier: AGPL-3.0-or-later
"""``pid_alive``'s Windows branch (RDR-224, nexus-f9bgu.25).

On Windows ``os.kill(pid, 0)`` does not probe liveness: CPython maps
signal 0 to ``CTRL_C_EVENT`` and sends it to the target's console process
group. So the Windows branch must never reach ``os.kill``; it asks the
kernel through ``OpenProcess`` + ``WaitForSingleObject(handle, 0)``.

The platform and the Windows API are both injected, so every branch below
runs on macOS and Linux too. Only :class:`TestRealWindowsKernel` needs a
real Windows host, and it asserts it exercised a real handle.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from nexus.daemon.service_registry import (
    ERROR_ACCESS_DENIED,
    ERROR_INVALID_PARAMETER,
    WAIT_FAILED,
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    _ctypes_win_process_api,
    pid_alive,
)
from tests._module_seam import patch_in


class _FakeWinApi:
    """Scripted ``OpenProcess`` / ``WaitForSingleObject`` / ``CloseHandle``."""

    def __init__(
        self,
        *,
        handle: int | None = 42,
        last_error: int = 0,
        wait_result: int = WAIT_TIMEOUT,
    ) -> None:
        self._handle = handle
        self._last_error = last_error
        self._wait_result = wait_result
        self.opened: list[int] = []
        self.waited: list[int] = []
        self.closed: list[int] = []

    def open_process(self, pid: int) -> tuple[int | None, int]:
        self.opened.append(pid)
        return self._handle, self._last_error

    def wait_zero(self, handle: int) -> int:
        self.waited.append(handle)
        return self._wait_result

    def close(self, handle: int) -> None:
        self.closed.append(handle)


def _no_os_kill(*_a, **_k):
    raise AssertionError(
        "Windows branch reached os.kill: signal 0 is CTRL_C_EVENT there"
    )


class TestWindowsBranch:
    def test_running_process_is_alive_and_handle_closed(self) -> None:
        api = _FakeWinApi(wait_result=WAIT_TIMEOUT)
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=_no_os_kill):
            assert pid_alive(1234, platform="win32", win_api=api) is True
        assert api.opened == [1234]
        assert api.closed == [42]

    def test_exited_process_is_dead_and_handle_closed(self) -> None:
        # A process object outlives its exit while any handle is open; the
        # signalled wait is what says it exited. No zombie state to model.
        api = _FakeWinApi(wait_result=WAIT_OBJECT_0)
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=_no_os_kill):
            assert pid_alive(1234, platform="win32", win_api=api) is False
        assert api.closed == [42]

    def test_wait_failed_is_alive(self) -> None:
        # Ambiguity reads as alive, the same discipline as the POSIX arm.
        api = _FakeWinApi(wait_result=WAIT_FAILED)
        assert pid_alive(1234, platform="win32", win_api=api) is True
        assert api.closed == [42]

    def test_no_such_pid_is_dead(self) -> None:
        api = _FakeWinApi(handle=None, last_error=ERROR_INVALID_PARAMETER)
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=_no_os_kill):
            assert pid_alive(1234, platform="win32", win_api=api) is False
        assert api.waited == [] and api.closed == []

    def test_access_denied_is_alive(self) -> None:
        # Another user's (or a protected) process exists; the POSIX arm
        # reads EPERM the same way.
        api = _FakeWinApi(handle=None, last_error=ERROR_ACCESS_DENIED)
        assert pid_alive(1234, platform="win32", win_api=api) is True

    def test_other_open_error_is_alive(self) -> None:
        api = _FakeWinApi(handle=None, last_error=1450)  # ERROR_NO_SYSTEM_RESOURCES
        assert pid_alive(1234, platform="win32", win_api=api) is True

    @pytest.mark.parametrize("pid", [0, -1])
    def test_non_positive_pid_is_dead_without_asking(self, pid: int) -> None:
        api = _FakeWinApi()
        assert pid_alive(pid, platform="win32", win_api=api) is False
        assert api.opened == []

    def test_pid_beyond_dword_is_dead_without_asking(self) -> None:
        api = _FakeWinApi()
        assert pid_alive(2**32, platform="win32", win_api=api) is False
        assert api.opened == []


class TestPosixBranchUnchanged:
    """The injected platform selects the POSIX arm on any host."""

    def test_posix_uses_signal_zero(self) -> None:
        api = _FakeWinApi()
        with patch_in("nexus.daemon.service_registry", "os.kill") as kill:
            assert pid_alive(1234, platform="linux", win_api=api) is True
        kill.assert_called_once_with(1234, 0)
        assert api.opened == []

    def test_posix_esrch_is_dead(self) -> None:
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=ProcessLookupError()):
            assert pid_alive(1234, platform="darwin") is False

    def test_posix_eperm_is_alive(self) -> None:
        with patch_in("nexus.daemon.service_registry", "os.kill", side_effect=PermissionError()):
            assert pid_alive(1234, platform="darwin") is True


@pytest.mark.skipif(sys.platform != "win32", reason="needs the real Windows kernel")
class TestRealWindowsKernel:
    def test_own_pid_alive_and_exited_child_dead(self) -> None:
        api = _ctypes_win_process_api()
        handle, _err = api.open_process(os.getpid())
        # Non-vacuity: the real kernel handed back a real handle.
        assert handle, "OpenProcess on our own pid returned no handle"
        api.close(handle)

        assert pid_alive(os.getpid()) is True
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=30)
        # The Popen object still holds a handle, so the process object
        # exists; the signalled wait is what must read it as dead.
        assert pid_alive(child.pid) is False
