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
atomic): a header (session id, trigger, UTC time), then, in cut-proof order, the
session's ``threads`` board entry (looked up by the session's exact name; the judgment
half of a session lives there and a hook cannot write it), the branch and HEAD, the
in-progress beads, the uncommitted files, the open PRs and the recent commits from
``nx command-context continuation``. Ready beads and memory titles are not kept: the
SessionStart group that runs on the same event already injects them. Continuation runs
in the worktree named after the session when the launch directory's repository has one
(the payload ``cwd`` is the launch directory, usually the shared primary checkout), and
the in-progress list keeps only the beads the board entry names, because every session
claims beads under one assignee (both measured on halcyon's first live compaction,
2026-11-01).

POST appends the ``compact_summary`` to the same file and logs one warning line to
``<config>/logs/hook.log`` when the summary names none of the session's board title,
the checkpoint path, or a bead id from the checkpoint. It never appends to, or
rewrites, a checkpoint whose ``written:`` stamp is older than the freshness window
(30 minutes) or missing: that file belongs to an earlier compaction, because pre did
not run for this one. It archives it, writes a stub that says so, and logs a warning.
It has no decision control and prints nothing.

INJECT prints ``hookSpecificOutput.additionalContext`` with this session's checkpoint
(capped near 8 KB) and one line naming the full file. Freshness is judged by the
file's own ``written:`` stamp, never its mtime (post rewrites the file), and the intro
states the checkpoint's age and trigger. It reads one file: no network, no ``nx``
call, because the same event runs seven other verbs and must stay light (nexus-75w5d).
It prints nothing when no checkpoint exists for this session id, the stamp is missing
or older than the window, or the file is a stub.

NEVER VETOES. Every mode exits 0 whatever happens; errors go to ``hook.log`` and
stderr only. A PreCompact veto on the context-limit recovery path fails the request,
so a veto only defers the compaction. Nothing here exits 2 or prints a ``decision``.

Standard library only (hook scripts run under ``uv tool run python`` on a machine that
may have no CLI), plus the plugin's own ``_cli_lookup`` and ``_endpoint_resolve``, which
``main`` imports inside its guard so an ImportError also exits 0 and logs.
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
#: The plugin's sibling modules, bound by ``_load_siblings`` inside ``main``'s guard.
_cli_lookup = None
_endpoint_resolve = None


def _load_siblings() -> None:
    global _cli_lookup, _endpoint_resolve
    import _cli_lookup as cli_lookup  # noqa: PLC0415 -- must follow the sys.path insert
    import _endpoint_resolve as endpoint_resolve  # noqa: PLC0415

    _cli_lookup, _endpoint_resolve = cli_lookup, endpoint_resolve


#: Wall clock for ``nx command-context continuation`` (measured 5.4 s); hooks.json gives
#: this hook 45 s, so the whole of pre (budget below) stays inside it.
_NX_TIMEOUT_S = 25.0
#: Bound on the board lookup, and the budget the whole pre run spends on ``nx`` calls.
_BOARD_TIMEOUT_S = 6.0
#: Bound on ``git worktree list``, run before the continuation call.
_GIT_TIMEOUT_S = 3.0
_TOTAL_BUDGET_S = 32.0
#: A checkpoint whose own ``written:`` stamp is older than this is not injected, and post
#: does not append to it: it belongs to an earlier compaction.
_FRESH_S = 30 * 60.0
#: Clock skew tolerated for a ``written:`` stamp that lies in the future.
_SKEW_S = 300.0
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
#: A nexus bead id: ``nexus-`` and five lowercase alphanumerics, optional ``.N`` suffix.
#: Ordinary hyphenated words (ready-beads, auto-memory) are not ids.
_BEAD_ID_RE = re.compile(r"\bnexus-[0-9a-z]{5}(?![0-9a-z])(?:\.\d{1,3})?")
_WRITTEN_RE = re.compile(r"(?m)^- written: (\S+)\s*$")
_TRIGGER_RE = re.compile(r"(?m)^- trigger: (.{1,32})$")
#: Subsections of the continuation output that are not kept: the same-event SessionStart
#: group (session_context.py) already injects ready beads and memory titles, and
#: MEMORY.md is loaded.
_OMIT_SECTIONS = ("### Ready beads", "### nx memory", "### Feedback memories")
#: Kept subsections, in file order after the board entry. A hard cut takes the tail.
_KEEP_ORDER = ("### In-progress beads", "### Uncommitted", "### Open PRs", "### Recent commits")
_STUB_BODY = "[no pre-compaction checkpoint for this compaction]"
_ARCHIVE_STAMP = "%Y%m%dT%H%M%SZ"
_NAME_LINE = "- session name: "
#: Subsections of the working-state block that go first when the injection is over its
#: cap, in this order.
_DROP_FIRST = ("### Recent commits", "### Open PRs", "### Uncommitted")
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
    except Exception:  # noqa: BLE001 -- includes the siblings not having loaded
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
    except OSError as exc:
        _log("WARNING", f"cannot list {directory}: {exc}; the board entry is skipped")
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
            _log("WARNING", f"{path.name} matches session {session_id} but carries no usable name; "
                            "the board entry is skipped")
            return None
    _log("WARNING", f"no {directory}/*.json carries session id {session_id} ({len(entries)} files read); "
                    "Claude Code's sessions layout may have changed; the board entry is skipped")
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
    """``(stdout, error)``. On a timeout the partial output is kept only if it reached the pipe
    (a child that block-buffers its stdout loses it); the error line is always written. Never raises."""
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


