# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Behaviour of :mod:`nexus.bounded_subprocess` (nexus-t10nc).

The load-bearing test is :func:`test_timed_out_call_leaves_no_orphan`, and
it is written as a DIFFERENTIAL against plain ``subprocess.run``: same
argv, same timeout, one bounded and one not. A test that only asserted
``run_bounded`` raises ``TimeoutExpired`` would pass against a one-line
wrapper forwarding to ``subprocess.run``, and would prove nothing about the
defect this module exists for.

WHAT THE DIFFERENTIAL OBSERVES, and why it is not "the stock call hangs":
an earlier draft of this file asserted exactly that, and it FAILED — on
macOS the stock call returned at 1.00s against its 1.0s timeout. Four
grandchild-holding-the-pipe shapes were then probed and none blocked — a
finding that lived only in prose until a fix-check pass noticed it had no
artifact, and that is now
:func:`test_stock_subprocess_run_does_not_hang_on_posix` below.
CPython 3.12.11's ``subprocess.run`` puts the unbounded post-kill
``communicate()`` inside ``if _mswindows`` and calls ``process.wait()`` on
POSIX, so the hang is Windows-only. The POSIX defect is a LEAK: ``wait()``
reaps the direct child and leaves its descendants running. That is what
this differential observes, because it is what is observable here. See the
module docstring for the full correction.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time

import pytest

from nexus.bounded_subprocess import kill_child_and_descendants, run_bounded
from nexus.util.process_group import safe_killpg_group
from tests._child_process import pid_alive
from tests._module_seam import delattr_in
from tests.test_win_job import live_children, reap_live_children

#: A child that spawns a grandchild inheriting the stdout pipe, then exits
#: immediately itself. Killing only the direct child -- which is what
#: CPython does on TimeoutExpired -- leaves the grandchild running and
#: holding the pipe's write end. This is the defect, in one argv.
_ORPHAN_GRANDCHILD = ["sh", "-c", "sleep 30 & exit 0"]

_BOUND_S = 1.0

posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="process groups; the Windows branch is tested by monkeypatch below",
)


def _marker_pattern(marker: str) -> str:
    """A pgrep/pkill pattern that matches ``sleep <marker>`` and nothing else (ERE, anchored)."""
    return "sleep " + marker.replace(".", "[.]") + "$"


def _sleeper_alive(marker: str) -> bool:
    """True while a ``sleep <marker>`` process is running."""
    found = subprocess.run(  # noqa: S603 - test-local probe
        ["pgrep", "-f", _marker_pattern(marker)], capture_output=True, text=True, timeout=10
    )
    return found.returncode == 0


#: The four shapes probed when the POSIX/Windows split was established. Each
#: leaves a grandchild holding the stdout pipe; on Windows each would hang
#: the post-kill drain. The claim they support is NEGATIVE — that none of
#: them hangs on POSIX — which is why there are four rather than one.
_POSIX_NON_HANG_SHAPES: dict[str, list[str]] = {
    "sh_backgrounds_and_exits": ["sh", "-c", "sleep 30 & exit 0"],
    "sh_backgrounds_and_stays": ["sh", "-c", "sleep 30 & sleep 30"],
    "python_grandchild": [
        sys.executable,
        "-c",
        "import subprocess,sys;subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);raise SystemExit(0)",
    ],
    "explicit_stdout_redirect": ["sh", "-c", "sleep 30 >&1 & exit 0"],
}


@posix_only
@pytest.mark.parametrize("shape", sorted(_POSIX_NON_HANG_SHAPES))
def test_stock_subprocess_run_does_not_hang_on_posix(shape: str) -> None:
    """Stock ``subprocess.run`` honours its timeout on POSIX, every shape.

    THIS TEST EXISTS BECAUSE ITS CLAIM WAS PROSE. The RDR-218 text, this
    module's docstring and ``bounded_subprocess.py`` all asserted "four
    distinct grandchild-holding-the-pipe shapes all returned at 1.00s
    against a 1.0s timeout" on the strength of an interactive probe that
    was never recorded anywhere runnable. A fix-check pass caught that the
    figure appeared in three co-shipped prose sites and no artifact. This
    is the artifact.

    The claim is NEGATIVE — that the drain hang does not occur here — so it
    is asserted against the STOCK call, not against ``run_bounded``. If
    CPython ever moves the untimed post-kill ``communicate()`` out from
    under ``if _mswindows``, this goes red and the POSIX half of the
    module's account needs rewriting.
    """
    argv = _POSIX_NON_HANG_SHAPES[shape]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603 - the stock call is the subject here
            argv, capture_output=True, text=True, timeout=_BOUND_S
        )
    elapsed = time.monotonic() - started

    assert elapsed < _BOUND_S + 2.0, (
        f"stock subprocess.run took {elapsed:.2f}s against a {_BOUND_S}s timeout "
        f"for shape {shape!r} -- the post-kill drain blocked, which on POSIX it "
        "is not supposed to do. The module docstring's Windows-only claim is "
        "now wrong and needs re-deriving from CPython's current source."
    )


