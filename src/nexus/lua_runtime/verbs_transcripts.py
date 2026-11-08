# SPDX-License-Identifier: AGPL-3.0-or-later
"""``transcripts.sessions(since, until, project, bodies, fields)`` (RDR-242
Technical Design 10; the record is RDR-240 Technical Design 4's; nexus-mdbds.8).

Reads Claude Code transcripts under ``$CLAUDE_CONFIG_DIR/projects`` (default
``~/.claude/projects``) and returns one record per transcript file: a main
session (``<project>/<session>.jsonl``) or a subagent
(``<project>/<session>/subagents/**/<agent>.jsonl``, workflow agents included).
Symbolic links are not followed.

A transcript holds everything its session saw, so a record carries no
message body unless ``bodies`` is true, and then only the assistant's own
text: never a tool result, a user prompt, thinking, or an attachment's
content. A tool call's input (a Bash command, a ``memory_get`` title) is not
a body and is carried whenever ``fields`` selects it (always, when ``fields``
is nil); ``tool_calls.input_bytes`` reports its size without carrying it. Nothing here logs, and no error message
carries a transcript's content.

The window is half-open, ``[since, until)``, in UTC; a bare date is that
date's 00:00 UTC, and a timestamp with no offset is read as UTC. A
transcript belongs to the window that holds its first timestamped record
(its start) and to no other, so a session that runs past midnight is
counted once. A file last written before ``since`` cannot start inside the
window and is not opened (a restored copy with an older mtime would be
missed).

The record:
- ``session_id``, ``main``, ``agent_id`` (a subagent's ``agentId``),
  ``project``, ``start`` and ``end`` (timestamps as written);
- ``models``; ``assistant_turns``, the number of distinct API messages (one
  message can be several transcript records, about 1.6 per message on real
  data, so this is not ``skill_routing_census.py``'s assistant-record
  count); ``tokens`` summed once per message; ``first_context_tokens``, the
  first message's input, cache-read and cache-creation tokens;
- ``tool_calls``: ``name``, ``input``, ``time``, ``result_time`` and
  ``is_error`` (from the result's marker, never its content; null when no
  result came);
- ``instruction_loads``: ``path``, ``kind`` (the attachment), ``type`` (User,
  Project and the like) and ``size`` (UTF-8 bytes of the file's content,
  never the content);
- ``slash_commands``: the name of each command a user ran, read only from a
  prompt the harness wrote (it starts with its ``<command-name>`` or
  ``<command-message>`` tag), one per prompt, main sessions only;
- ``deferred_tools`` and ``deferred_tools_dropped`` (names past 4,096);
- ``tool_call_count``, the tool calls in the file (after the replay dedup,
  before any ``fields`` filter or cut);
- ``bytes`` and ``records`` (lines) of the file; ``truncated``; and with
  ``bodies``, ``assistant_text``.

``fields`` (nexus-qo7ie.31) narrows the record to the named fields while the
file is parsed: an unselected field, above all a tool call's input, is never
cleaned or kept, and ``bodies`` true without ``assistant_text`` collects no
text. That cuts the result's size and the memory a scan retains; it does not
cut the read or, by itself, the decode. The decode is cut whatever ``fields``
says, and only for a line of 4 KB or more whose type is not ``user``,
``assistant`` or ``attachment`` (``_inert``); those three types are always
decoded. See ``_parse_fields`` for the grammar. ``project`` may also be
the reserved value ``repo``: every project directory holding the sessions of
the repository the run started in (``transcripts_projects``); a project
directory literally named ``repo`` cannot be asked for by name.

Every field is read defensively: a value of the wrong type is ignored, and a
line that is not JSON is skipped, so one malformed record never fails a
call. Strings are made valid UTF-8 (a lone surrogate becomes U+FFFD) and
bounded.

Limits:
- the deadline (gate r3 S2): checked before each transcript file and every
  ``LINES_PER_DEADLINE_CHECK`` lines or ``BYTES_PER_DEADLINE_CHECK`` bytes
  inside one; past it, ``VerbTimeout``.
- one record (RDR-240 TD4): over ``RECORD_CAP_BYTES`` of JSON, its tool-call
  inputs and assistant text are cut, then its lists lose their tails
  (counted in ``<list>_dropped``), until it fits; it is marked
  ``truncated: true``.
- the whole result (gate r3 S3): over the verb result cap the verb refuses
  with a ``VerbError`` whose message starts ``result-too-large:``, so a
  procedure can narrow its window; it never drops records silently.
"""

from __future__ import annotations

