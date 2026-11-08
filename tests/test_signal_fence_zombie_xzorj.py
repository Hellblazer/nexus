# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-xzorj red fix: a zombie is not a target the fence can count.

The first fail-mode run on develop (745ea0128, shard 3) counted one foreign SIGKILL: process_group's sparing sweep
signalled a member of a test's own group that had already exited and not been reaped (a zombie, its /proc
cmdline empty). A signal to a zombie delivers nothing, so the fence classifies a target by its process STATE ('Z'
in /proc/<pid>/stat), never by an empty command line: a live process can have an empty argv, and judging by that
could hide a real foreign kill. Both sides are pinned here.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import _signal_fence as fence

_linux_fence = pytest.mark.skipif(not fence.active(), reason="the fence reads /proc: Linux only, and not switched off")
_SCRUBBED = {"PATH": "/usr/bin:/bin"}


def _ids() -> tuple[int, str]:
    return fence._root, fence._token


def _wait_state(pid: int, want: str) -> None:
    deadline = time.monotonic() + 10
    while fence._state(pid) != want:
        assert time.monotonic() < deadline, f"pid {pid} never reached state {want!r} (now {fence._state(pid)!r})"
        time.sleep(0.02)


@_linux_fence
def test_a_zombie_reads_unclassified_whoever_it_belongs_to() -> None:
    """An exited, unreaped child: by ancestry it is ours, but a signal to it delivers nothing, so it is not counted
    at all (and an orphaned zombie of a non-reaping namespace init, the case CI met, is not counted as foreign)."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        _wait_state(child.pid, "Z")
        root, token = _ids()
        assert fence.classify("kill", child.pid, signal.SIGKILL, root=root, token=token,
                              basetemp=fence._basetemp()) == ("unclassified", child.pid)
    finally:
        child.wait()


@_linux_fence
def test_signalling_a_zombie_through_the_wrapper_counts_no_foreign(monkeypatch) -> None:
    monkeypatch.setattr(fence, "_counts", {"signals": 0, "own": 0, "foreign": 0, "unclassified": 0})
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        _wait_state(child.pid, "Z")
        os.kill(child.pid, signal.SIGKILL)  # delivers nothing to a zombie; the call itself succeeds
        assert fence._counts["unclassified"] == 1 and fence._counts["foreign"] == 0
    finally:
        child.wait()


@_linux_fence
def test_a_live_process_with_an_empty_argv_is_still_judged_by_the_rules(tmp_path) -> None:
    """The other side: an empty command line is not a zombie. A live, detached, scrubbed process whose argv[0] is
    empty and names nothing of ours is foreign, exactly as before."""
    if fence._root <= 1:
        pytest.skip("pytest is pid 1 of its namespace: every orphan reparents to the root")
    pidfile = tmp_path / "blank.pid"
    code = (
        "import subprocess; "
        "p = subprocess.Popen(['bash', '-c', 'exec -a \"\" sleep 60'], start_new_session=True, env="
        f"{_SCRUBBED!r}); open({str(pidfile)!r}, 'w').write(str(p.pid))"
    )
    subprocess.run([sys.executable, "-c", code], check=True, env=_SCRUBBED)
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    try:
        deadline = time.monotonic() + 10
        while Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0] != b"":
            assert time.monotonic() < deadline, "the process never showed an empty argv[0]"
            time.sleep(0.02)
        assert fence._state(pid) not in ("Z", "X")
        root, token = _ids()
        assert fence.classify("kill", pid, signal.SIGTERM, root=root, token=token,
                              basetemp=fence._basetemp()) == ("foreign", pid)
    finally:
        try:
            fence._real_kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@_linux_fence
def test_a_live_child_with_an_empty_argv_is_still_the_sessions() -> None:
    p = subprocess.Popen(["bash", "-c", 'exec -a "" sleep 60'])
    try:
        deadline = time.monotonic() + 10
        while Path(f"/proc/{p.pid}/cmdline").read_bytes().split(b"\0", 1)[0] != b"":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        root, token = _ids()
        assert fence.classify("kill", p.pid, signal.SIGTERM, root=root, token=token,
                              basetemp=fence._basetemp()) == ("own", p.pid)
    finally:
        p.kill()
        p.wait()


@_linux_fence
def test_a_group_of_zombies_only_is_not_counted() -> None:
    """killpg of a group whose leader and members have all exited unreaped: nothing can be delivered, unclassified."""
    leader = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    try:
        _wait_state(leader.pid, "Z")
        root, token = _ids()
        assert fence.classify("killpg", leader.pid, signal.SIGKILL, root=root, token=token,
                              basetemp=fence._basetemp())[0] == "unclassified"
    finally:
        leader.wait()