def _sweep(*markers: str) -> None:
    """Kill this run's own ``sleep <marker>`` processes.

    Each marker is unique to one run (``_run_marker``), so this never reaches another run's
    sleeper: CI jobs on hellmini share one user and one PID namespace, as chas's did until its
    lab-runner gained PrivatePIDs on 2026-11-02, and a fixed marker let one job's sweep kill
    another's (nexus-79yg6, nexus-8byc4).
    """
    for marker in markers:
        subprocess.run(  # noqa: S603 - test-local cleanup of this run's own marker
            ["pkill", "-f", _marker_pattern(marker)], capture_output=True, timeout=10
        )


def _run_marker(tag: int) -> str:
    """A sleep duration unique to this run: about two minutes, so a sleeper an interrupted run
    leaves behind ends on its own; pid and clock make it distinct from any other run's."""
    return f"120.{os.getpid()}{time.monotonic_ns() % 10**6:06d}{tag}"


def _stock_run_times_out(argv: list[str]) -> tuple[bool, str]:
    """Whether stock subprocess.run times out on *argv*; when it does not, what it returned."""
    started = time.monotonic()
    try:
        r = subprocess.run(  # noqa: S603 - this is the defect under test, deliberately
            argv, capture_output=True, text=True, timeout=_BOUND_S
        )
    except subprocess.TimeoutExpired:
        return True, ""
    return False, (f"returned in {time.monotonic() - started:.2f}s, rc={r.returncode}, "
                   f"stdout={r.stdout[-300:]!r}, stderr={r.stderr[-300:]!r}")


@posix_only
def test_timed_out_call_leaves_no_orphan() -> None:
    """run_bounded kills the grandchild; stock subprocess.run orphans it.

    Both calls get the same argv and the same 1.0s timeout. The stock one
    reaps only its direct child, so the grandchild survives; the bounded
    one group-kills, so it does not. That difference IS the POSIX defect.
    """
    # The marker is the sleep DURATION, which is the one part of the
    # grandchild's argv that survives into the running process. Two earlier
    # drafts learned this the hard way: a bare extra argv word made it
    # `sleep 30 <marker>`, a usage error that exited instantly; moving the
    # marker into a trailing `# comment` failed differently, because sh
    # exec-optimizes a single-command `-c` string and the comment never
    # reaches the process table. A distinctive duration cannot be dropped.
    stock_marker = _run_marker(1)
    bounded_marker = _run_marker(2)

    def _argv(marker: str) -> list[str]:
        # The `sleep` inherits the stdout pipe and outlives its parent sh.
        return ["sh", "-c", f"sleep {marker} & exit 0"]

    # The markers are unique to this run and about two minutes long, so a sleeper an
    # interrupted run leaves behind (an interrupt, or a falsification probe against a degraded
    # helper, which is how a fixed marker once bit) ends on its own and never matches the NEXT
    # run's markers; no sweep before the run is needed, and none can reach another run's.

    # Stock: times out, reaps the direct child, orphans the grandchild. Both CI misses
    # (nexus-79yg6 on 10-25, nexus-8byc4 on 11-01, each a py3.13 shard on chas while chas's CI
    # jobs still shared one PID namespace and one uid) were most probably a concurrent twin
    # shard's sweep of the then-fixed marker killing this run's sleeper inside the 1 s wait: the
    # mechanism reproduces and the twins' progress lines overlap the misses, but no log shows the
    # kill itself. The per-run marker removes that mechanism, so there is no retry here: a miss
    # is a real change in the premise, and ``detail`` carries what the next one needs.
    timed_out, detail = _stock_run_times_out(_argv(stock_marker))
    if not timed_out:
        _sweep(stock_marker)
        pytest.fail("stock subprocess.run did not time out on a backgrounded sleeper holding its "
                    "stdout, so the premise of this differential no longer holds: " + detail)

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded(_argv(bounded_marker), timeout=_BOUND_S)
    bounded_elapsed = time.monotonic() - started

    time.sleep(0.5)  # let both kills land
    try:
        # The premise. If the stock grandchild is NOT alive, this test has
        # stopped exercising the defect -- CPython changed, or this shell
        # stopped passing stdout to background jobs -- and the failure says
        # to re-derive it rather than to trust a green run.
        assert _sleeper_alive(stock_marker), (
            "stock subprocess.run left no orphan, so the premise of this "
            "differential no longer holds; re-derive it before trusting it"
        )
        # The claim.
        assert not _sleeper_alive(bounded_marker), (
            "run_bounded left an orphaned grandchild -- the group kill did not reach it"
        )
    finally:
        _sweep(stock_marker, bounded_marker)

    assert bounded_elapsed < _BOUND_S + 3.0, (
        f"run_bounded took {bounded_elapsed:.1f}s against a {_BOUND_S}s timeout"
    )


