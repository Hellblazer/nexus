# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``run.py --hangup-check``: does ``tmux kill-server`` hang up a child process group (nexus-iz8ub)? Moved out of run.py
(its file-size ceiling) with no change to what it checks. run.py loads this file by path, as it loads sandbox_work.py,
and passes in the two things it shares: ``args_of`` (a pid's command line, to tell the child from a reused pid) and
``guard`` (sandbox_work, whose ``kill_server`` asks ``nx identity may-stop`` before every stop, RDR-253 Step 6)."""
# ruff: noqa: T201 -- a gate harness: stdout carries its verdict line
from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: The child of the hangup probe: reports its pid and process group, then waits with a SIGHUP trap that
#: writes a marker (what nx-mcp's handler does, minus the teardown), and ends its own sleep.
_HANGUP_CHILD = (
    'echo "$$ $(ps -o pgid= -p $$ | tr -d " ")" > "$1"\n'
    "trap 'echo hup > \"$2\"; kill $! 2>/dev/null; exit 0' HUP\n"
    "sleep 60 &\n"
    "wait\n"
)


def tmux_hangup_probe(workdir: Path, shape: str, *, args_of: Callable[[int], str], guard: Any, wait_s: float = 10.0,
                      child_script: str = _HANGUP_CHILD) -> list[str]:
    """Does ``tmux kill-server`` hang up a child process group (nexus-iz8ub)? nx-mcp's SIGHUP teardown
    (75e22da36) only matters if the sandbox's own kill-server delivers one, and that was verified only for
    Desktop sessions' process groups. Runs on a private tmux server (``-L``, ``-f /dev/null``, never the
    default one) with a child that is NOT the group leader (as nx-mcp is not: claude -> uv -> nx-mcp share
    the session's foreground group), in the two pane shapes the sandbox uses: ``shell`` (a bash pane, the
    command typed into it) and ``direct`` (``exec`` as the pane's own process, rs_direct). Needs only tmux
    and sh. Returns the findings, empty when SIGHUP reached the child."""
    if shape not in ("shell", "direct"):
        raise ValueError(shape)
    workdir.mkdir(parents=True, exist_ok=True)
    child, ready, marker = workdir / "child.sh", workdir / "ready", workdir / "hup"
    child.write_text(child_script, encoding="utf-8")
    sock = f"nx-hup-{os.getpid()}-{shape}"
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}

    def tmux(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
        return subprocess.run(["tmux", "-L", sock, "-f", "/dev/null", *args], capture_output=True, text=True,
                              env=env, timeout=timeout)

    cmd = f"sh -c 'sh {shlex.quote(str(child))} {shlex.quote(str(ready))} {shlex.quote(str(marker))} & wait'"
    # Every kill-server here goes through sandbox_work.kill_server, which asks `nx identity may-stop` first (RDR-253
    # Step 6): no stop of a tmux server bypasses the ownership check, even this probe's own pid-named one.
    server = types.SimpleNamespace(sock=sock, real_nx=shutil.which("nx") or "", tmux=tmux)
    notes: list[str] = []
    findings: list[str] = []
    pid = 0
    try:
        if shape == "direct":
            started = tmux("new-session", "-d", "-s", "hup", "-x", "200", "-y", "50", f"exec {cmd}")
        else:
            started = tmux("new-session", "-d", "-s", "hup", "-x", "200", "-y", "50", "bash --noprofile --norc")
            if started.returncode == 0:
                started = tmux("send-keys", "-t", "hup", "-l", cmd)
                tmux("send-keys", "-t", "hup", "Enter")
        if started.returncode != 0:
            return [f"{shape}: tmux did not start: {started.stderr.strip()[:200]}"]
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.1)
        try:
            pid, pgid = (int(x) for x in ready.read_text(encoding="utf-8").split())
        except (OSError, ValueError):
            return [f"{shape}: the child never reported its process group"]
        if pid == pgid:
            findings.append(f"{shape}: the child leads its own group (pid {pid}); it must be a member, as nx-mcp is")
        time.sleep(1)
        if marker.exists():
            findings.append(f"{shape}: the marker was written before kill-server: the probe proves nothing")
        guard.kill_server(server, notes)
        findings += [f"{shape}: {n}" for n in notes if "NOT sent" in n]
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.1)
        if not marker.exists():
            findings.append(f"{shape}: kill-server sent no SIGHUP to the child (pid {pid}, group {pgid}) within "
                            f"{wait_s:.0f} s")
    except (OSError, subprocess.TimeoutExpired) as exc:
        findings.append(f"{shape}: probe failed to run: {type(exc).__name__}: {exc}")
    finally:
        try:
            guard.kill_server(server, [])
        except OSError:
            pass
        if pid > 1 and str(child) in args_of(pid):  # still the child, not a reused pid
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            Path(os.environ.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid()}", sock).unlink()
    return findings


def hangup_check_main(*, args_of: Callable[[int], str], guard: Any) -> int:
    """``run.py --hangup-check``: the probe in both pane shapes; needs no claude and no token."""
    if not shutil.which("tmux"):
        print("TMUX HANGUP CHECK UNVERIFIED: tmux not on PATH", flush=True)
        return 2
    with tempfile.TemporaryDirectory(prefix="nx-hangup-") as d:
        found = [f for shape in ("shell", "direct") for f in tmux_hangup_probe(Path(d) / shape, shape, args_of=args_of, guard=guard)]
    for f in found:
        print(f"FAIL {f}", flush=True)
    print(f"TMUX HANGUP CHECK {'FAILED' if found else 'PASSED'}: kill-server "
          f"{'did not reach' if found else 'hung up'} the child process group in a bash pane and an exec pane", flush=True)
    return 1 if found else 0
