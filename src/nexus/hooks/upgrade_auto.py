# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-hook upgrade-auto``: the SessionStart self-upgrade (RDR-215 item 6).

``hooks.json`` used to declare this as a shell string::

    nx upgrade --auto 2>/dev/null || echo '<guidance>' >&2

Exec form has no shell, so the redirect and the ``||`` have nowhere to run.
This module is where they went. It is deliberately a thin wrapper: the work
still happens in a separate ``nx upgrade --auto`` PROCESS, exactly as before.

**Why a subprocess and not an in-process call.** ``nexus.commands.upgrade``
is importable, and calling it here would save a spawn. It would also run the
upgrade ladder inside the hook's own interpreter — and that ladder installs
generations and flips ``<tools>/current`` underneath the caller. ``nx`` is a
shim that resolves ``current`` at spawn time and is meant to BE the process
being upgraded; ``nx-hook`` is not. Keeping the boundary keeps today's
semantics unchanged, which is the whole ask for a re-declaration bead.

**What a nonzero exit means.** ``--auto`` is documented "exit 0 always" and
the code backs it: :func:`nexus.commands.upgrade.upgrade` catches every
exception under ``auto_mode`` and returns. So a nonzero status is not an
upgrade that failed — it is an ``nx`` that is missing, or too old to know the
flag. That is version skew, and :data:`SKEW_GUIDANCE` is what the shell's
``|| echo`` said about it.

**The guidance is not the only skew path, and is not the load-bearing one.**
``version_lockstep_hook.py`` stays plugin-resident and stdlib-only precisely
so it still runs when ``nx`` is old or absent; it detects the skew, emits an
``additionalContext`` nudge naming the versions, and dispatches the detached
reinstall. This line is the cruder duplicate that predates it, kept because a
box with no ``nx`` at all is one the lockstep hook cannot upgrade either, and
then a sentence on stderr is the only thing left.

**The child is detached and the hook does not wait for it (nexus-wozn6).**
Since 7.68 ``nx upgrade --auto`` walks the RDR-192 rung, which is pending
after every package-version change and whose census read every collection in
turn: measured 51.5 s over 98 collections on 2026-10-05, plus about 2.7 s of
fixed work, against this hook's 30 s cap. The verb used to wait on the child
with its streams on pipes, on the stated premise that Claude Code's kill would
leave the child running to finish in the background. The ledger refutes that:
7.70 and 7.71 were each installed, each saw a run of cancelled SessionStarts
(84 cancelled across the transcripts, every one at the 30 s timeout), and
neither ever recorded the rung, which one surviving 55 s child would have
done. The child died with the hook, so every session after a version change
paid 30 s and converged nothing. The nexus-34f7r measurement of the
SessionEnd launcher shows the mechanism: a cancelled hook's kill walks the
process tree and reaches a child through its still-living parent. So the
child now starts in a session of its own with its streams on the null device,
and the hook waits at most :data:`_SKEW_WAIT_S` and returns, long before any
cancel; an orphan nobody kills then runs its upgrade to completion.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import warnings
from typing import Any

from nexus._hook_runtime._io import HookResult

#: The exact sentence the shell form echoed. Kept verbatim: it is the only
#: user-facing string in this path, it names a remedy, and a box reading it
#: is by definition one whose tooling is too old to have anything better.
SKEW_GUIDANCE = (
    "conexus plugin requires conexus >= 4.2.0 — run: nx self install "
    "(or uv tool upgrade conexus on an un-migrated box)"
)


#: How long the hook waits for the child before leaving it to run on. Long
#: enough to see the skew case: an ``nx`` that does not know ``--auto`` exits
#: 2 on the flag in about 0.17 s (measured 2026-10-05). Far short of the 30 s
#: cap in hooks.json, so the hook is never the process a cancel kills.
_SKEW_WAIT_S = 2.0

# Windows creation flags, by value so this module never imports anything for
# them: DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_BREAKAWAY_FROM_JOB
# (the same three ``nexus._session_end_launcher`` uses, nexus-34f7r).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _spawn_detached(argv: list[str]) -> subprocess.Popen[Any]:
    """Start *argv* outside the hook's session (POSIX) or process group and
    job (Windows), with every stream on the null device. Raises ``OSError``
    when no attempt could start it."""
    common: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name != "nt":
        return subprocess.Popen(argv, start_new_session=True, **common)  # noqa: S603 — argv list, resolved binary, no shell
    base = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    try:
        return subprocess.Popen(argv, creationflags=base | _CREATE_BREAKAWAY_FROM_JOB, **common)  # noqa: S603 — argv list, resolved binary, no shell
    except OSError:
        # A job that forbids breakaway refuses the flag; start without it.
        return subprocess.Popen(argv, creationflags=base, **common)  # noqa: S603 — argv list, resolved binary, no shell


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — reads no stdin, as the shell form read none
    """Start ``nx upgrade --auto`` detached; emit :data:`SKEW_GUIDANCE` if it
    fails inside :data:`_SKEW_WAIT_S`.

    Returns a stdout-silent :class:`HookResult` on every path. The child's
    streams go to the null device, which is what ``2>/dev/null`` did for
    stderr and is a deliberate tightening for stdout: under the shell form the
    child inherited fd 1, so anything ``nx`` printed there was handed to
    Claude Code as this hook's JSON decision. Null rather than a pipe because
    the child outlives this process, and a write to a pipe whose reader has
    exited kills the writer with SIGPIPE.

    Discarding stdout loses nothing, and that was checked rather than assumed.
    Every ``click.echo`` on this command's path is guarded by
    ``not auto_mode`` (``nexus.commands.upgrade``: the precondition lines, the
    per-rung convergence lines, the empty-registry line, the deferred-rung
    notices), and the two unguarded ones sit inside ``if dry_run:``, which no
    hook invocation reaches. Under ``--auto`` and without ``--dry-run`` the
    command is silent on stdout by construction, so the shell form was
    forwarding an empty stream to the decision channel.

    No timeout kills the child, deliberately: a kill partway through a ladder
    rung is worse than letting it finish unattended, and the rung's own
    cross-process lock turns concurrent session starts into quick deferrals.
    """
    nx = shutil.which("nx")
    if nx is None:
        sys.stderr.write(SKEW_GUIDANCE + "\n")
        return HookResult()
    try:
        proc = _spawn_detached([nx, "upgrade", "--auto"])
    except (OSError, ValueError):
        # The spawn itself failed (no fork, bad exec). The shell saw this as a
        # nonzero status and fired the same `||`.
        sys.stderr.write(SKEW_GUIDANCE + "\n")
        return HookResult()
    try:
        returncode = proc.wait(timeout=_SKEW_WAIT_S)
    except subprocess.TimeoutExpired:
        # Still running: an upgrade doing real work, not skew. Leave it. The
        # handle is dropped on purpose; Popen.__del__ would otherwise print
        # "subprocess N is still running" on the hook's stderr.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            del proc
        return HookResult()
    if returncode != 0:
        sys.stderr.write(SKEW_GUIDANCE + "\n")
    return HookResult()
