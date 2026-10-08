# SPDX-License-Identifier: AGPL-3.0-or-later
"""``test_embed_parity._start_service`` tears its child down when the port wait fails.

The parity gate spawns the engine JVM itself, through ``tests._child_process.popen_in_group``,
and waits for the port. Before nexus-f9bgu's residuals pass, a port-wait timeout raised
without stopping the child, so every failed boot leaked a JVM (and its whole group). This
runs on every host with no JVM: ``jar_argv`` is swapped for a Python child that starts a
grandchild in its group, and the port wait is forced to time out once that grandchild exists.
"""
from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

import pytest

from tests._child_process import kill_group, pid_alive
import tests.db.test_embed_parity as parity


def _child_script(pidfile: Path) -> str:
    tmp = f"{pidfile}.tmp"
    return (
        "import os, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
        f"open({tmp!r}, 'w').write(str(p.pid))\n"
        f"os.replace({tmp!r}, {str(pidfile)!r})\n"
        "time.sleep(600)\n"
    )


def _gone_within(pid: int, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def test_a_port_wait_timeout_stops_the_spawned_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pidfile = tmp_path / "grandchild.pid"
    monkeypatch.setattr(parity, "jar_argv", lambda _java, _jar: [sys.executable, "-c", _child_script(pidfile)])

    spawned = []
    real_popen_in_group = parity.popen_in_group

    def _recording(*args, **kwargs):
        proc = real_popen_in_group(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(parity, "popen_in_group", _recording)

    def _forced_timeout(host: str, port: int, timeout: float = 0.0) -> None:
        deadline = time.monotonic() + 30
        while not pidfile.exists():
            assert time.monotonic() < deadline, "the child never started its grandchild"
            time.sleep(0.05)
        raise TimeoutError(f"port {port} on {host} not reachable after {timeout}s")

    monkeypatch.setattr(parity, "_wait_tcp", _forced_timeout)

    try:
        with pytest.raises(TimeoutError, match="did not bind"):
            parity._start_service({"port": 1, "dbname": "x", "user": "u"}, "tok", timeout=0.1)
        assert len(spawned) == 1, spawned
        (proc,) = spawned
        grandchild = int(pidfile.read_text())
        # Non-vacuity: the grandchild is a distinct process, so its death is the group's.
        assert grandchild != proc.pid
        assert proc.poll() is not None, "the spawned child outlived the failed port wait"
        assert _gone_within(grandchild, 10), "a process in the spawned group outlived the failed port wait"
    finally:
        # A red run must not leak what it found: the group, then the grandchild by pid
        # (it outlives a leader-only kill). os.kill with SIGTERM is TerminateProcess on
        # Windows, which is the intent here.
        for proc in spawned:
            if proc.poll() is None:
                kill_group(proc)
                proc.wait(timeout=10)
        if pidfile.exists():
            stray = int(pidfile.read_text())
            if pid_alive(stray):
                os.kill(stray, signal.SIGTERM)
