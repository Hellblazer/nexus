# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""What is true now for one Claude session, checked against what its board says (nexus-ct74m).

The threads board and the compaction checkpoint record what a session last wrote. Across 12
sessions (T2 nexus/continuity-reflection-2026-11-13) that lagged the facts: waits on tags and CI
runs that had finished, refs to beads closed days before, work with no board block, background
shells running unseen for hours, and a worktree picked by cwd. This module reads the facts; the
PreCompact checkpoint (``compact_checkpoint.py``) and the restart script
(``scripts/claude-restart-sessions``) print them.

Pure parsing and formatting take their inputs as arguments; the few functions that read the
machine (``ps``, ``lsof``/``/proc``, a reflog, ``scheduled_tasks.json``) take an injectable runner
or path and never raise. Standard library only: hook scripts run under ``uv tool run python``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

#: A Claude Code Bash-tool shell: ``/bin/zsh -c source <config>/shell-snapshots/snapshot-...``.
SNAPSHOT_MARK = "/shell-snapshots/snapshot-"
#: The most process or task lines one listing prints; the rest are counted.
MAX_LISTED = 8

_EVAL_RE = re.compile(r"\beval '((?:[^']|'\\'')*)'")
_PS_ROW_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$")

Runner = Callable[[list[str], float], "tuple[int, str]"]


def run(argv: list[str], timeout: float, cwd: str | None = None) -> tuple[int, str]:
    """``(returncode, stdout)``; ``(-1, "")`` when the command cannot run or times out. Never raises."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=max(timeout, 0.2), check=False, cwd=cwd)
    except (subprocess.SubprocessError, OSError, ValueError):
        return -1, ""
    return done.returncode, done.stdout


# ── processes ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    start: str  # ``ps -o lstart``: "Fri Nov 13 19:33:02 2026"
    command: str


def process_table(runner: Runner | None = None) -> list[Proc]:
    """Every process, from ``ps -A -o pid=,ppid=,lstart=,command=`` (macOS and Linux). ``[]`` on Windows or failure."""
    if sys.platform == "win32":
        return []
    code, out = (runner or (lambda a, t: run(a, t)))(["ps", "-A", "-o", "pid=,ppid=,lstart=,command="], 3.0)
    if code != 0:
        return []
    rows = []
    for line in out.splitlines():
        m = _PS_ROW_RE.match(line)
        if m:
            rows.append(Proc(int(m.group(1)), int(m.group(2)), " ".join(m.group(3).split()), m.group(4)))
    return rows


def claude_pid(session_id: str, sessions_dir: Path) -> int | None:
    """The pid of the Claude Code process that runs *session_id*, from ``<config>/sessions/<pid>.json``."""
    try:
        entries = sorted(sessions_dir.glob("*.json"))[:500]
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
            pid = data.get("pid")
            return pid if isinstance(pid, int) and pid > 0 else None
    return None


def descendants(table: Iterable[Proc], root: int) -> list[Proc]:
    """Every process below *root*, in table order."""
    rows = list(table)
    children: dict[int, list[Proc]] = {}
    for p in rows:
        children.setdefault(p.ppid, []).append(p)
    found, stack, seen = [], [root], {root}
    while stack:
        for child in children.get(stack.pop(), []):
            if child.pid not in seen:
                seen.add(child.pid)
                found.append(child)
                stack.append(child.pid)
    order = {p.pid: i for i, p in enumerate(rows)}
    return sorted(found, key=lambda p: order[p.pid])


def ancestors(table: Iterable[Proc], pid: int) -> set[int]:
    """*pid* and every process above it."""
    parent = {p.pid: p.ppid for p in table}
    out, cur = set(), pid
    while cur and cur not in out:
        out.add(cur)
        cur = parent.get(cur, 0)
    return out


def task_command(command: str) -> str:
    """The command a Bash-tool shell runs: the ``eval '...'`` body, unquoted; else the whole line."""
    m = _EVAL_RE.search(command)
    body = m.group(1).replace("'\\''", "'") if m else command
    return " ".join(body.split())


def background_tasks(session_id: str, sessions_dir: Path, table: list[Proc], exclude: frozenset[int] | set[int] = frozenset()) -> list[Proc]:
    """The session's Bash-tool shells still running: Claude Code's background tasks and Monitors.

    They are the shells below the session's Claude process whose command line carries a shell
    snapshot. At PreCompact no foreground tool is running, so every such shell is a background one;
    *exclude* removes the caller's own ancestry (the hook, the restart script).
    """
    pid = claude_pid(session_id, sessions_dir)
    if pid is None:
        return []
    return [p for p in descendants(table, pid) if SNAPSHOT_MARK in p.command and p.pid not in exclude]


def process_cwds(runner: Runner | None = None) -> dict[int, str]:
    """``{pid: cwd}`` for the user's processes: ``/proc/<pid>/cwd`` on Linux, ``lsof -d cwd`` on macOS."""
    if sys.platform.startswith("linux"):
        out: dict[int, str] = {}
        try:
            for entry in os.scandir("/proc"):
                if entry.name.isdigit():
                    try:
                        out[int(entry.name)] = os.readlink(f"/proc/{entry.name}/cwd")
                    except OSError:
                        continue
        except OSError:
            return {}
        return out
    if sys.platform != "darwin":
        return {}
    code, text = (runner or (lambda a, t: run(a, t)))(["lsof", "-a", "-d", "cwd", "-u", str(os.getuid()), "-Fpn"], 3.0)
    if code not in (0, 1):  # lsof exits 1 when some process could not be read
        return {}
    out, pid = {}, 0
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid:
            out[pid] = line[1:]
    return out


