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
grandchild-holding-the-pipe shapes were then probed and none blocked.
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
    monkeypatch.delattr(os, "killpg", raising=True)

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


def test_already_dead_child_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    class _GoneProc:
        pid = 4321
        args = ["fake"]

        def kill(self) -> None:
            raise ProcessLookupError

    monkeypatch.delattr(os, "killpg", raising=True)
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
