# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression tests for the test-teardown daemon-leak guard (nexus-scoo5).

Locks in the behaviour of ``tests._daemon_leak_guard.reap_tmp_daemons`` and
verifies the autouse backstop fixture is wired into the root conftest.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from nexus.daemon.discovery import discovery_path
from tests._daemon_leak_guard import reap_tmp_daemons


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _write_discovery(config_dir: Path, tier: str, pid: int) -> None:
    disc = discovery_path(config_dir, tier=tier)
    disc.parent.mkdir(parents=True, exist_ok=True)
    disc.write_text(json.dumps({"format_version": 1, "pid": pid}))


def _spawn_daemon_like(argv0: str) -> subprocess.Popen:
    """Spawn a long-lived process whose argv0 mimics an nx daemon, so the
    reaper's cmdline PID-reuse guard recognises it. ``exec -a`` rewrites
    argv0 in place; the resulting pid is the sleeper itself."""
    return subprocess.Popen(["bash", "-c", f"exec -a '{argv0}' sleep 30"])


@pytest.fixture
def _reap_residue() -> list[subprocess.Popen]:
    """Kill any sleeper a test forgot to clean up, so the test file itself
    never leaks (the very class it guards)."""
    procs: list[subprocess.Popen] = []
    yield procs
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=5)


def test_reaps_live_daemon_recorded_in_discovery(
    tmp_path: Path, _reap_residue: list[subprocess.Popen],
) -> None:
    config_dir = tmp_path / ".config" / "nexus"
    proc = _spawn_daemon_like("nx daemon t2 start")
    _reap_residue.append(proc)
    # Let exec -a settle so ps reports the spoofed argv0.
    time.sleep(0.3)
    _write_discovery(config_dir, "t2", proc.pid)

    reaped = reap_tmp_daemons(config_dir)

    assert reaped == [proc.pid]
    # The sleeper is a direct child of pytest, so after termination it is a
    # zombie until waited (``os.kill(pid, 0)`` still succeeds on a zombie).
    # ``proc.wait`` reaps it and confirms it was killed by a signal
    # (negative returncode). In production the daemon is detached/reparented
    # to init, so this zombie window does not arise.
    ret = proc.wait(timeout=3)
    assert ret != 0, f"expected signal-termination, got returncode {ret}"


def test_skips_pid_reuse_unrelated_process(
    tmp_path: Path, _reap_residue: list[subprocess.Popen],
) -> None:
    """A recorded pid recycled by an unrelated process must NOT be killed."""
    config_dir = tmp_path / ".config" / "nexus"
    # Plain sleep — argv0 is 'sleep', not an nx daemon.
    proc = subprocess.Popen(["sleep", "30"])
    _reap_residue.append(proc)
    time.sleep(0.2)
    _write_discovery(config_dir, "t2", proc.pid)

    reaped = reap_tmp_daemons(config_dir)

    assert reaped == []
    assert _pid_alive(proc.pid), "unrelated process must survive the guard"


def test_noop_when_no_discovery_file(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "nexus"
    assert reap_tmp_daemons(config_dir) == []


def test_noop_when_pid_already_dead(tmp_path: Path) -> None:
    config_dir = tmp_path / ".config" / "nexus"
    proc = subprocess.Popen(["sleep", "0.01"])
    proc.wait(timeout=5)
    _write_discovery(config_dir, "t2", proc.pid)
    assert reap_tmp_daemons(config_dir) == []


def test_autouse_backstop_fixture_is_registered() -> None:
    """The conftest must expose the autouse teardown reaper, scoped to the
    per-test tmp config dir (nexus-scoo5 backstop layer)."""
    import inspect

    import tests.conftest as conftest

    fn = getattr(conftest, "_reap_spawned_daemons", None)
    assert fn is not None, "conftest is missing _reap_spawned_daemons"
    src = inspect.getsource(fn)
    # Strictly scoped to tmp_path so it can never touch the real ~/.config.
    assert "tmp_path" in src
    assert "reap_tmp_daemons" in src
