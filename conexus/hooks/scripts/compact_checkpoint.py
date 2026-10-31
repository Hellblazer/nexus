#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
# /// script
# requires-python = ">=3.12"
# ///
"""Auto-continuation across compaction (bead nexus-3lqxv): one script, three hooks.

``compact_checkpoint.py pre``     PreCompact (manual and auto compaction)
``compact_checkpoint.py post``    PostCompact
``compact_checkpoint.py inject``  SessionStart, matcher ``compact`` only

Measured on session linda (e6613e51), 2026-10-31: two automatic compactions at about
969,000 tokens each produced summaries of about 0.5 percent of the context. The state
that let the session resume lived in git, beads and T2, not in the summary. This
script writes that state down before the compaction and puts it back after it.

PRE writes ``<nexus config dir>/compact-checkpoints/<session_id>.md`` (mode 0600,
atomic): a header (session id, trigger, UTC time), the output of
``nx command-context continuation`` (branch, HEAD, uncommitted files, recent commits,
in-progress and ready beads, memory titles), and, when the session's name resolves,
that session's ``threads`` board entry from ``nx memory get``. The judgment half of a
session lives in the board entry, which sessions keep current; a hook is a command,
not the model, and cannot write it.

POST appends the ``compact_summary`` to the same file and logs one warning line to
``<config>/logs/hook.log`` when the summary names none of the session's board title,
the checkpoint path, or a bead id from the checkpoint. It has no decision control and
prints nothing.

INJECT prints ``hookSpecificOutput.additionalContext`` with this session's checkpoint
(capped near 8 KB; the ready-beads and memory sections are cut first) and one line
naming the full file. It reads one file: no network, no ``nx`` call, because the same
event runs seven other verbs and must stay light (nexus-75w5d). It prints nothing when
no checkpoint exists for this session id or the file is older than a few hours.

NEVER VETOES. Every mode exits 0 whatever happens; errors go to ``hook.log`` and
stderr only. A PreCompact veto on the context-limit recovery path fails the request,
so a veto only defers the compaction. Nothing here exits 2 or prints a ``decision``.

Standard library only (hook scripts run under ``uv tool run python`` on a machine that
may have no CLI), plus the plugin's own ``_cli_lookup`` and ``_endpoint_resolve``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _cli_lookup  # noqa: E402 -- must follow the sys.path insert
import _endpoint_resolve  # noqa: E402 -- must follow the sys.path insert

#: Wall clock for ``nx command-context continuation`` (measured 5.4 s); hooks.json gives
#: this hook 45 s, so the whole of pre (budget below) stays inside it.
_NX_TIMEOUT_S = 25.0
#: Bound on the board lookup, and the budget the whole pre run spends on ``nx`` calls.
_BOARD_TIMEOUT_S = 6.0
_TOTAL_BUDGET_S = 32.0
#: A checkpoint older than this is not injected: it belongs to an earlier compaction.
_FRESH_S = 4 * 3600.0
#: Files older than this are removed from the checkpoint directory on each pre run.
_PRUNE_AFTER_S = 2 * 24 * 3600.0
#: Superseded checkpoints kept per session, beside the current one.
_KEEP_ARCHIVES = 3
#: Bytes of additionalContext (UTF-8), the footer line included.
_INJECT_CAP_BYTES = 8192
#: Bytes of any one ``nx`` output kept in the file.
_OUTPUT_CAP_BYTES = 64 * 1024

_BOARD_PROJECT = "threads"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_BEAD_ID_RE = re.compile(r"\b[a-z][a-z0-9]{1,9}-[a-z0-9]{3,6}(?:\.\d{1,3})?\b")
_ARCHIVE_STAMP = "%Y%m%dT%H%M%SZ"
_NAME_LINE = "- session name: "
#: Subsections of the working-state block that go first when the injection is over its
#: cap, in this order.
_DROP_FIRST = ("### Ready beads", "### nx memory", "### Feedback memories")
_SUMMARY_HEADING = "## Compact summary"


def _config_dir() -> Path:
    return _endpoint_resolve.default_config_dir()


def _checkpoint_dir() -> Path:
    return _config_dir() / "compact-checkpoints"


def _log(level: str, message: str) -> None:
    """One line to ``<config>/logs/hook.log`` and stderr. Never raises."""
    line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {level} compact-checkpoint: {message}\n"
    try:
        sys.stderr.write(line)
    except (OSError, ValueError):
        pass
    try:
        logs = _config_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with open(logs / "hook.log", "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _read_payload() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _session_id(payload: dict) -> str | None:
    sid = str(payload.get("session_id") or "").strip()
    return sid if _SESSION_ID_RE.match(sid) else None


def _claude_dir() -> Path:
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(raw) if raw else Path.home() / ".claude"


def _session_name(session_id: str) -> str | None:
    """The session's name (the title of its ``threads`` board entry), or ``None``.

    The hook payload does not carry it. Claude Code keeps one ``<pid>.json`` per live
    session under its config directory's ``sessions/``, with ``sessionId`` and ``name``;
    the ``*.key`` files beside them are not read. A session without a name, or a
    directory that cannot be read, resolves to ``None`` and the board is skipped.
    """
    directory = _claude_dir() / "sessions"
    try:
        entries = sorted(directory.glob("*.json"))[:500]
    except OSError:
        return None
    for path in entries:
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
    return None


def _nx_command() -> list[str] | None:
    exe = _cli_lookup.resolve("nx")
    return [exe] if exe else None


def _decode(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value if isinstance(value, str) else ""


def _cap(text: str) -> str:
    data = text.encode("utf-8")
    if len(data) <= _OUTPUT_CAP_BYTES:
        return text
    return data[:_OUTPUT_CAP_BYTES].decode("utf-8", "ignore") + "\n[output truncated]\n"


def _run_nx(args: list[str], cwd: str | None, timeout: float) -> tuple[str, str | None]:
    """``(stdout, error)``. Partial output is kept on a timeout. Never raises."""
    base = _nx_command()
    if base is None:
        return "", "the nx CLI was not found"
    try:
        result = subprocess.run(
            [*base, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=max(timeout, 0.5), cwd=cwd if cwd and os.path.isdir(cwd) else None, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _decode(exc.stdout), f"`nx {' '.join(args)}` timed out after {timeout:.0f}s"
    except (subprocess.SubprocessError, OSError) as exc:
        return "", f"`nx {' '.join(args)}` failed to start: {type(exc).__name__}: {exc}"
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        return result.stdout, f"`nx {' '.join(args)}` exited {result.returncode}: {tail[0][:200]}"
    return result.stdout, None


def _write_atomic(path: Path, text: str) -> None:
    """Write *text* to *path* through a temp file in the same directory. ``mkstemp`` creates it mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _archive_and_prune(directory: Path, session_id: str, now: float) -> None:
    """Keep the last few superseded checkpoints of this session; drop files past two days."""
    current = directory / f"{session_id}.md"
    if current.is_file():
        stamp = datetime.fromtimestamp(current.stat().st_mtime, timezone.utc).strftime(_ARCHIVE_STAMP)
        try:
            os.replace(current, directory / f"{session_id}.{stamp}.md")
        except OSError as exc:
            _log("WARNING", f"could not archive the previous checkpoint: {exc}")
    pattern = re.compile(rf"^{re.escape(session_id)}\.\d{{8}}T\d{{6}}Z\.md$")
    archives = sorted((p for p in directory.iterdir() if pattern.match(p.name)), key=lambda p: p.name)
    for old in archives[:-_KEEP_ARCHIVES]:
        try:
            old.unlink()
        except OSError:
            pass
    for path in directory.iterdir():
        try:
            if path.is_file() and (path.suffix == ".md" or path.name.startswith(".tmp-")):
                if now - path.stat().st_mtime > _PRUNE_AFTER_S:
                    path.unlink()
        except OSError:
            continue


