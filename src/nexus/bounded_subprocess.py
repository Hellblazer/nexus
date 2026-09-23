# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""A ``subprocess.run`` whose ``timeout=`` actually bounds the call.

THE DEFECT THIS EXISTS FOR (nexus-t10nc), and a CORRECTION to the account
in that bead. The shape under suspicion is::

    subprocess.run(argv, capture_output=True, timeout=5)

nexus-t10nc's census found 81 sites of it in ``src/nexus`` and exactly one
that did it correctly. The bead states the mechanism as: on
``TimeoutExpired`` CPython kills the direct child and then keeps draining
the pipes until EOF, which never arrives while a grandchild holds the write
end, so ``timeout=`` bounds nothing.

THAT IS TRUE ON WINDOWS AND FALSE ON POSIX, and the difference was measured
here rather than reasoned about. ``subprocess.run`` in CPython 3.12.11
reads::

    except TimeoutExpired as exc:
        process.kill()
        if _mswindows:
            exc.stdout, exc.stderr = process.communicate()   # NO timeout
        else:
            process.wait()                                   # child only
        raise

So the unbounded post-kill drain is inside ``if _mswindows``. On POSIX the
call is ``process.wait()`` on a just-SIGKILLed direct child. ``wait()`` is
itself untimed, so the step that makes it finite is worth stating: the child
is already dying when it is called, and no descendant can delay it — where
the Windows ``communicate()`` waits on the PIPE, which any descendant can
hold open.

That is pinned by
``tests/test_bounded_subprocess.py::test_stock_subprocess_run_does_not_hang_on_posix``,
which runs four grandchild-holding-the-pipe shapes against the STOCK call
and asserts each returns inside its own timeout. It is a test rather than a
sentence here because this claim first shipped as prose in three places at
once, resting on an interactive probe that was recorded nowhere runnable,
and a fix-check pass caught that. It goes red if CPython moves the drain out
from under ``if _mswindows``.

The two platforms therefore have DIFFERENT defects, and this module fixes
both for different reasons:

* WINDOWS — a genuine unbounded hang. The post-kill ``communicate()`` has
  no timeout and blocks until every holder of the pipe's write end exits.
  This is where the one MEASURED instance lives: ``hooks/verification_
  config.py`` ran ``git -C ... rev-parse --git-common-dir`` with a 5.0s
  timeout and was sampled still blocked at 25s (nexus-c1, on qwentescence,
  from a stack dump of the already-bounded hook). That is what
  ``hooks.json`` wires on Stop, which is why a Windows session answers and
  then sits. :func:`run_bounded` reaps with an explicit
  ``timeout=REAP_TIMEOUT_S``, so the drain is bounded on Windows too.

* POSIX — not a hang, a LEAK. ``process.wait()`` reaps the direct child and
  says nothing about its descendants, so every timed-out call can leave
  grandchildren running indefinitely. :func:`run_bounded` group-kills, so
  they do not survive the bound.

THE FINDING THAT STILL DOES NOT FIT, kept because it is the only one that
was actually measured and because smoothing it over is how this area
produced two confident wrong mechanisms in two days. ``rev-parse`` with
stdout piped is leaf-shaped: the pager is suppressed, no hooks run, no
credential helper runs. So a git-spawned grandchild does not explain the
25s block, and on Windows the write end was held by something that is not a
descendant of that git. A labelled HYPOTHESIS — Windows handle inheritance
leaking a pipe write end into a SIBLING process spawned concurrently from
another thread, which the MCP server's hook path does by construction — is
carried in nexus-t10nc and in RDR-218 Gap 4. Nobody has run the two-thread
experiment that would settle it. This module bounds the symptom either way,
which is not the same as having explained it.

WHY A SHARED HELPER RATHER THAN 81 FIXES. The correct pattern already
existed, in exactly one place: ``aspect_extractor._run_claude_isolated``
sets ``start_new_session=True`` and group-kills before reaping. It has been
right since it was written and nothing else reached for it, because there
was nothing to reach for. That is the same shape as ``_locking.py`` being
bypassed by a direct ``fcntl`` import — a correct pattern with no reusable
form gets re-derived or, more often, not.

THE RESIDUAL, inherited from that original and not solved here: a
grandchild that calls ``setsid`` and creates its OWN session is in a
different process group and is not reached by the group kill. Bounding that
needs a process-tree walk or a cgroup, neither of which is justified by any
site this repo has.

WINDOWS (the branch nexus-34f7r builds on; do not widen it without that
bead). Two facts, both of which an ``except OSError`` gets wrong:

* ``os.killpg`` does not EXIST on Windows. It raises ``AttributeError``,
  not ``OSError``/``ProcessLookupError``, so a killpg wrapped in an
  OSError-only handler is a silent no-op there rather than a fallback.
* ``start_new_session=True`` is ACCEPTED AND IGNORED on Windows. There is
  no session, so there is no group to kill.

The branch therefore cannot be "kill the group, fall back to the process".
It is "there is no group; kill the process and say so" — :func:`kill_child_
and_descendants` returns the reach it actually achieved, and the caller
logs it, so a Windows timeout is visibly weaker rather than quietly weaker.

The known Windows equivalent is a Job Object, or ``CREATE_NEW_PROCESS_
GROUP`` plus ``taskkill /T /F``. Neither is implemented here and this is
deliberate: nobody on this project has run either on Windows, adding an
unverified ``taskkill`` spawn INSIDE a timeout handler adds a second
unbounded call to the path that is already hanging, and this repo has spent
two days on confident wrong mechanisms in this exact area. It belongs to
nexus-34f7r, behind a measurement.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Mapping, Sequence
from typing import IO, Any, Literal

