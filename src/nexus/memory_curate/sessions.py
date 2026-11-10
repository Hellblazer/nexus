# SPDX-License-Identifier: AGPL-3.0-or-later
"""The repository's session starts for ``nx memory curate`` (RDR-238 TD5, nexus-qo7ie.27).

Curate reads each main transcript's first record, and nothing else of a transcript, to count the sessions that
started in its window. The rule and the file listing are RDR-240's own (``nexus.lua_runtime.transcripts_read``'s
``first_start`` and ``repo_main_transcripts``, which ``transcripts.sessions`` uses), so there is one rule; ``tests/test_memory_curate_reads.py`` pins
the agreement with ``sessions()`` on one fixture. Calling ``sessions()`` itself measured 0.4 s to 25 s on 255 sessions.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from nexus.lua_runtime import header, transcripts_read as read

#: How far back the session listing looks (Claude Code keeps transcripts about ``RETENTION_DAYS``; a
#: longer ``cleanupPeriodDays`` leaves older ones, which only matter as "the history is longer than the window").
SESSION_LOOKBACK_DAYS = 365
#: The wall-time bound of listing the repository's sessions (project resolution and every first-record read).
SESSION_LIST_SECONDS = 30.0
#: Bytes read from the top of a main transcript looking for its first timestamped record
#: (``transcripts_read.START_SCAN_BYTES``); a file with none in that span is counted apart, never as a session.
START_SCAN_BYTES = read.START_SCAN_BYTES


class SessionStarts(NamedTuple):
    """The start of every main session found, and how many transcripts had no start in the first
    :data:`START_SCAN_BYTES` bytes (not counted)."""

    starts: list[datetime]
    unscanned: int = 0


def repo_session_starts(cwd: Path, now: datetime) -> SessionStarts | str:
    """The start of every main session of this repository still on disk (looking back
    :data:`SESSION_LOOKBACK_DAYS` days), or why they could not be listed: the window's oldest-session
    cap needs the sessions that started before the window too.

    One rule with the verb: the files come from ``transcripts_read.repo_main_transcripts`` (project ``repo``,
    links excluded, skipped as ``sessions`` skips them) and each start from ``transcripts_read.first_start``,
    which reads one record's worth of the file: about 0.5 s on 255 main sessions where a full
    ``sessions()`` read took 23 to 27 s (nexus-qo7ie.27). A transcript the verb lists but cannot place
    (gone since the listing, no stamped record) adds nothing; one whose first stamp lies past
    :data:`START_SCAN_BYTES` is counted in ``unscanned``."""
    root = read.projects_root()
    if not root.is_dir():
        return f"no Claude Code projects directory at {root}"
    deadline = time.monotonic() + SESSION_LIST_SECONDS
    floor = (now - timedelta(days=SESSION_LOOKBACK_DAYS)).timestamp()
    starts: list[datetime] = []
    unscanned = 0
    try:
        probe = header.probe_repo(cwd)
        for path in read.repo_main_transcripts(probe.root if probe else None, floor, deadline):
            if time.monotonic() >= deadline:
                return "the repository's sessions could not be listed: the time budget ran out"
            start, bounded = read.first_start(path, deadline, START_SCAN_BYTES)
            if start is not None:
                starts.append(start)
            elif bounded:
                unscanned += 1
    except Exception as exc:  # noqa: BLE001 — a failed listing is a reason, never "no sessions"
        return f"the repository's sessions could not be listed: {type(exc).__name__}: {exc}"
    return SessionStarts(starts, unscanned)


def unscanned_note(n: int) -> str:
    """The row text for *n* transcripts left uncounted at the scan bound, or ``""`` for none."""
    if not n:
        return ""
    return (f"{n} transcript{'' if n == 1 else 's'} had no start in the first "
            f"{START_SCAN_BYTES // (1024 * 1024)} MiB and {'was' if n == 1 else 'were'} not counted")
