# SPDX-License-Identifier: AGPL-3.0-or-later
"""Name the test that leaves SIGTERM ignored or blocked in a pytest worker (nexus-yqzpw).

build-lease_test.sh Test 4 failed in CI shard 3 at 5588beead because its lease holder never
acted on TERM. A child inherits an ignored TERM and the spawning thread's signal mask across
exec, and the shell suites start from an xdist worker, so a test that left either behind
breaks a later, unrelated one. Checked around every test, the warning names the leaker
rather than the victim, and the state is put back so the victim still runs clean.
``run_suite`` is the spawn side: a shell suite started through it gets TERM at its
default whatever the worker carries.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import warnings
from collections.abc import Iterator
from pathlib import Path


@contextlib.contextmanager
def term_left_as_found(nodeid: str) -> Iterator[None]:
    """Warn naming *nodeid* when the body leaves TERM newly ignored or blocked, then undo it.

    Off the main thread ``signal.signal`` raises ValueError; the change is still reported.
    Windows has no signal mask and nothing here applies there.
    """
    if os.name != "posix":
        yield
        return
    before = signal.getsignal(signal.SIGTERM)
    was_blocked = signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, [])
    yield
    left: list[str] = []
    if signal.getsignal(signal.SIGTERM) is signal.SIG_IGN and before is not signal.SIG_IGN:
        left.append("ignored")
        if before is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGTERM, before)
    if not was_blocked and signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []):
        left.append("blocked")
        signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
    if left:
        warnings.warn(
            f"{nodeid} left SIGTERM {' and '.join(left)} in this worker (put back). A child "
            "spawned later inherits it and cannot act on TERM (nexus-yqzpw).",
            stacklevel=1,
        )


def term_state_of_this_worker() -> str | None:
    """How this process would pass SIGTERM to a child, or None when at the default.

    build-lease_test.sh Test 4 failed in CI (5588beead) because its suite started with
    TERM ignored or blocked. A child inherits both across exec, and the suites start from
    this xdist worker, so an earlier test that left either in place is the candidate.
    Windows has no signal mask, so it answers None there.
    """
    if os.name != "posix":
        return None
    found = []
    if signal.getsignal(signal.SIGTERM) is signal.SIG_IGN:
        found.append("TERM ignored")
    if signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, []):
        found.append("TERM blocked")
    return ", ".join(found) or None


@contextlib.contextmanager
def term_default_for_spawn() -> Iterator[None]:
    """Hold TERM at its default and unblocked in this thread while a child is spawned.

    A child inherits an ignored TERM and the spawning thread's mask across exec. The reset
    is done here in the parent, not in a ``preexec_fn``, because a ``preexec_fn`` forces
    the fork path that tests/_posix_spawn.py exists to avoid on macOS. Off the main thread
    ``signal.signal`` raises ValueError, and the disposition is left as it is.
    """
    if os.name != "posix":
        yield
        return
    before = signal.getsignal(signal.SIGTERM)
    reset = False
    mask: set[signal.Signals] | None = None
    try:
        if before is signal.SIG_IGN:
            try:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                reset = True
            except ValueError:
                pass
        mask = signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGTERM])
        yield
    finally:
        if mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        if reset:
            signal.signal(signal.SIGTERM, before)


def run_suite(path: Path, *, cwd: Path, env: dict[str, str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run one suite with TERM reset, naming a leaked TERM state in a warning first.

    Suites that signal their own children (build-lease_test.sh, mvnw-leased_test.sh) test
    the scripts, not whatever an earlier test in this worker left behind. Reported, not
    failed: the suite runs with TERM reset either way, and the warning names the state for
    whoever traces the test that left it (nexus-yqzpw).
    """
    if (leaked := term_state_of_this_worker()) is not None:
        warnings.warn(f"{path.name}: this pytest worker has {leaked}", stacklevel=2)
    argv = ["bash", str(path)]
    with term_default_for_spawn():
        proc = subprocess.Popen(
            argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env
        )
    with proc:
        # subprocess.run's own handling: after a timeout POSIX waits on the child only, since
        # a background lease holder the suite started can keep the pipes open indefinitely.
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            if os.name == "posix":
                proc.wait()
            else:
                proc.communicate()
            raise
        except BaseException:
            proc.kill()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)