def run_pre(payload: dict) -> None:
    session_id = _session_id(payload)
    if session_id is None:
        _log("WARNING", "pre: the payload carried no usable session id; nothing written")
        return
    started = time.monotonic()
    trigger = str(payload.get("trigger") or "unknown")[:32]
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None

    state, error = _run_nx(["command-context", "continuation"], cwd, _NX_TIMEOUT_S)
    if error:
        _log("WARNING", f"pre: {error}")
    name = _session_name(session_id)
    board = ""
    board_error: str | None = None
    if name:
        left = _TOTAL_BUDGET_S - (time.monotonic() - started)
        if left > 1.0:
            board, board_error = _run_nx(
                ["memory", "get", "--project", _BOARD_PROJECT, "--title", name], cwd, min(_BOARD_TIMEOUT_S, left)
            )
        else:
            board_error = "no time left for the board lookup"
        if board_error:
            _log("WARNING", f"pre: board entry for {name!r}: {board_error}")

    parts = [
        "# Compaction checkpoint\n",
        f"- session: {session_id}",
        f"- trigger: {trigger}",
        f"- written: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
    ]
    if name:
        parts.append(f"{_NAME_LINE}{name}")
    parts.append("")
    parts.append("## Working state\n")
    if state.strip():
        parts.append(_cap(state).rstrip() + "\n")
    if error:
        parts.append(f"[working state incomplete: {error}]\n")
    if name:
        parts.append(f"## Threads board entry ({_BOARD_PROJECT}/{name})\n")
        if board.strip():
            parts.append(_cap(board).rstrip() + "\n")
        else:
            parts.append(f"[no board entry: {board_error or 'empty'}]\n")
    directory = _checkpoint_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _archive_and_prune(directory, session_id, time.time())
    _write_atomic(directory / f"{session_id}.md", "\n".join(parts) + "\n")