import json
import math
import os
import re
import stat
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from nexus.lua_runtime import verb_types
from nexus.lua_runtime.transcripts_projects import repo_project_dirs
from nexus.lua_runtime.verb_types import (
    JSONValue,
    ParamSpec,
    VerbArgError,
    VerbContext,
    VerbError,
    VerbSpec,
    VerbTimeout,
)

VERB = "transcripts.sessions"
REPO_PROJECT = "repo"
REPO_NEEDED = (
    f'{VERB}: project "{REPO_PROJECT}" needs a repository (the run started outside one)'
)
TOO_LARGE = "result-too-large"
RECORD_TOO_LARGE = "record-too-large"
RECORD_CAP_BYTES = 1024 * 1024
LINES_PER_DEADLINE_CHECK = 2000
BYTES_PER_DEADLINE_CHECK = 8 * 1024 * 1024
INPUT_STRING_CAP = 256 * 1024  # one string of a tool-call input, as read
_CUT_STEPS = (65536, 16384, 4096, 1024, 256)
_LAST_CUTS = (64, 0)
_DROPPABLE = (
    "tool_calls",
    "assistant_text",
    "instruction_loads",
    "slash_commands",
    "deferred_tools",
)
_MAX_DEPTH = 64
_MAX_MODELS = 64
_MAX_DEFERRED = 4096
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SURROGATE = re.compile("[\ud800-\udfff]")
# A slash command is a user prompt the harness wrote: it starts with one of
# these tags, and its name is a command name, never free text.
_COMMAND_STARTS = ("<command-name>", "<command-message>")
_COMMAND = re.compile(r"<command-name>/?([A-Za-z0-9_:.\-/]{1,64})</command-name>")
_COMMAND_HEAD = 512
MAX_LINE_BYTES = 64 * 1024 * 1024  # a longer line is skipped, never held
LINE_CHUNK = 1024 * 1024
_LUA_INT_MAX = 2**63 - 1

_now = time.monotonic


# ---- values ----------------------------------------------------------------


def _text(value: Any, limit: int) -> str | None:
    """``value`` as valid UTF-8 cut to ``limit`` characters, or None."""
    if not isinstance(value, str):
        return None
    value = value[:limit]
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        value = _SURROGATE.sub("\ufffd", value)
    return value


def _count(value: Any) -> int:
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 <= value <= _LUA_INT_MAX
    ):
        return value
    return 0


def _clean(value: Any, cut: list[bool], depth: int = 0) -> JSONValue:
    """A tool-call input made safe to carry: valid UTF-8, bounded, finite."""
    if depth > _MAX_DEPTH:
        cut[0] = True
        return None
    if isinstance(value, str):
        if len(value) > INPUT_STRING_CAP:
            cut[0] = True
        return _text(value, INPUT_STRING_CAP)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value if -_LUA_INT_MAX - 1 <= value <= _LUA_INT_MAX else None
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            _text(k, 256) or "": _clean(v, cut, depth + 1)
            for k, v in value.items()
            if isinstance(k, str)
        }
    if isinstance(value, list):
        return [_clean(v, cut, depth + 1) for v in value]
    return None


def _cut(value: JSONValue, limit: int) -> JSONValue:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, dict):
        return {k: _cut(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_cut(v, limit) for v in value]
    return value


def _size(value: JSONValue) -> int:
    """Bytes as the wire carries them: encode_result's compact UTF-8 form."""
    return len(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    )


def _bound(value: str, name: str) -> datetime:
    shown = value[:64]
    try:
        if _DATE.fullmatch(value):
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day, tzinfo=UTC)
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise VerbError(
            f"{VERB}: {name} {shown!r} is not a UTC date or timestamp"
        ) from None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _stamp(value: Any) -> tuple[str, datetime] | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value, (t.replace(tzinfo=UTC) if t.tzinfo is None else t)


def _check(deadline: float) -> None:
    if _now() >= deadline:
        raise VerbTimeout(f"{VERB}: the run's time budget ran out reading transcripts")


# ---- fields ------------------------------------------------------------------

_SCALARS = (
    "session_id", "main", "agent_id", "project", "start", "end", "models",
    "assistant_turns", "tokens", "first_context_tokens", "tool_call_count",
    "deferred_tools", "deferred_tools_dropped", "bytes", "records", "truncated",
)  # fmt: skip
_SUBS: dict[str, tuple[str, ...]] = {
    "tool_calls": ("name", "input", "time", "result_time", "is_error", "input_bytes"),
    "instruction_loads": ("path", "kind", "type", "size", "time"),
    "slash_commands": ("name", "time"),
    "assistant_text": ("time", "text"),
}
# A bare list name, and a nil ``fields``, keep today's keys: ``input_bytes`` is
# a derived measure that must be asked for by name.
_BARE: dict[str, tuple[str, ...]] = {
    k: tuple(x for x in v if x != "input_bytes") for k, v in _SUBS.items()
}
_ALWAYS = ("session_id", "agent_id", "main", "truncated")
_INPUT_AT = "input@"


