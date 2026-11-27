# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The one detector for a ``bd`` that is installed but has no beads database (GH #1689).

Three readers tell that case from a ``bd`` that is broken, and each must say which it saw: the console's needs-sam read
(``nexus.console.routes.threads``: "skipped", not "error"), the ``nx brief beads`` topic (``nexus.hooks.session_context``)
and the ``nx command-context`` preamble (``nexus.commands.command_context``). They share this module so the sentinel has
one owner. Stdlib only: the SessionStart hook module imports it and stays import-light.
"""
from __future__ import annotations

__all__ = ["NO_BEADS_DATABASE", "NO_BEADS_DATABASE_NOTE", "failure_summary", "is_no_beads_database"]

#: bd's own error text (compared lower case) when the working directory has no beads database.
NO_BEADS_DATABASE: str = "no beads database found"

#: What a reader tells its audience in that case; no ``bd`` is broken, there is just nothing to list.
NO_BEADS_DATABASE_NOTE: str = "there is no beads database here (`bd init` creates one)"


def is_no_beads_database(*texts: str | None) -> bool:
    """True when any of *texts* (a ``bd`` run's stderr and stdout) carries bd's "no beads database found" error."""
    return any(NO_BEADS_DATABASE in (t or "").lower() for t in texts)


def failure_summary(returncode: int, *texts: str | None) -> str:
    """``exit N: <first non-empty line>`` of the first of *texts* that has one, or ``exit N``; for a ``bd`` that failed."""
    for text in texts:
        for line in (text or "").strip().splitlines():
            if line.strip():
                return f"exit {returncode}: {line.strip()[:120]}"
    return f"exit {returncode}"
