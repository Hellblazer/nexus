# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Regression for the MagicMock-pgid-1 hazard that hung CI on v4.7.0.

``MagicMock()`` implements ``__index__`` returning ``1``. A naive
``os.killpg(os.getpgid(proc.pid), SIGKILL)`` on a mock fixture therefore
signals ``pgid=1`` (init / launchd) — benign on macOS (EPERM), deadlock-
inducing on GitHub Actions ubuntu-latest containers. The canonical
``safe_killpg`` helper guards with ``isinstance(pid, int)`` so mock
fixtures fall through cleanly.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from unittest.mock import MagicMock

import pytest

from nexus.util.process_group import KILL_SIGNAL, safe_killpg
from tests.daemon._children import CHILD_PYTHON, OWN_GROUP

#: The primitive ``safe_killpg`` signals through on this host: ``os.killpg`` where it
#: exists, ``os.kill`` on Windows (one process, no group). A test that traces "no
#: signal was delivered" traces THIS name, so the property is checked on both.
_KILL_PRIMITIVE = "killpg" if hasattr(os, "killpg") else "kill"

#: A real group-less kill of a live pid exists on both platforms; the liveness probe
#: ``sig=0`` does not (``os.kill(pid, 0)`` is ``CTRL_C_EVENT`` on Windows), so it is
#: taken only where ``os.killpg`` is.
_POSIX = hasattr(os, "killpg")


class TestMockGuard:
    def test_magicmock_proc_returns_false_without_signalling(self):
        """The core regression: MagicMock.pid coerces to int=1; the helper
        must refuse to signal pgid=1.
        """
        proc = MagicMock()

        # Sanity check: the dangerous coercion is still real. If a future
        # Python release changes the MagicMock __index__ contract, this
        # line will change (but the helper's guard will still be correct).
        assert int(proc.pid) == 1, (
            "MagicMock.pid no longer coerces to 1 — "
            "verify the safe_killpg guard is still needed"
        )

        # The helper must return False and must NOT invoke os.killpg.
        # We use signal 0 (liveness probe only — never delivers a kill
        # signal) for the real-pgid branch, but MagicMock should never
        # reach it.
        import nexus.util.process_group as mod
        original = getattr(os, _KILL_PRIMITIVE)
        calls: list[tuple] = []

        def _trace(target, sig):
            calls.append((target, sig))
            return original(target, sig)

        setattr(mod.os, _KILL_PRIMITIVE, _trace)
        try:
            assert safe_killpg(proc, KILL_SIGNAL) is False
        finally:
            setattr(mod.os, _KILL_PRIMITIVE, original)
        assert calls == [], (
            f"safe_killpg invoked os.{_KILL_PRIMITIVE} {calls!r} for a mock proc — "
            "the isinstance(pid, int) guard is broken"
        )

    def test_magicmock_bare_pid_returns_false(self):
        """Same contract when a bare mock is passed instead of a proc wrapper."""
        mock_pid = MagicMock()
        assert safe_killpg(mock_pid, KILL_SIGNAL) is False


