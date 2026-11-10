# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a Claude Code transcript's lines, and the first record of one (RDR-240; nexus-qo7ie.27).

What ``transcripts.sessions`` (:mod:`nexus.lua_runtime.verbs_transcripts`) and ``nx memory curate`` share, so the
start rule is written once: the project directory and the file listing (``projects_root``, ``_files``,
``repo_main_transcripts``), the line framing with its deadline checks (``_lines``), the cheap stand-in for a large
line (``_inert``), the decode (``_decode``), the sidechain test (``_replayed``), the timestamp rule (``_stamp``) and
``first_start``, which reads a main transcript only as far as its first record. The verb's per-transcript state
(``_Session``) stays in the verb module. ``verbs_transcripts`` re-exports the public names.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from nexus.lua_runtime.transcripts_projects import repo_project_dirs
from nexus.lua_runtime.verb_types import VerbError, VerbTimeout

VERB = "transcripts.sessions"
REPO_PROJECT = "repo"
REPO_NEEDED = (
    f'{VERB}: project "{REPO_PROJECT}" needs a repository (the run started outside one)'
)
LINES_PER_DEADLINE_CHECK = 2000
BYTES_PER_DEADLINE_CHECK = 8 * 1024 * 1024
MAX_LINE_BYTES = 64 * 1024 * 1024  # a longer line is skipped, never held
LINE_CHUNK = 1024 * 1024
_SURROGATE = re.compile("[\ud800-\udfff]")

_now = time.monotonic


class _InertState(Protocol):
    """What ``_inert`` reads of the per-transcript state: ``_Session`` has it, and so does ``_Probe``."""

    main: bool
    inert_kinds: frozenset[str]
    session_id: str | None
    agent_id: str | None


class _Probe:
    """The state of a main transcript read with ``fields="start"`` (what ``_Session(True, ...)`` holds for ``_inert``):
    an attachment is inert, and no id is known yet. ``tests/lua_runtime/test_verbs_transcripts_first_start.py`` pins
    that it equals the real thing."""

    main = True
    inert_kinds = frozenset({"attachment"})
    session_id: str | None = None
    agent_id: str | None = None


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


# ---- files -----------------------------------------------------------------


def projects_root() -> Path:
    """``$CLAUDE_CONFIG_DIR/projects`` (default ``~/.claude/projects``): where the verb reads."""
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
# interpreter's digit limit, which ``json.loads`` drops. One of these can come
# from the harness: a session that crashes mid-write and resumes appends its
# next record to the torn line, and the skip keeps the torn head's type and
# stamp where the decode drops the whole line, so that transcript's start or
# end can move to the torn event's time. For an attachment line the type, stamp
# and ids come from the resumed record's tail, and the torn record is lost
# either way. Sam accepted this residual for harness-written transcripts (ask
# baton/q37-residual, 2026-11-08).
#
# An assistant or user line is never skipped. An attachment line is skipped only
# while no selected field reads it (``_Session.inert_kinds``), and its type or
# stamp then often follows the nested ``attachment`` object, out of the opening
# run's reach. ``_tail`` reads the members after the last nested value: text of
# nothing but ``,"key":scalar`` members from the last ``}`` or ``]`` to the
# closing brace. In well-formed JSON those are members of the top-level object
# (a string there has no ``"`` or ``\``, so each ``,"`` is a member boundary),
# the same trust the run has; the line is decoded when a key is in both the run
# and the tail. What it cannot reach is a stamp before a nested value that ends
# the line: Claude Code writes ``rendered`` (an array) after an attachment's
# stamp, so those lines are decoded. Finding where the attachment object ends
# by an anchor in its own text (``},"uuid":"``) would reach them, and would
# assume no nested object is ever followed by such a key: that is not proven
# here, and the regexes that walk the object itself ran slower than
# ``json.loads`` on its string content.

_PREPARSE = True  # a test seam: the tests switch it off to compare
_PREPARSE_MIN = 4096  # bytes; under it json.loads is as cheap as the scan
_ACTING = frozenset({"user", "assistant", "attachment"})