def run_post(payload: dict) -> None:
    session_id = _session_id(payload)
    if session_id is None:
        _log("WARNING", "post: the payload carried no usable session id; nothing written")
        return
    summary = str(payload.get("compact_summary") or "")
    path = _checkpoint_dir() / f"{session_id}.md"
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = f"# Compaction checkpoint\n\n- session: {session_id}\n\n[no pre-compaction checkpoint was written]\n"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _write_atomic(path, f"{existing.rstrip()}\n\n{_SUMMARY_HEADING} ({stamp})\n\n{summary.strip()}\n")

    name = None
    for line in existing.splitlines():
        if line.startswith(_NAME_LINE):
            name = line[len(_NAME_LINE):].strip()
            break
    pointers: list[str] = [str(path), "compact-checkpoints"]
    if name:
        pointers.append(name)
    beads = set(_BEAD_ID_RE.findall(_in_progress_section(existing))) or set(_BEAD_ID_RE.findall(existing))
    pointers.extend(sorted(beads))
    if not any(p and p in summary for p in pointers):
        _log("WARNING", f"post: the compact summary for session {session_id} names none of its threads board "
                        f"title, its checkpoint path ({path}) or its in-flight beads; the checkpoint is the record")


def _in_progress_section(text: str) -> str:
    match = re.search(r"^### In-progress beads\n(.*?)(?=^#{2,3} |\Z)", text, re.M | re.S)
    return match.group(1) if match else ""


def _split_sections(text: str) -> list[str]:
    """Split on ``## `` and ``### `` heading lines, each piece keeping its heading."""
    pieces = re.split(r"(?m)^(?=#{2,3} )", text)
    return [p for p in pieces if p]


def _fit(body: str, footer: str) -> str:
    def size(t: str) -> int:
        return len((t + footer).encode("utf-8"))

    if size(body) <= _INJECT_CAP_BYTES:
        return body + footer
    pieces = _split_sections(body)
    for prefix in _DROP_FIRST:
        for i, piece in enumerate(pieces):
            if piece.startswith(prefix):
                heading = piece.split("\n", 1)[0]
                pieces[i] = f"{heading}\n[omitted for size; the full file has it]\n\n"
        if size("".join(pieces)) <= _INJECT_CAP_BYTES:
            return "".join(pieces) + footer
    marker = "\n[truncated for size; the full file has the rest]\n"
    room = _INJECT_CAP_BYTES - len((marker + footer).encode("utf-8"))
    joined = "".join(pieces).encode("utf-8")[: max(room, 0)].decode("utf-8", "ignore")
    return joined + marker + footer


def run_inject(payload: dict) -> None:
    session_id = _session_id(payload)
    if session_id is None:
        return
    path = _checkpoint_dir() / f"{session_id}.md"
    try:
        if time.time() - path.stat().st_mtime > _FRESH_S:
            return
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    # The compact summary is already in the new context; the injection is the state that
    # was written down before it.
    cut = text.find(f"\n{_SUMMARY_HEADING}")
    body = text[:cut] if cut != -1 else text
    body = body.rstrip() + "\n\n"
    intro = ("The conexus PreCompact hook wrote this working-state checkpoint just before this "
             "session was compacted. The state lives in git, beads and the threads board, not in the summary.\n\n")
    footer = f"Full checkpoint (with the compact summary): {path}\n"
    context = _fit(intro + body, footer)
    sys.stdout.write(json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    ) + "\n")
    sys.stdout.flush()


_MODES = {"pre": run_pre, "post": run_post, "inject": run_inject}


def main(argv: list[str] | None = None) -> int:
    """Always 0. See the module docstring: a compaction hook never vetoes."""
    args = sys.argv[1:] if argv is None else argv
    mode = args[0] if args else ""
    try:
        handler = _MODES.get(mode)
        if handler is None:
            _log("WARNING", f"unknown mode {mode!r}; expected pre, post or inject")
            return 0
        handler(_read_payload())
    except BaseException as exc:  # noqa: BLE001 -- the no-veto contract is the reason
        _log("ERROR", f"{mode}: unexpected {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