import structlog

_log = structlog.get_logger(__name__)

#: Seconds allowed for the post-kill reap. The group is already SIGKILLed
#: when this starts, so it bounds a drain of pipes nobody is writing to any
#: more; it exists so a pathological case cannot re-hang the handler.
REAP_TIMEOUT_S: float = 5.0

#: What :func:`kill_child_and_descendants` managed to reach. ``"group"`` is
#: the child and every descendant that stayed in its process group;
#: ``"process"`` is the direct child only, which is all Windows can do and
#: all that is left on POSIX when the group is already gone.
KillReach = Literal["group", "process", "none"]


def kill_child_and_descendants(proc: subprocess.Popen[Any]) -> KillReach:
    """SIGKILL ``proc``'s whole process group, returning the reach achieved.

    This is the single named platform branch for "stop this child and
    everything under it". nexus-34f7r's audit of the process primitives
    builds on this function rather than beside it, so the AttributeError
    fact below has one home.

    Returns ``"group"`` when the process group was signalled, ``"process"``
    when only the direct child could be killed (always the case on Windows,
    where ``os.killpg`` does not exist), and ``"none"`` when the child was
    already gone.

    CALLER CONTRACT: ``proc`` was spawned with ``start_new_session=True``,
    which is what :func:`run_bounded` does. That makes the child a session
    and group leader, so its pgid EQUALS its pid, and the second lookup
    below relies on it.

    THE macOS DIVERGENCE, measured here and not guessed: ``os.getpgid`` on
    a child that has exited but not been reaped raises
    ``ProcessLookupError`` on macOS where it succeeds on Linux. That is not
    an edge case for this function -- it is the MAIN case. The scenario a
    group kill exists for is "the direct child exited and its grandchildren
    survive holding the pipe", and in that scenario the direct child is
    precisely a zombie. A first draft of this function called ``getpgid``
    and fell back to ``proc.kill()`` on failure, so on macOS it degraded to
    killing only the already-dead child and left the grandchild holding the
    pipe for the whole reap -- the exact defect this module was written to
    remove, reinstated by its own error handler. The differential test is
    what caught it; a test that only asserted ``TimeoutExpired`` would have
    passed on the broken version.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        # Windows. Not a failure to be retried or fallen back from -- the
        # primitive is absent, and start_new_session was ignored at spawn,
        # so there was never a group. Kill the child and report the weaker
        # reach honestly.
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            return "none"
        return "process"

    # Ask the OS for the group while the child is still alive to be asked.
    try:
        killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return "group"
    except (ProcessLookupError, PermissionError, OSError):
        pass

    # The child is a zombie (macOS) or otherwise unlookupable. Under the
    # caller contract above its pid IS its pgid, and the group can still
    # hold live grandchildren, so signal it directly rather than giving up
    # on the group.
    try:
        killpg(proc.pid, signal.SIGKILL)
        return "group"
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        return "none"
    return "process"


def run_bounded(
    argv: Sequence[str],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: str | bytes | None = None,  # noqa: A002 - mirrors subprocess.run's own name
    stdin: IO[Any] | int | None = None,
    text: bool = True,
    check: bool = False,
    stdout: IO[Any] | int = subprocess.PIPE,
    stderr: IO[Any] | int = subprocess.PIPE,
) -> subprocess.CompletedProcess[Any]:
    """``subprocess.run(capture_output=True, timeout=...)`` that is actually bounded.

    Drop-in for the capture+timeout shape: same return type, same
    ``TimeoutExpired``/``CalledProcessError`` contract. The difference is
    that on timeout the child's whole process group is killed BEFORE the
    reaping drain, so no surviving grandchild can hold the pipe open past
    ``timeout``.

    ``timeout`` is required and has no default. Every site that reaches for
    this helper is one that already decided how long it was willing to
    wait; a default would let a new site inherit someone else's number.

    ``stdout`` and ``stderr`` default to ``PIPE``, which is the capture
    shape this exists for. They are overridable — with ``DEVNULL``, or with
    an open file — because the reason to reach for this helper is the group
    kill, not the capture: a site that redirects to a file still leaks the
    descendants of a timed-out child, and that is the POSIX half of the
    defect. ``nexus-t10nc`` drained one such site
    (``commands/doctor.py``'s MinerU parse probe, which redirects
    PRECISELY because a pipe deadlocked against the pool it spawns).

    Raises ``subprocess.TimeoutExpired`` on timeout, after the kill and the
    reap, so callers that already handle it keep working unchanged.
    """
    if input is not None and stdin is not None:
        raise ValueError("run_bounded: pass input= or stdin=, not both")

    proc = subprocess.Popen(  # noqa: S603 - argv is a sequence, never a shell string
        argv,
        stdin=subprocess.PIPE if input is not None else stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        cwd=cwd,
        env=env,
        # POSIX: makes the child a session/group leader so the whole group
        # is killable as a unit. Windows: accepted and ignored -- see the
        # module docstring; kill_child_and_descendants owns that fact.
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        reach = kill_child_and_descendants(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=REAP_TIMEOUT_S)
        _log.warning(
            "bounded_subprocess_timeout",
            argv0=argv[0] if argv else None,
            timeout_s=timeout,
            kill_reach=reach,
            # "process" means descendants of this child, if any, survived
            # the bound. On Windows that is every time; on POSIX it means
            # the group was already gone.
            descendants_reached=(reach == "group"),
        )
        raise

    completed = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
    if check:
        completed.check_returncode()
    return completed
