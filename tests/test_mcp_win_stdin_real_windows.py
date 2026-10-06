# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jg99b against the real kernel: a pending read on the stdin pipe blocks
calls that inspect the same handle, and isolate_stdin() ends that.

A child process gets a pipe for stdin and a thread blocked reading it, the way
the MCP stdio transport runs. It then times GetFileSizeEx on STD_INPUT_HANDLE
(one of the calls OpenBLAS's DLL load makes) and, in the isolated case, a first
``import numpy``. The parent writes one line after HOLD seconds, which is the
only thing that can release a blocked call. Without the fix the probe must wait
for that write (the control: it proves the hazard exists on this host, so the
fixed case is not passing vacuously); with the fix it returns at once, numpy
imports, and the reader still receives the line on the private descriptor.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows pipe semantics")

HOLD: float = 4.0

CHILD = r"""
import ctypes, sys, threading, time
from ctypes import wintypes
if sys.argv[1] == "isolated":
    from nexus.mcp._win_stdin import isolate_stdin
    assert isolate_stdin().isolated is True
got = []
reader = threading.Thread(target=lambda: got.append(sys.stdin.buffer.readline()), daemon=True)
reader.start()
time.sleep(0.5)
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.GetStdHandle.argtypes = [wintypes.DWORD]
k.GetStdHandle.restype = wintypes.HANDLE
k.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
h = k.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))
t0 = time.monotonic(); k.GetFileSizeEx(h, ctypes.byref(ctypes.c_longlong()))
print("probe %.2f" % (time.monotonic() - t0), flush=True)
if sys.argv[1] == "isolated":
    t0 = time.monotonic(); import numpy
    print("numpy %.2f" % (time.monotonic() - t0), flush=True)
reader.join(30)
print("read %r" % (got[0] if got else None), flush=True)
"""


def _run(mode: str) -> dict[str, str]:
    p = subprocess.Popen([sys.executable, "-c", CHILD, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(HOLD)
        assert p.stdin is not None
        p.stdin.write(b"line-after-hold\n")
        p.stdin.flush()
        out, err = p.communicate(timeout=120)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 0, err.decode(errors="replace")
    return dict(line.split(" ", 1) for line in out.decode().splitlines() if " " in line)


class TestRealWindows:
    def test_control_a_pending_stdin_read_blocks_the_probe(self) -> None:
        assert sys.platform == "win32", "non-vacuity: this class must run on Windows"
        r = _run("plain")
        assert float(r["probe"]) >= HOLD - 1.5, f"the hazard did not reproduce on this host: {r}"

    def test_isolated_stdin_unblocks_the_probe_and_numpy_and_keeps_the_protocol(self) -> None:
        assert sys.platform == "win32", "non-vacuity: this class must run on Windows"
        r = _run("isolated")
        assert float(r["probe"]) < 2.0, r
        assert float(r["numpy"]) < 30.0, r
        assert r["read"] == repr(b"line-after-hold\n"), r
