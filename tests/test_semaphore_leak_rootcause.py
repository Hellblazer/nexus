# SPDX-License-Identifier: AGPL-3.0-or-later
"""Process-group cleanup regressions for ``nexus-ze2a`` + ``nexus-dc57``.

Both bugs surface as ``[Errno 28] No space left on device`` on
``_multiprocessing.SemLock()`` and share a single root cause:

When a long-running Python subprocess (T1 ChromaDB / MinerU) dies
uncleanly, its **worker children** get orphaned. Workers hold POSIX
named semaphores allocated by ``multiprocessing``. Without a process-
group kill that also reaches ``resource_tracker``, those semaphores
are never ``sem_unlink()``-ed until reboot.

The fix: spawn with ``start_new_session=True`` (own process group) +
cleanup via ``os.killpg(pgid, …)`` so SIGTERM / SIGKILL reaches the
whole subtree.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest


# ── session.start_t1_server: must isolate into a new process group ──────────


def test_start_t1_server_uses_start_new_session(monkeypatch) -> None:
    """Regression for nexus-dc57 — the T1 chroma spawn MUST use
    ``start_new_session=True`` so its workers share a killable pgid."""
    from nexus import session

    captured: dict[str, object] = {}

    class _FakeProc:
        pid = 12345
        returncode = None
        def poll(self):
            return None
        def kill(self):
            pass

    def _fake_popen(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        # Short-circuit the poll loop by returning a proc that immediately
        # appears ready at the socket layer via the next monkeypatch.
        return _FakeProc()

    def _fake_create_connection(*args, **kwargs):
        class _FakeConn:
            def close(self): pass
        return _FakeConn()

    monkeypatch.setattr(session, "_find_chroma", lambda: "/fake/chroma")
    monkeypatch.setattr(session.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(session.socket, "create_connection", _fake_create_connection)

    host, port, pid, tmpdir = session.start_t1_server()
    assert pid == 12345
    # The critical assertion: Popen must have isolated the child.
    assert captured["kwargs"].get("start_new_session") is True


# ── stop_t1_server: must kill the whole process group ───────────────────────


def test_stop_t1_server_uses_killpg(monkeypatch) -> None:
    """Regression for nexus-dc57 — SIGTERM/SIGKILL must reach the entire
    subtree so chroma's workers' semaphores get unlinked before exit."""
    from nexus import session

    calls: list[tuple] = []

    def _fake_killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))
        # Fake the liveness check: after first SIGTERM call, pretend dead.
        if sig == signal.SIGTERM:
            return
        if sig == 0:
            raise ProcessLookupError("gone")

    def _fake_kill(pid, sig):
        # Permitted ONLY for readiness probes (sig==0), never SIGTERM/SIGKILL
        # on the head PID — those must go via killpg.
        if sig in (signal.SIGTERM, signal.SIGKILL):
            raise AssertionError(
                f"stop_t1_server used os.kill({pid}, {sig}) — "
                f"must use os.killpg to reach worker subtree"
            )

    def _fake_getpgid(pid):
        return pid

    monkeypatch.setattr(session.os, "killpg", _fake_killpg)
    monkeypatch.setattr(session.os, "kill", _fake_kill)
    monkeypatch.setattr(session.os, "getpgid", _fake_getpgid)
    # Fast-fail alive-check to let the cleanup short-circuit.
    def _fake_is_alive(pid):
        return False
    monkeypatch.setattr(session, "_process_alive", _fake_is_alive, raising=False)

    session.stop_t1_server(99999)
    kpg = [c for c in calls if c[0] == "killpg"]
    assert any(c[2] == signal.SIGTERM for c in kpg), (
        f"expected at least one killpg(SIGTERM) call, got {calls!r}"
    )


# ── mineru.stop: same killpg discipline ─────────────────────────────────────


def test_mineru_stop_uses_killpg(monkeypatch, tmp_path) -> None:
    """Regression for nexus-ze2a — MinerU stop must reach worker subtree."""
    from nexus.commands import mineru
    from click.testing import CliRunner

    calls: list[tuple] = []

    def _fake_killpg(pgid, sig):
        calls.append(("killpg", pgid, sig))

    def _fake_kill(pid, sig):
        if sig in (signal.SIGTERM, signal.SIGKILL):
            raise AssertionError(
                f"mineru.stop used os.kill({pid}, {sig}) on head — "
                f"must use killpg to reach worker subtree"
            )

    pid_path = tmp_path / "mineru.pid.json"
    import json as _json
    from datetime import datetime, timezone
    pid_path.write_text(_json.dumps({
        "pid": 77777, "port": 12345,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }))

    # Initial liveness probe must say "alive" so the stop logic enters
    # the kill path; later probes return "dead" so the poll loop exits.
    alive_states = [True, False, False, False]
    def _alive(pid: int) -> bool:
        return alive_states.pop(0) if alive_states else False

    monkeypatch.setattr(mineru, "_pid_file_path", lambda: pid_path)
    monkeypatch.setattr(mineru, "_is_process_alive", _alive)
    monkeypatch.setattr(mineru.os, "killpg", _fake_killpg)
    monkeypatch.setattr(mineru.os, "kill", _fake_kill)
    monkeypatch.setattr(mineru.os, "getpgid", lambda pid: pid)

    runner = CliRunner()
    result = runner.invoke(mineru.mineru_group, ["stop"])
    assert any(c[0] == "killpg" for c in calls), (
        f"expected killpg call on stop, got {calls!r}"
    )


# ── End-to-end process-group spawn + kill + semaphore audit ─────────────────


@pytest.mark.skipif(
    sys.platform not in ("darwin", "linux"),
    reason="POSIX-only: Windows has no sem_unlink.",
)
def test_process_group_spawn_and_killpg_contract() -> None:
    """Concrete POSIX test: spawn a Python child that uses multiprocessing,
    kill its process group, and confirm the child + its workers all exit.

    Does NOT assert zero semaphore leak (that depends on Python internals
    we cannot observe directly). Instead it pins the contract we rely on:
    ``start_new_session=True`` → ``os.killpg(getpgid(pid), SIGKILL)``
    terminates the whole subtree.
    """
    # Minimal child: fork a plain subprocess (no multiprocessing complexity)
    # that prints its own pid and sleeps. That's all we need to validate
    # the contract: killpg reaches a grandchild that's in the same group.
    child_script = (
        "import os, sys, time\n"
        "p = os.fork()\n"
        "if p == 0:\n"
        "    sys.stdout.write(str(os.getpid()) + '\\n'); sys.stdout.flush()\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    sys.stdout.write('parent\\n'); sys.stdout.flush()\n"
        "    time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_script],
        start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Loop until we read the child's pid line (fork output order is
        # non-deterministic — parent's "parent\n" may race).
        worker_pid = None
        deadline = time.time() + 5.0
        while time.time() < deadline and worker_pid is None:
            line = proc.stdout.readline().strip()
            if line.isdigit():
                worker_pid = int(line)
                break
        assert worker_pid is not None, "never saw a child pid line"
        # Both parent and worker should be alive.
        os.kill(proc.pid, 0)
        os.kill(worker_pid, 0)
        # The fix under test: killpg reaches both.
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        time.sleep(0.3)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Confirm both are gone.
        deadline = time.time() + 2.0
        while time.time() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"worker pid={worker_pid} survived killpg")
    finally:
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


# ── nx doctor --check=resources: detect semaphore pressure ──────────────────


def test_doctor_check_resources_reports_available(
    tmp_path, monkeypatch,
) -> None:
    """``nx doctor --check=resources`` probes POSIX semaphores and reports
    the current state. Healthy machine → exit 0 + 'ok'/'available' in output."""
    from click.testing import CliRunner
    from nexus.cli import main

    result = CliRunner().invoke(main, ["doctor", "--check-resources"])
    # On a healthy CI machine, probe succeeds.
    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "semaphore" in lowered or "ok" in lowered


def test_doctor_check_resources_surfaces_errno28(monkeypatch) -> None:
    """When the SemLock probe fails with Errno 28, the doctor exits 2
    and prints an actionable message pointing at the known sources."""
    from click.testing import CliRunner
    from nexus.cli import main
    from nexus.commands import doctor as doctor_cmd

    def _fake_probe() -> tuple[bool, str]:
        return False, "[Errno 28] No space left on device"

    monkeypatch.setattr(
        doctor_cmd, "_probe_semaphore_namespace", _fake_probe, raising=False,
    )
    result = CliRunner().invoke(main, ["doctor", "--check-resources"])
    assert result.exit_code == 2, result.output
    out = result.output.lower()
    assert "errno 28" in out or "semaphore" in out