def _inside(path: str, root: str) -> bool:
    path, root = os.path.normcase(os.path.realpath(path)), os.path.normcase(os.path.realpath(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def worktree_processes(table: list[Proc], cwds: dict[int, str], worktrees: Iterable[str],
                       skip: frozenset[int] | set[int] = frozenset()) -> list[tuple[Proc, str]]:
    """Processes whose working directory is inside one of *worktrees*, with that worktree.

    Shells are left out when their parent is in the list (a pipeline shows once), as are *skip*.
    """
    roots = [w for w in worktrees if w]
    hits: list[tuple[Proc, str]] = []
    for p in table:
        cwd = cwds.get(p.pid)
        if p.pid in skip or not cwd:
            continue
        root = next((r for r in roots if _inside(cwd, r)), None)
        if root:
            hits.append((p, root))
    pids = {p.pid for p, _ in hits}
    return [(p, r) for p, r in hits if p.ppid not in pids]


def task_lines(tasks: list[Proc], procs: list[tuple[Proc, str]], crons: list[str], *, cron_note: str) -> list[str]:
    """The checkpoint's listing of what runs for this session, as markdown bullets."""
    lines = []
    for p in tasks[:MAX_LISTED]:
        lines.append(f"- background task pid {p.pid}, started {p.start}: `{task_command(p.command)[:160]}`")
    if len(tasks) > MAX_LISTED:
        lines.append(f"- {len(tasks) - MAX_LISTED} more background tasks not listed")
    if not tasks:
        lines.append("- background tasks: none running")
    for p, root in procs[:MAX_LISTED]:
        lines.append(f"- process pid {p.pid} in {root}, started {p.start}: `{' '.join(p.command.split())[:160]}`")
    if len(procs) > MAX_LISTED:
        lines.append(f"- {len(procs) - MAX_LISTED} more processes in this session's worktrees not listed")
    lines.extend(f"- durable cron: {c}" for c in crons)
    lines.append(f"- {cron_note}")
    return lines


def durable_crons(project_dirs: Iterable[str], session_id: str = "") -> list[str]:
    """Durable scheduled tasks from ``<dir>/.claude/scheduled_tasks.json``, one line each.

    A session-only cron lives in the Claude Code process and no file shows it, so the caller says so.
    The file's shape is read loosely: a list, or an object holding one under ``tasks``.
    """
    lines, seen = [], set()
    for d in project_dirs:
        path = Path(d) / ".claude" / "scheduled_tasks.json"
        if str(path) in seen:
            continue
        seen.add(str(path))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows = data.get("tasks", []) if isinstance(data, dict) else data
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            owner = str(row.get("sessionId") or row.get("session_id") or "")
            if owner and session_id and owner != session_id:
                continue
            when = row.get("cron") or row.get("schedule") or "?"
            what = " ".join(str(row.get("prompt") or row.get("command") or "").split())[:100]
            lines.append(f"`{when}` {what} ({path})")
    return lines


# ── worktrees ────────────────────────────────────────────────────────────────


def _gitdir(worktree: str) -> Path | None:
    try:
        dot = Path(worktree) / ".git"
        if dot.is_dir():
            return dot
        pointer = dot.read_text(encoding="utf-8", errors="replace").strip()
    except (OSError, ValueError):
        return None
    if not pointer.startswith("gitdir:"):
        return None
    gitdir = Path(pointer[len("gitdir:"):].strip())
    return gitdir if gitdir.is_absolute() else Path(worktree) / gitdir


#: Reflog actions that are a commit made in this worktree (a checkout, reset or pull is not).
_COMMIT_ACTIONS = ("commit", "cherry-pick", "revert", "merge ", "rebase (finish)", "rebase (pick)")


def last_commit_time(worktree: str) -> float:
    """When a commit was last made in *worktree*, from its own HEAD reflog; ``0.0`` when none.

    A worktree cut at a peer's commit has a recent HEAD date and no commit of its own, so the
    commit date of HEAD is not the signal; the reflog's own commit entries are.
    """
    gitdir = _gitdir(worktree)
    if gitdir is None:
        return 0.0
    try:
        with open(gitdir / "logs" / "HEAD", "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return 0.0
    newest = 0.0
    for line in tail.splitlines():
        head, _, message = line.partition("\t")
        if not message.startswith(_COMMIT_ACTIONS):
            continue
        parts = head.split()
        if len(parts) >= 2 and parts[-2].isdigit():
            newest = max(newest, float(parts[-2]))
    return newest


@dataclass(frozen=True)
class WorktreeWork:
    path: str
    branch: str
    unpushed: int  # commits whose patch is not on the base (``git cherry`` "+" rows); -1 unknown
    dirty: int  # tracked files changed; -1 unknown


def worktree_work(path: str, branch: str, git: str, timeout: float = 3.0) -> WorktreeWork:
    """Unlanded commits (against the branch's upstream, else ``origin/develop``) and changed tracked files.
    An empty *branch* is looked up (``HEAD`` stays empty: detached)."""
    if not branch:
        code, head = run([git, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], timeout)
        branch = head.strip() if code == 0 and head.strip() != "HEAD" else ""
    code, upstream = run([git, "-C", path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], timeout)
    base = upstream.strip() if code == 0 and upstream.strip() else "origin/develop"
    code, cherry = run([git, "-C", path, "cherry", base, "HEAD"], timeout)
    unpushed = sum(1 for ln in cherry.splitlines() if ln.startswith("+")) if code == 0 else -1
    code, status = run([git, "-C", path, "status", "--porcelain", "--untracked-files=no"], timeout)
    dirty = len([ln for ln in status.splitlines() if ln.strip()]) if code == 0 else -1
    return WorktreeWork(path, branch, unpushed, dirty)


def unboarded(work: Iterable[WorktreeWork], board: str) -> list[str]:
    """Lines for worktrees holding work (unlanded commits, changed files) that no board thread names.

    A thread names a worktree by its path or directory name, its branch, or a bead id the branch carries.
    """
    lines = []
    for w in work:
        if w.unpushed <= 0 and w.dirty <= 0:
            continue
        name = Path(w.path).name
        # A branch carries its bead as ``feature/<bead-id>-<slug>``: the id ends at the slug's hyphen.
        beads = re.findall(r"(?:^|/)([a-z][a-z0-9]*-[0-9a-z]{5}(?:\.\d{1,3})*)(?=$|[-/])", w.branch)
        named = (re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", board) is not None
                 or bool(w.branch and w.branch in board) or any(_word(b, board) for b in beads))
        if named:
            continue
        what = []
        if w.unpushed > 0:
            what.append(f"{w.unpushed} commit(s) not landed")
        if w.dirty > 0:
            what.append(f"{w.dirty} changed file(s)")
        lines.append(f"- no board thread names {w.path} [{w.branch or 'detached'}]: {', '.join(what)}")
    return lines


# ── the board ────────────────────────────────────────────────────────────────

#: A bead id: ``<prefix>-<5 base36>``, children as ``.N``. Unknown ids are dropped after lookup.
BEAD_RE = re.compile(r"(?<![\w.-])([a-z][a-z0-9]*-[0-9a-z]{5})((?:\.\d{1,3})*)(?![\w-])")
_ENGINE_TAG_RE = re.compile(r"\bengine-service-v(\d+\.\d+\.\d+)\b")
_ENGINE_WORDS_RE = re.compile(r"\bengine(?:-service)?\s+(?:v)?(\d+\.\d+\.\d+)\b|\b(\d+\.\d+\.\d+)\s+(?:engine\s+)?cut\b"
                              r"|\b(0\.1\.\d+)\b")
_CLIENT_TAG_RE = re.compile(r"(?<![\w.-])v(\d+\.\d+\.\d+)\b|\bconexus\s+(\d+\.\d+\.\d+)\b")
_RUN_RE = re.compile(r"(?:actions/runs/|\bruns?\s+)(\d{9,12})\b")
_PR_RE = re.compile(r"(?:\bPR\s*#|/pull/)(\d{1,6})\b")
#: Thread fields this module reads. Keys match as the board parser reads them (marlowe, src/nexus/console/
#: threads.py): case-insensitive, with ``-``, a space and ``_`` alike (``Waiting-on :``).
_FIELD_RE = re.compile(r"^\s*(ref|waiting[-_ ]on|due|worktree)\s*:\s*(.*)$", re.IGNORECASE)
_UNSET = frozenset({"", "none", "no", "n/a", "-", "—"})


def _word(token: str, text: str) -> bool:
    return re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-]|\.\d)", text) is not None


@dataclass
class Thread:
    title: str
    fields: list[tuple[str, str]] = field(default_factory=list)  # (key, value) for ref/waiting_on/due/worktree


def threads(board: str) -> list[Thread]:
    """The board's ``## `` thread blocks with their ref, waiting_on, due and worktree lines."""
    out: list[Thread] = []
    for line in board.splitlines():
        if line.startswith("## "):
            out.append(Thread(line[3:].strip()))
        elif out and (m := _FIELD_RE.match(line)):
            key = re.sub(r"[-_ ]", "_", m.group(1).lower())
            value = m.group(2).strip()
            if key in ("due", "worktree") and value.lower() in _UNSET:
                continue
            out[-1].fields.append((key, value))
    return out


@dataclass(frozen=True)
class Wanted:
    """Every fact the board's ref and waiting_on lines point at."""
    beads: frozenset[str]
    engine_tags: frozenset[str]
    client_tags: frozenset[str]
    runs: frozenset[str]
    prs: frozenset[str]


def facts_named(text: str) -> Wanted:
    beads = {m.group(1) + m.group(2) for m in BEAD_RE.finditer(text)}
    engine = set(_ENGINE_TAG_RE.findall(text))
    for groups in _ENGINE_WORDS_RE.findall(text):
        engine.update(g for g in groups if g)
    client = {g for groups in _CLIENT_TAG_RE.findall(_ENGINE_TAG_RE.sub("", text)) for g in groups if g}
    client -= engine
    return Wanted(frozenset(beads), frozenset(f"engine-service-v{v}" for v in engine),
                  frozenset(f"v{v}" for v in client), frozenset(_RUN_RE.findall(text)), frozenset(_PR_RE.findall(text)))


def board_wants(board: str) -> Wanted:
    """The facts named on the board's ref and waiting_on lines."""
    text = "\n".join(v for t in threads(board) for k, v in t.fields if k in ("ref", "waiting_on"))
    return facts_named(text)


@dataclass
class Facts:
    """What the resolvers found. A key absent from a map was not checked (a timeout, no tool)."""
    beads: dict[str, tuple[str, str, int]] = field(default_factory=dict)  # id -> (status, type, open children)
    tags: dict[str, bool] = field(default_factory=dict)  # tag -> exists on origin
    runs: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (status, conclusion)
    prs: dict[str, str] = field(default_factory=dict)  # number -> state
    unchecked: list[str] = field(default_factory=list)  # why a class was not checked


#: The only datetime shape a ``due:`` accepts (the board's contract with marlowe's parser): a space
#: separator, a compact date or a bare hour is not a date.
_DUE_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?")


def parse_date(value: str) -> datetime | None:
    """A ``due:`` value as the board defines it (docs/agents/threads-board.md): ``YYYY-MM-DD`` is due from
    local midnight of that day; an ISO datetime with no offset is local time, with ``Z`` or ``+hh:mm`` exact."""
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            d = date.fromisoformat(value)
            return datetime(d.year, d.month, d.day).astimezone()
        if not _DUE_DATETIME_RE.fullmatch(value):
            return None
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.astimezone()


def _bead_note(bead: str, facts: Facts, key: str) -> str | None:
    got = facts.beads.get(bead)
    if got is None:
        return None
    status, kind, open_children = got
    if status == "closed":
        return f"{key} {bead}: closed, so this line is stale"
    if kind == "epic" and open_children == 0 and key == "ref":
        return f"{key} {bead}: open epic with no open child; close it or name what is left"
    if key == "waiting_on":
        return f"{key} {bead}: still {status}"
    return None


def verify(board: str, facts: Facts, now: datetime) -> list[str]:
    """Per thread, the lines whose claim the facts settle, as markdown. Lines that still hold are not listed,
    except a wait on a bead that is still open (it says the wait is real)."""
    out: list[str] = []
    for t in threads(board):
        notes: list[str] = []
        dues = [i for i, (k, _v) in enumerate(t.fields) if k == "due"]
        for i, (key, value) in enumerate(t.fields):
            if key == "due" and i != dues[-1]:
                continue  # given twice, the last due: line counts
            if key in ("ref", "waiting_on"):
                wanted = facts_named(value)
                for bead in sorted(wanted.beads):
                    if (note := _bead_note(bead, facts, key)):
                        notes.append(note)
                for tag in sorted(wanted.engine_tags | wanted.client_tags):
                    if tag in facts.tags:
                        if facts.tags[tag] and key == "waiting_on":
                            notes.append(f"waiting_on {tag}: the tag exists on origin, so this wait may be over")
                        elif not facts.tags[tag] and key == "ref":
                            notes.append(f"ref {tag}: no such tag on origin")
                for run_id in sorted(wanted.runs):
                    if run_id in facts.runs:
                        status, conclusion = facts.runs[run_id]
                        if status == "completed":
                            notes.append(f"{key} CI run {run_id}: completed, {conclusion or 'no conclusion'}"
                                         + (", so this wait is over" if key == "waiting_on" else ""))
                for pr in sorted(wanted.prs):
                    if pr in facts.prs and facts.prs[pr] in ("MERGED", "CLOSED"):
                        notes.append(f"{key} PR #{pr}: {facts.prs[pr].lower()}"
                                     + (", so this wait is over" if key == "waiting_on" else ""))
            elif key == "due":
                when = parse_date(value)
                if when is not None and when <= now:
                    notes.append(f"due {value}: overdue")
            elif key == "worktree":
                path = os.path.expanduser(value)  # verbatim; a slug or a relative id is not checked
                if os.path.isabs(path) and not os.path.isdir(path):
                    notes.append(f"worktree {value}: no such directory")
        if notes:
            out.append(f"- {t.title}")
            out.extend(f"  - {n}" for n in dict.fromkeys(notes))
    return out


def board_worktree(board: str) -> list[str]:
    """Absolute paths the board's ``worktree:`` lines name, in order."""
    paths = []
    for t in threads(board):
        for key, value in t.fields:
            if key == "worktree":
                path = os.path.expanduser(value)  # verbatim (marlowe): a path may hold spaces
                if os.path.isabs(path):
                    paths.append(path)
    return paths


# ── resolvers (each one call, bounded) ───────────────────────────────────────


def _bd_rows(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    rows = data.get("issues", []) if isinstance(data, dict) else data
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)] if isinstance(rows, list) else []


