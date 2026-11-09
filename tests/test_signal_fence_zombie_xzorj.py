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
        while Path(f"/proc/{pid}/cmdline").read_bytes() != b"\x0060\x00":  # the finished exec, not mid-exec
            assert time.monotonic() < deadline, "the process never showed an empty argv[0]"
            time.sleep(0.02)
        assert fence._state(pid) not in ("Z", "X")
        assert not fence._imageless(pid), "an empty argv[0] with arguments and an environment is a program"
        root, token = _ids()
        assert fence.classify("kill", pid, signal.SIGTERM, root=root, token=token,
                              basetemp=fence._basetemp()) == ("foreign", pid)
        # The same process read as between programs (exiting or mid-exec) is not counted: the routing classify uses.
        real = fence._imageless
        fence._imageless = lambda target: target == pid or real(target)
        try:
            assert fence.classify("kill", pid, signal.SIGKILL, root=root, token=token,
                                  basetemp=fence._basetemp()) == ("unclassified", pid)
        finally:
            fence._imageless = real
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
        # The exact argv of the exec'd sleep (["", "60"]), not just an empty first field: mid-exec the whole area
        # reads empty for a moment, which an argv[0] test would take for the finished exec.
        while Path(f"/proc/{p.pid}/cmdline").read_bytes() != b"\x0060\x00":
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



@_linux_fence
def test_a_group_whose_leader_and_members_are_between_programs_is_not_counted() -> None:
    """The group path of the rule (review r1 of b4fc45368): killpg of the session's own group whose leader and only
    member both read as between programs is unclassified. With either the leader check or the member filter left on
    the zombie test alone, the session's own leader or child would be judged and read own."""
    leader = subprocess.Popen(
        [sys.executable, "-c", "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)"],
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not (members := [m for m in fence._group_members(leader.pid) if m != leader.pid]):
            assert time.monotonic() < deadline, "the leader never started its member"
            time.sleep(0.02)
        between = {leader.pid, *members}
        real = fence._imageless
        fence._imageless = lambda target: target in between or real(target)
        try:
            root, token = _ids()
            assert fence.classify("killpg", leader.pid, signal.SIGKILL, root=root, token=token,
                                  basetemp=fence._basetemp())[0] == "unclassified"
        finally:
            fence._imageless = real
        root, token = _ids()
        assert fence.classify("killpg", leader.pid, signal.SIGKILL, root=root, token=token,
                              basetemp=fence._basetemp())[0] == "own", "the control: read as programs, it is ours"
    finally:
        try:
            fence._real_killpg(leader.pid, signal.SIGKILL)
        except (ProcessLookupError, AttributeError):
            leader.kill()
        leader.wait()

def _fake_proc(monkeypatch, tmp_path: Path, pid: int, *, flags: int, cmdline: bytes, environ: bytes | None) -> None:
    """Point the fence's /proc reads for *pid* at files under *tmp_path* (a real task cannot be held mid-exit)."""
    d = tmp_path / "proc" / str(pid)
    d.mkdir(parents=True)
    # Fields after the command name: state ppid pgrp session tty_nr tpgid flags ...
    (d / "stat").write_text(f"{pid} (x) R 1 {pid} {pid} 0 -1 {flags} 0 0 0 0\n")
    (d / "cmdline").write_bytes(cmdline)
    if environ is not None:
        (d / "environ").write_bytes(environ)
    real_path = Path
    monkeypatch.setattr(fence, "Path", lambda s: real_path(str(s).replace("/proc", str(tmp_path / "proc"), 1)))


def test_an_exiting_task_is_between_programs(monkeypatch, tmp_path) -> None:
    """develop shard 3 at aeaf9e4a0: kill_tree SIGKILLed an orphaned member of a test's group with an empty command
    line, and the rules read it foreign. PF_EXITING alone settles it, whatever the reads show."""
    _fake_proc(monkeypatch, tmp_path, 4242, flags=0x4 | 0x400040, cmdline=b"python\0x\0", environ=b"A=1\0")
    assert fence._imageless(4242)


def test_no_argv_and_no_environment_is_between_programs(monkeypatch, tmp_path) -> None:
    """Memory released on the way out, or a new image whose argument area is not written yet."""
    _fake_proc(monkeypatch, tmp_path, 4243, flags=0x400040, cmdline=b"", environ=b"")
    assert fence._imageless(4243)


@pytest.mark.parametrize(("cmdline", "environ"), [
    (b"", b"PATH=/usr/bin\0"),  # an empty argv with an environment: a program, judged by the rules
    (b"\x0060\x00", b""),  # an empty environment with arguments: a program
    (b"", None),  # environment unreadable (another user's process): judged by the rules, never excused
])
def test_a_program_is_not_between_programs(monkeypatch, tmp_path, cmdline: bytes, environ: bytes | None) -> None:
    _fake_proc(monkeypatch, tmp_path, 4244, flags=0x400040, cmdline=cmdline, environ=environ)
    assert not fence._imageless(4244)

