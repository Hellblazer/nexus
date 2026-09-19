# SPDX-License-Identifier: AGPL-3.0-or-later
"""The error boundary every sn hook script runs its entry point inside.

A Claude Code hook must never fail the event it fires on. The bash layer
got that for free by simply not setting ``set -e`` — and, in
``auto-approve-sn-mcp.sh``'s case, by an unconditional trailing ``exit 0``
that ALSO hid a Python crash completely: the wrapper reported success
whatever the Python did, so a broken allowlist was indistinguishable from
an empty one. RDR-215 deletes those wrappers, which makes each script's own
exit code the event's exit code, so the boundary has to live here.

Python has no "just don't set the flag" equivalent, so every script needs
an explicit one. Logged, not swallowed: stderr is a hook's only diagnostic
surface (the same choice ``conexus/hooks/scripts/mailbox_drain.py`` makes),
and it does not fail the event.

Stdlib only, and no import of the conexus wheel: sn ships no Python package
and no server of its own, and nothing here may make it depend on one.
"""
from __future__ import annotations

import sys
import traceback
from collections.abc import Callable


def guard(entry: Callable[[], int], tag: str) -> int:
    """Run *entry*, returning its exit code; on any exception log and return 0.

    *tag* names the script in the stderr line so a crash is attributable
    without a traceback-only clue about which of four hooks produced it.
    """
    try:
        return entry()
    except Exception:  # noqa: BLE001 — last resort: a hook never fails its event
        print(f"[{tag}] crashed, event continues:", file=sys.stderr)  # noqa: T201 — stderr is this hook's only diagnostic surface
        traceback.print_exc()
        return 0
