# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart: one line naming the bound identity's own T2 notes (RDR-253 Step 6 part 3, nexus-hczdo).

A seat keeps its own standing context (open questions, recurring commands, what it owns) in T2 project ``identity/<id>``.
This prints up to five of its titles, newest first, as ONE line: ``Your notes (identity/i5): a, b, c (+2 more)``. It
prints nothing when the project is empty or the session has no identity (no token file names it). That is the only line
this change adds to SessionStart.

* The read is :meth:`~nexus.db.t2.http_memory_store.HttpMemoryStore.list_entries`, a title list. A ``get`` or a ``search``
  would count as a read of each entry and move ``access_count``, which is the measure the 30-day retire rule uses (the
  guidance goes if no seat reads its notes), so the hook must not touch it.
* An ephemeral or delegated identity cannot write the project (:mod:`nexus.identity.notes`), so for one the project is
  empty and nothing prints; no engine call asks the identity's kind.
* The lookup has one hard budget (``NX_IDENTITY_NOTES_BUDGET_S``, default 2 s) and runs in a daemon thread the hook
  stops waiting for, as :mod:`nexus.hooks.board_due` does. Over budget, or on any error, it prints nothing and logs a
  warning to the hook log: a note list must never cost a session its start.
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
#: A title is cut here, and the whole line is cut at MAX_LINE: titles are free text the seat wrote.
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


def _engine_store(timeout: float) -> Any:
    from nexus.db.t2.http_memory_store import HttpMemoryStore  # noqa: PLC0415 — only a real lookup pays for it

    return HttpMemoryStore(timeout=timeout)


def _render(identity: str, titles: list[str], *, capped: bool) -> list[str]:
    if not titles:
        return []
    shown = ", ".join(_one_line(t, _MAX_TITLE) for t in titles[:_SHOWN])
    rest = len(titles) - _SHOWN
    more = f" (+{rest}{'+' if capped else ''} more)" if rest > 0 else ""
    line = f"Your notes (identity/{identity}): {shown}{more}"
    return [line if len(line) <= MAX_LINE else line[: MAX_LINE - 1] + "…"]


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
    result: list[list[str]] = []
    failure: list[str] = []

    def work() -> None:
        try:
            owner = identity
            if owner is None:
                from nexus.identity.board import identity_for_session  # noqa: PLC0415 — hook import cost

                owner = identity_for_session(session_id)
            if not owner:
                return
            from nexus.db.t2.http_memory_store import identity_project  # noqa: PLC0415

            reader = store
            if reader is None:
                from nexus._hook_runtime._io import configure_hook_logging  # noqa: PLC0415 — before the store's own logs

                configure_hook_logging()
                reader = _engine_store(_env_seconds("NX_IDENTITY_NOTES_TIMEOUT_S", _DEFAULT_TIMEOUT_S))
            rows = reader.list_entries(project=identity_project(owner), limit=_READ_LIMIT)
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
