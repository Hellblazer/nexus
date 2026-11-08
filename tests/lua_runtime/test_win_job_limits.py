# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The worker's Windows limits in ``nexus.util.win_job`` (RDR-242 Technical Design 6, "OS limits").

Shape tests with a stand-in kernel32, as ``tests/test_win_job.py`` does: they
prove the right flags and struct fields are passed. That the real kernel32
ends a process at the limit is tested by the ``win32`` tests of
``test_worker_process.py`` on native Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any

import pytest

from nexus.util import win_job
from tests.lua_runtime._support import platform_only


class _Kernel32:
    def __init__(self, terminated: int = 0) -> None:
        self.flags: int | None = None
        self.time_limit: int | None = None
        self.memory_limit: int | None = None
        self._terminated = terminated

    def CreateJobObjectW(self, sec: Any, name: Any) -> int:  # noqa: N802
        return 4242

    def SetInformationJobObject(
        self, handle: int, klass: int, ptr: Any, size: int
    ) -> int:  # noqa: N802
        info = ptr.contents
        self.flags = info.BasicLimitInformation.LimitFlags
        self.time_limit = info.BasicLimitInformation.PerProcessUserTimeLimit
        self.memory_limit = info.ProcessMemoryLimit
        return 1

    def QueryInformationJobObject(
        self, handle: int, klass: int, ptr: Any, size: int, ret: Any
    ) -> int:  # noqa: N802
        assert klass == 1  # JobObjectBasicAccountingInformation
        ptr.contents.TotalTerminatedProcesses = self._terminated
        return 1

    def CloseHandle(self, handle: int) -> int:  # noqa: N802
        return 1


@pytest.fixture
def kernel(monkeypatch: pytest.MonkeyPatch) -> _Kernel32:
    k = _Kernel32()
    monkeypatch.setattr(win_job, "IS_WINDOWS", True)
    monkeypatch.setattr(win_job, "_kernel32", k)
    return k


def test_a_plain_job_still_carries_only_kill_on_close(kernel: _Kernel32) -> None:
    assert win_job.create_job() == 4242
    assert (
        kernel.flags == 0x2000 and kernel.time_limit == 0 and kernel.memory_limit == 0
    )


def test_the_worker_job_adds_a_per_process_time_limit_and_a_per_process_memory_limit(
    kernel: _Kernel32,
) -> None:
    assert (
        win_job.create_job(
            per_process_user_time_100ns=65 * 10_000_000, process_memory_limit=1 << 30
        )
        == 4242
    )
    assert (
        kernel.flags == 0x2000 | 0x2 | 0x100
    )  # kill-on-close, PROCESS_TIME, PROCESS_MEMORY
    assert kernel.time_limit == 650_000_000 and kernel.memory_limit == 1 << 30


def test_terminated_by_limit_reads_the_accounting_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(win_job, "IS_WINDOWS", True)
    monkeypatch.setattr(win_job, "_kernel32", _Kernel32(terminated=1))
    assert win_job.terminated_by_limit(4242) is True
    monkeypatch.setattr(win_job, "_kernel32", _Kernel32(terminated=0))
    assert win_job.terminated_by_limit(4242) is False


def test_terminated_by_limit_is_false_for_a_missing_job_and_never_raises() -> None:
    assert win_job.terminated_by_limit(None) is False
    assert win_job.terminated_by_limit(0) is False


@platform_only("win32")
def test_windows_the_real_job_ends_a_spinning_process_at_its_user_time_limit_and_counts_it() -> (
    None
):
    """Assumption of research 33, on native Windows: a per-process user-time limit of the job ends a spinner and the job says so."""
    job = win_job.create_job(
        per_process_user_time_100ns=2 * 10_000_000, process_memory_limit=1 << 30
    )
    assert job, "CreateJobObject failed"
    proc = subprocess.Popen([sys.executable, "-I", "-c", "while True: pass"])
    try:
        assert win_job.assign_process(job, proc.pid)
        t0 = time.monotonic()
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pytest.fail(
                "the job did not end a process 15x past its 2 s user-time limit"
            )
        took = time.monotonic() - t0
        sys.stdout.write(
            f"MEASURE platform=win32 metric=job_time_limit_kill took_s={took:.2f} exit_code={rc}\n"
        )
        assert 1.0 <= took <= 12.0, f"ended after {took:.1f}s for a 2 s limit"
        assert win_job.terminated_by_limit(job) is True
    finally:
        proc.kill()
        proc.wait()
        win_job.close_job(job)


@platform_only("win32")
def test_windows_a_child_with_an_empty_environment_cannot_open_a_socket_and_systemroot_fixes_it() -> (
    None
):
    """Assumption of research 33, on native Windows: the worker's environment is {SYSTEMROOT}, not {}.

    Measured on qwent (Windows 11, uv CPython 3.13, ``-I``): an empty environment still starts Python and imports
    ``ssl``, ``socket`` and ``lupa.lua54``, but creating a socket fails (``WinError 10106``, Winsock cannot
    initialise), and ``SYSTEMROOT`` alone fixes it. So the claim holds for what a verb-free worker needs only in the
    weak sense; it is the Winsock case, which the worker never uses, that needs the variable. The worker keeps
    SYSTEMROOT (the RDR's rule) so a later import that opens a socket does not break it.
    """
    plain = [
        sys.executable,
        "-I",
        "-c",
        "import os, ssl, socket, sys, lupa.lua54; os.urandom(8); sys.stdout.write('up')",
    ]
    sock = [
        sys.executable,
        "-I",
        "-c",
        "import socket, sys; socket.socket().close(); sys.stdout.write('up')",
    ]
    root = {"SYSTEMROOT": os.environ["SYSTEMROOT"]}

    def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, env=env, capture_output=True, text=True, timeout=60, check=False
        )

    bare_plain, bare_sock, root_sock = run(plain, {}), run(sock, {}), run(sock, root)
    sys.stdout.write(
        f"MEASURE platform=win32 metric=systemroot bare_import_rc={bare_plain.returncode} "
        f"bare_socket_rc={bare_sock.returncode} bare_socket_err={bare_sock.stderr.strip()[-70:]!r} "
        f"systemroot_socket_rc={root_sock.returncode}\n"
    )
    assert bare_plain.returncode == 0 and bare_plain.stdout == "up", bare_plain.stderr
    assert bare_sock.returncode != 0 and "10106" in bare_sock.stderr, (
        "an empty environment opened a socket"
    )
    assert root_sock.returncode == 0 and root_sock.stdout == "up", root_sock.stderr
