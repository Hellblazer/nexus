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
"""
from __future__ import annotations

import shutil
import subprocess
import sys

from nexus._hook_runtime._io import HookResult

#: The exact sentence the shell form echoed. Kept verbatim: it is the only
#: user-facing string in this path, it names a remedy, and a box reading it
#: is by definition one whose tooling is too old to have anything better.
SKEW_GUIDANCE = (
    "conexus plugin requires conexus >= 4.2.0 — run: nx self install "
    "(or uv tool upgrade conexus on an un-migrated box)"
)


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — reads no stdin, as the shell form read none
    """Spawn ``nx upgrade --auto``; emit :data:`SKEW_GUIDANCE` if it fails.

    Returns a stdout-silent :class:`HookResult` on every path. The child's
    streams are CAPTURED rather than inherited, which is what ``2>/dev/null``
    did for stderr and is a deliberate tightening for stdout: under the shell
    form the child inherited fd 1, so anything ``nx`` printed there was handed
    to Claude Code as this hook's JSON decision.

    Capturing stdout loses nothing, and that was checked rather than assumed.
    Every ``click.echo`` on this command's path is guarded by
    ``not auto_mode`` (``nexus.commands.upgrade``: the precondition lines, the
    per-rung convergence lines, the empty-registry line, the deferred-rung
    notices), and the two unguarded ones sit inside ``if dry_run:``, which no
    hook invocation reaches. Under ``--auto`` and without ``--dry-run`` the
    command is silent on stdout by construction, so the shell form was
    forwarding an empty stream to the decision channel.
    """
    nx = shutil.which("nx")
    if nx is None:
        sys.stderr.write(SKEW_GUIDANCE + "\n")
        return HookResult()
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, resolved binary, no shell
            [nx, "upgrade", "--auto"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        # The spawn itself failed (no fork, bad exec). The shell saw this as a
        # nonzero status and fired the same `||`.
        sys.stderr.write(SKEW_GUIDANCE + "\n")
        return HookResult()
    # NO `timeout=` HERE, AND THAT IS THE DECISION, not an omission. Code
    # review proposed one, citing `_cycle_storage_service_to_current`'s
    # `timeout=60` as the house pattern. It is the wrong pattern for this
    # call. `hooks.json` gives this hook 30 s; Claude Code enforces that on
    # the `nx-hook` process, and an orphaned `nx upgrade --auto` then RUNS TO
    # COMPLETION in the background and takes effect at the next session --
    # which is the accepted shape RDR-143 CA-4 already relies on for the
    # detached lockstep upgrade ("the new CLI takes effect on the next
    # session, not the current one"). A `subprocess.run(timeout=...)` would
    # instead SIGKILL a legitimate in-flight upgrade partway through a ladder
    # rung. Letting it finish unattended is the better failure, so the
    # 30 s budget stays where it is and this call does not add a second one.
    if proc.returncode != 0:
        sys.stderr.write(SKEW_GUIDANCE + "\n")
    return HookResult()
