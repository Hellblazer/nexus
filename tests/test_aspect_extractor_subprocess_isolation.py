# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-173 RF-8 (bead nexus-4r9ja) — never orphan the ``claude -p`` child.

``extract_aspects`` runs ``claude -p`` via subprocess. Two independent death
scenarios must not leave claude orphaned burning API quota:

1. **Subprocess timeout** — the child runs in its OWN session/group
   (``start_new_session=True``); on ``TimeoutExpired`` the WHOLE group is
   SIGKILL'd (``os.killpg``), reaching grandchildren that stay in claude's group
   (a grandchild that creates its own session escapes — documented residual gap).
2. **Parent (daemon) death** — the child is armed with ``PR_SET_PDEATHSIG`` via
   ``preexec_fn`` so the kernel kills it when the aspect-worker daemon dies by
   ANY means, including an uncatchable SIGKILL. This is the actual RF-8 close.

DECISION (nexus-4r9ja, brainstorming-gate 2026-07-01): on graceful daemon
SIGTERM the ``stop()`` join gives a BOUNDED drain window (default 10 s) — a
short in-flight extraction finishes (quota already spent); a longer one is
killed by PR_SET_PDEATHSIG at process exit (bounded quota-burn, no orphan). The
unbounded in-flight-completion drain is the separate RDR-173 P4
``drain_worker`` / ``stop_claiming`` path, not the SIGTERM stop. macOS has no
PR_SET_PDEATHSIG, so a daemon SIGKILL there can orphan claude until its own
timeout; accepted (prod is Linux; reclaim-first recovers the row; macOS is
dev-only).
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
    # timeout=3.0 (not 1.5) so the child reliably starts python + spawns the
    # grandchild + writes the pidfile BEFORE the kill fires, even on loaded CI.
    with pytest.raises(subprocess.TimeoutExpired):
        ax._run_claude_isolated(
            "x", timeout=3.0,
            _argv=["python", "-c", child_prog, str(pidfile)],
        )

    assert _wait_until(pidfile.exists, timeout=4.0), "child never spawned the grandchild"
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


def test_run_claude_isolated_redirects_stdin_from_file_not_pipe(monkeypatch) -> None:
    """nexus-0bkjm: the same stdin-race shape nexus-vzy2v fixed in
    ``claude_dispatch`` (``Popen(stdin=PIPE)`` + a prompt fed via a later
    ``communicate(input=...)`` call, racing the Claude Code CLI's 3s
    stdin-wait) existed here too. The prompt must now be written to disk
    BEFORE ``Popen`` is called and the child's stdin redirected from that
    file — never a live PIPE fed post-spawn, and never handed to
    ``communicate()`` as ``input=`` (that only works when stdin IS a
    PIPE, so its presence would mean the race shape is still there)."""
    captured: dict = {}

    class _Reaped:
        args = ["python"]
        returncode = 0

        def communicate(self, *a, **k):
            captured["communicate_args"] = a
            captured["communicate_kwargs"] = k
            return ("", "")

    def _spy_popen(argv, **kw):
        captured["popen_kwargs"] = kw
        return _Reaped()

    monkeypatch.setattr(ax.subprocess, "Popen", _spy_popen)
    ax._run_claude_isolated("race-free-prompt", timeout=1, _argv=["python", "-c", "pass"])

    stdin_arg = captured["popen_kwargs"].get("stdin")
    assert stdin_arg is not subprocess.PIPE, (
        "stdin must be a real file redirected before spawn, not a PIPE fed "
        "after spawn (the vzy2v race shape)"
    )
    assert hasattr(stdin_arg, "read"), "stdin must be an open file object"
    assert "input" not in captured["communicate_kwargs"], (
        "communicate(input=...) only works with a PIPE stdin -- its presence "
        "means the prompt is still being written to a live pipe post-spawn"
    )
    assert not captured["communicate_args"]


def test_default_argv_carries_strict_mcp_config(monkeypatch) -> None:
    """RDR-196 .p0b audit fold F1 consequence 2 (nexus-nyry9.6): aspect
    extraction is a second, un-modernized ``claude -p`` call site that
    never got the RDR-196 Gap 4 fix (``--strict-mcp-config`` landed on
    ``claude_dispatch`` at f1ae257d0, NOT here). Without it, every
    ``nx enrich aspects`` dispatch loads the user's entire ambient MCP
    server set for no reason (this call is tool-free by construction --
    it never passes ``--allowedTools``), paying the ~2x context/cost
    overhead 196-R2 measured. Pinned at the DEFAULT-argv path (``_argv``
    not supplied) -- the ``_argv`` override used by other isolation
    tests intentionally bypasses the real argv to swap in a test
    double, so it must NOT be asserted against here.
    """
    captured_argv: list[list[str]] = []

    class _Reaped:
        args = ["claude"]
        returncode = 0

        def communicate(self, *a, **k):
            return ('{"result": "{}"}', "")

    def _spy_popen(argv, **kw):
        captured_argv.append(argv)
        return _Reaped()

    monkeypatch.setattr(ax.subprocess, "Popen", _spy_popen)
    ax._run_claude_isolated("x", timeout=1)  # no _argv -- exercises the real default
    assert len(captured_argv) == 1
    assert "--strict-mcp-config" in captured_argv[0], (
        f"default argv missing --strict-mcp-config: {captured_argv[0]!r}"
    )


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


# ── Stdin-race stderr recognition (nexus-0bkjm) ──────────────────────────────


def test_stdin_race_error_stderr_is_transient_not_hard(monkeypatch) -> None:
    """nexus-0bkjm: before this fix, a stdin-race stderr from the CLI --
    'Error: Input must be provided either through stdin or as a prompt
    argument when using --print' -- was NOT in _TRANSIENT_STDERR_PATTERNS,
    so a race hit returned _HardFailure on the FIRST attempt: no retry, a
    silently lost aspect extraction."""
    cp = subprocess.CompletedProcess(
        ["claude"], 1, "",
        "Error: Input must be provided either through stdin or as a "
        "prompt argument when using --print",
    )
    monkeypatch.setattr(ax, "_run_claude_isolated", lambda *a, **k: cp)
    with pytest.raises(ax._TransientFailure):
        ax._invoke_once("some prompt")


def test_stdin_race_warning_stderr_is_transient(monkeypatch) -> None:
    """Companion signal: 'Warning: no stdin data received in 3s,
    proceeding without it' -- the CLI's soft variant of the same race
    (it proceeds with no prompt rather than exiting outright); must also
    retry rather than hard-fail."""
    cp = subprocess.CompletedProcess(
        ["claude"], 1, "",
        "Warning: no stdin data received in 3s, proceeding without it",
    )
    monkeypatch.setattr(ax, "_run_claude_isolated", lambda *a, **k: cp)
    with pytest.raises(ax._TransientFailure):
        ax._invoke_once("some prompt")


def test_stdin_race_error_stderr_is_transient_on_batch_path(monkeypatch) -> None:
    """Same recognition, batch call site -- _invoke_once_batch shares
    _TRANSIENT_STDERR_PATTERNS with _invoke_once, so one fix covers
    both."""
    cp = subprocess.CompletedProcess(
        ["claude"], 1, "",
        "Error: Input must be provided either through stdin or as a "
        "prompt argument when using --print",
    )
    monkeypatch.setattr(ax, "_run_claude_isolated", lambda *a, **k: cp)
    with pytest.raises(ax._TransientFailure):
        ax._invoke_once_batch("some batch prompt", timeout=30)
