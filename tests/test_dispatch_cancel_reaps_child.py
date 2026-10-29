# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-jypmj.17: a ``claude -p`` call cut from outside leaves no live child.

The coverage assessment cuts each concurrent call with ``asyncio.wait_for`` at its own timeout. ``CliBackend.run``
used to kill the process group only for its own ``asyncio.TimeoutError``; a ``CancelledError`` (what ``wait_for``
delivers to the inner coroutine) skipped the kill, so the child ran to completion with its spend on no record
(the 2026-10-29 code review's probe: ``wait_for(claude_dispatch(..., timeout=3.0), timeout=3.0)`` left a live
``sleep``). A real child, a real process group, a real cut.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nexus.operators.dispatch import CliBackend, claude_dispatch
from tests._module_seam import patch_in

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="needs a POSIX process group")

_SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}}}

#: A fake ``claude`` that records its own pid and a grandchild's, then sleeps (never emits a result event).
_SCRIPT = """\
import subprocess, sys, os, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
with open(sys.argv[1] + ".tmp", "w") as fh:
    fh.write(f"{os.getpid()} {child.pid}")
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    """True while *pid* is a live, un-reaped-or-running process (a zombie child counts as gone once reaped)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers signal 0; read its state where /proc or ps exists.
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


async def _wait_for_pids(path: Path, limit: float = 20.0) -> tuple[int, int]:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if path.exists():
            leader, grandchild = path.read_text().split()
            return int(leader), int(grandchild)
        await asyncio.sleep(0.05)
    raise AssertionError("the fake claude never started")


async def _gone(pids: tuple[int, ...], limit: float = 6.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        await asyncio.sleep(0.05)
    return False


def _spawn_fake(script: Path, pid_file: Path):
    real = asyncio.create_subprocess_exec

    async def _side_effect(*_args: object, **kwargs: object) -> "asyncio.subprocess.Process":
        return await real(sys.executable, str(script), str(pid_file), **kwargs)

    return _side_effect


@pytest.mark.asyncio
async def test_a_call_cut_by_wait_for_kills_the_whole_process_tree(tmp_path: Path) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text(_SCRIPT)
    pid_file = tmp_path / "pids"
    pids: tuple[int, ...] = ()
    try:
        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=_spawn_fake(script, pid_file)):
            # the dispatcher's own timeout is long: the OUTER cut is what ends this call, as in the assessment stage
            task = asyncio.ensure_future(claude_dispatch("prompt", _SCHEMA, timeout=60.0, backend=CliBackend()))
            pids = await _wait_for_pids(pid_file)
            assert all(_alive(p) for p in pids), "the fake claude must be running before it is cut"
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=0.5)
        assert await _gone(pids), f"the cut call left a live child (pids {pids})"
    finally:
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_a_cancelled_call_kills_its_child_and_reraises_cancellation(tmp_path: Path) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text(_SCRIPT)
    pid_file = tmp_path / "pids"
    pids: tuple[int, ...] = ()
    try:
        with patch_in("nexus.operators.dispatch", "asyncio.create_subprocess_exec", side_effect=_spawn_fake(script, pid_file)):
            task = asyncio.ensure_future(claude_dispatch("prompt", _SCHEMA, timeout=60.0, backend=CliBackend()))
            pids = await _wait_for_pids(pid_file)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await _gone(pids), f"the cancelled call left a live child (pids {pids})"
        # the drain tasks were cancelled too: nothing of this call is left pending on the loop
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert pending == [], f"tasks left pending: {pending}"
    finally:
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except ProcessLookupError:
                pass
