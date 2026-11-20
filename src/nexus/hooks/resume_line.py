# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The SessionStart resume line (RDR-253 Step 6 part 2): where the session left off, after ``/clear``.

The checkpoint hook (``conexus/hooks/scripts/_resume_pointer.py``, at PreCompact and at SessionEnd with reason ``clear``)
keeps one entry tagged ``resume-pointer`` in the identity scratch scope: the current thread, the worktree, that thread's
``state:`` and ``waiting_on:``. This prints the newest one as exactly ONE line of at most 400 bytes, with its age, and only
for source ``clear``: ``startup`` has no earlier session to resume, ``compact`` already gets the whole checkpoint,
``fork`` is a new line of work, and ``resume`` (an in-place ``/restart`` included) restores the conversation, so a pointer
would repeat what the model already holds. An entry counts only with the exact tag AND the ``resume <stamp> | `` head; one
older than 48 hours is dropped.

Never blocks, never raises: no identity, an engine that does not answer, an entry that is not a pointer, a read past
:data:`DEADLINE_SECONDS` (a daemon thread and a join, as ``memory_curation_check`` does) or any other failure prints
nothing and costs the guidance it is appended to nothing. It rides the ``session-start`` verb, so it adds no hook entry
and no process (the three-line SessionStart budget of the RDR-253 Step 6 proposal).

One structured log event per ``clear`` start, ``resume_line`` with ``source``, ``outcome`` (``hit``, ``empty``, ``error`` or
``timeout``) and ``age_s``, so the hit rate after ``/clear`` is a count, not a guess.

Standard library only at module scope: it runs on every session start.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, NamedTuple

from nexus._hook_runtime._io import _emit  # noqa: PLC2701 — the shared never-stdout logging spine

SOURCES = frozenset({"clear"})
TAG = "resume-pointer"
DEADLINE_SECONDS = 3.0
#: The longest line printed, in UTF-8 bytes (part 3's line is 300 B; the SessionStart design target is under 3 KB).
MAX_BYTES = 400
#: A pointer older than this is a different piece of work: dropped.
MAX_AGE_SECONDS = 48 * 3600
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_HEAD_LEN = len("resume 2026-11-20T09:00:00Z | ")


class _Result(NamedTuple):
    line: str | None
    outcome: str  # hit | empty | error | skip
    age_s: int | None
    error: str


def _open_store() -> Any:
    """The identity scope store. It needs no T1 session lease (the engine admits an identity-scope call with no session
    token) and creates no session, so it works at SessionStart(clear), before the new session id has a lease. If
    ``identity_scratch()`` fails (no identity, or the engine is not answering), the error reaches the caller, which logs the
    ``error`` outcome and prints nothing."""
    from nexus.identity.scratch_scope import identity_scratch  # noqa: PLC0415 — deferred: only a clear start pays for it

    return identity_scratch()


def _parse(entry: dict, now: datetime) -> tuple[datetime, str] | None:
    """``(written, body)`` of a pointer entry, or None: the exact tag and the ``resume <stamp> | `` head are both required."""
    tags = {t.strip() for t in str(entry.get("tags") or "").split(",")}
    content = entry.get("content")
    if TAG not in tags or not isinstance(content, str) or not content.startswith("resume ") or content[_HEAD_LEN - 3:_HEAD_LEN] != " | ":
        return None
    try:
        written = datetime.strptime(content[len("resume "):_HEAD_LEN - 3], _STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return written, content[_HEAD_LEN:]


def _age_label(age_s: int) -> str:
    if age_s < 60:
        return "just now"
    if age_s < 3600:
        return f"{age_s // 60}m ago"
    if age_s < 2 * 86400:
        return f"{age_s // 3600}h ago"
    return f"{age_s // 86400}d ago"


def _one_line(body: str, age_s: int) -> str:
    """The printed line: a label with the age, then the pointer's fields on one printable line, at most 400 bytes."""
    body = "".join(c if c.isprintable() else " " for c in body)
    body = " ".join(body.split())
    line = f"Resume pointer (identity scratch, written {_age_label(age_s)}): {body}"
    data = line.encode("utf-8")
    return line if len(data) <= MAX_BYTES else data[:MAX_BYTES].decode("utf-8", "ignore").rstrip()


def _read(payload: dict | None, open_store: Callable[[], Any] | None, now: datetime | None) -> _Result:
    """The line for this start and what happened, without logging. ``skip`` is every source but ``clear``."""
    source = payload.get("source") if isinstance(payload, dict) else None
    if source not in SOURCES:
        return _Result(None, "skip", None, "")
    try:
        store = (open_store or _open_store)()
        clock = now or datetime.now(timezone.utc)
        found = [p for e in store.list_entries() if (p := _parse(e, clock)) is not None]
        if not found:
            return _Result(None, "empty", None, "")
        written, body = max(found, key=lambda p: p[0])  # max keeps the first of equal stamps: list order, newest first
        age_s = max(0, int((clock - written).total_seconds()))
        if age_s > MAX_AGE_SECONDS:
            return _Result(None, "empty", age_s, "")
        return _Result(_one_line(body, age_s), "hit", age_s, "")
    except Exception as exc:  # noqa: BLE001 — a SessionStart line never fails a session
        return _Result(None, "error", None, f"{type(exc).__name__}: {str(exc)[:160]}")


def _log(result: _Result) -> None:
    fields: dict[str, Any] = {"source": "clear", "outcome": result.outcome, "age_s": result.age_s}
    if result.error:
        fields["error"] = result.error
    _emit("info", "resume_line", **fields)


def resume_line(payload: dict | None, *, open_store: Callable[[], Any] | None = None, now: datetime | None = None) -> str | None:
    """The one resume line for a ``clear`` start, or None, logging one ``resume_line`` event. *open_store* and *now* are the
    test seams."""
    result = _read(payload, open_store, now)
    if result.outcome != "skip":
        _log(result)
    return result.line


def _bounded_line(payload: dict | None, open_store: Callable[[], Any] | None, now: datetime | None) -> str | None:
    """:func:`resume_line` run in a daemon thread and waited for at most :data:`DEADLINE_SECONDS`; None on a timeout (the
    thread is abandoned and logs nothing when it finishes) or on any failure."""
    box: list[_Result] = []
    lock = threading.Lock()
    state = {"timed_out": False}

    def work() -> None:
        try:
            result = _read(payload, open_store, now)
        except BaseException:  # noqa: BLE001 — _read swallows already; this guards a replaced one
            return
        with lock:
            if not state["timed_out"]:
                box.append(result)

    worker = threading.Thread(target=work, name="resume-line", daemon=True)
    worker.start()
    worker.join(DEADLINE_SECONDS)
    with lock:
        if not box:
            source = payload.get("source") if isinstance(payload, dict) else None
            if worker.is_alive():
                state["timed_out"] = True
                if source in SOURCES:
                    _log(_Result(None, "timeout", None, ""))
            return None
        result = box[0]
    if result.outcome != "skip":
        _log(result)
    return result.line


def with_resume_line(output: str | None, payload: dict | None, *, open_store: Callable[[], Any] | None = None,
                     now: datetime | None = None) -> str | None:
    """*output* with the resume line appended after a blank line, or *output* unchanged."""
    line = _bounded_line(payload, open_store, now)
    if not line:
        return output
    return f"{output}\n\n{line}" if output else line
