# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""An aura's block in this session's threads entry (RDR-249 Technical Design 1, display.threads; bead
nexus-mdbds.11.1).

The entry is the one in T2 project ``threads`` whose ``session:`` line names this session, read with the board's
own reader (:func:`nexus.console.threads.parse_session`), never found by title. The host never creates an entry,
and when two carry the line (the board's duplicate-session finding) it writes nothing. The block is the heading
``## aura: <threads value>`` and one line, ``state: <text>``, with the text kept to printable ASCII on one line and
cut to 200 characters, so procedure text cannot open a heading, an ``ask:`` line or another field.

Every read-modify-write in this process holds one lock (:data:`_LOCK`), for all auras, fires and clears. T2 has no
compare-and-set, so an agent's own ``memory_put`` between the read and the write can still be lost; the RDR accepts
that window.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from typing import Any

import structlog

from nexus.console.threads import parse_session

_log = structlog.get_logger(__name__)

#: The T2 project the board reads.
THREADS_PROJECT: str = "threads"
#: The longest state text, after sanitising.
MAX_STATE_CHARS: int = 200

#: One lock for every read-modify-write of a threads entry in this process (rule 21).
_LOCK = threading.Lock()
# A heading the board reads (nexus.console.threads._HEADING): it ends an aura's block.
_HEADING = re.compile(r"^#{1,3}\s+\S")
_NOT_PRINTABLE = re.compile(r"[^\x20-\x7e]")

ReadEntries = Callable[[], list[Mapping[str, Any]]]
PutEntry = Callable[[str, str, str], None]


def sanitize_state(text: str) -> str:
    """*text* as one line of printable ASCII: each newline or tab a space, everything else outside 0x20-0x7e
    dropped, cut to :data:`MAX_STATE_CHARS`."""
    one_line = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    return _NOT_PRINTABLE.sub("", one_line)[:MAX_STATE_CHARS]


def _heading(threads_value: str) -> str:
    return f"## aura: {threads_value}"


def _without_block(lines: list[str], heading: str) -> tuple[list[str], bool]:
    """*lines* with the block under *heading* taken out (up to the next heading), and whether there was one."""
    out: list[str] = []
    found = False
    skipping = False
    for line in lines:
        if line.strip() == heading:
            found = True
            skipping = True
            continue
        if skipping and _HEADING.match(line.strip()):
            skipping = False
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return out, found


class ThreadsBlockWriter:
    """Writes and removes aura blocks in one session's threads entry.

    *read_entries* returns the rows of T2 project ``threads`` (``title``, ``content``, ``tags``); *put* writes
    ``(title, content, tags)`` back to it. :func:`t2_threads_io` gives both over the CLI's T2 handle.
    """

    def __init__(self, session_id: str, *, read_entries: ReadEntries, put: PutEntry) -> None:
        self.session_id = session_id
        self._read = read_entries
        self._put = put

    def _entry(self, aura: str) -> Mapping[str, Any] | None:
        mine = [
            row for row in self._read()
            if parse_session(str(row.get("title", "")), str(row.get("content", ""))).session_id == self.session_id
        ]
        if not mine:
            _log.info("aura_threads_no_entry", aura=aura, session_id=self.session_id)
            return None
        if len(mine) > 1:
            _log.warning(
                "aura_threads_ambiguous", aura=aura, session_id=self.session_id,
                titles=sorted(str(r.get("title", "")) for r in mine),
            )
            return None
        return mine[0]

    def set_block(self, aura: str, threads_value: str, text: str) -> bool:
        """Replace this aura's block with ``state: <text>`` (sanitised). True when the entry was written."""
        heading = _heading(threads_value)
        with _LOCK:
            row = self._entry(aura)
            if row is None:
                return False
            lines, _found = _without_block(str(row.get("content", "")).splitlines(), heading)
            lines += ["", heading, f"state: {sanitize_state(text)}"]
            self._put(str(row.get("title", "")), "\n".join(lines) + "\n", str(row.get("tags", "") or ""))
            return True

    def remove_block(self, aura: str, threads_value: str) -> bool:
        """Remove this aura's block. True when the entry was written; an entry with no such block is not."""
        heading = _heading(threads_value)
        with _LOCK:
            row = self._entry(aura)
            if row is None:
                return False
            lines, found = _without_block(str(row.get("content", "")).splitlines(), heading)
            if not found:
                return False
            self._put(str(row.get("title", "")), "\n".join(lines) + "\n", str(row.get("tags", "") or ""))
            return True


def t2_threads_io() -> tuple[ReadEntries, PutEntry]:
    """The board's reader and a whole-entry writer for T2 project ``threads``, over the CLI's T2 handle."""
    from nexus.commands._helpers import t2_handle  # noqa: PLC0415 -- deferred: the MCP server imports this module

    def read() -> list[Mapping[str, Any]]:
        with t2_handle() as db:
            return list(db.memory.get_all(project=THREADS_PROJECT))

    def put(title: str, content: str, tags: str) -> None:
        with t2_handle() as db:
            db.memory.put(project=THREADS_PROJECT, title=title, content=content, tags=tags)

    return read, put