class TestRealSubprocess:
    def test_real_subprocess_pgid_is_signalled(self):
        """A real child spawned with start_new_session=True must be
        signalable via safe_killpg.
        """
        proc = subprocess.Popen(
            [CHILD_PYTHON, "-c", "import time; time.sleep(10)"],
            **OWN_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if _POSIX:
                # Probe that the PID exists first (signal 0 is the portable
                # "liveness" test on POSIX — does not deliver a signal).
                assert safe_killpg(proc, 0) is True, (
                    "live subprocess is unreachable via its own pgid"
                )

            # Now actually kill it.
            assert safe_killpg(proc, KILL_SIGNAL) is True
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    def test_bare_int_pid_accepted(self):
        """Callers that hold a bare PID (e.g. reading from a PID file) can
        pass the int directly instead of wrapping in a proc-like object.
        """
        proc = subprocess.Popen(
            [CHILD_PYTHON, "-c", "import time; time.sleep(10)"],
            **OWN_GROUP,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if _POSIX:
                assert safe_killpg(proc.pid, 0) is True
            assert safe_killpg(proc.pid, KILL_SIGNAL) is True
        finally:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


class TestErrorSwallowing:
    def test_nonexistent_pid_returns_false(self):
        """PID that was reaped (or never existed) → False, no exception."""
        # Very high PID unlikely to be in use. If by chance it maps to a
        # real process, the test will still be correct — safe_killpg
        # returns True only when the signal is actually delivered.
        if _POSIX:  # sig 0 is CTRL_C_EVENT on Windows, not a probe
            assert safe_killpg(2**30 - 1, 0) in (False, True)
        # The False path is what we're testing. Force it with a sentinel
        # that OS will always reject — negative PIDs are never valid.
        assert safe_killpg(-1, KILL_SIGNAL) is False

    def test_never_raises_for_common_failure_modes(self):
        """Helper must swallow ProcessLookupError / PermissionError / OSError."""
        # Non-int — handled by the mock-guard branch.
        assert safe_killpg("not-a-pid", KILL_SIGNAL) is False
        assert safe_killpg(None, KILL_SIGNAL) is False
        # Negative int — kernel rejects.
        assert safe_killpg(-42, KILL_SIGNAL) is False


class TestNonPositivePidGuard:
    """Regression for the review-flagged Critical hazard: ``pid <= 0`` would
    route to the caller's own process group (``os.getpgid(0)`` returns the
    caller's pgid) and kill the running ``nx`` CLI. A truncated mineru
    pidfile that parses as 0 must never self-terminate the CLI.
    """

    def test_pid_zero_returns_false_without_signalling(self):
        """pid=0 must not signal the caller's own process group."""
        import nexus.util.process_group as mod
        calls: list[tuple] = []
        original_killpg = getattr(os, _KILL_PRIMITIVE)

        def _trace_killpg(pgid, sig):
            calls.append((pgid, sig))
            return original_killpg(pgid, sig)

        setattr(mod.os, _KILL_PRIMITIVE, _trace_killpg)
        try:
            assert safe_killpg(0, KILL_SIGNAL) is False
        finally:
            setattr(mod.os, _KILL_PRIMITIVE, original_killpg)
        assert calls == [], (
            f"safe_killpg(0, …) invoked os.killpg {calls!r} — the "
            "pid <= 0 guard is broken and would kill the caller's own "
            "process group"
        )

    def test_negative_pid_returns_false_without_signalling(self):
        """pid=-1 / pid=-42 / etc. must also be rejected before any kernel
        call — do not trust the OS to always reject and never reach killpg."""
        import nexus.util.process_group as mod
        calls: list[tuple] = []
        original_killpg = getattr(os, _KILL_PRIMITIVE)

        def _trace_killpg(pgid, sig):
            calls.append((pgid, sig))
            return original_killpg(pgid, sig)

        setattr(mod.os, _KILL_PRIMITIVE, _trace_killpg)
        try:
            assert safe_killpg(-1, KILL_SIGNAL) is False
            assert safe_killpg(-42, KILL_SIGNAL) is False
        finally:
            setattr(mod.os, _KILL_PRIMITIVE, original_killpg)
        assert calls == [], (
            "safe_killpg invoked os.killpg for a negative pid — guard broken"
        )


# nexus-5ny9r: sweeping by the recorded group id reaches children the
# leader abandoned via os._exit, which safe_killpg's live-pid resolution
# cannot see once the leader is reaped.


@pytest.mark.skipif(
    not _POSIX,
    reason="a process GROUP of an exited leader exists only on POSIX; on Windows safe_killpg_group "
    "refuses by design (a recorded pid is not safe to kill by number), pinned by "
    "test_group_sweep_refuses_unsafe_ids and the Windows-shaped probe below",
)
def test_group_sweep_reaches_children_of_an_exited_leader(tmp_path):
    import subprocess
    import sys
    import time

    from nexus.util.process_group import safe_killpg, safe_killpg_group

    pid_file = tmp_path / "child.pid"
    leader = subprocess.Popen(
        [sys.executable, "-c",
         "import os, subprocess, sys, time\n"
         "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
         f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
         "os._exit(0)\n"],
        start_new_session=True,
    )
    pgid = leader.pid
    leader.wait(timeout=30)
    for _ in range(50):
        if pid_file.is_file() and pid_file.read_text().strip():
            break
        time.sleep(0.1)
    child_pid = int(pid_file.read_text())
    os.kill(child_pid, 0)  # alive: the leader's os._exit abandoned it
    assert safe_killpg(leader, signal.SIGKILL) is False  # leader reaped: nothing to resolve
    assert safe_killpg_group(pgid, signal.SIGKILL) is True

    def _gone(pid: int) -> bool:
        # A killed child of a dead leader is reparented and reaped by init
        # asynchronously; until then it is a zombie, which os.kill(pid, 0)
        # still "finds". Dead-or-zombie is the property under test.
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return stat == "" or stat.startswith("Z")

    for _ in range(100):
        if _gone(child_pid):
            break
        time.sleep(0.1)
    assert _gone(child_pid), f"child {child_pid} survived the group sweep"


@pytest.mark.parametrize("pgid", [MagicMock(), "12", 0, 1, -5, True])
def test_group_sweep_refuses_unsafe_ids(pgid, monkeypatch):
    from nexus.util.process_group import safe_killpg_group

    calls: list = []
    monkeypatch.setattr(os, "killpg", lambda g, s: calls.append(g), raising=False)
    assert safe_killpg_group(pgid) is False
    assert calls == []


# nexus-34f7r: Windows has no os.killpg, os.getpgid or signal.SIGKILL. The
# helper's SIGKILL defaults raised AttributeError at IMPORT there, and every
# cleanup branch calling it threw from inside its own except/finally. The
# probe runs in a subprocess that deletes the three names BEFORE importing
# the module, because this process already holds the module with the real
# names bound. Positive control: against 2dfa8f0e9 the import itself fails.
_WINDOWS_SHAPED_PROBE = r"""
import json, os, signal, subprocess, sys, time
for name in ("killpg", "getpgid"):
    if hasattr(os, name):
        delattr(os, name)
if hasattr(signal, "SIGKILL"):
    del signal.SIGKILL
from nexus.util import process_group as pg

# The base interpreter: a venv python.exe on Windows is a launcher whose child is
# the process that sleeps, and a kill by the launcher's pid would not reach it.
child = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(60)"])
time.sleep(0.2)
group = pg.safe_killpg_group(child.pid)
alive_after_group = child.poll() is None
single = pg.safe_killpg(child)
child.wait(timeout=10)
print(json.dumps({
    "kill_signal_is_sigterm": pg.KILL_SIGNAL == signal.SIGTERM,
    "group": group,
    "alive_after_group": alive_after_group,
    "single": single,
    "returncode": child.returncode,
}))
"""


def test_windows_shaped_platform_imports_and_degrades_honestly():
    proc = subprocess.run(
        [sys.executable, "-c", _WINDOWS_SHAPED_PROBE],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    result = __import__("json").loads(proc.stdout.strip().splitlines()[-1])
    assert result["kill_signal_is_sigterm"] is True
    # No groups: refuse rather than kill a leader pid by number.
    assert result["group"] is False
    assert result["alive_after_group"] is True
    # One process: signalled directly, and reported as delivered.
    assert result["single"] is True
    # TerminateProcess's exit code is the signal number; POSIX reports it negated.
    assert result["returncode"] == (signal.SIGTERM if sys.platform == "win32" else -signal.SIGTERM)


def test_kill_signal_is_the_platforms_hard_kill():
    """SIGKILL where the platform has it; SIGTERM, which is TerminateProcess,
    on Windows, which does not."""
    assert KILL_SIGNAL == getattr(signal, "SIGKILL", signal.SIGTERM)
    assert (KILL_SIGNAL == getattr(signal, "SIGKILL", None)) is hasattr(signal, "SIGKILL")


# nexus-6y4e0: real Windows containment via a Job Object. isolation_popen_kwargs
# / contain / kill_tree dispatch on os.killpg's presence, exactly like the rest
# of this module -- monkeypatching it away (in-process; these three read it
# dynamically at call time, unlike KILL_SIGNAL's import-time binding above)
# forces the Windows branch without a real Windows box. The win_job half of
# that branch (the actual ctypes calls) is exercised by tests/test_win_job.py;
# this file only pins the DISPATCH -- which function calls which win_job
# primitive, and with what.


class TestIsolationPopenKwargsPosix:
    @pytest.mark.skipif(
        not _POSIX,
        reason="the POSIX arm dispatches on os.killpg being present; a Windows host takes the Windows arm "
        "(TestIsolationPopenKwargsWindowsShaped pins it, and runs here too)",
    )
    def test_returns_start_new_session_true(self):
        from nexus.util.process_group import isolation_popen_kwargs

        assert isolation_popen_kwargs() == {"start_new_session": True}


class TestIsolationPopenKwargsWindowsShaped:
    def test_returns_create_new_process_group_creationflags(self, monkeypatch):
        from nexus.util import process_group as pg
        from nexus.util import win_job

        monkeypatch.delattr(os, "killpg", raising=False)
        assert isinstance(pg.isolation_popen_kwargs(), dict)
        assert pg.isolation_popen_kwargs() == {
            "creationflags": win_job.CREATE_NEW_PROCESS_GROUP,
        }


class TestContainPosix:
    @pytest.mark.skipif(
        not _POSIX,
        reason="contain() is a no-op only where os.killpg exists; a Windows host takes the Job Object arm "
        "(TestContainAndKillTreeWindowsShaped pins it, and runs here too)",
    )
    def test_returns_none_without_touching_win_job(self, monkeypatch):
        """On POSIX, start_new_session=True already contains the tree via a
        process group -- contain() must be a pure no-op, never reaching for
        win_job at all."""
        from nexus.util import process_group as pg

        calls: list[str] = []
        monkeypatch.setattr(
            pg.win_job, "create_job", lambda: calls.append("create_job") or None,
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert pg.contain(proc) is None
            assert calls == []
        finally:
            proc.kill()
            proc.wait(timeout=5)


class TestContainAndKillTreeWindowsShaped:
    """Force the Windows branch by deleting os.killpg for the duration of
    the test (restored by monkeypatch's own teardown), and fake win_job's
    kernel32 so no real Windows API is needed."""

    def _windows_shaped(self, monkeypatch):
        from nexus.util import process_group as pg
        from nexus.util import win_job
        from tests.test_win_job import _FakeKernel32

        monkeypatch.delattr(os, "killpg", raising=False)
        monkeypatch.setattr(win_job, "IS_WINDOWS", True)
        fake = _FakeKernel32()
        monkeypatch.setattr(win_job, "_kernel32", fake)
        return pg, fake

    def test_contain_creates_job_and_assigns_pid(self, monkeypatch):
        pg, fake = self._windows_shaped(monkeypatch)
        job = pg.contain(4242)
        assert job is not None
        assign_call = next(
            c for c in fake.calls if c[0] == "AssignProcessToJobObject"
        )
        assert assign_call[1] == job

    def test_contain_reads_pid_off_a_proc_like_object(self, monkeypatch):
        pg, fake = self._windows_shaped(monkeypatch)
        proc = MagicMock()
        proc.pid = 4242
        job = pg.contain(proc)
        assert job is not None
        assign_call = next(
            c for c in fake.calls if c[0] == "AssignProcessToJobObject"
        )
        assert assign_call[2] in fake.closed_handles

    def test_contain_refuses_non_positive_and_non_int_pids(self, monkeypatch):
        pg, fake = self._windows_shaped(monkeypatch)
        assert pg.contain(0) is None
        assert pg.contain(-1) is None
        assert pg.contain(True) is None
        assert pg.contain(MagicMock()) is None
        assert fake.calls == []

    def test_contain_returns_none_when_job_creation_fails(self, monkeypatch):
        pg, fake = self._windows_shaped(monkeypatch)
        fake.create_ok = False
        assert pg.contain(4242) is None

    def test_contain_closes_job_when_assignment_fails(self, monkeypatch):
        pg, fake = self._windows_shaped(monkeypatch)
        fake.assign_ok = False
        assert pg.contain(4242) is None
        # The job handle created for a failed assignment must not leak.
        job_call = next(c for c in fake.calls if c[0] == "CreateJobObjectW")
        del job_call  # the handle itself isn't returned to us on this path
        assert len(fake.closed_handles) >= 1

    def test_contain_degrades_when_win_job_raises(self, monkeypatch):
        """nexus-6y4e0 review: win_job's own exception guard covers its
        three functions individually; this pins that contain() -- the
        caller one level up -- sees only the degraded None/False return
        and never a propagated exception, end to end."""
        pg, fake = self._windows_shaped(monkeypatch)
        fake.raise_from.add("CreateJobObjectW")
        assert pg.contain(4242) is None

    def test_kill_tree_with_a_job_closes_it_and_never_calls_safe_killpg(
        self, monkeypatch,
    ):
        pg, fake = self._windows_shaped(monkeypatch)
        job = pg.contain(4242)
        assert job is not None

        killpg_calls: list = []
        monkeypatch.setattr(
            pg, "safe_killpg", lambda *a, **kw: killpg_calls.append((a, kw)) or True,
        )
        assert pg.kill_tree(4242, job) is True
        assert job in fake.closed_handles
        assert killpg_calls == []

    def test_kill_tree_without_a_job_falls_back_to_safe_killpg(self, monkeypatch):
        pg, _fake = self._windows_shaped(monkeypatch)
        calls: list = []
        monkeypatch.setattr(
            pg, "safe_killpg", lambda *a, **kw: calls.append((a, kw)) or True,
        )
        assert pg.kill_tree(4242, None) is True
        assert calls == [((4242,), {})] or calls[0][0][0] == 4242


def test_kill_tree_on_posix_with_no_job_delegates_to_safe_killpg(monkeypatch):
    from nexus.util import process_group as pg

    calls: list = []
    monkeypatch.setattr(
        pg, "safe_killpg", lambda *a, **kw: calls.append((a, kw)) or True,
    )
    assert pg.kill_tree(4242, None) is True
    assert calls
