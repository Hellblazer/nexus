# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx-hook self-gc``: the SessionStart generation sweep (RDR-215 item 6).

``hooks.json`` used to declare this as a shell string::

    nx self gc >/dev/null 2>&1 || true

Both redirects and the ``|| true`` are this module's job now. The contract is
the simplest one in the hook layer: reclaim what can be reclaimed, and say
nothing whatever happens. ``nx self gc`` removes superseded install
generations that no live process still holds; a session start is a good
moment to try and a terrible moment to report about it.

The subprocess boundary is kept for the same reason
:mod:`nexus.hooks.upgrade_auto` keeps it — ``nx self gc`` reasons about
generation trees and live holders, and ``nx-hook`` is running out of one of
them. See that module's docstring for the full argument.
"""
from __future__ import annotations

import shutil
import subprocess

from nexus._hook_runtime._io import HookResult


def run(payload: dict | None) -> HookResult:  # noqa: ARG001 — reads no stdin, as the shell form read none
    """Spawn ``nx self gc``, discard everything it says, always succeed.

    No branch writes to stdout or stderr. A missing ``nx``, a nonzero exit and
    a failed spawn are all the ``|| true`` case: this hook has no opinion worth
    interrupting a session start for.
    """
    nx = shutil.which("nx")
    if nx is None:
        return HookResult()
    try:
        subprocess.run(  # noqa: S603 — argv list, resolved binary, no shell
            [nx, "self", "gc"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        pass
    return HookResult()
