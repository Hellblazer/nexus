# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 RF-8 (bead nexus-4r9ja) — never orphan the ``claude -p`` child.

``extract_aspects`` runs ``claude -p`` via subprocess. Two independent death
scenarios must not leave claude orphaned burning API quota:

1. **Subprocess timeout** — the child runs in its OWN session/group
   (``start_new_session=True``); on ``TimeoutExpired`` the WHOLE group is
   SIGKILL'd (``os.killpg``), so grandchildren claude spawned (e.g. MCP servers)
   die too — not just the direct child.
2. **Parent (daemon) death** — the child is armed with ``PR_SET_PDEATHSIG`` via
   ``preexec_fn`` so the kernel kills it when the aspect-worker daemon dies by
   ANY means, including an uncatchable SIGKILL. This is the actual RF-8 close.

DECISION (nexus-4r9ja, brainstorming-gate 2026-07-01): graceful daemon SIGTERM
is DRAIN, not abort — the in-flight extraction finishes (quota already spent),
bounded by the subprocess timeout. Only hard death (SIGKILL) or timeout kills
claude. macOS has no PR_SET_PDEATHSIG, so a daemon SIGKILL there can orphan
claude until its own timeout; accepted (prod is Linux; reclaim-first recovers
the row; macOS is dev-only).
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

import pytest

import nexus.aspect_extractor as ax
import nexus.pdeathsig as pdeathsig


def _pid_alive(pid: int) -> bool:
    """True while *pid* exists (signal 0 probes without delivering)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists but not ours (won't happen in-test)
        return True
    return True


def _wait_until(pred, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


def test_happy_path_returns_completed_process() -> None:
    """The isolated runner round-trips stdin → stdout and returns a normal
    CompletedProcess (behavioral parity with the old subprocess.run)."""
    cp = ax._run_claude_isolated(
        "hello-stdin", timeout=10,
        _argv=["python", "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
    )
    assert cp.returncode == 0
    assert cp.stdout == "hello-stdin"


def test_timeout_kills_grandchild_not_just_direct_child(tmp_path) -> None:
    """NON-VACUOUS group-kill: the child spawns a REAL grandchild in the same
    group and blocks; on TimeoutExpired the whole group is SIGKILL'd, so the
    grandchild (a stand-in for claude's MCP-server children) actually DIES —
    not merely that ``os.killpg`` was called (the prior test's weakness)."""
    pidfile = tmp_path / "grandchild.pid"
    child_prog = (
        "import subprocess, sys, time\n"
        "gc = subprocess.Popen(['sleep', '30'])\n"          # grandchild in child's group
        "open(sys.argv[1], 'w').write(str(gc.pid))\n"
        "time.sleep(30)\n"                                   # keep the group alive
    )
    with pytest.raises(subprocess.TimeoutExpired):
        ax._run_claude_isolated(
            "x", timeout=1.5,
            _argv=["python", "-c", child_prog, str(pidfile)],
        )

    assert _wait_until(pidfile.exists, timeout=2.0), "child never spawned the grandchild"
    gc_pid = int(pidfile.read_text().strip())
    try:
        assert _wait_until(lambda: not _pid_alive(gc_pid)), (
            f"grandchild {gc_pid} survived the group kill — killpg only reached the direct child"
        )
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(gc_pid, signal.SIGKILL)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="PR_SET_PDEATHSIG is Linux-only",
)
def test_pdeathsig_kills_child_when_parent_is_sigkilled(tmp_path) -> None:
    """RF-8 core: a child armed with the shared pdeathsig preexec dies when its
    parent is SIGKILL'd (no Python cleanup runs). Proves the mechanism actually
    fires, not just that the preexec is wired."""
    assert pdeathsig.LIBC is not None, "Linux must expose libc/prctl"
    pidfile = tmp_path / "armed_child.pid"
    # Intermediate parent: spawns a sleeper armed with the SAME preexec the
    # extractor uses, records its pid, then blocks. We SIGKILL this parent.
    parent_prog = (
        "import subprocess, sys, time\n"
        "from nexus.pdeathsig import set_pdeathsig_preexec, LIBC\n"
        "child = subprocess.Popen(\n"
        "    ['sleep', '60'], start_new_session=True,\n"
        "    preexec_fn=set_pdeathsig_preexec if LIBC is not None else None,\n"
        ")\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen(
        ["python", "-c", parent_prog, str(pidfile)],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert _wait_until(pidfile.exists, timeout=5.0), "parent never armed the child"
        child_pid = int(pidfile.read_text().strip())
        assert _pid_alive(child_pid)
        parent.kill()  # SIGKILL the parent — no cleanup code can run
        assert _wait_until(lambda: not _pid_alive(child_pid)), (
            f"armed child {child_pid} survived parent SIGKILL — PR_SET_PDEATHSIG did not fire"
        )
    finally:
        with contextlib.suppress(Exception):
            parent.kill()
        if pidfile.exists():
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pidfile.read_text().strip()), signal.SIGKILL)


def test_run_claude_isolated_arms_pdeathsig_preexec(monkeypatch) -> None:
    """The Popen call arms the shared pdeathsig preexec (gated on LIBC), so the
    parent-death protection is actually installed, on Linux."""
    captured: dict = {}

    class _Reaped:
        args = ["python"]
        returncode = 0

        def communicate(self, *a, **k):
            return ("", "")

    def _spy_popen(argv, **kw):
        captured.update(kw)
        return _Reaped()

    monkeypatch.setattr(ax.subprocess, "Popen", _spy_popen)
    ax._run_claude_isolated("x", timeout=1, _argv=["python", "-c", "pass"])
    assert captured.get("start_new_session") is True
    if pdeathsig.LIBC is not None:
        assert captured.get("preexec_fn") is pdeathsig.set_pdeathsig_preexec
    else:  # non-Linux: no preexec (documented OS gap)
        assert captured.get("preexec_fn") is None


def test_invoke_once_uses_isolated_runner(monkeypatch) -> None:
    """The single-paper extract path must route through _run_claude_isolated
    (not bare subprocess.run) so the hardening actually applies in production."""
    seen: dict = {}

    def _fake_runner(prompt, timeout, **kw):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(["claude"], 0, '{"is_paper": false}', "")

    monkeypatch.setattr(ax, "_run_claude_isolated", _fake_runner)
    with contextlib.suppress(Exception):
        ax._invoke_once("some prompt")
    assert seen["timeout"] == 180   # single-paper budget, via the isolated runner


def test_invoke_once_batch_uses_isolated_runner(monkeypatch) -> None:
    """MEDIUM-1: the BATCH extract path must ALSO route through
    _run_claude_isolated with its per-call timeout — the batch site was
    unasserted before nexus-4r9ja."""
    seen: dict = {}

    def _fake_runner(prompt, timeout, **kw):
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(["claude"], 0, "{}", "")

    monkeypatch.setattr(ax, "_run_claude_isolated", _fake_runner)
    with contextlib.suppress(Exception):
        ax._invoke_once_batch("some batch prompt", timeout=321)
    assert seen["timeout"] == 321   # forwarded verbatim through the isolated runner