def _shape_state(state: str) -> str:
    """The continuation output reordered for a cut from the tail, minus what is not kept.

    Drops the skill's ``**Target file:**`` and ``**Topic:**`` lines (a stray handoff-write cue)
    and the omitted subsections, and puts the kept ones in ``_KEEP_ORDER``; the lines before
    the first ``### `` heading (cwd, branch, HEAD) stay first.
    """
    lines = [ln for ln in state.splitlines(keepends=True) if not ln.startswith(("**Target file:**", "**Topic:**"))]
    pieces = re.split(r"(?m)^(?=### )", "".join(lines))
    preamble = pieces[0] if pieces and not pieces[0].startswith("### ") else ""
    preamble = re.sub(r"(?m)^## Working state\s*\n", "", preamble).strip()
    sections = [p for p in pieces if p.startswith("### ") and not p.startswith(_OMIT_SECTIONS)]
    ordered: list[str] = []
    for prefix in _KEEP_ORDER:
        ordered += [p for p in sections if p.startswith(prefix)]
    ordered += [p for p in sections if not p.startswith(_KEEP_ORDER)]
    return "\n\n".join(x for x in [preamble, *(p.strip() for p in ordered)] if x)


def _board_entry(name: str, cwd: str | None, timeout: float) -> tuple[str, str | None]:
    """The ``threads`` entry titled exactly *name*, as ``(body, error)``.

    ``nx memory get --title`` is exact-then-prefix and does not print the title it resolved,
    so a session ``linda`` with no entry would get ``linda_2``'s board. The listing is read
    for the exact title and the entry is fetched by its id.
    """
    started = time.monotonic()
    listing, error = _run_nx(["memory", "list", "--project", _BOARD_PROJECT], cwd, timeout)
    if error:
        return "", error
    entry_id = None
    for line in listing.splitlines():
        match = re.match(r"^\[(\d+)\] " + re.escape(_BOARD_PROJECT) + r"/(.*)$", line)
        if match and match.group(2).rsplit("  (", 1)[0] == name:
            entry_id = match.group(1)
            break
    if entry_id is None:
        return "", f"no {_BOARD_PROJECT} entry is titled exactly {name!r}"
    left = max(timeout - (time.monotonic() - started), 1.0)
    return _run_nx(["memory", "get", entry_id, "--project", _BOARD_PROJECT], cwd, left)


def _git_worktrees(cwd: str | None, timeout: float) -> list[str]:
    """Paths of the worktrees of the repository at *cwd* (``git worktree list``). Never raises."""
    if not cwd or not os.path.isdir(cwd):
        return []
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "worktree", "list", "--porcelain"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log("WARNING", f"pre: `git worktree list` failed: {type(exc).__name__}: {exc}")
        return []
    if result.returncode != 0:
        return []
    return [ln[len("worktree "):].strip() for ln in result.stdout.splitlines() if ln.startswith("worktree ")]


def _session_worktree(name: str | None, cwd: str | None) -> str | None:
    """The worktree named after the session (``../nexus-wt/<session>``), or ``None``.

    The payload's ``cwd`` is where the session was launched, which for a session that works
    in its own worktree is the shared primary checkout: its branch and HEAD are not the
    session's. Only a worktree whose directory name is exactly the session name counts.
    """
    if not name:
        return None
    for path in _git_worktrees(cwd, _GIT_TIMEOUT_S):
        if Path(path).name == name and os.path.isdir(path):
            return path
    return None


