# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart: one line per threads-board thread of this session whose ``due:`` has come (nexus-ct74m item 6).

The session's board entry is found as the PreCompact checkpoint finds it (``compact_checkpoint._board_entry``):

1. every ``threads`` entry whose own ``session:`` line names the payload's session id, by one full-text search of
   project ``threads`` for the id (another entry that merely mentions the id does not count);
2. when none does, the entry titled exactly the session's name, read from Claude Code's
   ``<config dir>/sessions/<pid>.json`` (``sessionId`` and ``name``).

Step 2 is what makes a ``due:`` survive a restart, ``/clear``, resume or fork: each gives the session a new id, and
the entry's ``session:`` line still names the old one until the session rewrites it. A session with no name and a
stale ``session:`` line gets no reminder.

Each request is bounded (``NX_BOARD_DUE_TIMEOUT_S``, default 2 s); building the store may also wait for the engine's
endpoint, as every T2 caller in this hook does. Any failure prints nothing and logs a warning to the hook log: a
due reminder must never cost a session its start.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from nexus.console.threads import Session, due_at, due_threads, has_value, parse_session

__all__ = ["due_lines", "session_name"]

_log = structlog.get_logger(__name__)

_BOARD_PROJECT = "threads"
_DEFAULT_TIMEOUT_S = 2.0
#: The heading tells a session how a reminder stops: nothing is stored, the line itself is the state.
HEADING = "## Due on your threads board ({entry}): move or delete each due: line once you have acted on it"

Search = Callable[[str], Iterable[Mapping[str, Any]]]
ByTitle = Callable[[str], Mapping[str, Any] | None]


def _timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("NX_BOARD_DUE_TIMEOUT_S", _DEFAULT_TIMEOUT_S)))
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def session_name(session_id: str, claude_dir: Path | None = None) -> str:
    """The session's name from Claude Code's ``sessions/<pid>.json``, or ``""`` (the checkpoint's ``_session_name``)."""
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    directory = (claude_dir or (Path(raw) if raw else Path.home() / ".claude")) / "sessions"
    try:
        paths = sorted(directory.glob("*.json"))[:500]
    except OSError:
        return ""
    for path in paths:
        try:
            if path.stat().st_size > 65536:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") == session_id:
            name = str(data.get("name") or "").strip()
            if name and len(name) <= 128 and name.isprintable():
                return name
    return ""


def _engine() -> tuple[Search, ByTitle]:
    from nexus._hook_runtime._io import configure_hook_logging  # noqa: PLC0415 — only a real lookup pays for it
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415

    configure_hook_logging()  # the store's own structlog lines must not reach the hook's stdout
    timeout = _timeout()
    store = HttpMemoryStore(timeout=timeout)

    def search(q: str) -> Iterable[Mapping[str, Any]]:
        return store.search(q, project=_BOARD_PROJECT, access="silent", timeout=timeout, retry_read_timeout=False)

    def by_title(title: str) -> Mapping[str, Any] | None:
        return store.get(project=_BOARD_PROJECT, title=title, timeout=timeout, retry_read_timeout=False)

    return search, by_title


def _entries(session_id: str, name: str, search: Search, by_title: ByTitle) -> list[tuple[str, Session]]:
    found = []
    for row in search(session_id):
        title = str(row.get("title", ""))
        session = parse_session(title, str(row.get("content", "")))
        if session.session_id == session_id:
            found.append((title, session))
    if found or not name:
        return found
    row = by_title(name)
    if row is not None and str(row.get("title", "")) == name:
        found.append((name, parse_session(name, str(row.get("content", "")))))
    return found


def _section(title: str, session: Session, now: datetime) -> list[str]:
    lines = []
    for t, _when in due_threads(session, now):
        extra = [f"ref {', '.join(t.refs)}"] if t.refs else []
        if has_value(t.worktree):
            extra.append(f"worktree {t.worktree}")
        lines.append(f"- {t.title}: due {t.due}" + (f" ({'; '.join(extra)})" if extra else ""))
    for t in session.threads:
        if has_value(t.due) and due_at(t.due, now.tzinfo) is None:  # type: ignore[arg-type] — now is aware
            lines.append(f"- {t.title}: `due: {t.due}` is not an ISO date or datetime, so it never comes due")
    return [HEADING.format(entry=title), *lines, ""] if lines else []


def due_lines(
    session_id: str,
    now: datetime | None = None,
    *,
    search: Search | None = None,
    by_title: ByTitle | None = None,
    name: str | None = None,
) -> list[str]:
    """The SessionStart section for *session_id*'s board: a heading, one line per due thread (and per ``due:`` that
    cannot be read), a blank; or ``[]``.

    *now* defaults to local time, so a ``due:`` date comes due at the operator's midnight. *search* and *by_title*
    default to the engine's ``threads`` project; *name* defaults to :func:`session_name`.
    """
    if not session_id:
        return []
    try:
        if search is None or by_title is None:
            engine_search, engine_by_title = _engine()
            search = search or engine_search
            by_title = by_title or engine_by_title
        resolved = session_name(session_id) if name is None else name
        clock = now or datetime.now().astimezone()
        out: list[str] = []
        for title, session in _entries(session_id, resolved, search, by_title):
            out.extend(_section(title, session, clock))
        return out
    except Exception as exc:  # noqa: BLE001 — a hook must never fail; no reminder is the cost, and it is logged
        _log.warning("board_due_lookup_failed", session_id=session_id, error=f"{type(exc).__name__}: {exc}")
        return []
