# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart: one line naming the bound identity's own T2 notes (RDR-253 Step 6 part 3, nexus-hczdo).

A seat keeps its own standing context (open questions, recurring commands, what it owns) in T2 project ``identity/<id>``.
This prints up to five of its titles, newest first, as ONE line: ``Your notes (identity/i5): a, b, c (+2 more)``. It
prints nothing when the project is empty or the session has no identity (no token file names it). That is the only line
this change adds to SessionStart, at most :data:`MAX_LINE` bytes.

* The read is :meth:`~nexus.db.t2.http_memory_store.HttpMemoryStore.list_entries`, a title list, never a ``get`` or a
  ``search``. The 30-day retire rule counts transcript ``memory_get`` calls of ``identity/*`` titles (RDR-238's own
  measure), not ``access_count``: MCP ``memory_put`` re-reads each write, so a written note has been read once already.
* The identity is the one a token file binds *session_id* to. Claude Code starts the hooks of a matcher group with no
  guaranteed order, so ``nx-hook session-start`` (the bind) may not have listed a new session id yet; then the token file of
  this hook's own Claude ancestor names the identity (a delegated child, which keeps no notes, never takes this path). A
  fresh start the bind has not reached at all prints nothing; the next start does.
* An ephemeral or delegated identity cannot write the project (:mod:`nexus.identity.notes`), so for one the project is
  empty and nothing prints; no engine call asks the identity's kind.
* The lookup has one hard budget (``NX_IDENTITY_NOTES_BUDGET_S``, default 2 s) and runs in a daemon thread the hook
  stops waiting for, as :mod:`nexus.hooks.board_due` does: hook logging is configured on the main thread first, a data-token
  mint is cut to the budget, and the read is one attempt with a per-call timeout. Over budget, or on any error, it prints
  nothing and logs a warning to the hook log: a note list must never cost a session its start.
"""
from __future__ import annotations

import os
import threading
from typing import Any

import structlog

__all__ = ["MAX_LINE", "notes_lines"]

_log = structlog.get_logger(__name__)

_DEFAULT_BUDGET_S = 2.0
_DEFAULT_TIMEOUT_S = 1.5
#: Titles shown; the rest are counted.
_SHOWN = 5
#: Rows read. A project with more is shown as ``+N+ more`` (a lower bound), so the read stays small.
_READ_LIMIT = 50
#: A title is cut here (characters), and the whole line at MAX_LINE bytes: titles are free text the seat wrote.
_MAX_TITLE = 60
MAX_LINE = 300


def _env_seconds(name: str, default: float) -> float:
    try:
        return max(0.5, float(os.environ.get(name, default)))
    except ValueError:
        return default


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _cut_bytes(line: str, limit: int) -> str:
    """*line* cut to at most *limit* UTF-8 bytes (the SessionStart budgets count bytes, and ``…`` is three of them)."""
    if len(line.encode("utf-8")) <= limit:
        return line
    cut = line.encode("utf-8")[: limit - len("…".encode("utf-8"))].decode("utf-8", "ignore")
    return cut + "…"


def _identity_of(session_id: str) -> str:
    """The identity *session_id* belongs to: the token file that lists it, else the token file of this hook's own Claude
    ancestor (the bind may not have listed a new session id yet). ``""`` for none, and always for a delegated child."""
    from nexus.identity.board import identity_for_session  # noqa: PLC0415 — hook import cost

    found = identity_for_session(session_id)
    if found or os.environ.get("NX_IDENTITY_TOKEN", "").strip():
        return found
    from nexus.identity import token_header  # noqa: PLC0415
    from nexus.identity.token_file import read_token_file  # noqa: PLC0415

    path = token_header.token_file_path()
    record = read_token_file(path) if path is not None else None
    return str(record.identity) if record is not None else ""


def _engine_store(timeout: float) -> Any:
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — only a real lookup pays for it

    return HttpMemoryStore(timeout=timeout)


def _render(identity: str, titles: list[str], *, capped: bool) -> list[str]:
    if not titles:
        return []
    shown = ", ".join(_one_line(t, _MAX_TITLE) for t in titles[:_SHOWN])
    rest = len(titles) - _SHOWN
    more = f" (+{rest}{'+' if capped else ''} more)" if rest > 0 else ""
    return [_cut_bytes(f"Your notes (identity/{identity}): {shown}{more}", MAX_LINE)]


def notes_lines(
    session_id: str, *, identity: str | None = None, store: Any = None, budget_s: float | None = None
) -> list[str]:
    """The one SessionStart line for *session_id*'s identity, or ``[]``. Never raises; returns within *budget_s*
    (default ``NX_IDENTITY_NOTES_BUDGET_S``) plus thread start-up.

    *identity* defaults to the one a token file binds *session_id* to; *store* to the engine's memory store (a test
    passes one answering ``list_entries``)."""
    if not session_id:
        return []
    budget = _env_seconds("NX_IDENTITY_NOTES_BUDGET_S", _DEFAULT_BUDGET_S) if budget_s is None else budget_s
    timeout = _env_seconds("NX_IDENTITY_NOTES_TIMEOUT_S", _DEFAULT_TIMEOUT_S)
    if store is None:
        # before the store exists: its structlog lines, and ours, must not reach the hook's stdout (and this rewires the
        # process's logging, so it is done once, on the thread that spawns the worker)
        try:
            from nexus._hook_runtime._io import configure_hook_logging  # noqa: PLC0415

            configure_hook_logging()
        except Exception:  # noqa: BLE001 — a hook must never fail
            return []
    result: list[list[str]] = []
    failure: list[str] = []

    def work() -> None:
        try:
            from nexus.db.data_token import mint_deadline  # noqa: PLC0415 — a ContextVar: set in this thread

            owner = identity if identity is not None else _identity_of(session_id)
            if not owner:
                return
            from nexus.db.t2.http_memory_store import identity_project  # noqa: PLC0415

            with mint_deadline(budget):
                reader = store if store is not None else _engine_store(timeout)
                rows = reader.list_entries(
                    project=identity_project(owner), limit=_READ_LIMIT, timeout=timeout, retry_read_timeout=False)
            titles = [str(r.get("title", "")) for r in rows if r.get("title")]
            result.append(_render(owner, titles, capped=len(titles) >= _READ_LIMIT))
        except Exception as exc:  # noqa: BLE001 — a hook must never fail; no line is the cost, and it is logged
            failure.append(f"{type(exc).__name__}: {exc}")

    worker = threading.Thread(target=work, name="identity-notes", daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        _log.warning("identity_notes_over_budget", session_id=session_id, budget_s=budget)
        return []
    if failure:
        _log.warning("identity_notes_lookup_failed", session_id=session_id, error=failure[0])
        return []
    return result[0] if result else []
