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
in the session's worktree when the launch directory's repository has one: a worktree
named ``<session>`` or ``<session>-<topic>``, the one whose branch carries a board ``ref:``
bead when several match, else the most recently active, with the others listed (the
payload ``cwd`` is the launch directory, usually the shared primary checkout), and
the in-progress list keeps only the beads the board entry names, because every session
claims beads under one assignee (both measured on halcyon's first live compaction,
2026-11-01).

POST appends the ``compact_summary`` to the same file and logs one warning line to
``<config>/logs/hook.log`` when the summary names none of the session's board title,
the checkpoint path, or a bead id from the checkpoint. It never appends to a
checkpoint an earlier compaction already used (it carries a summary or an
``injected:`` line), or one whose ``written:`` stamp is missing or past the prune
window: pre did not run for this compaction. It archives that file, writes a stub
that says so, and logs a warning. It has no decision control and prints nothing.

A checkpoint is paired with its compaction by use, not by age (nexus-702gy). Idle
compaction (Claude Code ``idleCompaction``) runs PreCompact once the session has sat
idle for about an hour and applies the summary at the next message, which can come
hours later; post and inject may run at either time, so an age window would discard
the very checkpoint the compaction wrote.

INJECT prints ``hookSpecificOutput.additionalContext`` with this session's checkpoint
(capped near 8 KB) and one line naming the full file, then writes an ``injected:``
line into the file's header so no later compaction injects it again. Age is judged by
the file's own ``written:`` stamp, never its mtime (post rewrites the file); the intro
states the real age and trigger, and past ten minutes it says the state may have
changed since. It reads and rewrites one file: no network, no ``nx`` call, because the
same event runs seven other verbs and must stay light (nexus-75w5d). It prints nothing
when no checkpoint exists for this session id, the stamp is missing or past the prune
window, the file is a stub, or it was already injected.

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
#: Clock skew tolerated for a ``written:`` stamp that lies in the future.
_SKEW_S = 300.0
#: Files older than this are removed from the checkpoint directory on each pre run.
_PRUNE_AFTER_S = 2 * 24 * 3600.0
#: A checkpoint whose own ``written:`` stamp is older than this is not injected, and post
#: does not append to it. It is the prune window, not a freshness window: an idle
#: compaction's checkpoint is used at the next message, however late (nexus-702gy).
_MAX_AGE_S = _PRUNE_AFTER_S
#: Past this age the inject intro says the state may have changed since it was written.
_STALE_NOTE_S = 10 * 60.0
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
#: A ``bd list`` row: optional tree glyphs, a status glyph, then the bead id (any prefix).
_BD_ROW_RE = re.compile(r"^[\s│├└─]*[○◐●✓❄]\s+([a-z][a-z0-9_]*(?:-[a-z0-9_]+)*-[0-9a-z]{2,}(?:\.\d+)*)\s")
#: A bead id of any repo's prefix in a board ``ref:`` line (``nexus-v1929``, ``labrun-56v.1``).
_REF_ID_RE = re.compile(r"\b[a-z][a-z0-9_]*-[0-9a-z]{3,}(?:\.\d+)*")
_WRITTEN_RE = re.compile(r"(?m)^- written: (\S+)\s*$")
_INJECTED_RE = re.compile(r"(?m)^- injected: \S+\s*$")
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


