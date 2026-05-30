# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backstop reaper for T2/T3 daemons a test spawned under its own
isolated ``NEXUS_CONFIG_DIR`` (nexus-scoo5).

A test that drives a real ``nx upgrade`` (or any path that calls ``nx
daemon t2 ensure-running``) spawns a *detached* ``nx daemon t2 start``
bound to the per-test ``NEXUS_CONFIG_DIR``. ``subprocess.run`` returns as
soon as the daemon is up, so the process outlives the test body. The
autouse ``_isolate_config_dir`` fixture contains the *db location* (a tmp
dir, never the user's real ``~/.config/nexus``) but nothing reaps the
*process*. Result: orphan ``nx daemon t2 start`` processes accumulate,
each holding a now-deleted tmp db dir open.

This is the process-level analog of the ``pytest_sessionfinish``
cache-file leak guard. It is scoped *strictly* to the per-test tmp config
dir its caller passes, and double-guarded by a cmdline check, so it can
never signal the user's real daemon (whose discovery file lives under
``~/.config/nexus``, never under ``tmp_path``).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path


def _read_daemon_pid(config_dir: Path, tier: str) -> int | None:
    """Return the recorded daemon pid from *config_dir*'s discovery file
    for *tier*, or ``None`` when absent/unparseable."""
    from nexus.daemon.discovery import discovery_path

    disc = discovery_path(config_dir, tier=tier)
    if not disc.exists():
        return None
    try:
        pid = json.loads(disc.read_text()).get("pid")
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return pid if isinstance(pid, int) and pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _cmdline_is_nx_daemon(pid: int) -> bool:
    """True when *pid*'s command line looks like an ``nx daemon`` process.

    Guards against PID reuse: between reading the discovery file and
    signalling, the recorded pid could have died and been recycled by an
    unrelated process. We only ever signal a pid whose live cmdline still
    names the nx daemon. Works on both macOS and Linux ``ps``.
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "daemon" in out and ("nx" in out or "nexus" in out)


def reap_tmp_daemons(
    config_dir: Path, *, tiers: tuple[str, ...] = ("t2", "t3"),
) -> list[int]:
    """SIGTERM (escalating to SIGKILL) any daemon recorded in *config_dir*'s
    discovery files. Returns the pids that were signalled.

    Best-effort and bounded; never raises. Only signals a pid whose live
    cmdline still names the nx daemon (PID-reuse guard).
    """
    reaped: list[int] = []
    for tier in tiers:
        pid = _read_daemon_pid(config_dir, tier)
        if pid is None or not _pid_alive(pid):
            continue
        if not _cmdline_is_nx_daemon(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        if _pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        reaped.append(pid)
    return reaped