def resolve_beads(ids: Iterable[str], bd: str | None, cwd: str | None, timeout: float) -> tuple[dict[str, tuple[str, str, int]], str]:
    """``({id: (status, type, open children)}, why-not)``: one ``bd list`` of the beads not closed, then one
    ``--id`` call for the named beads that list did not hold (closed, or not a bead). Unknown ids are left out."""
    ids = sorted(set(ids))
    if not ids:
        return {}, ""
    if not bd:
        return {}, "beads not checked: bd not found"
    base = [bd, "list", "--flat", "--skip-labels", "--limit", "0", "--json"]
    code, raw = run([*base, "--status", "open,in_progress,blocked,deferred"], timeout, cwd)
    if code != 0:
        return {}, "beads not checked: bd list failed or timed out"
    open_rows = _bd_rows(raw)
    by_id = {r["id"]: r for r in open_rows}
    out: dict[str, tuple[str, str, int]] = {}

    def children(epic: str) -> int:
        return sum(1 for r in open_rows if r.get("parent") == epic or r["id"].startswith(epic + "."))

    missing = [i for i in ids if i not in by_id]
    for i in ids:
        if i in by_id:
            out[i] = (str(by_id[i].get("status")), str(by_id[i].get("issue_type")), children(i))
    if missing:
        code, raw = run([*base, "--all", "--id", ",".join(missing)], timeout, cwd)
        if code == 0:
            for r in _bd_rows(raw):
                if r["id"] in missing:
                    out[r["id"]] = (str(r.get("status")), str(r.get("issue_type")), 0)
    return out, ""