_VALUE = rb'(?:"[^"\\\x00-\x1f]{0,512}"|-?[0-9][0-9.eE+\-]{0,31}|true|false|null)'
_PAIR = re.compile(rb'"([A-Za-z0-9_]+)":(' + _VALUE + rb")")
_MEMBER = rb'"[A-Za-z0-9_]+":' + _VALUE
_RUN = re.compile(rb"(?:" + _MEMBER + rb"(?:," + _MEMBER + rb")*+)?+")
_NEXT = re.compile(rb',"[A-Za-z0-9_]+":|[ \t\r\n]*\}[ \t\r\n]*\Z')
_TAIL = re.compile(rb"(?:," + _MEMBER + rb")++")


def _inert(raw: bytes, s: _InertState) -> dict[str, Any] | None:
    """The stamp and ids of a line that adds nothing else, or None to decode it.

    The result stands in for the decoded record: ``type``, and where the line
    has them as top-level strings, ``timestamp``, ``sessionId`` and
    ``agentId``, and ``isSidechain`` when true. ``s`` only widens what must be
    proven: ``isSidechain`` for a main file, and an id while ``s`` still lacks
    one (a line that names such a key outside what is read is decoded).

    A flat line is read from its opening run. An attachment line that
    ``s.inert_kinds`` names may have its type or stamp after the nested
    attachment object, so the members after the last nested value (``_tail``)
    are read too.
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
    if _NEXT.match(raw, end) is None:
        return None
    kind = _plain(found.get(b"type"))
    acting = kind is None or kind in _ACTING
    if acting and (
        not s.inert_kinds or (kind is not None and kind not in s.inert_kinds)
    ):
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
    missing = [k for k in need if k not in found]
    if acting:
        # A stamp the opening run lacks must be in the tail (a line with none is
        # decoded); any other key is looked for there only if the line names it.
        want = [k for k in missing if k == b"timestamp"] or [
            k for k in missing if b'"' + k + b'"' in raw
        ]
        if kind is None:
            want.append(b"type")
    if acting and want:
        tail = _tail(raw, end, want)
        if tail is None or found.keys() & tail.keys():
            return None  # no flat tail, or a key twice: the decode keeps the last
        found.update(tail)
        kind = _plain(found.get(b"type"))
        if kind not in s.inert_kinds:
            return None
        missing = [k for k in missing if k not in found]
    for key in missing:
        if b'"' + key + b'"' in raw:
            return None  # named in the line, but not where read: it may be top-level
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


def _tail(raw: bytes, start: int, want: list[bytes]) -> dict[bytes, bytes] | None:
    """The members after the last nested value of a line's top-level object.

    The text after the last ``}`` or ``]`` before the closing brace must be
    nothing but ``,"key":scalar`` members up to that brace, and must follow
    ``start`` (the end of the opening run). Each is then a member of the
    top-level object: a string there is quote-delimited with no escape, so a
    ``,"`` is a member boundary and never string content, and the closer before
    the run ends a nested value. A closer inside a string of the tail leaves a
    remainder that does not match, and the line is decoded. A closer inside a
    nested string cannot be followed by ``,"KEY":``: the quote after the comma
    would close that string, and a bare ``KEY"`` after it is not well-formed
    JSON. None when that does not hold, when a key of ``want`` is not in the
    text, or when a key repeats.
    """
    stop = len(raw) - 8 + len(raw[-8:].rstrip(b" \t\r\n")) - 1  # the closing brace
    at = max(raw.rfind(b"}", start, stop), raw.rfind(b"]", start, stop))
    if at < 0 or any(raw.find(b'"' + k + b'":', at, stop) < 0 for k in want):
        return None  # no tail, or it cannot hold what is wanted: a cheap way out
    if (m := _TAIL.match(raw, at + 1, stop)) is None or m.end() != stop:
        return None
    found: dict[bytes, bytes] = {}
    for key, value in _PAIR.findall(raw, at + 1, stop):
        if key in found:
            return None
        found[key] = value
    return found


def _plain(token: bytes | None) -> str | None:
    """A JSON string token with no escape in it, as text; None otherwise."""
    if token is None or token[:1] != b'"' or token[-1:] != b'"' or len(token) < 2:
        return None
    try:
        return token[1:-1].decode("utf-8", "surrogatepass")
    except UnicodeDecodeError:
        return None


def _lines(fh: Any, deadline: float, limit: int | None = None) -> Iterator[bytes]:
    """Each line of the open transcript *fh*, whole (``b""`` for a line over ``MAX_LINE_BYTES``, which is
    skipped, never held), with the deadline checks ``_scan`` makes: every ``LINES_PER_DEADLINE_CHECK`` lines and
    ``BYTES_PER_DEADLINE_CHECK`` bytes, inside a long line too. With *limit*, it ends once that many bytes
    have been read (inside a line, the line is dropped): ``first_start`` bounds its read with it."""
    lines = 0
    read = 0
    next_bytes = BYTES_PER_DEADLINE_CHECK
    while True:
        raw = fh.readline(LINE_CHUNK)
        if not raw:
            return
        whole = raw.endswith(b"\n") or len(raw) < LINE_CHUNK
        lines += 1
        read += len(raw)
        long_line = False
        while not whole:  # a line longer than one chunk
            if read >= next_bytes:
                _check(deadline)
                next_bytes = read + BYTES_PER_DEADLINE_CHECK
            if limit is not None and read >= limit:
                return
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
        if limit is not None and read > limit:
            return
        yield b"" if long_line else raw
        if limit is not None and read >= limit:
            return


def _decode(raw: bytes, s: _InertState) -> Any:
    """The record a line stands for (``_inert``'s stand-in, else a full decode), or None for a line that is not
    JSON. A constant such as NaN reads as null."""
    rec = _inert(raw, s)
    if rec is None:
        try:
            rec = json.loads(raw, parse_constant=lambda _c: None)
        except (ValueError, RecursionError):
            return None
    return rec


def _replayed(rec: dict[str, Any], main: bool) -> bool:
    """Subagent traffic replayed into a main session file: it is not that session's record."""
    return main and rec.get("isSidechain") is True


START_SCAN_BYTES = 8 * 1024 * 1024  # what ``first_start`` reads of one transcript at most


def _readable_since(path: Path, floor: float) -> bool:
    """A plain file last written at or after *floor* (a time.time() value): the files ``sessions`` opens."""
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_mtime >= floor


def repo_main_transcripts(repo_root: Path | None, floor: float, deadline: float) -> Iterator[Path]:
    """The main-session transcripts of the repository containing *repo_root* (project ``repo``: the main
    checkout and its worktrees, links excluded) that ``sessions`` would open for a window starting at *floor*.
    Raises ``VerbError`` when there is no projects directory or *repo_root* is None, ``VerbTimeout`` past
    *deadline*."""
    for _project, path, main in _files(projects_root(), REPO_PROJECT, deadline, repo_root):
        if main and _readable_since(path, floor):
            yield path


def first_start(
    path: Path, deadline: float, limit: int = START_SCAN_BYTES
) -> tuple[datetime | None, bool]:
    """``(start, bounded)`` of a main transcript: its start by ``sessions``'s own rule, reading no further than
    its first record, and whether the read stopped at *limit* bytes instead.

    The rule is ``_scan``'s: the first line that decodes (``_decode``) to a JSON object, is not a replayed
    sidechain record (``_replayed``) and carries a timestamp ``_stamp`` reads (a naive one is UTC). The lines
    are ``_lines``'s, so framing, the long-line skip and the deadline are the verb's. The selection is
    ``fields="start"``, the narrowest, so ``_inert`` stands in for a large line as it does there. ``start`` is
    None when the file holds no such record, cannot be opened, or *limit* ended the read (``bounded`` true: a
    file with its first stamp past *limit* bytes is not counted, so a huge file without one cannot stall a
    caller; ``sessions`` reads on)."""
    s = _Probe()
    try:
        fh = path.open("rb")
    except OSError:
        return None, False  # gone between the listing and the read (a live session)
    with fh:
        for raw in _lines(fh, deadline, limit):
            if not raw.strip():
                continue
            rec = _decode(raw, s)
            if not isinstance(rec, dict) or _replayed(rec, True):
                continue
            stamped = _stamp(rec.get("timestamp"))
            if stamped is not None:
                return stamped[1], False
        return None, fh.tell() >= limit  # no stamp in the first *limit* bytes, whether or not more follows
