# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jg99b against the real kernel: a pending read on the stdin pipe blocks
calls that inspect the same file object, and isolate_stdin() ends that.

A child process gets a pipe for stdin and a thread blocked reading it, the way
the MCP stdio transport runs. It reports each probe on stdout as it finishes.
The parent writes the one line that can release a blocked call only after the
child says it is done, or after WAIT seconds of silence. So a probe that
finishes before the release finished on its own, and one that needed the
release is caught: the timing cannot rescue a regression on a slow host.

Controls without the fix prove the hazard exists on this host (GetFileSizeEx on
STD_INPUT_HANDLE and a first ``import numpy`` both wait for the release), so
the fixed case is not passing vacuously.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows pipe semantics")

WAIT: float = 8.0

CHILD = r"""
import ctypes, subprocess, sys, threading, time
from ctypes import wintypes
mode = sys.argv[1]
if mode == "isolated":
    from nexus.mcp._win_stdin import isolate_stdin
    r = isolate_stdin()
    print("isolation %s %s" % (r.isolated, r.step), flush=True)
got = []
reader = threading.Thread(target=lambda: got.append(sys.stdin.buffer.readline()), daemon=True)
reader.start()
time.sleep(0.5)
assert not got, "the reader must still be blocked on the pipe"
print("ready", flush=True)
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.GetStdHandle.argtypes = [wintypes.DWORD]
k.GetStdHandle.restype = wintypes.HANDLE
k.GetFileSizeEx.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_longlong)]
k.GetFileSizeEx.restype = wintypes.BOOL
if mode in ("plain", "isolated"):
    h = k.GetStdHandle(wintypes.DWORD(-10 & 0xFFFFFFFF))
    ok = k.GetFileSizeEx(h, ctypes.byref(ctypes.c_longlong()))
    print("probe GetFileSizeEx returned=%d" % ok, flush=True)
if mode == "isolated":
    buf = ctypes.create_string_buffer(256)
    for crt in ("ucrtbase", "msvcrt"):
        rc = getattr(ctypes.cdll, crt)._fstat64(0, buf)
        print("probe %s._fstat64 rc=%d" % (crt, rc), flush=True)
    child = subprocess.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.buffer.read()))"],
                           capture_output=True, timeout=30)
    print("child-stdin %s" % child.stdout.decode().strip(), flush=True)
if mode in ("plain-numpy", "isolated"):
    import numpy
    print("probe numpy", flush=True)
print("done", flush=True)
reader.join(30)
print("read %r" % (got[0] if got else None), flush=True)
"""


def _run(mode: str) -> dict[str, object]:
    p = subprocess.Popen(
        [sys.executable, "-c", CHILD, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    lines: "queue.Queue[str | None]" = queue.Queue()

    def pump() -> None:
        assert p.stdout is not None
        for raw in p.stdout:
            lines.put(raw.decode(errors="replace").strip())
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    before: list[str] = []
    after: list[str] = []
    released = False
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=WAIT if not released else 60)
            except queue.Empty:
                if released:
                    break
                line = "<silence>"
            if line is None:
                break
            (after if released else before).append(line)
            if not released and (line == "done" or line == "<silence>"):
                assert p.stdin is not None
                p.stdin.write(b"line-after-release\n")
                p.stdin.flush()
                released = True
        p.wait(timeout=60)
    finally:
        if p.poll() is None:
            p.kill()
    err = p.stderr.read().decode(errors="replace") if p.stderr else ""
    assert p.returncode == 0, err
    return {"before": before, "after": after}


def _has(seq: object, prefix: str) -> bool:
    return any(str(line).startswith(prefix) for line in seq)  # type: ignore[union-attr]


class TestRealWindows:
    def test_control_a_pending_stdin_read_blocks_get_file_size(self) -> None:
        assert sys.platform == "win32", "non-vacuity: this class must run on Windows"
        r = _run("plain")
        assert _has(r["before"], "ready"), r
        assert not _has(r["before"], "probe GetFileSizeEx"), f"the hazard did not reproduce on this host: {r}"
        assert _has(r["after"], "probe GetFileSizeEx"), r

    def test_control_a_pending_stdin_read_blocks_the_first_numpy_import(self) -> None:
        assert sys.platform == "win32", "non-vacuity: this class must run on Windows"
        r = _run("plain-numpy")
        assert _has(r["before"], "ready"), r
        assert not _has(r["before"], "probe numpy"), f"the numpy hazard did not reproduce on this host: {r}"
        assert _has(r["after"], "probe numpy"), r

    def test_isolated_stdin_unblocks_every_probe_and_keeps_the_protocol(self) -> None:
        assert sys.platform == "win32", "non-vacuity: this class must run on Windows"
        r = _run("isolated")
        before = r["before"]
        assert "isolation True done" in before, r
        for probe in ("probe GetFileSizeEx", "probe ucrtbase._fstat64", "probe msvcrt._fstat64", "probe numpy"):
            assert _has(before, probe), f"{probe} needed the release: {r}"
        assert "done" in before, r
        assert "child-stdin b''" in before, f"a default-stdin child must inherit NUL, not the protocol pipe: {r}"
        assert "read b'line-after-release\\n'" in r["after"], f"the reader lost the protocol: {r}"