class _Fields:
    """What a call selects. ``top`` is the record's keys; ``keys`` the sub-keys
    each selected list keeps per element. A call keeps its ``input`` when
    ``input_all``, or its tool name is in ``input_exact`` or ends with one of
    ``input_suffix``."""

    def __init__(
        self,
        top: frozenset[str],
        keys: dict[str, frozenset[str]],
        input_all: bool = True,
        input_exact: frozenset[str] = frozenset(),
        input_suffix: tuple[str, ...] = (),
    ) -> None:
        self.top = top
        self.keys = keys
        self.input_all = input_all
        self.input_exact = input_exact
        self.input_suffix = input_suffix

    def wants_input(self, tool: str | None) -> bool:
        if "input" not in self.keys.get("tool_calls", ()):
            return False
        if self.input_all:
            return True
        return tool is not None and (
            tool in self.input_exact or tool.endswith(self.input_suffix)
        )


def _all_fields(bodies: bool) -> _Fields:
    lists = {
        k: frozenset(v) for k, v in _BARE.items() if bodies or k != "assistant_text"
    }
    return _Fields(frozenset(_SCALARS) | frozenset(lists), lists)


def _input_bytes(value: Any) -> int:
    """UTF-8 bytes of an input's top-level string values, as parsed (before any clean)."""
    if not isinstance(value, dict):
        return 0
    return sum(
        len(v.encode("utf-8", "surrogatepass"))
        for v in value.values()
        if isinstance(v, str)
    )


def _parse_fields(fields: str | None, bodies: bool) -> _Fields:
    """The selection ``fields`` names; nil is every field, as without ``fields``.

    ``fields`` is comma-separated names, whitespace around a name ignored. A
    name is a record key, ``<list>.<sub>`` for a list's per-element key, or
    ``tool_calls.input@<Tool>``: the input only on calls named exactly
    ``<Tool>``, or with ``*<tail>``, on calls whose name ends with ``<tail>``
    (repeatable; one leading ``*`` and a non-empty tail, no ``*`` elsewhere, no
    whitespace inside the name). ``tool_calls.input``, plain or ``@``, needs
    ``tool_calls.name`` too, so no row is an anonymous ``{}``.
    ``tool_calls.input_bytes`` is the UTF-8 size of the input's top-level
    string values, on every call. A bare list name keeps every sub-key but
    ``input_bytes``, and so wins over sub-keys named beside it; a plain
    ``tool_calls.input`` wins over ``input@`` forms. ``session_id``,
    ``agent_id``, ``main`` and ``truncated`` always come back. An empty string
    or name, an unknown name, or ``assistant_text`` without ``bodies`` is a
    ``VerbArgError``.
    """
    if fields is None:
        return _all_fields(bodies)
    top: set[str] = set()
    bare: set[str] = set()
    subs: dict[str, set[str]] = {}
    exact: set[str] = set()
    suffix: set[str] = set()
    for raw in fields.split(","):
        name = raw.strip()
        if not name:
            raise VerbArgError(f"{VERB}: fields has an empty name")
        shown = name[:64]
        head, dot, tail = name.partition(".")
        if not dot:
            if head not in _SCALARS and head not in _SUBS:
                raise VerbArgError(f"{VERB}: unknown field {shown!r}")
            top.add(head)
            if head in _SUBS:
                bare.add(head)
        else:
            if head not in _SUBS:
                raise VerbArgError(f"{VERB}: unknown field {shown!r}")
            if head == "tool_calls" and tail.startswith(_INPUT_AT):
                tool = tail[len(_INPUT_AT) :]
                match = tool[1:] if tool.startswith("*") else tool
                if not match or "*" in match or match != match.strip():
                    raise VerbArgError(
                        f"{VERB}: field {shown!r} must name a tool (or *<tail>) "
                        "with no other * and no surrounding whitespace"
                    )
                (suffix if tool.startswith("*") else exact).add(match)
            elif tail in _SUBS[head]:
                subs.setdefault(head, set()).add(tail)
            else:
                raise VerbArgError(f"{VERB}: unknown field {shown!r}")
            top.add(head)
    if "assistant_text" in top and not bodies:
        raise VerbArgError(f"{VERB}: assistant_text needs bodies to be true")
    keys: dict[str, frozenset[str]] = {}
    for lst in _SUBS:
        if lst in top:
            keys[lst] = frozenset(
                (set(_BARE[lst]) | set(subs.get(lst, ())))
                if lst in bare
                else subs.get(lst, ())
            )
    calls = keys.get("tool_calls")
    input_all = False
    if calls is not None:
        input_all = "input" in calls
        if (input_all or exact or suffix) and "name" not in calls:
            raise VerbArgError(
                f"{VERB}: tool_calls.input needs tool_calls.name, so a call is "
                "never an anonymous row"
            )
        if exact or suffix:
            keys["tool_calls"] = calls | {"input"}
    return _Fields(
        frozenset(top) | frozenset(_ALWAYS),
        keys,
        input_all,
        frozenset(exact),
        tuple(sorted(suffix)),
    )


