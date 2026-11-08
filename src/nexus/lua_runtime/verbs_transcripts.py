# SPDX-License-Identifier: AGPL-3.0-or-later
"""``transcripts.sessions(since, until, project, bodies)`` (RDR-242 Technical
Design 10; the record is RDR-240 Technical Design 4's; nexus-mdbds.8).

Reads Claude Code transcripts under ``$CLAUDE_CONFIG_DIR/projects`` (default
``~/.claude/projects``) and returns one record per transcript file: a main
session (``<project>/<session>.jsonl``) or a subagent
(``<project>/<session>/subagents/<agent>.jsonl``).

A transcript holds everything its session saw, so a record carries no
message body unless ``bodies`` is true, and then only the assistant's own
text: never a tool result, a user prompt or an attachment's content. A
tool call's input (a Bash command, a ``memory_get`` title) is not a body and
is always carried. Nothing here logs a transcript's content.

The window is half-open, ``[since, until)``, in UTC; a bare date is that
date's 00:00 UTC, and a timestamp with no offset is read as UTC. A
transcript belongs to the window that holds its first timestamped record
(its start) and to no other, so a session that runs past midnight is
counted once.

Limits:
- the deadline (gate r3 S2): checked before each transcript file and every
  ``LINES_PER_DEADLINE_CHECK`` lines inside one; past it, ``VerbTimeout``.
- one record (RDR-240 TD4): over ``RECORD_CAP_BYTES`` of JSON, its tool-call
  inputs and assistant text are cut, then its longest lists lose their
  tail, until it fits; it is marked ``truncated: true``.
- the whole result (gate r3 S3): over ``VERB_RESULT_CAP_BYTES`` of JSON the
  verb refuses with ``VerbError``; it never drops records silently.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from nexus.lua_runtime.verb_types import (
    VERB_RESULT_CAP_BYTES,
    JSONValue,
    ParamSpec,
    VerbError,
    VerbSpec,
    VerbTimeout,
)

VERB = "transcripts.sessions"
RECORD_CAP_BYTES = 1024 * 1024
LINES_PER_DEADLINE_CHECK = 2000
_CUT_STEPS = (65536, 16384, 4096, 1024, 256, 64, 0)
_DROPPABLE = ("tool_calls", "assistant_text", "instruction_loads", "deferred_tools")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")

_now = time.monotonic


def _projects_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


def _bound(value: str, name: str) -> datetime:
    try:
        if _DATE.fullmatch(value):
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise VerbError(
            f"{VERB}: {name} {value!r} is not a UTC date or timestamp"
        ) from None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t


def _check(deadline: float) -> None:
    if _now() >= deadline:
        raise VerbTimeout(f"{VERB}: the run's time budget ran out reading transcripts")


def _size(value: JSONValue) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _files(root: Path, project: str) -> Iterator[tuple[str, Path, bool, str | None]]:
    if not root.is_dir():
        raise VerbError(f"{VERB}: no Claude Code projects directory at {root}")
    if project == "all":
        dirs = sorted(p for p in root.iterdir() if p.is_dir())
    else:
        if project in ("", ".", "..") or any(c in project for c in "/\\\x00"):
            raise VerbError(
                f"{VERB}: project {project!r} is not a project directory name"
            )
        d = root / project
        if not d.is_dir():
            raise VerbError(f"{VERB}: no project {project!r} under {root}")
        dirs = [d]
    for d in dirs:
        for main in sorted(d.glob("*.jsonl")):
            yield d.name, main, True, None
        for sub in sorted(d.glob("*/subagents/*.jsonl")):
            yield d.name, sub, False, sub.stem


def _scan(
    path: Path,
    project: str,
    main: bool,
    agent_id: str | None,
    since: datetime,
    until: datetime,
    bodies: bool,
    deadline: float,
) -> dict[str, Any] | None:
    """The record for one transcript, or None when it starts outside the window."""
    start: str | None = None
    end: str | None = None
    session_id: str | None = None
    models: set[str] = set()
    seen_messages: set[str] = set()
    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    calls: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    loads: list[dict[str, Any]] = []
    deferred: set[str] = set()
    text: list[dict[str, Any]] = []
    lines = 0
    try:
        fh = path.open(encoding="utf-8", errors="replace")
        size = path.stat().st_size
    except OSError:
        return None  # gone between the listing and the read (a live session)
    with fh:
        for raw in fh:
            if not raw.strip():
                continue
            lines += 1
            if lines % LINES_PER_DEADLINE_CHECK == 0:
                _check(deadline)
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            stamp = rec.get("timestamp")
            if start is None:
                t = _ts(stamp)
                if t is None:
                    continue
                if not since <= t < until:
                    return None
                start = stamp
            if main and rec.get("isSidechain") is True:
                continue  # subagent traffic replayed into the session file
            if isinstance(rec.get("sessionId"), str):
                session_id = session_id or rec["sessionId"]
            if isinstance(stamp, str):
                end = stamp
            kind = rec.get("type")
            message = rec.get("message")
            if kind == "assistant" and isinstance(message, dict):
                if isinstance(message.get("model"), str):
                    models.add(message["model"])
                usage = message.get("usage")
                mid = message.get("id")
                if isinstance(usage, dict) and mid not in seen_messages:
                    if mid is not None:
                        seen_messages.add(mid)
                    tokens["input"] += int(usage.get("input_tokens") or 0)
                    tokens["output"] += int(usage.get("output_tokens") or 0)
                    tokens["cache_read"] += int(
                        usage.get("cache_read_input_tokens") or 0
                    )
                    tokens["cache_creation"] += int(
                        usage.get("cache_creation_input_tokens") or 0
                    )
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        call = {
                            "name": block.get("name"),
                            "input": block.get("input"),
                            "time": stamp,
                            "result_time": None,
                            "is_error": None,
                        }
                        calls.append(call)
                        if isinstance(block.get("id"), str):
                            pending[block["id"]] = call
                    elif bodies and block.get("type") == "text":
                        text.append({"time": stamp, "text": block.get("text") or ""})
            elif kind == "user" and isinstance(message, dict):
                content = message.get("content")
                for block in content if isinstance(content, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        call = pending.pop(str(block.get("tool_use_id")), None)
                        if call is not None:
                            call["is_error"] = block.get("is_error") is True
                            call["result_time"] = stamp
            elif kind == "attachment" and isinstance(rec.get("attachment"), dict):
                att = rec["attachment"]
                akind = att.get("type")
                if akind == "deferred_tools_record":
                    for e in att.get("entries") or []:
                        if (
                            isinstance(e, dict)
                            and e.get("defer_loading")
                            and isinstance(e.get("name"), str)
                        ):
                            deferred.add(e["name"])
                elif akind == "deferred_tools_delta":
                    deferred.update(
                        n for n in att.get("addedNames") or [] if isinstance(n, str)
                    )
                elif akind == "instructions":
                    for f in att.get("files") or []:
                        if isinstance(f, dict) and isinstance(f.get("path"), str):
                            loads.append(
                                {"path": f["path"], "kind": akind, "time": stamp}
                            )
                elif akind == "nested_memory" and isinstance(att.get("path"), str):
                    loads.append({"path": att["path"], "kind": akind, "time": stamp})
    if start is None:
        return None
    record: dict[str, Any] = {
        "session_id": session_id or (path.stem if main else path.parent.parent.name),
        "main": main,
        "agent_id": agent_id,
        "project": project,
        "start": start,
        "end": end,
        "models": sorted(models),
        "tokens": tokens,
        "tool_calls": calls,
        "instruction_loads": loads,
        "deferred_tools": sorted(deferred),
        "bytes": size,
        "records": lines,
        "truncated": False,
    }
    if bodies:
        record["assistant_text"] = text
    return _fit(record)


def _cut(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {k: _cut(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, limit) for v in value]
    return value


def _fit(record: dict[str, Any]) -> dict[str, Any]:
    """Cut a record over ``RECORD_CAP_BYTES`` until it fits, marking it."""
    if _size(record) <= RECORD_CAP_BYTES:
        return record
    calls = record["tool_calls"]
    text = record.get("assistant_text")
    record["truncated"] = True
    for limit in _CUT_STEPS:
        record["tool_calls"] = [{**c, "input": _cut(c["input"], limit)} for c in calls]
        if text is not None:
            record["assistant_text"] = [{**t, "text": t["text"][:limit]} for t in text]
        if _size(record) <= RECORD_CAP_BYTES:
            return record
    over = _size(record) - RECORD_CAP_BYTES
    for key in _DROPPABLE:
        items = record.get(key)
        if not items:
            continue
        dropped = 0
        while items and over > 0:
            over -= _size(items.pop()) + 1
            dropped += 1
        record[f"{key}_dropped"] = dropped
        if over <= 0:
            break
    while _size(record) > RECORD_CAP_BYTES:  # the dropped-count fields' own bytes
        for key in _DROPPABLE:
            if record.get(key):
                record[key].pop()
                record[f"{key}_dropped"] = record.get(f"{key}_dropped", 0) + 1
                break
        else:
            break
    return record


def sessions(
    since: str,
    until: str | None,
    project: str,
    bodies: bool | None,
    *,
    deadline: float,
) -> JSONValue:
    """One record per transcript that starts in ``[since, until)``, in start order.

    ``project`` is a Claude Code project directory name, or ``all``.
    ``until`` defaults to now. ``bodies`` adds the assistant's text.
    """
    _check(deadline)
    lo = _bound(since, "since")
    hi = _bound(until, "until") if until is not None else datetime.now(UTC)
    if lo >= hi:
        raise VerbError(f"{VERB}: since {since!r} is not before until {until!r}")
    out: list[dict[str, Any]] = []
    total = 2
    for proj, path, main, agent_id in _files(_projects_root(), project):
        _check(deadline)
        rec = _scan(path, proj, main, agent_id, lo, hi, bool(bodies), deadline)
        if rec is None:
            continue
        out.append(rec)
        total += _size(rec) + 1
        if total > VERB_RESULT_CAP_BYTES:
            raise VerbError(
                f"{VERB}: more than {VERB_RESULT_CAP_BYTES} bytes of records after "
                f"{len(out)} sessions; narrow the window or the project"
            )
    out.sort(key=lambda r: (_ts(r["start"]) or lo, not r["main"], r["agent_id"] or ""))
    return out


VERBS: tuple[VerbSpec, ...] = (
    VerbSpec(
        VERB,
        (
            ParamSpec("since", "string"),
            ParamSpec("until", "string", optional=True),
            ParamSpec("project", "string"),
            ParamSpec("bodies", "boolean", optional=True),
        ),
        sessions,
    ),
)