def _git_worktrees(cwd: str | None, timeout: float) -> list[tuple[str, str]]:
    """``(path, branch)`` for each worktree of the repository at *cwd* (``git worktree list``).

    *branch* is the short branch name, or ``""`` for a detached or bare worktree. Never raises.
    """
    git = _cli_lookup.resolve("git")
    if not cwd or not os.path.isdir(cwd) or not git:
        return []
    try:
        result = subprocess.run(
            [git, "-C", cwd, "worktree", "list", "--porcelain"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        _log("WARNING", f"pre: `git worktree list` failed: {type(exc).__name__}: {exc}")
        return []
    if result.returncode != 0:
        tail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        _log("WARNING", f"pre: `git worktree list` exited {result.returncode}: {tail[0][:200]}")
        return []
    rows: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            rows.append((line[len("worktree "):].strip(), ""))
        elif line.startswith("branch ") and rows:
            rows[-1] = (rows[-1][0], line[len("branch "):].strip().removeprefix("refs/heads/"))
    return rows


def _last_active(path: str) -> float:
    """When the worktree last moved: the newest mtime of its git dir's ``logs/HEAD``, ``HEAD``
    and ``index`` (a commit, a checkout, a stage). Reads the ``.git`` pointer file, so no git
    process is spawned. ``0.0`` when none can be read."""
    try:
        pointer = (Path(path) / ".git").read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):  # ValueError: a NUL in the path
        return 0.0
    if not pointer.startswith("gitdir:"):
        return 0.0
    gitdir = Path(pointer[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = Path(path) / gitdir
    newest = 0.0
    for leaf in ("logs/HEAD", "HEAD", "index"):
        try:
            newest = max(newest, (gitdir / leaf).stat().st_mtime)
        except (OSError, ValueError):  # ValueError: a NUL in the pointer
            continue
    return newest


#: The most other-worktree lines a checkpoint header lists; the rest are counted.
_OTHER_WORKTREES_SHOWN = 5


def _session_worktrees(
    name: str | None, cwd: str | None, board: str = "",
) -> tuple[str | None, list[tuple[str, str, float]]]:
    """The session's worktree and the other candidates, as ``(chosen, others)``.

    The payload's ``cwd`` is where the session was launched, which for a session that works
    in its own worktree is the shared primary checkout: its branch and HEAD are not the
    session's. A worktree is a candidate when its directory is named after the session,
    ``<name>`` or ``<name>-<topic>`` (``nexus-wt/marlowe-owner-onestmt``); ``linda_2`` is
    not a topic of ``linda``. Candidates rank by, in order:

    1. the launch ``cwd`` is inside it (a session started in its own worktree);
    2. the board entry names its path (``nexus-wt/marlowe-rdr189-p13``);
    3. its branch carries a bead a board ``ref:`` line names, or a child of one (the
       convention is ``feature/<bead-id>-<slug>``; ``ref: nexus-z1d0l.1`` names
       ``feature/nexus-z1d0l.1.3-x``, and ``ref: nexus-z1d0l.1.3`` does not name
       ``feature/nexus-z1d0l.1-x``);
    4. its directory is exactly ``<name>`` (7.92.0's only rule);
    5. it was active most recently (:func:`_last_active`).

    The rest come back as ``(path, branch, last_active)`` so the checkpoint can list them.
    Never raises: a failure keeps the launch directory.
    """
    if not name:
        return None, []
    try:
        candidates = [
            (path, branch) for path, branch in _git_worktrees(cwd, _GIT_TIMEOUT_S)
            if (Path(path).name == name or Path(path).name.startswith(name + "-")) and os.path.isdir(path)
        ]
        if not candidates:
            return None, []
        refs = "\n".join(ln for ln in board.splitlines() if re.match(r"^\s*ref:", ln))
        ref_ids = {m.group(0) for m in _REF_ID_RE.finditer(refs)}
        # Both sides take the platform's one spelling before the os.sep prefix test, so the
        # test does not rely on realpath alone to normalize git's forward slashes and a
        # drive letter's case on Windows (defensive; realpath does both there today).
        def canon(p: str) -> str:
            return os.path.normcase(os.path.normpath(os.path.realpath(p)))

        launch = canon(cwd) if cwd else ""

        def inside(path: str) -> bool:
            real = canon(path)
            return bool(launch) and (launch == real or launch.startswith(real + os.sep))

        def path_named(path: str) -> bool:
            return re.search(rf"[/\\]{re.escape(Path(path).name)}(?![\w-])", board) is not None

        def ref_named(branch: str) -> bool:
            return any(re.search(rf"(?:^|/){re.escape(bead)}(?:$|[-/.])", branch) for bead in ref_ids)

        ranked = sorted(
            ((path, branch, _last_active(path)) for path, branch in candidates),
            key=lambda row: (inside(row[0]), path_named(row[0]), ref_named(row[1]), Path(row[0]).name == name, row[2]),
            reverse=True,
        )
    except Exception as exc:  # noqa: BLE001 -- a lookup failure keeps the launch directory, never the checkpoint
        _log("WARNING", f"pre: worktree lookup failed: {type(exc).__name__}: {exc}")
        return None, []
    return ranked[0][0], ranked[1:]


def _other_worktree_lines(others: list[tuple[str, str, float]]) -> list[str]:
    """Header lines for the candidates not chosen, newest first, capped."""
    lines = []
    for path, branch, active in sorted(others, key=lambda row: row[2], reverse=True)[:_OTHER_WORKTREES_SHOWN]:
        when = datetime.fromtimestamp(active, timezone.utc).strftime("%Y-%m-%dT%H:%MZ") if active else "unknown"
        lines.append(f"- other worktree: {path} [{branch or 'detached'}], last active {when}")
    if len(others) > _OTHER_WORKTREES_SHOWN:
        lines.append(f"- other worktrees: {len(others) - _OTHER_WORKTREES_SHOWN} more not listed")
    if others:
        lines.append("- worktree choice: if it is wrong, read the right worktree's git state before acting")
    return lines


def _named_exactly(token: str, text: str) -> bool:
    """*token* occurs in *text* as a whole id: ``nexus-z1d0l.1`` is not named by ``nexus-z1d0l.1.5``."""
    return re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-]|\.\w)", text) is not None


def _filter_in_progress(shaped: str, board: str) -> str:
    """Keep the in-progress beads the board entry names, and their children; fail open.

    Every session claims beads under one assignee, so the in-progress list is everyone's,
    and the board entry's ``ref:`` lines are this session's own record of its beads (a
    ``waiting_on:`` line names peers' beads, so it is not read). Rows are read by
    ``bd list``'s row shape (a status glyph, then the id), so any repo's id prefix works.
    The list is left whole when it has no bead rows (``bd`` unavailable) or when the board
    names none of them (another repo, a board written before the session's newest claim,
    a board naming only closed beads): dropping the session's own bead is worse than
    keeping other sessions'. ``bd`` caps the list (``--limit``), so the count says "listed".
    """
    match = re.search(r"(?ms)^(### In-progress beads[^\n]*\n)(.*?)(?=^### |\Z)", shaped)
    refs = "\n".join(ln for ln in board.splitlines() if re.match(r"^\s*ref:", ln))
    if match is None or not refs:
        return shaped
    rows: list[tuple[str, str]] = []
    for line in match.group(2).splitlines():
        row = _BD_ROW_RE.match(line)
        if row:
            rows.append((row.group(1), line))

    def mine(bead_id: str) -> bool:
        parts = bead_id.split(".")
        return any(_named_exactly(".".join(parts[:n]), refs) for n in range(len(parts), 0, -1))

    kept = [line for bead_id, line in rows if mine(bead_id)]
    if not kept:
        return shaped
    note = (f"({len(kept)} of the {len(rows)} in-progress beads listed: the ones this session's board entry "
            "names; the others are not named on the board, which may include a bead claimed since it was written)")
    body = "\n".join([*kept, note]) + "\n\n"
    return shaped[: match.start(2)] + body + shaped[match.end(2):].lstrip("\n")


def _written_age(text: str, now: datetime | None = None) -> float | None:
    """Seconds since the checkpoint's own ``written:`` stamp, or ``None`` when it is missing,
    unparseable, in the future beyond clock skew, or older than ``_MAX_AGE_S``.

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
    return age if -_SKEW_S <= age <= _MAX_AGE_S else None


def _injected(text: str) -> bool:
    return _INJECTED_RE.search(text.split("\n## ", 1)[0]) is not None


def _used(text: str) -> bool:
    """True when an earlier compaction already took this checkpoint: post appended its
    summary, or inject emitted it."""
    return _injected(text) or f"\n{_SUMMARY_HEADING}" in text


def run_pre(payload: dict) -> None:
    session_id = _session_id(payload)
    if session_id is None:
        _log("WARNING", "pre: the payload carried no usable session id; nothing written")
        return
    started = time.monotonic()
    trigger = str(payload.get("trigger") or "unknown")[:32]
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    name = _session_name(session_id)
    # The board is read first: its ref: lines choose between the session's worktrees.
    board = ""
    board_error: str | None = None
    if name:
        board, board_error = _board_entry(name, cwd, _BOARD_TIMEOUT_S)
        if board_error:
            _log("WARNING", f"pre: board entry for {name!r}: {board_error}")
    worktree, other_worktrees = _session_worktrees(name, cwd, board)
    if worktree:
        cwd = worktree

    left = _TOTAL_BUDGET_S - (time.monotonic() - started)
    state, error = _run_nx(["command-context", "continuation"], cwd, max(1.0, min(_NX_TIMEOUT_S, left)))
    if error:
        _log("WARNING", f"pre: {error}")

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
        parts.extend(_other_worktree_lines(other_worktrees))
    elif name:
        parts.append(f"- worktree: none named {name} or {name}-*; the working state is the launch directory's")
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
    if existing is None or _written_age(existing) is None or _used(existing):
        # Pre did not run for this compaction (killed at its timeout, a cold start, an error
        # before the write): whatever is here is an earlier compaction's state and must not be
        # appended to or refreshed, or inject would pass it as current. Pairing is by use, not
        # age: an idle compaction's post can run hours after its pre (nexus-702gy).
        if existing is not None:
            _archive_and_prune(directory, session_id, time.time())
        _log("WARNING", f"post: no unused pre-compaction checkpoint for session {session_id} "
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
    if age < 90:
        return "under a minute"
    minutes = round(age / 60)
    if minutes < 90:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


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
        _log("INFO", f"inject: {path} has no usable written: stamp (older than {_MAX_AGE_S / 3600:.0f} h, "
                     "missing, or a stub); nothing injected")
        return
    if _injected(text):
        _log("INFO", f"inject: {path} was already injected by an earlier compaction; nothing injected")
        return
    header = text.split("\n## ", 1)[0]
    trigger_match = _TRIGGER_RE.search(header)
    trigger = trigger_match.group(1) if trigger_match else "unknown"
    # The compact summary is already in the new context; the injection is the state that
    # was written down before it.
    cut = text.find(f"\n{_SUMMARY_HEADING}")
    body = text[:cut] if cut != -1 else text
    body = body.rstrip() + "\n\n"
    if age < _STALE_NOTE_S:
        when = "just before this session was compacted."
    else:
        # An idle compaction writes its checkpoint when the session goes idle and is applied
        # at the next message (nexus-702gy).
        when = ("when this compaction began. A compaction prepared while the session sat idle "
                "is applied at the next message, so the state may have changed since it was "
                "written: check git, beads and the board before acting on it.")
    intro = (f"The conexus PreCompact hook wrote this working-state checkpoint {_age_label(age)} ago "
             f"(trigger {trigger}), {when} The state lives in "
             "version control, beads and the threads board, not in the summary.\n\n")
    footer = f"Full checkpoint (with the compact summary): {path}\n"
    context = _fit(intro + body, footer)
    sys.stdout.write(json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    ) + "\n")
    sys.stdout.flush()
    # Mark it used, so a later compaction whose pre did not run cannot inject it again.
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    marked = re.sub(r"(?m)^(- written: \S+)\s*$", rf"\1\n- injected: {stamp}", header, count=1)
    try:
        _write_atomic(path, marked + text[len(header):])
    except OSError as exc:
        _log("WARNING", f"inject: could not mark {path} injected: {exc}")


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