# ---- files -----------------------------------------------------------------


def _projects_root() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


def _plain_dir(p: Path) -> bool:
    try:
        return stat.S_ISDIR(p.lstat().st_mode)
    except OSError:
        return False


def _linked(path: Path, top: Path) -> bool:
    """Whether ``path`` or a directory between it and ``top`` is a link."""
    p = path
    while p != top and p != p.parent:
        if p.is_symlink():
            return True
        p = p.parent
    return False


def _files(
    root: Path, project: str | None, deadline: float, repo_root: Path | None
) -> Iterator[tuple[str, Path, bool]]:
    """Transcript files under ``root`` (which may itself be a link), links below it excluded."""
    if project == REPO_PROJECT and repo_root is None:
        raise VerbError(REPO_NEEDED)
    if not root.is_dir():
        raise VerbError(f"{VERB}: no Claude Code projects directory at {root}")
    if project is None or project == "all":
        dirs = sorted(p for p in root.iterdir() if _plain_dir(p))
    elif project == REPO_PROJECT:
        try:
            names = repo_project_dirs(root, repo_root, deadline=deadline)
        except ValueError:
            raise VerbError(REPO_NEEDED) from None
        dirs = sorted(d for d in (root / n for n in names) if _plain_dir(d))
    else:
        if (
            project in ("", ".", "..")
            or len(project) > 255
            or any(c in project for c in "/\\:\x00")
        ):
            raise VerbError(
                f"{VERB}: project {project[:64]!r} is not a project directory name"
            )
        d = root / project
        if d.parent != root or not _plain_dir(d):
            raise VerbError(f"{VERB}: no project {project!r} under {root}")
        dirs = [d]
    for d in dirs:
        _check(deadline)
        name = _text(d.name, 255) or ""
        for main in sorted(d.glob("*.jsonl")):
            if not _linked(main, d):
                yield name, main, True
        for sub in sorted(d.glob("*/subagents/**/*.jsonl")):
            if not _linked(sub, d):
                yield name, sub, False


# ---- one transcript ----------------------------------------------------------


def _byte_size(content: Any) -> int | None:
    """UTF-8 bytes of an instruction file's content (never the content), or None."""
    if not isinstance(content, str):
        return None
    return len(content.encode("utf-8", "surrogatepass"))


_OMIT: Any = object()


def _pick(keys: frozenset[str], **values: Any) -> dict[str, Any]:
    """The selected ``keys`` of ``values``; a callable is called only if selected.

    A callable returning ``_OMIT`` leaves its key out.
    """
    out: dict[str, Any] = {}
    for k, v in values.items():
        if k not in keys:
            continue
        if callable(v):
            v = v()
            if v is _OMIT:
                continue
        out[k] = v
    return out