def resolve_tags(tags: Iterable[str], git: str | None, cwd: str | None, timeout: float) -> tuple[dict[str, bool], str]:
    """``({tag: exists on origin}, why-not)``, one ``git ls-remote --tags origin`` call."""
    tags = sorted(set(tags))
    if not tags:
        return {}, ""
    if not git:
        return {}, "tags not checked: git not found"
    code, out = run([git, "ls-remote", "--tags", "origin", *(f"refs/tags/{t}" for t in tags)], timeout, cwd)
    if code != 0:
        return {}, "tags not checked: git ls-remote failed or timed out"
    present = {ln.split("refs/tags/", 1)[1].removesuffix("^{}") for ln in out.splitlines() if "refs/tags/" in ln}
    return {t: t in present for t in tags}, ""


def resolve_run(run_id: str, gh: str | None, cwd: str | None, timeout: float) -> tuple[str, str] | None:
    if not gh:
        return None
    code, out = run([gh, "run", "view", run_id, "--json", "status,conclusion"], timeout, cwd)
    try:
        d = json.loads(out) if code == 0 else None
    except ValueError:
        d = None
    return (str(d.get("status") or ""), str(d.get("conclusion") or "")) if isinstance(d, dict) else None


def resolve_pr(number: str, gh: str | None, cwd: str | None, timeout: float) -> str | None:
    if not gh:
        return None
    code, out = run([gh, "pr", "view", number, "--json", "state"], timeout, cwd)
    try:
        d = json.loads(out) if code == 0 else None
    except ValueError:
        d = None
    return str(d.get("state") or "") if isinstance(d, dict) else None
