# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Which live processes are still running from which generation — Python half.

nexus-utpuw.10. The twin of ``src/nexus/_install/census.sh``, pinned by
``tests/test_install_census_twins_agree.py``. Two implementations exist for the
same reason ``layout.sh`` and ``install_layout.py`` both do: the callers have
incompatible import constraints. The shell half is sourced by GC and the
installer, which run with nothing installed and cannot import nexus. This half
is imported by ``upgrade_finish.py`` and ``health.py``, which run after the
install and can.

WHAT THIS REPLACES, AND WHY IT IS NOT A REFACTOR. ``upgrade_finish`` decided
which processes were stale by matching hardcoded substrings —
``_PROC_MARKERS = ('uv/tools/conexus', '.local/bin/nx')``. Under the generation
layout nothing lives at ``uv/tools/conexus`` any more, so the markers matched
nothing, and NOTHING FAILED: the pass reported success having examined an empty
set. That is the failure class this whole arc keeps removing — an inventory
somebody has to maintain, whose staleness is silent.

Attribution here is STRUCTURAL. A holder is a process whose argv names the
generation directory followed by a path separator. There is no class list, no
vocabulary of daemon names, and therefore nothing to keep in sync: a daemon
class invented tomorrow is attributed correctly on the day it ships, because
the question asked is "did you exec out of this tree", not "are you one of the
things I know about".

ONE SNAPSHOT PER CENSUS. ``ps`` runs once and every generation is attributed
from that single view. Per-generation calls would let a process exit between
them and appear to hold two trees or none, and a caller acting on that would be
acting on a state that never existed at any instant.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

__all__ = [
    "ps_snapshot",
    "generation_holder_pids",
    "generation_match_prefixes",
    "PS_COMMAND",
]

#: The snapshot command, byte-identical to the shell half's ``_nx_ps_snapshot``.
PS_COMMAND = ("ps", "ax", "-o", "pid=,command=")


def ps_snapshot() -> str:
    """One process snapshot. Empty string when ``ps`` is unavailable.

    A ps-less box (minimal container, stripped host) yields no holders rather
    than an exception: ``nexus-p78a0`` is the record of what happens when this
    leg raises and takes unrelated work down with it.
    """
    try:
        r = subprocess.run(PS_COMMAND, capture_output=True, text=True, timeout=10)  # noqa: S603
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _match_prefix(generation: Path | str) -> str:
    """The string an argv must contain to count as running from *generation*.

    Mirrors the shell half exactly, including two properties that were paid for:

    * A generation entry MAY be a symlink — ``.7`` registers the legacy uv tree
      as a ``gen-*`` pointer outside ``tools/``. A live holder's argv names the
      REAL path it exec'd from, never the ledger pointer, so one level of
      readlink happens before matching. One level is enough: everything that
      registers a pseudo-generation writes a direct absolute symlink.
    * A trailing slash is normalised away before the boundary is appended.
      Without that, ``<gen>/`` + ``/`` builds ``<gen>//``, which no ps line can
      contain, and a HELD tree reports zero holders (nexus-qzawu). That is the
      under-reporting direction — the one that lets a caller act as though a
      tree were free.
    """
    path = Path(generation)
    if path.is_symlink():
        resolved = os.readlink(path)
        if resolved:
            path = Path(resolved)

    text = str(path).rstrip("/")
    if not text:
        # "/" normalises to empty, and an empty match makes the boundary "/" —
        # every process on the machine a holder of everything. Refuse instead;
        # answering "no holders" would be worse, being the answer that invites
        # a reap.
        raise ValueError(
            "refusing to census the filesystem root as a generation"
        )
    return text + "/"


def generation_holder_pids(
    generation: Path | str, snapshot: str | None = None
) -> list[int]:
    """PIDs running from *generation*, in snapshot order.

    Pass *snapshot* to attribute several generations from ONE view of the
    process table; omit it and one is taken for this call alone.

    A process that merely NAMES a path inside the tree without running from it
    is counted. That is deliberate and pinned by test: narrowing to argv[0]
    would end the over-attribution and buy under-reporting instead, and
    under-reporting is the direction that lets a live tree look free.
    """
    prefix = _match_prefix(generation)
    text = ps_snapshot() if snapshot is None else snapshot

    pids: list[int] = []
    for line in text.splitlines():
        if prefix not in line:
            continue
        head = line.split(maxsplit=1)
        if not head:
            continue
        try:
            pids.append(int(head[0]))
        except ValueError:
            # A ps line whose first field is not a pid is not a process row.
            continue
    return pids


def generation_match_prefixes(*, tools: Path | None = None) -> tuple[str, ...]:
    """Every string that marks a process as running from SOME generation.

    The plural of :func:`_match_prefix`, and deliberately the only other way to
    ask the question. ``upgrade_finish`` needs to enumerate holders of ANY
    generation rather than one, and giving it its own notion of what a marker
    looks like is how the markers it already had drifted out of matching
    anything at all.

    Enumerated, never hardcoded: the generations that exist ARE the marker set,
    so a generation created tomorrow is matched the day it appears and there is
    nothing to keep in sync. Returns empty when the layout cannot be read, which
    the caller must treat as "cannot tell" rather than "none".
    """
    try:
        from nexus import install_layout  # noqa: PLC0415 — deferred, avoids an import cycle

        generations = install_layout.list_generations(tools=tools)
    except Exception:  # noqa: BLE001 — layout unreadable: say nothing rather than guess
        return ()

    prefixes: list[str] = []
    for gen in generations:
        try:
            prefixes.append(_match_prefix(gen))
        except ValueError:
            continue
    return tuple(prefixes)