class _Session:
    def __init__(self, main: bool, f: _Fields) -> None:
        self.main = main
        self.f = f
        self.call_keys = f.keys.get("tool_calls")
        self.load_keys = f.keys.get("instruction_loads")
        self.command_keys = f.keys.get("slash_commands")
        self.text_keys = f.keys.get("assistant_text")
        self.track_results = self.call_keys is not None and bool(
            self.call_keys & {"result_time", "is_error"}
        )
        self.want_deferred = bool(f.top & {"deferred_tools", "deferred_tools_dropped"})
        self.call_count = 0
        self.start: str | None = None
        self.end: str | None = None
        self.session_id: str | None = None
        self.agent_id: str | None = None
        self.models: set[str] = set()
        self.messages: set[str] = set()
        self.turns = 0
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        self.first_context: int | None = None
        self.calls: list[dict[str, Any]] = []
        self.pending: dict[str, dict[str, Any]] = {}
        self.call_ids: set[str] = set()
        self.loads: list[dict[str, Any]] = []
        self.deferred: set[str] = set()
        self.deferred_dropped = 0
        self.text: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.cut = [False]

    def take(self, rec: dict[str, Any], stamp: str | None) -> None:
        if self.session_id is None:
            self.session_id = _text(rec.get("sessionId"), 128)
        if self.agent_id is None and not self.main:
            self.agent_id = _text(rec.get("agentId"), 128)
        if stamp is not None:
            self.end = stamp
        kind = rec.get("type")
        message = rec.get("message")
        if kind == "assistant" and isinstance(message, dict):
            self._assistant(message, stamp)
        elif kind == "user" and isinstance(message, dict):
            self._user(message, stamp)
        elif kind == "attachment" and isinstance(rec.get("attachment"), dict):
            self._attachment(rec["attachment"], stamp)

    def _assistant(self, message: dict[str, Any], stamp: str | None) -> None:
        model = _text(message.get("model"), 128)
        if model and len(self.models) < _MAX_MODELS:
            self.models.add(model)
        mid = message.get("id")
        new = not (isinstance(mid, str) and mid in self.messages)
        if isinstance(mid, str):
            self.messages.add(mid)
        usage = message.get("usage")
        if new:
            self.turns += 1
            if isinstance(usage, dict):
                fresh = _count(usage.get("input_tokens"))
                read = _count(usage.get("cache_read_input_tokens"))
                made = _count(usage.get("cache_creation_input_tokens"))
                self.tokens["input"] += fresh
                self.tokens["output"] += _count(usage.get("output_tokens"))
                self.tokens["cache_read"] += read
                self.tokens["cache_creation"] += made
                if self.first_context is None:
                    self.first_context = fresh + read + made
        content = message.get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tid = block.get("id")
                if isinstance(tid, str):
                    if tid in self.call_ids:
                        continue  # a replayed record
                    self.call_ids.add(tid)
                self.call_count += 1
                if self.call_keys is not None:
                    call = self._call(block, stamp)
                    self.calls.append(call)
                    if self.track_results and isinstance(tid, str):
                        self.pending[tid] = call
            elif btype == "text" and self.text_keys is not None:
                body = block.get("text")
                if isinstance(body, str):
                    self.text.append(
                        _pick(
                            self.text_keys,
                            time=stamp,
                            text=lambda: _text(body, len(body)),
                        )
                    )

    def _call(self, block: dict[str, Any], stamp: str | None) -> dict[str, Any]:
        """One tool call with the selected keys only: an unselected input is never cleaned."""
        keys = self.call_keys or frozenset()
        name = _text(block.get("name"), 256)
        return _pick(
            keys,
            name=name,
            input=lambda: (
                _clean(block.get("input"), self.cut)
                if self.f.wants_input(name)
                else _OMIT
            ),
            time=stamp,
            result_time=None,
            is_error=None,
            input_bytes=lambda: _input_bytes(block.get("input")),
        )

    def _user(self, message: dict[str, Any], stamp: str | None) -> None:
        content = message.get("content")
        if isinstance(content, str):
            self._commands(content, stamp)
            return
        first = True
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                first = False
                continue
            if block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                call = self.pending.pop(tid, None) if isinstance(tid, str) else None
                if call is not None:
                    if "is_error" in call:
                        call["is_error"] = block.get("is_error") is True
                    if "result_time" in call:
                        call["result_time"] = stamp
            elif (
                first
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                self._commands(block["text"], stamp)
            first = False

    def _commands(self, text: str, stamp: str | None) -> None:
        """One command name from a prompt the harness wrote, never prompt text."""
        if not self.main:
            return
        head = text[:_COMMAND_HEAD].lstrip()
        if not head.startswith(_COMMAND_STARTS):
            return
        m = _COMMAND.search(head)
        if m is not None and self.command_keys is not None:
            self.commands.append(_pick(self.command_keys, name=m.group(1), time=stamp))

    def _attachment(self, att: dict[str, Any], stamp: str | None) -> None:
        akind = att.get("type")
        if akind in ("deferred_tools_record", "deferred_tools_delta"):
            if self.want_deferred:
                self._deferred(akind, att)
        elif self.load_keys is None:
            return
        elif akind == "instructions":
            files = att.get("files")
            for f in files if isinstance(files, list) else []:
                if isinstance(f, dict):
                    self._load(
                        f.get("path"), f.get("type"), f.get("content"), akind, stamp
                    )
        elif akind == "nested_memory":
            inner = att.get("content")
            inner = inner if isinstance(inner, dict) else {}
            self._load(
                att.get("path"), inner.get("type"), inner.get("content"), akind, stamp
            )

    def _deferred(self, akind: str, att: dict[str, Any]) -> None:
        if akind == "deferred_tools_record":
            entries = att.get("entries")
            for e in entries if isinstance(entries, list) else []:
                if isinstance(e, dict) and e.get("defer_loading") is True:
                    self._defer(e.get("name"))
        else:
            names = att.get("addedNames")
            for n in names if isinstance(names, list) else []:
                self._defer(n)

    def _defer(self, name: Any) -> None:
        n = _text(name, 256)
        if not n or n in self.deferred:
            return
        if len(self.deferred) < _MAX_DEFERRED:
            self.deferred.add(n)
        else:
            self.deferred_dropped += 1

    def _load(
        self, path: Any, ftype: Any, content: Any, kind: str, stamp: str | None
    ) -> None:
        p = _text(path, 4096)
        if p is None or self.load_keys is None:
            return
        self.loads.append(
            _pick(
                self.load_keys,
                path=p,
                kind=kind,
                type=lambda: _text(ftype, 64),
                size=lambda: _byte_size(content),
                time=stamp,
            )
        )


# ---- lines no selected field needs -------------------------------------------
#
# ``json.loads`` is a third to a half of a scan. Most of the bytes of a
# transcript are lines that add nothing to the record but a stamp and ids:
# ``api-request``, ``api-request-blob``, ``api-request-shape``,
# ``queue-operation`` and the like, whose type is not ``user``, ``assistant`` or
# ``attachment``. Such a line still counts for four things: its timestamp (the
# first stamped line decides the window, the last sets ``end``), its
# ``isSidechain`` (a main file drops such a line), and its ``sessionId`` and
# ``agentId`` (the first of each wins). ``_inert`` reads those from the raw
# text and returns them in place of the decoded record.
#
# The run is consumed member by member from the opening brace (``_RUN``). Its
# string pattern bans ``"`` and ``\``, so a member is a whole ``"key":scalar``
# and the run holds no nested value: each member in it is a member of the
# top-level object. The first byte that does not continue the run ends it, and
# ``_NEXT`` demands a plain-key member (or the closing brace) there, so an
# unreadable member does not hide an escaped key right after the run. The type
# and each wanted key must be in the run, or the wanted key's quoted name must
# occur nowhere in the line; otherwise the line is decoded. A line must end in
# a newline, so the torn last write of a live file is decoded (and dropped) as
# before; a complete last line without one is decoded too.
#
# What the text does not prove, and why it is assumed: the harness writes
# ``JSON.stringify`` output, which has no repeated key, no ``\u`` escape on an
# ASCII character of a key, and, once a torn write is excluded, no malformed
# interior. A repeated or ``\u``-spelled key past the run, or a malformed
# interior with a newline, would be taken as valid; so would a line nested
# deeper than ``RecursionError`` allows, or holding an integer past the
# interpreter's digit limit, which ``json.loads`` drops.
#
# User, assistant and attachment lines are never skipped: measured on real transcripts,
# the regexes that prove a tool_result or a prompt line's shape run slower than
# ``json.loads`` on its string content, and an attachment's stamp sits behind a
# nested object that only a full parse can step over.

_PREPARSE = True  # a test seam: the tests switch it off to compare
_PREPARSE_MIN = 4096  # bytes; under it json.loads is as cheap as the scan
_ACTING = frozenset({"user", "assistant", "attachment"})

_VALUE = rb'(?:"[^"\\\x00-\x1f]{0,512}"|-?[0-9][0-9.eE+\-]{0,31}|true|false|null)'
_PAIR = re.compile(rb'"([A-Za-z0-9_]+)":(' + _VALUE + rb")")
_MEMBER = rb'"[A-Za-z0-9_]+":' + _VALUE
_RUN = re.compile(rb"(?:" + _MEMBER + rb"(?:," + _MEMBER + rb")*+)?+")
_NEXT = re.compile(rb',"[A-Za-z0-9_]+":|[ \t\r\n]*\}[ \t\r\n]*\Z')


def _inert(raw: bytes, s: _Session) -> dict[str, Any] | None:
    """The stamp and ids of a line that adds nothing else, or None to decode it.

    The result stands in for the decoded record: ``type``, and where the line
    has them as top-level strings, ``timestamp``, ``sessionId`` and
    ``agentId``, and ``isSidechain`` when true. ``s`` only widens what must be
    proven: ``isSidechain`` for a main file, and an id while ``s`` still lacks
    one (a line that names such a key outside the run is decoded).
    """
    n = len(raw)
    if not _PREPARSE or n < _PREPARSE_MIN or raw[:1] != b"{":
        return None
    end = _RUN.match(raw, 1).end()  # type: ignore[union-attr]  # always matches
    found: dict[bytes, bytes] = {}
    for key, value in _PAIR.findall(raw, 1, end):
        if key in found:
            return None
        found[key] = value
    kind = _plain(found.get(b"type"))
    if kind is None or kind in _ACTING or _NEXT.match(raw, end) is None:
        return None
    if not raw.endswith(b"\n"):
        return None  # a torn last write, or a last line the decode reads
    if not raw[-8:].rstrip(b" \t\r\n").endswith(b"}"):
        return None  # cut short
    need = [b"timestamp"]
    if s.main:
        need.append(b"isSidechain")
    if s.session_id is None:
        need.append(b"sessionId")
    if not s.main and s.agent_id is None:
        need.append(b"agentId")
    for key in need:
        if key not in found and b'"' + key + b'"' in raw:
            return None  # named in the line, but not in the run: it may be top-level
    if not raw.isascii():
        try:
            raw.decode("utf-8", "surrogatepass")
        except UnicodeDecodeError:
            return None
    rec: dict[str, Any] = {"type": kind}
    if found.get(b"isSidechain") == b"true":
        rec["isSidechain"] = True
    for key in (b"timestamp", b"sessionId", b"agentId"):
        token = found.get(key)
        if token is None or token[:1] != b'"':
            continue  # absent, or not a string: the decoded record reads it as None
        value = _plain(token)
        if value is None:
            return None
        rec[key.decode()] = value
    return rec


def _plain(token: bytes | None) -> str | None:
    """A JSON string token with no escape in it, as text; None otherwise."""
    if token is None or token[:1] != b'"' or token[-1:] != b'"' or len(token) < 2:
        return None
    try:
        return token[1:-1].decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return None


def _scan(
    path: Path,
    project: str,
    main: bool,
    since: datetime,
    until: datetime,
    f: _Fields,
    deadline: float,
) -> tuple[str, dict[str, Any]] | None:
    """The transcript's start and record, or None when it starts outside the window."""
    s = _Session(main, f)
    lines = 0
    read = 0
    next_bytes = BYTES_PER_DEADLINE_CHECK
    try:
        fh = path.open("rb")
    except OSError:
        return None  # gone between the listing and the read (a live session)
    with fh:
        try:
            size = os.fstat(fh.fileno()).st_size
        except OSError:
            return None
        while True:
            raw = fh.readline(LINE_CHUNK)
            if not raw:
                break
            whole = raw.endswith(b"\n") or len(raw) < LINE_CHUNK
            lines += 1
            read += len(raw)
            long_line = False
            while not whole:  # a line longer than one chunk
                if read >= next_bytes:
                    _check(deadline)
                    next_bytes = read + BYTES_PER_DEADLINE_CHECK
                more = fh.readline(LINE_CHUNK)
                read += len(more)
                if len(raw) + len(more) > MAX_LINE_BYTES:
                    long_line = True
                    raw = b""
                if not long_line:
                    raw += more
                whole = not more or more.endswith(b"\n") or len(more) < LINE_CHUNK
            if lines % LINES_PER_DEADLINE_CHECK == 0 or read >= next_bytes:
                _check(deadline)
                next_bytes = read + BYTES_PER_DEADLINE_CHECK
            if long_line or not raw.strip():
                continue
            rec = _inert(raw, s)
            if rec is None:
                try:
                    rec = json.loads(raw, parse_constant=lambda _c: None)
                except (ValueError, RecursionError):
                    continue
            if not isinstance(rec, dict):
                continue
            if main and rec.get("isSidechain") is True:
                continue  # subagent traffic replayed into the session file
            stamped = _stamp(rec.get("timestamp"))
            stamp = stamped[0] if stamped else None
            if s.start is None:
                if stamped is None:
                    continue
                if not since <= stamped[1] < until:
                    return None
                s.start = stamp
            try:
                s.take(rec, stamp)
            except Exception:  # noqa: BLE001 -- a malformed record is skipped; its content never reaches a message
                continue
    if s.start is None:
        return None
    agent_id = None
    if not main:
        agent_id = s.agent_id or _text(path.stem.removeprefix("agent-"), 128)
    everything: dict[str, Any] = {
        "session_id": s.session_id
        or _text(path.stem if main else _session_dir(path).name, 128),
        "main": main,
        "agent_id": agent_id,
        "project": project,
        "start": s.start,
        "end": s.end,
        "models": sorted(s.models),
        "assistant_turns": s.turns,
        "tokens": s.tokens,
        "first_context_tokens": s.first_context,
        "tool_calls": s.calls,
        "tool_call_count": s.call_count,
        "instruction_loads": s.loads,
        "slash_commands": s.commands,
        "deferred_tools": sorted(s.deferred),
        "deferred_tools_dropped": s.deferred_dropped,
        "bytes": size,
        "records": lines,
        "truncated": s.cut[0],
    }
    if "assistant_text" in f.top:
        everything["assistant_text"] = s.text
    record = {k: v for k, v in everything.items() if k in f.top}
    return s.start, _fit(record)


def _session_dir(path: Path) -> Path:
    """``<project>/<session>`` for a file under ``<session>/subagents/``."""
    for p in path.parents:
        if p.name == "subagents":
            return p.parent
    return path.parent


def _fit(record: dict[str, Any]) -> dict[str, Any]:
    """Cut a record over ``RECORD_CAP_BYTES`` until it fits, marking it.

    Strings first, down to 256 characters, so the short inputs a query reads
    (a Bash command, a ``memory_get`` title) survive; then the lists' tails;
    then strings to nothing. A record that still does not fit is refused.
    """
    if _size(record) <= RECORD_CAP_BYTES:
        return record
    calls = record.get("tool_calls")
    text = record.get("assistant_text")
    record["truncated"] = True

    def cut_to(limit: int) -> None:
        if calls is not None:
            record["tool_calls"] = [
                {**c, "input": _cut(c["input"], limit)} if "input" in c else c
                for c in calls
            ]
        if text is not None:
            record["assistant_text"] = [
                {**t, "text": t["text"][:limit]} if "text" in t else t for t in text
            ]

    for limit in _CUT_STEPS:
        cut_to(limit)
        if _size(record) <= RECORD_CAP_BYTES:
            return record
    for key in _DROPPABLE:
        items = record.get(key)
        if not items:
            continue
        over = _size(record) - RECORD_CAP_BYTES
        dropped = 0
        while items and over > 0:
            over -= _size(items.pop()) + 1
            dropped += 1
        record[f"{key}_dropped"] = dropped
        calls = record.get("tool_calls")
        text = record.get("assistant_text")
        while items and _size(record) > RECORD_CAP_BYTES:  # the count's own bytes
            items.pop()
            record[f"{key}_dropped"] += 1
        if _size(record) <= RECORD_CAP_BYTES:
            return record
    for limit in _LAST_CUTS:
        cut_to(limit)
        if _size(record) <= RECORD_CAP_BYTES:
            return record
    raise VerbError(
        f"{RECORD_TOO_LARGE}: {VERB}: one transcript's record does not fit in 1 MB"
    )


# ---- the verb ----------------------------------------------------------------


def sessions(
    since: str,
    until: str,
    project: str | None = None,
    bodies: bool | None = None,
    fields: str | None = None,
    *,
    deadline: float,
    ctx: VerbContext,
) -> JSONValue:
    """One record per transcript that starts in ``[since, until)``, in start order.

    ``project`` is a Claude Code project directory name; nil or ``all`` means
    every project; ``repo`` means every project directory of the repository
    the run started in (``ctx.repo_root``). ``bodies`` adds the assistant's
    text. ``fields`` narrows each record (``_parse_fields``); it never
    changes which records come back or their order.
    """
    _check(deadline)
    lo = _bound(since, "since")
    hi = _bound(until, "until")
    f = _parse_fields(fields, bool(bodies))
    if project == REPO_PROJECT and lo >= hi:
        if ctx.repo_root is None:
            raise VerbError(REPO_NEEDED)
        return []  # an empty window asks the resolver nothing
    root = _projects_root()
    files = list(_files(root, project, deadline, ctx.repo_root))
    if lo >= hi:
        return []
    cap = verb_types.VERB_RESULT_CAP_BYTES
    floor = lo.timestamp()
    out: list[tuple[str, dict[str, Any]]] = []
    total = 1  # "[" now; each record brings its "," or the closing "]"
    for proj, path, main in files:
        _check(deadline)
        try:
            st = path.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_mtime < floor:
            continue  # not a plain file, or last written before the window opened
        got = _scan(path, proj, main, lo, hi, f, deadline)
        if got is None:
            continue
        out.append(got)
        total += _size(got[1]) + 1
        if total > cap:
            raise VerbError(
                f"{TOO_LARGE}: {VERB}: more than {cap} bytes of records after "
                f"{len(out)} transcripts; narrow the window or the project"
            )
    out.sort(
        key=lambda g: (
            (_stamp(g[0]) or ("", lo))[1],
            not g[1]["main"],
            g[1]["agent_id"] or "",
        )
    )
    return [rec for _, rec in out]


VERBS: tuple[VerbSpec, ...] = (
    VerbSpec(
        VERB,
        (
            ParamSpec("since", "string"),
            ParamSpec("until", "string"),
            ParamSpec("project", "string", optional=True),
            ParamSpec("bodies", "boolean", optional=True),
            ParamSpec("fields", "string", optional=True),
        ),
        sessions,
    ),
)
