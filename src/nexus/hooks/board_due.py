# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart: one line per threads-board thread of this session whose ``due:`` has come (nexus-ct74m item 6).

The session's board entry is found in the same two steps as the PreCompact checkpoint's (``compact_checkpoint._board_entry``),
except that step 1 reads every matching entry where the checkpoint takes the first (RDR-253 Phase 2: the entry's
``session:`` line is never read, so an entry that has one and no ``identity:`` line is not found, and neither is any
entry for a session no token file binds to an identity):

1. every ``threads`` entry whose own ``identity:`` line names the identity the session's id is bound to, by one full-text
   search of project ``threads`` for ``identity: <id>`` (another entry that merely mentions it does not count);
2. when none does, the entry titled exactly the session's name, read from Claude Code's
   ``<config dir>/sessions/<pid>.json`` (``sessionId`` and ``name``), taken only when its own ``identity:`` line matches.

A session with no name, a renamed one whose entry keeps the old title, and a Desktop session filed under a handle get no
reminder when the search finds nothing.

The whole lookup has one hard budget (``NX_BOARD_DUE_BUDGET_S``, default 3 s): it runs in a daemon thread that
the hook stops waiting for when the budget is spent, so the store's own waits (an endpoint re-resolve, gateway
backoff, a data-token mint, each bounded by a deadline of its own) cannot hold up the rest of SessionStart. Inside
the thread a mint is also cut to the budget (``mint_deadline``) and each request to ``NX_BOARD_DUE_TIMEOUT_S``
(default 2 s). Over budget, or any failure, prints nothing and logs a warning to the hook log: a due reminder must
never cost a session its start.
"""
from __future__ import annotations

import json
import os
import threading
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
_DEFAULT_BUDGET_S = 3.0
#: A printed line is cut here: titles, refs and worktree are free text a session wrote.
_MAX_LINE = 300
#: At most this many thread lines per entry; the rest are counted in one line.
_MAX_LINES = 20
#: The heading tells a session how a reminder stops: nothing is stored, the line itself is the state.
HEADING = "## Due on your threads board ({entry}): move or delete each due: line once you have acted on it"

Search = Callable[[str], Iterable[Mapping[str, Any]]]
ByTitle = Callable[[str], Mapping[str, Any] | None]


def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.5, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _clip(line: str) -> str:
    return line if len(line) <= _MAX_LINE else line[: _MAX_LINE - 1] + "…"


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
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — only a real lookup pays for it

    timeout = _env_seconds("NX_BOARD_DUE_TIMEOUT_S", _DEFAULT_TIMEOUT_S)
    store = HttpMemoryStore(timeout=timeout)

    def search(q: str) -> Iterable[Mapping[str, Any]]:
        return store.search(q, project=_BOARD_PROJECT, access="silent", timeout=timeout, retry_read_timeout=False)

    def by_title(title: str) -> Mapping[str, Any] | None:
        return store.get(project=_BOARD_PROJECT, title=title, timeout=timeout, retry_read_timeout=False)

    return search, by_title


def _entries(name: str, search: Search, by_title: ByTitle, identity: str = "") -> list[tuple[str, Session]]:
    """The entries whose ``identity:`` line is *identity* (RDR-253 Phase 2): found across ``/clear`` and restart. A
    session no token file binds to an identity (empty *identity*) finds none, and an entry with only a ``session:`` line
    is not found."""
    found: list[tuple[str, Session]] = []
    if not identity:
        return found
    for row in search(f"identity: {identity}"):  # full-text on both words; the parsed line decides
        title = str(row.get("title", ""))
        session = parse_session(title, str(row.get("content", "")))
        if session.identity == identity:
            found.append((title, session))
    if found or not name:
        return found
    row = by_title(name)
    if row is not None and str(row.get("title", "")) == name:
        session = parse_session(name, str(row.get("content", "")))
        if session.identity == identity:  # a same-named entry that is not this identity's is not ours
            found.append((name, session))
    return found


def _section(title: str, session: Session, now: datetime) -> list[str]:
    lines = []
    for t, _when in due_threads(session, now):
        extra = [f"ref {', '.join(t.refs)}"] if t.refs else []
        if has_value(t.worktree):
            extra.append(f"worktree {t.worktree}")
        lines.append(_clip(f"- {t.title}: due {t.due}" + (f" ({'; '.join(extra)})" if extra else "")))
    for t in session.threads:
        if has_value(t.due) and due_at(t.due, now.tzinfo) is None:  # type: ignore[arg-type] — now is aware
            lines.append(_clip(f"- {t.title}: `due: {t.due}` is not an ISO date or datetime, so it never comes due"))
    if len(lines) > _MAX_LINES:
        lines = [*lines[:_MAX_LINES], f"- … and {len(lines) - _MAX_LINES} more; read the entry"]
    return [_clip(HEADING.format(entry=title)), *lines, ""] if lines else []


def _lookup(
    session_id: str, now: datetime | None, search: Search | None, by_title: ByTitle | None, name: str | None,
    identity: str | None = None,
) -> list[str]:
    if search is None or by_title is None:
        engine_search, engine_by_title = _engine()
        search = search or engine_search
        by_title = by_title or engine_by_title
    resolved = session_name(session_id) if name is None else name
    if identity is None:
        from nexus.identity.board import identity_for_session  # noqa: PLC0415 -- hook import cost

        identity = identity_for_session(session_id)
    clock = now or datetime.now().astimezone()
    out: list[str] = []
    for title, session in _entries(resolved, search, by_title, identity):
        out.extend(_section(title, session, clock))
    return out


def due_lines(
    session_id: str,
    now: datetime | None = None,
    *,
    search: Search | None = None,
    by_title: ByTitle | None = None,
    name: str | None = None,
    budget_s: float | None = None,
    identity: str | None = None,
) -> list[str]:
    """The SessionStart section for *session_id*'s board: a heading, one line per due thread (and per ``due:`` that
    cannot be read), a blank; or ``[]``.

    *now* defaults to local time, so a ``due:`` date comes due at the operator's midnight. *search* and *by_title*
    default to the engine's ``threads`` project; *name* defaults to :func:`session_name`; *identity* defaults to the
    one a token file binds *session_id* to (RDR-253), and finds the entry. Returns within *budget_s*
    (default ``NX_BOARD_DUE_BUDGET_S``) plus thread start-up, whatever the engine does.
    """
    if not session_id:
        return []
    budget = _env_seconds("NX_BOARD_DUE_BUDGET_S", _DEFAULT_BUDGET_S) if budget_s is None else budget_s
    if search is None or by_title is None:
        # before the store exists: its structlog lines, and ours, must not reach the hook's stdout
        from nexus._hook_runtime._io import configure_hook_logging  # noqa: PLC0415

        configure_hook_logging()
    result: list[list[str]] = []
    failure: list[str] = []

    def work() -> None:
        try:
            from nexus.db.data_token import mint_deadline  # noqa: PLC0415 — a ContextVar: set in this thread

            with mint_deadline(budget):
                result.append(_lookup(session_id, now, search, by_title, name, identity))
        except Exception as exc:  # noqa: BLE001 — a hook must never fail; no reminder is the cost, and it is logged
            failure.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=work, name="board-due", daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        _log.warning("board_due_over_budget", session_id=session_id, budget_s=budget)
        return []
    if failure:
        _log.warning("board_due_lookup_failed", session_id=session_id, error=failure[0])
        return []
    return result[0] if result else []