@posix_only
def test_kill_reaches_the_group_not_just_the_child() -> None:
    """A group kill leaves no surviving grandchild."""
    proc = subprocess.Popen(  # noqa: S603
        _ORPHAN_GRANDCHILD,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    time.sleep(0.5)  # let sh fork the grandchild and exit
    # start_new_session=True makes the child its own group leader, so this
    # holds without asking getpgid -- which on macOS raises
    # ProcessLookupError for the now-zombie sh. See the helper's docstring.
    pgid = proc.pid

    assert kill_child_and_descendants(proc) == "group"

    # Reap the direct child first. A zombie is still a group member, and
    # macOS answers a signal aimed at a group of zombies with EPERM rather
    # than ESRCH, so probing before the reap tests the reaping, not the
    # kill.
    proc.wait(timeout=5)

    # Nothing is left in that group -- in particular not the grandchild,
    # which is the whole point. os.killpg with signal 0 is the probe.
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_windows_branch_reports_process_reach_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With ``os.killpg`` absent, the helper kills the child and says so.

    This is the branch nexus-34f7r builds on. The specific regression it
    pins: ``os.killpg`` raises ``AttributeError`` on Windows, NOT
    ``OSError``, so an OSError-only handler is a silent no-op there. If the
    absent-primitive check is removed, this test raises AttributeError
    instead of returning.
    """
    delattr_in(monkeypatch, ("nexus.bounded_subprocess", "nexus.util.process_group"), "os.killpg", raising=True)

    killed: list[bool] = []

    class _FakeProc:
        pid = 4321
        args = ["fake"]

        def kill(self) -> None:
            killed.append(True)

    assert kill_child_and_descendants(_FakeProc()) == "process"  # type: ignore[arg-type]
    assert killed == [True], (
        "the direct child must still be killed when there is no group"
    )


def test_windows_branch_with_a_job_closes_it_for_group_reach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nexus-6y4e0: with a job handle (from ``process_group.contain`` at
    spawn), the Windows branch closes the JOB rather than killing only the
    direct child, and reports the stronger ``"group"`` reach -- the
    tree-kill this bead adds. Windows-shaped via a fake kernel32 (see
    tests/test_win_job.py); no real Windows box involved.
    """
    from nexus.util import win_job
    from tests.test_win_job import _FakeKernel32

    delattr_in(monkeypatch, ("nexus.bounded_subprocess", "nexus.util.process_group"), "os.killpg", raising=True)
    monkeypatch.setattr(win_job, "IS_WINDOWS", True)
    fake = _FakeKernel32()
    monkeypatch.setattr(win_job, "_kernel32", fake)

    killed: list[bool] = []

    class _FakeProc:
        pid = 4321
        args = ["fake"]

        def kill(self) -> None:
            killed.append(True)

    job = fake._mint()
    assert kill_child_and_descendants(_FakeProc(), job) == "group"  # type: ignore[arg-type]
    assert job in fake.closed_handles
    assert killed == [], (
        "the direct child must NOT be separately .kill()ed when the job "
        "close already reached the whole tree"
    )


def test_windows_branch_falls_back_to_process_when_job_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job handle that fails to close (already gone, or a genuine API
    failure) must not be treated as success -- fall back to killing the
    direct child, same as having no job at all."""
    from nexus.util import win_job
    from tests.test_win_job import _FakeKernel32

    delattr_in(monkeypatch, ("nexus.bounded_subprocess", "nexus.util.process_group"), "os.killpg", raising=True)
    monkeypatch.setattr(win_job, "IS_WINDOWS", True)
    fake = _FakeKernel32()
    fake.close_ok = False
    monkeypatch.setattr(win_job, "_kernel32", fake)

    killed: list[bool] = []

    class _FakeProc:
        pid = 4321
        args = ["fake"]

        def kill(self) -> None:
            killed.append(True)

    job = fake._mint()
    assert kill_child_and_descendants(_FakeProc(), job) == "process"  # type: ignore[arg-type]
    assert killed == [True]


def test_already_dead_child_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class _GoneProc:
        pid = 4321
        args = ["fake"]

        def kill(self) -> None:
            raise ProcessLookupError

    delattr_in(monkeypatch, ("nexus.bounded_subprocess", "nexus.util.process_group"), "os.killpg", raising=True)
    assert kill_child_and_descendants(_GoneProc()) == "none"  # type: ignore[arg-type]


def test_returns_completed_process_on_success() -> None:
    result = run_bounded([sys.executable, "-c", "print('ok')"], timeout=30)
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and signals")
@pytest.mark.parametrize("kill_on_interrupt", [True, False], ids=["kill", "default-leaves-it"])
def test_kill_on_interrupt_reaps_a_child_that_ignores_signals(tmp_path, kill_on_interrupt: bool) -> None:
    """nexus-j738q: an interrupt of the wait kills the child's group only when asked.

    The child ignores INT/TERM/HUP, so only the group kill ends it. By default an interrupt leaves it running
    (the pre-existing behaviour every other caller keeps).
    """
    import signal  # noqa: PLC0415
    import threading  # noqa: PLC0415

    pidfile = tmp_path / "pid"
    script = f"trap '' INT TERM HUP; echo $$ > {pidfile}; while :; do sleep 0.05; done"

    def fire() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip():
                time.sleep(0.3)
                os.kill(os.getpid(), signal.SIGINT)  # a real signal: it ends the main thread's select
                return
            time.sleep(0.02)

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)  # CI starts pytest with SIGINT ignored
    pid = None
    try:
        threading.Thread(target=fire, daemon=True).start()
        with pytest.raises(KeyboardInterrupt):
            run_bounded(["/bin/sh", "-c", script], timeout=60, kill_on_interrupt=kill_on_interrupt)
        pid = int(pidfile.read_text(encoding="utf-8"))
        assert pid_alive(pid) is (not kill_on_interrupt)
    finally:
        signal.signal(signal.SIGINT, previous)
        if pid is not None:
            safe_killpg_group(pid)  # only does anything if the code under test left the child running


def test_check_true_raises_called_process_error() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run_bounded(
            [sys.executable, "-c", "raise SystemExit(3)"], timeout=30, check=True
        )


def test_input_is_delivered() -> None:
    result = run_bounded(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(sys.stdin.read().upper())",
        ],
        timeout=30,
        input="hello",
    )
    assert result.stdout == "HELLO"


def test_input_and_stdin_together_is_refused() -> None:
    with pytest.raises(ValueError, match="not both"):
        run_bounded(["true"], timeout=1, input="x", stdin=subprocess.DEVNULL)


# nexus-6y4e0 review (critic + code-review, commits 0373d414e..165390205):
# contain()'s job handle must close on EVERY outcome, not only the
# except-TimeoutExpired branch. run_bounded's own body called
# kill_child_and_descendants(proc, job) ONLY on TimeoutExpired, so the
# success and non-zero-return paths -- which never raise it -- leaked the
# handle every time. These drive run_bounded() ITSELF, end to end, against
# a real subprocess, Windows-shaped via a fake kernel32 (real Windows
# containment is verified on qwentescence separately -- see nexus-6y4e0's
# closing report for what that could and could not cover).


@pytest.fixture
def windows_shaped_real_spawn(monkeypatch: pytest.MonkeyPatch):
    """Route contain()/kill_tree() through win_job's Windows branch via a
    fake kernel32, while keeping the ACTUAL Popen spawn kwargs empty --
    this box is not Windows, so ``creationflags=`` would raise
    ``ValueError``, and the subject under test here is the job-HANDLE
    lifecycle, not platform spawn-kwarg selection (already covered by
    TestIsolationPopenKwargsWindowsShaped in
    tests/test_process_group_safety.py).
    """
    from nexus.util import process_group as pg
    from nexus.util import win_job
    from tests.test_win_job import _FakeKernel32

    delattr_in(monkeypatch, ("nexus.bounded_subprocess", "nexus.util.process_group"), "os.killpg", raising=False)
    delattr_in(monkeypatch, ("nexus.bounded_subprocess", "nexus.util.process_group"), "os.getpgid", raising=False)
    monkeypatch.setattr(win_job, "IS_WINDOWS", True)
    fake = _FakeKernel32()
    monkeypatch.setattr(win_job, "_kernel32", fake)
    monkeypatch.setattr(pg, "isolation_popen_kwargs", lambda: {})
    yield fake
    # The fake kernel32 closes the job without terminating anything, so a
    # child the code under test "killed" is in fact still running. Each
    # test reaps its own children; this check fails the test if one is
    # still alive afterwards, and kills it anyway so a failure leaks nothing.
    leaked = live_children(fake.opened_pids())
    reap_live_children(leaked)
    assert not leaked, f"child pid(s) {leaked} still alive after the test"


def _job_handle_from(fake) -> int:
    assign_call = next(c for c in fake.calls if c[0] == "AssignProcessToJobObject")
    return assign_call[1]


class TestJobHandleClosesOnEveryOutcome:
    def test_success_closes_the_job_exactly_once(
        self, windows_shaped_real_spawn,
    ) -> None:
        fake = windows_shaped_real_spawn
        run_bounded([sys.executable, "-c", "print('ok')"], timeout=10)
        job = _job_handle_from(fake)
        assert fake.closed_handles.count(job) == 1, (
            f"job {job} closed {fake.closed_handles.count(job)} times on the "
            "success path (want exactly 1) -- closed_handles="
            f"{fake.closed_handles}"
        )

    def test_nonzero_exit_closes_the_job_exactly_once(
        self, windows_shaped_real_spawn,
    ) -> None:
        fake = windows_shaped_real_spawn
        run_bounded(
            [sys.executable, "-c", "import sys; sys.exit(3)"], timeout=10,
        )
        job = _job_handle_from(fake)
        assert fake.closed_handles.count(job) == 1, (
            f"job {job} closed {fake.closed_handles.count(job)} times on a "
            "non-zero exit (want exactly 1) -- closed_handles="
            f"{fake.closed_handles}"
        )

    def test_timeout_closes_the_job_exactly_once(
        self, windows_shaped_real_spawn,
    ) -> None:
        fake = windows_shaped_real_spawn
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                run_bounded(
                    [sys.executable, "-c", "import time; time.sleep(999)"],
                    timeout=0.2,
                )
        finally:
            # The fake job close terminated nothing: the child is ours to kill.
            reap_live_children(fake.opened_pids())
        job = _job_handle_from(fake)
        assert fake.closed_handles.count(job) == 1, (
            f"job {job} closed {fake.closed_handles.count(job)} times on "
            "timeout (want exactly 1) -- closed_handles="
            f"{fake.closed_handles}"
        )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="drives a fake kernel32 on a non-Windows host (the fixture's own teardown check misreads real Windows "
    "pids, as it does for TestJobHandleClosesOnEveryOutcome); the real-Windows evidence is "
    "tests/mcp/test_setup_tools.py::test_the_real_servers_setup_detaches_a_run_that_outlives_the_call",
)
class TestUncontained:
    """``contained=False`` (nexus-sv2wx.16): a caller whose child must outlive the
    call. ``contain`` puts the child in a KILL_ON_JOB_CLOSE job with no breakaway
    and ``run_bounded`` closes it on return, which would kill what the child
    started, so such a caller asks for no job at all."""

    def test_no_job_is_created_or_assigned(self, windows_shaped_real_spawn) -> None:
        fake = windows_shaped_real_spawn
        done = run_bounded([sys.executable, "-c", "print('ok')"], timeout=10, contained=False)
        assert done.stdout.strip() == "ok"
        names = {call[0] for call in fake.calls}
        assert not names & {"CreateJobObjectW", "AssignProcessToJobObject", "SetInformationJobObject"}, fake.calls
        assert not fake.closed_handles, "nothing was opened, so nothing may be closed"

    def test_contained_is_the_default(self, windows_shaped_real_spawn) -> None:
        fake = windows_shaped_real_spawn
        run_bounded([sys.executable, "-c", "print('ok')"], timeout=10)
        assert any(call[0] == "AssignProcessToJobObject" for call in fake.calls)

    def test_a_timeout_still_kills_the_direct_child(self, windows_shaped_real_spawn) -> None:
        fake = windows_shaped_real_spawn
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, "-c", "import time; time.sleep(999)"], timeout=0.2, contained=False)
        finally:
            reap_live_children(fake.opened_pids())


def test_run_bounded_still_succeeds_when_win_job_raises(
    windows_shaped_real_spawn,
) -> None:
    """nexus-6y4e0 review: contain()'s degrade-on-exception contract must
    hold from run_bounded's own vantage point too -- a raising kernel32
    must not turn "containment failed" into "the whole dispatch failed".
    The spawn and the wait/communicate below are real; only job-object
    containment is faked and made to fail as loudly as ctypes can."""
    fake = windows_shaped_real_spawn
    fake.raise_from.add("CreateJobObjectW")
    result = run_bounded([sys.executable, "-c", "print('ok')"], timeout=10)
    assert result.stdout == "ok\n"
    assert not any(c[0] == "AssignProcessToJobObject" for c in fake.calls), (
        "contain() should never reach AssignProcessToJobObject once "
        "CreateJobObjectW itself raised"
    )


def test_run_markers_are_unique_to_the_run() -> None:
    """Two runs (or two calls in one run) never share a sleeper marker, so one sweep cannot kill
    another run's sleeper (nexus-8byc4)."""
    a, b = _run_marker(1), _run_marker(1)
    assert a != b and str(os.getpid()) in a
    assert 120 <= float(a) < 121  # an interrupted run's leftover sleeper ends on its own


@posix_only
def test_another_runs_sweep_never_reaches_this_runs_sleeper() -> None:
    """The cause of both CI misses, as a behaviour (nexus-79yg6, nexus-8byc4).

    A second process stands in for a concurrent run on the same box: it mints its marker and
    sweeps it, exactly as that run's cleanup does. This run's sleeper must survive. With a
    marker that does not depend on the run (the fixed ``30111`` both misses ran under), the
    twin's sweep kills it, which is what made the stock call return inside its 1 s wait.
    """
    mine = _run_marker(1)
    proc = subprocess.Popen(  # noqa: S603 - test-local sleeper, swept below
        ["sh", "-c", f"sleep {mine} & exit 0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=10)
    try:
        # The backgrounded sleep may still be between fork and exec when sh exits.
        started = time.monotonic() + 2.0
        while not _sleeper_alive(mine) and time.monotonic() < started:
            time.sleep(0.05)
        assert _sleeper_alive(mine), "the sleeper never started, so this test proves nothing"
        twin = subprocess.run(  # noqa: S603 - the twin run, same box, same user
            [sys.executable, "-c",
             "from tests.test_bounded_subprocess import _run_marker, _sweep; "
             "m = _run_marker(1); _sweep(m); print(m)"],
            capture_output=True, text=True, timeout=60,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        )
        assert twin.returncode == 0, twin.stderr[-2000:]
        time.sleep(0.3)  # let a kill, had there been one, land
        assert _sleeper_alive(mine), (
            f"the twin run's sweep of its marker {twin.stdout.strip()!r} killed this run's "
            f"sleeper {mine!r}: concurrent runs on one box can fail each other's stock leg"
        )
    finally:
        _sweep(mine)


def test_the_marker_pattern_matches_only_that_sleeper() -> None:
    pat = re.compile(_marker_pattern("120.4321"))
    assert pat.search("sleep 120.4321")
    assert not pat.search("sleep 120x4321")  # the dot is literal
    assert not pat.search("sleep 120.43210")  # anchored at the end
    assert not pat.search("pkill -f sleep 120[.]4321$")  # the sweep's own command line


@posix_only
def test_reap_group_on_return_kills_what_a_child_that_exited_left_behind() -> None:
    """labrun-pex: a child that exits on its own leaves its group alive on POSIX. Windows' default
    containment kills the job on every return; POSIX killed the group only on a timeout. The doctor's
    MinerU real-parse probe child left MinerU's render-executor pool running that way (0.28 GB a worker,
    alive to the end of a test shard). ``reap_group_on_return=True`` kills the group when the child is
    done. The first call is the premise: without the flag the grandchild survives the return."""
    kept, reaped = _run_marker(7), _run_marker(8)
    try:
        run_bounded(["sh", "-c", f"sleep {kept} >/dev/null 2>&1 & exit 0"], timeout=10,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run_bounded(["sh", "-c", f"sleep {reaped} >/dev/null 2>&1 & exit 0"], timeout=10,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, reap_group_on_return=True)
        time.sleep(0.3)
        assert _sleeper_alive(kept), "premise: without the flag the grandchild must outlive the call"
        assert not _sleeper_alive(reaped), "reap_group_on_return left the child's grandchild running"
    finally:
        _sweep(kept, reaped)
