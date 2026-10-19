# SPDX-License-Identifier: AGPL-3.0-or-later
"""Name the test that leaves SIGTERM ignored or blocked in a pytest worker (nexus-yqzpw).

build-lease_test.sh Test 4 failed in CI shard 3 at 5588beead because its lease holder never
acted on TERM. A child inherits an ignored TERM and the spawning thread's signal mask across
exec, and the shell suites start from an xdist worker, so a test that left either behind
breaks a later, unrelated one. Checked around every test, the warning names the leaker
rather than the victim, and the state is put back so the victim still runs clean.
"""

from __future__ import annotations

import contextlib
import os
import signal
import warnings
from collections.abc import Iterator


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