def _filter_in_progress(shaped: str, board: str) -> str:
    """Keep only the in-progress beads the board entry names (or their children).

    Every session claims beads under one assignee, so the in-progress list is everyone's.
    The board entry's ``ref:`` lines are this session's own record of its beads. With no
    bead id on the board the list is left whole.
    """
    own = set(_BEAD_ID_RE.findall(board))
    if not own:
        return shaped
    match = re.search(r"(?ms)^(### In-progress beads[^\n]*\n)(.*?)(?=^### |\Z)", shaped)
    if match is None:
        return shaped
    rows = [ln for ln in match.group(2).splitlines() if _BEAD_ID_RE.search(ln)]

    def mine(line: str) -> bool:
        return any(i == b or i.startswith(b + ".") for i in _BEAD_ID_RE.findall(line)[:1] for b in own)

    kept = [ln for ln in rows if mine(ln)]
    note = (f"({len(kept)} of {len(rows)} in-progress beads: the ones this session's board entry names; "
            "the rest are other sessions')")
    body = "\n".join([*kept, note]) + "\n\n"
    return shaped[: match.start(2)] + body + shaped[match.end(2):].lstrip("\n")


def _written_age(text: str, now: datetime | None = None) -> float | None:
    """Seconds since the checkpoint's own ``written:`` stamp, or ``None`` when it is missing,
    unparseable, in the future beyond clock skew, or older than the freshness window.

    Only the header (before the first ``## `` heading) is read. The file's mtime is never
    consulted: post rewrites the file, so an old checkpoint would look new.
    """
    header = text.split("\n## ", 1)[0]
    match = _WRITTEN_RE.search(header)
    if match is None:
        return None
    try:
        stamp = datetime.fromisoformat(match.group(1))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
    return age if -_SKEW_S <= age <= _FRESH_S else None


def run_pre(payload: dict) -> None:
    session_id = _session_id(payload)
    if session_id is None:
        _log("WARNING", "pre: the payload carried no usable session id; nothing written")
        return
    started = time.monotonic()
    trigger = str(payload.get("trigger") or "unknown")[:32]
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    name = _session_name(session_id)
    worktree = _session_worktree(name, cwd)
    if worktree:
        cwd = worktree

    state, error = _run_nx(["command-context", "continuation"], cwd, _NX_TIMEOUT_S)
    if error:
        _log("WARNING", f"pre: {error}")
    board = ""
    board_error: str | None = None
    if name:
        left = _TOTAL_BUDGET_S - (time.monotonic() - started)
        if left > 1.0:
            board, board_error = _board_entry(name, cwd, min(_BOARD_TIMEOUT_S, left))
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
    if worktree:
        parts.append(f"- worktree: {worktree}")
    parts.append("")
    if name:
        parts.append(f"## Threads board entry ({_BOARD_PROJECT}/{name})\n")
        if board.strip():
            parts.append(_cap(board).rstrip() + "\n")
        else:
            parts.append(f"[no board entry: {board_error or 'empty'}]\n")
    parts.append("## Working state\n")
    shaped = _filter_in_progress(_shape_state(state), board)
    if shaped:
        parts.append(_cap(shaped).rstrip() + "\n")
    if error:
        parts.append(f"[working state incomplete: {error}]\n")
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
    directory = _checkpoint_dir()
    path = directory / f"{session_id}.md"
    try:
        existing: str | None = path.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing is None or _written_age(existing) is None:
        # Pre did not run for this compaction (killed at its timeout, a cold start, an error
        # before the write): whatever is here is an earlier compaction's state and must not be
        # appended to or refreshed, or inject would pass it as current.
        if existing is not None:
            _archive_and_prune(directory, session_id, time.time())
        _log("WARNING", f"post: no fresh pre-compaction checkpoint for session {session_id} "
                        f"({'an older one was archived' if existing is not None else 'none exists'}); "
                        "writing a stub, which inject does not emit")
        existing = f"# Compaction checkpoint\n\n- session: {session_id}\n\n{_STUB_BODY}\n"
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


def _age_label(age: float) -> str:
    return "under a minute" if age < 90 else f"{round(age / 60)} min"


def run_inject(payload: dict) -> None:
    session_id = _session_id(payload)
    if session_id is None:
        return
    path = _checkpoint_dir() / f"{session_id}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    age = _written_age(text)
    if age is None:
        _log("INFO", f"inject: {path} has no fresh written: stamp (older than {_FRESH_S / 60:.0f} min, "
                     "missing, or a stub); nothing injected")
        return
    trigger_match = _TRIGGER_RE.search(text.split("\n## ", 1)[0])
    trigger = trigger_match.group(1) if trigger_match else "unknown"
    # The compact summary is already in the new context; the injection is the state that
    # was written down before it.
    cut = text.find(f"\n{_SUMMARY_HEADING}")
    body = text[:cut] if cut != -1 else text
    body = body.rstrip() + "\n\n"
    intro = (f"The conexus PreCompact hook wrote this working-state checkpoint {_age_label(age)} ago "
             f"(trigger {trigger}), just before this session was compacted. The state lives in "
             "version control, beads and the threads board, not in the summary.\n\n")
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
        _load_siblings()
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
