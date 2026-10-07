# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit tests for nexus.util.win_job (nexus-6y4e0).

No real Windows box is assumed. ``TestNonWindows`` exercises the actual
platform this suite runs on (macOS/Linux: ``IS_WINDOWS`` is really
``False`` here, no monkeypatching needed) and pins the no-op-degradation
contract. ``TestWindowsShaped*`` monkeypatches ``win_job.IS_WINDOWS`` and
``win_job._kernel32`` to a Python test double implementing the same five
methods, so the WINDOWS branch of every function runs and is asserted on
shape: the right constants, the right argument order, the right handles
closed. That proves this module CALLS the Win32 surface the design calls
for; it does not prove the real kernel32 behaves as documented — that is
what the qwentescence live run in nexus-6y4e0's closing report is for.
"""
from __future__ import annotations

import contextlib
import ctypes
import os
import signal

import pytest

from nexus.util import win_job


class _FakeKernel32:
    """Stand-in for the five kernel32 exports win_job.py binds.

    Every call is recorded verbatim so tests can assert on the exact
    arguments this module passes, and every failure mode is an
    independent toggle so each can be exercised without disturbing the
    others.
    """

    def __init__(self) -> None:
        self._next_handle = 1000
        self.calls: list[tuple] = []
        self.closed_handles: list[int] = []
        self.set_info_limit_flags: int | None = None

        self.create_ok = True
        self.set_info_ok = True
        self.open_process_ok = True
        self.assign_ok = True
        self.close_ok = True
        self.ctrl_break_ok = True

        #: Names of methods that should RAISE instead of returning a
        #: failure value -- the exception-guard regression (nexus-6y4e0
        #: review): win_job's module docstring promises every public
        #: function degrades to None/False rather than raising, but
        #: nothing enforced that until create_job/assign_process/
        #: close_job each wrapped their raw kernel32 calls + struct
        #: marshalling in ``except (OSError, ctypes.ArgumentError)``.
        self.raise_from: set[str] = set()

    def _mint(self) -> int:
        self._next_handle += 1
        return self._next_handle

    def _maybe_raise(self, name: str) -> None:
        if name in self.raise_from:
            raise OSError(f"fake kernel32: {name} raised")

    def CreateJobObjectW(self, sec_attrs, name):  # noqa: N802 - mirrors the real Win32 name
        self.calls.append(("CreateJobObjectW", sec_attrs, name))
        self._maybe_raise("CreateJobObjectW")
        if not self.create_ok:
            return 0
        return self._mint()

    def SetInformationJobObject(self, handle, info_class, ptr, size):  # noqa: N802
        self.calls.append(("SetInformationJobObject", handle, info_class, size))
        self._maybe_raise("SetInformationJobObject")
        if not self.set_info_ok:
            return 0
        info = ptr.contents
        self.set_info_limit_flags = info.BasicLimitInformation.LimitFlags
        return 1

    def OpenProcess(self, access, inherit, pid):  # noqa: N802
        self.calls.append(("OpenProcess", access, inherit, pid))
        self._maybe_raise("OpenProcess")
        if not self.open_process_ok:
            return 0
        return self._mint()

    def AssignProcessToJobObject(self, job, hproc):  # noqa: N802
        self.calls.append(("AssignProcessToJobObject", job, hproc))
        self._maybe_raise("AssignProcessToJobObject")
        return 1 if self.assign_ok else 0

    def CloseHandle(self, handle):  # noqa: N802
        self.calls.append(("CloseHandle", handle))
        self._maybe_raise("CloseHandle")
        self.closed_handles.append(handle)
        return 1 if self.close_ok else 0

    def GenerateConsoleCtrlEvent(self, event, pid):  # noqa: N802
        self.calls.append(("GenerateConsoleCtrlEvent", event, pid))
        self._maybe_raise("GenerateConsoleCtrlEvent")
        return 1 if self.ctrl_break_ok else 0

    def opened_pids(self) -> list[int]:
        """Pids of the REAL children ``win_job`` asked to contain.

        The fake closes job handles without terminating anything, so a
        test that spawns a real child through this fake owns that child's
        death: see :func:`reap_live_children` and :func:`live_children`.
        """
        return [c[3] for c in self.calls if c[0] == "OpenProcess"]


def live_children(pids: list[int]) -> list[int]:
    """The pids in ``pids`` that are still this process's un-reaped children.

    ``waitpid(WNOHANG)`` rather than ``kill(pid, 0)``: a zombie answers
    signal 0 as alive, and a pid that was already reaped (and possibly
    reused by an unrelated process) raises ``ChildProcessError`` here
    instead of being mistaken for ours. A child that has exited but was not
    yet reaped is reaped by this call and does not count as live. POSIX
    only; these Windows-shaped tests run on macOS/Linux.
    """
    live: list[int] = []
    for pid in pids:
        try:
            done, _status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if done == 0:
            live.append(pid)
    return live


def reap_live_children(pids: list[int]) -> None:
    """SIGKILL and reap every pid in ``pids`` that is still our live child.

    Only pids :func:`live_children` confirms as this process's own
    children are signalled, so a reused pid is never touched.
    """
    for pid in live_children(pids):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(pid, 0)


@pytest.fixture
def windows_shaped(monkeypatch: pytest.MonkeyPatch) -> _FakeKernel32:
    """Force every win_job function down its Windows branch, against a fake."""
    fake = _FakeKernel32()
    monkeypatch.setattr(win_job, "IS_WINDOWS", True)
    monkeypatch.setattr(win_job, "_kernel32", fake)
    return fake


class TestNonWindows:
    """The platform this suite actually runs on (macOS/Linux CI)."""

    def test_is_windows_is_false_here(self) -> None:
        assert win_job.IS_WINDOWS is False, (
            "this test asserts the no-op degradation path on a NON-Windows "
            "box; if this ever runs ON Windows, TestWindowsShaped* is redundant "
            "with it rather than this one being wrong"
        )

    def test_create_job_returns_none_without_touching_ctypes_windll(self) -> None:
        # The regression this guards: nexus-34f7r found that an unguarded
        # ctypes.WinDLL(...) reference raises AttributeError off Windows at
        # IMPORT time. win_job's own module-level _kernel32 binding already
        # proved that at import (this test file imported cleanly), but
        # every PUBLIC function must also refuse to touch it again.
        assert win_job.create_job() is None

    def test_assign_process_returns_false(self) -> None:
        assert win_job.assign_process(123, 456) is False

    def test_assign_process_returns_false_for_falsy_job(self) -> None:
        assert win_job.assign_process(None, 456) is False
        assert win_job.assign_process(0, 456) is False

    def test_close_job_returns_false(self) -> None:
        assert win_job.close_job(123) is False

    def test_close_job_returns_false_for_falsy_job(self) -> None:
        assert win_job.close_job(None) is False
        assert win_job.close_job(0) is False

    def test_send_ctrl_break_returns_false(self) -> None:
        assert win_job.send_ctrl_break(456) is False


class TestWin32ConstantsArePinnedAsLiterals:
    """The tests below compare a value to the module's own constant, which cannot fail when the
    constant is wrong (JB1, JB2). These pin the values the Windows headers define, spelled out,
    so a wrong constant fails here on any host."""

    def test_the_console_event_and_creation_flags(self) -> None:
        assert win_job.CTRL_BREAK_EVENT == 1  # wincon.h: CTRL_BREAK_EVENT
        assert win_job.CREATE_NEW_PROCESS_GROUP == 0x00000200
        assert win_job.CREATE_NO_WINDOW == 0x08000000
        assert win_job.CREATE_BREAKAWAY_FROM_JOB == 0x01000000

    def test_the_job_object_and_process_access_values(self) -> None:
        assert win_job._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x00002000
        assert win_job._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION == 9
        assert win_job._PROCESS_SET_QUOTA == 0x0100
        assert win_job._PROCESS_TERMINATE == 0x0001

    def test_the_console_module_agrees_with_the_headers_too(self) -> None:
        from nexus.util import win_console

        assert win_console.CTRL_BREAK_EVENT == 1
        assert win_console.DETACHED_PROCESS == 0x00000008
        assert win_console.ATTACH_PARENT_PROCESS == 0xFFFFFFFF
        assert win_console.ERROR_ACCESS_DENIED == 5


class TestWindowsShapedHappyPath:
    def test_create_job_sets_kill_on_close_limit_flag(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        assert job is not None
        assert isinstance(job, int)
        assert (
            windows_shaped.set_info_limit_flags
            == win_job._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        info_class_call = next(
            c for c in windows_shaped.calls if c[0] == "SetInformationJobObject"
        )
        assert info_class_call[2] == win_job._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION

    def test_assign_process_opens_with_set_quota_and_terminate(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        assert win_job.assign_process(job, 4242) is True

        open_call = next(c for c in windows_shaped.calls if c[0] == "OpenProcess")
        assert open_call[1] == win_job._PROCESS_SET_QUOTA | win_job._PROCESS_TERMINATE
        assert open_call[3] == 4242

        assign_call = next(
            c for c in windows_shaped.calls if c[0] == "AssignProcessToJobObject"
        )
        assert assign_call[1] == job
        # The PROCESS handle (not the job handle) must be closed right
        # after assignment -- it is not needed once the job holds the
        # membership, and leaking it would leak a kernel handle per spawn.
        assert assign_call[2] in windows_shaped.closed_handles
        assert job not in windows_shaped.closed_handles

    def test_close_job_closes_the_handle(self, windows_shaped: _FakeKernel32) -> None:
        job = win_job.create_job()
        assert win_job.close_job(job) is True
        assert job in windows_shaped.closed_handles

    def test_send_ctrl_break_uses_ctrl_break_event_constant(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        assert win_job.send_ctrl_break(777) is True
        call = next(
            c for c in windows_shaped.calls if c[0] == "GenerateConsoleCtrlEvent"
        )
        assert call[1] == win_job.CTRL_BREAK_EVENT
        assert call[2] == 777


class TestWindowsShapedFailureDegradesHonestly:
    """Every failure path returns None/False -- never raises."""

    def test_create_job_returns_none_when_create_job_object_fails(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        windows_shaped.create_ok = False
        assert win_job.create_job() is None

    def test_create_job_closes_the_handle_when_set_information_fails(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        windows_shaped.set_info_ok = False
        assert win_job.create_job() is None
        # The handle CreateJobObjectW minted must not leak just because the
        # limit could not be applied.
        assert len(windows_shaped.closed_handles) == 1

    def test_assign_process_returns_false_when_open_process_fails(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        windows_shaped.open_process_ok = False
        assert win_job.assign_process(job, 4242) is False

    def test_assign_process_returns_false_when_assign_fails(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        windows_shaped.assign_ok = False
        assert win_job.assign_process(job, 4242) is False
        # Even on a failed assignment, the opened process handle must not leak.
        assert len(windows_shaped.closed_handles) == 1

    def test_close_job_returns_false_when_close_handle_fails(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        windows_shaped.close_ok = False
        assert win_job.close_job(job) is False

    def test_send_ctrl_break_returns_false_when_event_fails(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        windows_shaped.ctrl_break_ok = False
        assert win_job.send_ctrl_break(777) is False


class TestWindowsShapedExceptionGuard:
    """nexus-6y4e0 review: the module docstring promises every public
    function here degrades to ``None``/``False`` rather than raising, but
    nothing enforced that -- a ctypes marshalling failure
    (``ctypes.ArgumentError``, e.g. a bad struct/pointer) or the kernel32
    call itself raising (``OSError``, ctypes' own errno-mapped failure
    path) both propagated uncaught. ``create_job``, ``assign_process`` and
    ``close_job`` now wrap their raw kernel32 calls and struct marshalling
    in ``except (OSError, ctypes.ArgumentError)``.
    """

    def test_create_job_degrades_when_create_job_object_raises(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        windows_shaped.raise_from.add("CreateJobObjectW")
        assert win_job.create_job() is None

    def test_create_job_degrades_and_closes_the_handle_when_set_information_raises(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        windows_shaped.raise_from.add("SetInformationJobObject")
        assert win_job.create_job() is None
        # The handle CreateJobObjectW minted before the raise must not leak.
        assert len(windows_shaped.closed_handles) == 1

    def test_assign_process_degrades_when_open_process_raises(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        windows_shaped.raise_from.add("OpenProcess")
        assert win_job.assign_process(job, 4242) is False

    def test_assign_process_degrades_and_closes_the_process_handle_when_assign_raises(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        windows_shaped.raise_from.add("AssignProcessToJobObject")
        assert win_job.assign_process(job, 4242) is False
        # The opened PROCESS handle (not the job handle) must still close.
        assert len(windows_shaped.closed_handles) == 1
        assert job not in windows_shaped.closed_handles

    def test_close_job_degrades_when_close_handle_raises(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        job = win_job.create_job()
        windows_shaped.raise_from.add("CloseHandle")
        assert win_job.close_job(job) is False

    def test_send_ctrl_break_degrades_when_the_api_raises(
        self, windows_shaped: _FakeKernel32,
    ) -> None:
        windows_shaped.raise_from.add("GenerateConsoleCtrlEvent")
        assert win_job.send_ctrl_break(1234) is False


def test_structures_have_consistent_sizes() -> None:
    """The ctypes structs must actually assemble -- a field-order or type
    mistake here would raise at import (caught by this file importing at
    all) or silently mis-marshal on a real Windows box (not caught by
    anything short of qwentescence). This pins the one thing checkable
    without either: the extended struct is not smaller than its embedded
    basic-limit struct plus its IO-counters struct.
    """
    basic = ctypes.sizeof(win_job._JobObjectBasicLimitInformation)
    io = ctypes.sizeof(win_job._IoCounters)
    extended = ctypes.sizeof(win_job._JobObjectExtendedLimitInformation)
    assert extended >= basic + io
