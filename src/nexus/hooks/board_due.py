# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart: one line per threads-board thread of this session whose ``due:`` has come (nexus-ct74m item 6).

A session's board entry is found the way the PreCompact checkpoint finds it: the ``threads`` entry whose body has a
``session: <session id>`` line, by one full-text search of project ``threads`` for the id. Only the entry whose own
``session:`` line names the id counts; another session's entry that merely mentions it does not. The search is
bounded (``NX_BOARD_DUE_TIMEOUT_S``, default 2 s) and any failure prints nothing: the T2 section of the same hook
already says when the engine cannot be reached, and a due reminder must never cost a session its start.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Any

from nexus.console.threads import due_threads, has_value, parse_session

__all__ = ["due_lines"]

_BOARD_PROJECT = "threads"
_DEFAULT_TIMEOUT_S = 2.0

Search = Callable[[str], Iterable[Mapping[str, Any]]]


def _timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("NX_BOARD_DUE_TIMEOUT_S", _DEFAULT_TIMEOUT_S)))
    except ValueError:
        return _DEFAULT_TIMEOUT_S


def _engine_search() -> Search:
    from nexus._hook_runtime._io import configure_hook_logging  # noqa: PLC0415 — only a real search pays for it
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415

    configure_hook_logging()  # the store's own structlog lines must not reach the hook's stdout
    timeout = _timeout()
    store = HttpMemoryStore(timeout=timeout)
    return lambda q: store.search(q, project=_BOARD_PROJECT, access="silent", timeout=timeout,
                                  retry_read_timeout=False)


def due_lines(session_id: str, now: datetime | None = None, *, search: Search | None = None) -> list[str]:
    """The SessionStart section for *session_id*'s due threads: a heading, one line each, a blank; or ``[]``.

    *now* defaults to local time, so a ``due:`` date comes due at the operator's midnight. *search* is the T2
    full-text search of project ``threads``; it defaults to the engine's.
    """
    if not session_id:
        return []
    try:
        rows = list((search or _engine_search())(session_id))
        clock = now or datetime.now().astimezone()
        for row in rows:
            title = str(row.get("title", ""))
            session = parse_session(title, str(row.get("content", "")))
            if session.session_id != session_id:
                continue
            lines = []
            for t, _when in due_threads(session, clock):
                extra = [f"ref {', '.join(t.refs)}"] if t.refs else []
                if has_value(t.worktree):
                    extra.append(f"worktree {t.worktree}")
                lines.append(f"- {t.title}: due {t.due}" + (f" ({'; '.join(extra)})" if extra else ""))
            return [f"## Due on your threads board ({title})", *lines, ""] if lines else []
    except Exception:  # noqa: BLE001 — a hook must never fail; no reminder is the cost
        return []
    return []
