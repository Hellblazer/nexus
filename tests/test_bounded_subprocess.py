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
import subprocess
import sys
import time

import pytest

from nexus.bounded_subprocess import kill_child_and_descendants, run_bounded
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


def _sleeper_alive(marker: str) -> bool:
    """True while a ``sleep <marker>`` process is running."""
    found = subprocess.run(  # noqa: S603 - test-local probe
        ["pgrep", "-f", f"sleep {marker}"], capture_output=True, text=True, timeout=10
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
    """Kill any ``sleep <marker>`` left over from a previous run."""
    for marker in markers:
        subprocess.run(  # noqa: S603 - test-local cleanup
            ["pkill", "-f", f"sleep {marker}"], capture_output=True, timeout=10
        )


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
    stock_marker = "30111"
    bounded_marker = "30222"

    def _argv(marker: str) -> list[str]:
        # The `sleep` inherits the stdout pipe and outlives its parent sh.
        return ["sh", "-c", f"sleep {marker} & exit 0"]

    # Both markers are swept BEFORE the run as well as after. This test
    # deliberately creates a surviving process, and anything that stops it
    # mid-way -- an interrupt, or a falsification probe run against a
    # degraded helper, which is exactly how this bit -- leaves one behind
    # that makes the NEXT run fail on a stale process rather than on the
    # code under test.
    _sweep(stock_marker, bounded_marker)

    # Stock: times out, reaps the direct child, orphans the grandchild.
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603 - this is the defect under test, deliberately
            _argv(stock_marker), capture_output=True, text=True, timeout=_BOUND_S
        )

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
