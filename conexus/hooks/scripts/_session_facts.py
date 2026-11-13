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

if os.path.dirname(os.path.abspath(__file__)) not in sys.path:  # loaded by path from the restart script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _exec_path  # noqa: E402 -- the plugin's own resolver; it must follow the path insert

#: A Claude Code Bash-tool shell: ``/bin/zsh -c source <config>/shell-snapshots/snapshot-...``.
SNAPSHOT_MARK = "/shell-snapshots/snapshot-"
#: The most process or task lines one listing prints; the rest are counted.
MAX_LISTED = 8

#: The body of a Bash-tool shell's ``eval '...'``. Its quotes come out as ``'\''`` or, in macOS ``ps``,
#: ``'"'"'``; a newline shows as ``\012``.
_EVAL_RE = re.compile(r"""\beval '((?:[^']|'\\''|'"'"')*)'""")
_PS_ROW_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.*)$")

Runner = Callable[[list[str], float], "tuple[int, str]"]


#: Every child runs in the C locale (``ps -o lstart`` day and month names in English) and never prompts.
_CHILD_ENV = {"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GH_PROMPT_DISABLED": "1"}


def run(argv: list[str], timeout: float, cwd: str | None = None) -> tuple[int, str]:
    """``(returncode, stdout)``; ``(-1, "")`` when the command cannot run or times out. Never raises."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=max(timeout, 0.2), check=False, cwd=cwd, stdin=subprocess.DEVNULL,
                              env={**os.environ, **_CHILD_ENV})
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
    """Every process, from ``ps -ww -A -o pid=,ppid=,lstart=,command=`` (macOS and Linux; ``-ww`` so Linux does
    not cut the command at COLUMNS). ``[]`` on Windows or failure."""
    if sys.platform == "win32":
        return []
    ps = _exec_path.which_off_cwd("ps")
    if runner is None and ps is None:
        return []
    code, out = (runner or (lambda a, t: run(a, t)))([str(ps), "-ww", "-A", "-o", "pid=,ppid=,lstart=,command="], 3.0)
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
    body = m.group(1).replace("'\\''", "'").replace("'\"'\"'", "'") if m else command
    return " ".join(body.replace("\\012", " ").split())


def background_tasks(session_id: str, sessions_dir: Path, table: list[Proc],
                     exclude: frozenset[int] | set[int] = frozenset()) -> list[Proc] | None:
    """The session's Bash-tool shells still running: Claude Code's background tasks and Monitors.

    They are the shells below the session's Claude process whose command line carries a shell
    snapshot. At PreCompact no foreground tool is running, so every such shell is a background one;
    *exclude* removes the caller's own ancestry (the hook, the restart script). ``None`` when it cannot
    be known (no session file names the id, or no process table): "none running" is said only on proof.
    """
    pid = claude_pid(session_id, sessions_dir)
    if pid is None or not table:
        return None
    return [p for p in descendants(table, pid) if SNAPSHOT_MARK in p.command and p.pid not in exclude]


def process_cwds(runner: Runner | None = None) -> dict[int, str] | None:
    """``{pid: cwd}`` for the user's processes: ``/proc/<pid>/cwd`` on Linux, ``lsof -d cwd`` on macOS;
    ``None`` when they cannot be read."""
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
            return None
        return out
    if sys.platform != "darwin":
        return None
    lsof = _exec_path.which_off_cwd("lsof")
    if runner is None and lsof is None:
        return None
    # Every process's cwd, not only this user's: a process the user cannot read is skipped by lsof itself, and
    # only cwds inside the session's own worktrees are kept by the caller.
    code, text = (runner or (lambda a, t: run(a, t)))([str(lsof), "-a", "-d", "cwd", "-Fpn"], 3.0)
    if code not in (0, 1) or not text:  # lsof exits 1 when some process could not be read
        return None
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


#: A background task that is the session's standing mail wake (the mailbox skill keeps one armed when the
#: session has no channel): it is meant to run, and a restart re-arms it.
_STANDING_RE = re.compile(r"nx tuple wait(\s+[^;&|]*)?")


def standing(command: str) -> bool:
    """The task is the bare wake (``nx tuple wait <space> [flags] [> log 2>&1]``), nothing chained to it."""
    body = re.sub(r"\d?>&\d", "", task_command(command)).strip()
    return _STANDING_RE.fullmatch(body) is not None


def task_lines(tasks: list[Proc] | None, procs: list[tuple[Proc, str]] | None, crons: list[str], *,
               cron_note: str, why_not: str = "") -> list[str]:
    """The checkpoint's listing of what runs for this session, as markdown bullets. ``None`` for *tasks* or
    *procs* means the lookup failed: it is said so, never read as "none"."""
    lines = []
    if tasks is None:
        lines.append(f"- background tasks: not listed ({why_not or 'the process table or the session file could not be read'})")
    elif not tasks:
        lines.append("- background tasks: none running")
    for p in (tasks or [])[:MAX_LISTED]:
        note = " (standing mail wait)" if standing(p.command) else ""
        lines.append(f"- background task pid {p.pid}, started {p.start}{note}: `{task_command(p.command)[:160]}`")
    if tasks and len(tasks) > MAX_LISTED:
        lines.append(f"- {len(tasks) - MAX_LISTED} more background tasks not listed")
    if procs is None:
        lines.append("- processes in this session's worktrees: not listed (their working directories could not be read)")
    for p, root in (procs or [])[:MAX_LISTED]:
        lines.append(f"- process pid {p.pid} in {root}, started {p.start}: `{' '.join(p.command.split())[:160]}`")
    if procs and len(procs) > MAX_LISTED:
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
            whose = "" if owner else ", owner unknown: the file names no session"
            lines.append(f"`{when}` {what} ({path}{whose})")
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


#: Reflog actions that are a commit made in this worktree. A checkout, reset, pull, fast-forward merge or
#: rebase pick of an old branch is not: they re-stamp a worktree nobody is working in.
_COMMIT_ACTIONS = ("commit", "cherry-pick", "revert")


def last_work_time(worktree: str) -> float:
    """When the session last worked here, from the worktree's own HEAD reflog: its newest commit, or its
    creation (the first entry) when that is newer. ``0.0`` when unreadable.

    The commit date of HEAD is not the signal (a worktree cut at a peer's commit carries a recent date and
    no work of its own), and neither is the launch cwd. Creation counts so a worktree made after the
    session's last commit elsewhere is not ranked below an older one before its first commit.
    """
    gitdir = _gitdir(worktree)
    if gitdir is None:
        return 0.0
    try:
        with open(gitdir / "logs" / "HEAD", "rb") as fh:
            first = fh.readline().decode("utf-8", "replace")
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 65536))
            tail = fh.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return 0.0

    def stamp(line: str) -> float:
        parts = line.partition("\t")[0].split()
        return float(parts[-2]) if len(parts) >= 2 and parts[-2].isdigit() else 0.0

    newest = stamp(first)
    for line in tail.splitlines():
        if line.partition("\t")[2].startswith(_COMMIT_ACTIONS):
            newest = max(newest, stamp(line))
    return newest


#: The most worktrees one checkpoint reads for unlanded work, newest first; the rest are counted.
MAX_WORKTREES = 12


@dataclass(frozen=True)
class WorktreeWork:
    path: str
    branch: str
    unlanded: int  # commits whose change is not on origin/develop; -1 unknown
    dirty: int  # tracked files changed; -1 unknown


def worktree_work(path: str, branch: str, git: str, timeout: float = 3.0) -> WorktreeWork:
    """Commits not landed on ``origin/develop`` and changed tracked files. "Landed" is against develop,
    never the branch's own upstream: a pushed feature branch whose tracking ref is behind would count work
    that is already on develop (marlowe-runner, review of 448ad2e1c). HEAD inside develop is 0; otherwise
    ``git cherry`` counts commits whose patch develop does not hold (a cherry-picked fix counts as landed).
    An empty *branch* is looked up (``HEAD`` stays empty: detached)."""
    if not branch:
        code, head = run([git, "-C", path, "rev-parse", "--abbrev-ref", "HEAD"], timeout)
        branch = head.strip() if code == 0 and head.strip() != "HEAD" else ""
    code, _ = run([git, "-C", path, "merge-base", "--is-ancestor", "HEAD", "origin/develop"], timeout)
    if code == 0:
        unlanded = 0
    elif code == 1:
        code, cherry = run([git, "-C", path, "cherry", "origin/develop", "HEAD"], timeout)
        unlanded = sum(1 for ln in cherry.splitlines() if ln.startswith("+")) if code == 0 else -1
    else:
        unlanded = -1
    code, status = run([git, "--no-optional-locks", "-C", path, "status", "--porcelain", "--untracked-files=no"], timeout)
    dirty = len([ln for ln in status.splitlines() if ln.strip()]) if code == 0 else -1
    return WorktreeWork(path, branch, unlanded, dirty)


#: A bead id inside a branch name: ``feature/<bead-id>-<slug>``; the id ends at the slug's hyphen.
_BRANCH_BEAD_RE = re.compile(r"(?:^|/)([a-z][a-z0-9]*-[0-9a-z]{5}(?:\.\d{1,3})*)(?=$|[-/])")


def bead_named(bead: str, text: str) -> bool:
    """*text* names *bead* or one of its parents (``ref: nexus-z1d0l.1`` names ``nexus-z1d0l.1.3``)."""
    parts = bead.split(".")
    return any(_word(".".join(parts[:n]), text) for n in range(len(parts), 0, -1))


def unboarded(work: Iterable[WorktreeWork], board: str) -> tuple[list[str], list[str]]:
    """``(lines, unchecked)``: worktrees holding work (unlanded commits, changed files) that no board thread
    names, and those whose work could not be read. A thread names a worktree by its path or directory name,
    its branch, or a bead id the branch carries (or a parent of it)."""
    lines, unchecked = [], []
    for w in work:
        # By path: the whole path, or ``<something>-wt/<dir>`` as boards write it. A bare directory name is not
        # enough: ``mailbox/tamsin`` on tamsin's board named the ``tamsin`` worktree and hid its work.
        name = Path(w.path).name
        named = (re.search(rf"{re.escape(w.path)}(?![\w.-])", board) is not None
                 or re.search(rf"wt[/\\]{re.escape(name)}(?![\w-])", board) is not None
                 or bool(w.branch and w.branch in board)
                 or any(bead_named(b, board) for b in _BRANCH_BEAD_RE.findall(w.branch)))
        if named:
            continue
        if w.unlanded < 0 or w.dirty < 0:
            unchecked.append(f"{w.path}: git could not read its work")
            continue
        what = []
        if w.unlanded > 0:
            what.append(f"{w.unlanded} commit(s) not on origin/develop (as of its last fetch)")
        if w.dirty > 0:
            what.append(f"{w.dirty} changed file(s)")
        if what:
            lines.append(f"- no board thread names {w.path} [{w.branch or 'detached'}]: {', '.join(what)}")
    if len(lines) > MAX_LISTED:
        lines = [*lines[:MAX_LISTED], f"- {len(lines) - MAX_LISTED} more unnamed worktrees holding work not listed"]
    return lines, unchecked


# ── the board ────────────────────────────────────────────────────────────────

#: A bead id: ``<prefix>-<5 base36>``, children as ``.N``. The tail refuses ``.<digit>`` so a longer child
#: id is never read as its parent. Ids bd does not know are dropped after lookup.
BEAD_RE = re.compile(r"(?<![\w./-])([a-z][a-z0-9]*-[0-9a-z]{5}(?:\.\d{1,3})*)(?![\w-]|\.\d)")
#: Engine tags: the tag itself, "engine 0.1.190", "0.1.190 cut", or a bare 0.1.N (the engine's own series).
_ENGINE_TAG_RE = re.compile(r"\bengine-service-v(\d+\.\d+\.\d+)\b")
_ENGINE_WORDS_RE = re.compile(r"\bengine(?:-service)?\s+v?(\d+\.\d+\.\d+)(?![\d.])|(?<![\d.])(\d+\.\d+\.\d+)\s+(?:engine\s+)?cut\b"
                              r"|(?<![\w.])(0\.1\.\d+)(?![\d.])")
#: Client tags only where the line says it is ours: "conexus 8.2.2", "conexus v8.2.2", "tag v8.2.2" or
#: "release v8.2.2". A bare vX.Y.Z can be any project's.
_CLIENT_TAG_RE = re.compile(r"\b(?:conexus|tag|release)\s+v?(\d+\.\d+\.\d+)(?![\d.])", re.IGNORECASE)
#: CI runs: a run id is unique, so a lookup in the wrong repository fails rather than answering wrongly.
_RUN_RE = re.compile(r"(?:github\.com/([\w.-]+/[\w.-]+)/actions/runs/|\bruns?\s+)(\d{9,12})\b")
#: PRs only with their repository: a URL or ``owner/repo#N``. A bare "PR #12" is not looked up, because the
#: same number in the cwd's repository is a different PR (conexus #703 vs nexus #703, review of 448ad2e1c).
_PR_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d{1,6})\b|(?<![\w/.-])([\w.-]+/[\w.-]+)#(\d{1,6})\b")
#: Thread fields this module reads. Keys match as the board parser reads them (marlowe, src/nexus/console/
#: threads.py): case-insensitive, with ``-``, a space and ``_`` alike (``Waiting-on :``).
_FIELD_RE = re.compile(r"^\s*(ref|waiting[-_ ]on|due|worktree)\s*:\s*(.*)$", re.IGNORECASE)
_UNSET = frozenset({"", "none", "no", "n/a", "-", "—"})
#: A thread heading: fields belong to the nearest ``#``, ``##`` or ``###`` heading (docs/agents/threads-board.md).
_HEADING_RE = re.compile(r"^#{1,3}\s+(\S.*)$")


def _word(token: str, text: str) -> bool:
    return re.search(rf"(?<![\w./-]){re.escape(token)}(?![\w-]|\.\d)", text) is not None


@dataclass
class Thread:
    title: str
    fields: list[tuple[str, str]] = field(default_factory=list)  # (key, value) for ref/waiting_on/due/worktree


def threads(board: str) -> list[Thread]:
    """The board's thread blocks with their ref, waiting_on, due and worktree lines."""
    out: list[Thread] = []
    for line in board.splitlines():
        if (h := _HEADING_RE.match(line)):
            out.append(Thread(h.group(1).strip()))
        elif out and (m := _FIELD_RE.match(line)):
            key = re.sub(r"[-_ ]", "_", m.group(1).lower())
            value = m.group(2).strip()
            if key in ("due", "worktree") and value.lower() in _UNSET:
                continue
            out[-1].fields.append((key, value))
    return out


@dataclass(frozen=True)
class Wanted:
    """Every fact a text names."""
    beads: frozenset[str]
    engine_tags: frozenset[str]
    client_tags: frozenset[str]
    runs: frozenset[tuple[str, str]]  # (repo or "", run id)
    prs: frozenset[tuple[str, str]]  # (repo, number)

    def count(self) -> int:
        return len(self.beads) + len(self.engine_tags) + len(self.client_tags) + len(self.runs) + len(self.prs)


def facts_named(text: str) -> Wanted:
    beads = set(BEAD_RE.findall(text))
    engine = set(_ENGINE_TAG_RE.findall(text))
    for groups in _ENGINE_WORDS_RE.findall(text):
        engine.update(g for g in groups if g)
    client = set(_CLIENT_TAG_RE.findall(_ENGINE_TAG_RE.sub("", text))) - engine
    prs = {(a or c, b or d) for a, b, c, d in _PR_RE.findall(text)}
    return Wanted(frozenset(beads), frozenset(f"engine-service-v{v}" for v in engine),
                  frozenset(f"v{v}" for v in client), frozenset(_RUN_RE.findall(text)), frozenset(prs))


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
    prs: dict[tuple[str, str], str] = field(default_factory=dict)  # (repo, number) -> state


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


@dataclass
class Verdict:
    lines: list[str]
    checked: int  # ref and waiting_on lines where at least one named fact was found
    opaque: int  # ref and waiting_on lines naming no bead, tag, run or PR at all (a person, a branch, a deploy)
    unresolved: int = 0  # lines naming one, where no lookup answered (it failed, timed out, or bd knows no such id)


#: Words a ref or waiting_on line uses around the facts it names, which name no other wait themselves.
_FILLER = frozenset({"and", "the", "a", "an", "on", "for", "of", "to", "in", "ci", "run", "runs", "pr", "tag", "cut",
                     "engine", "engine-service", "conexus", "release", "develop", "github", "com", "https", "http",
                     "actions", "pull", "nexus"})


def _nothing_else(value: str, wanted: Wanted) -> bool:
    """The line names nothing but *wanted*: with its facts and filler words removed, no word is left. Only
    then is a conclusion about the whole line drawn; a person, a sha or a second step keeps it unsaid
    (halcyon's "0.1.188 then 8.1.3", baton's "nexus-qo7ie.31; develop 68bc53766", review r2)."""
    rest = _PR_RE.sub(" ", re.sub(r"https?://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+", " ", value))
    for token in sorted({*wanted.beads, *wanted.engine_tags, *wanted.client_tags,
                         *(r for _repo, r in wanted.runs), *(f"{repo}#{n}" for repo, n in wanted.prs),
                         *(n for _repo, n in wanted.prs), *(t.removeprefix("engine-service-v") for t in wanted.engine_tags),
                         *(t.removeprefix("v") for t in wanted.client_tags)}, key=len, reverse=True):
        rest = rest.replace(token, " ")
    words = re.findall(r"[a-z0-9][a-z0-9.'-]*", rest.lower())
    return all(w.strip(".'-") in _FILLER for w in words)


def _line_notes(key: str, value: str, facts: Facts) -> list[str]:
    """What the facts say about one ref or waiting_on line. The fact is always reported; the conclusion
    ("this line is stale", "this wait is over") only when the line names nothing else, since one closed bead
    or one finished run says nothing about the other waits on the line."""
    wanted = facts_named(value)
    single = wanted.count() == 1 and _nothing_else(value, wanted)
    notes: list[str] = []
    beads = sorted(wanted.beads)
    known = [b for b in beads if b in facts.beads]
    closed = [b for b in known if facts.beads[b][0] == "closed"]
    if closed and len(closed) == len(beads) == len(known) and wanted.count() == len(beads) and _nothing_else(value, wanted):
        notes.append(f"{key} {', '.join(closed)}: closed, so this line is stale")
    else:
        notes.extend(f"{key}: {b} is closed" for b in closed)
    for b in known:
        status, kind, open_children = facts.beads[b]
        if status == "open" and kind == "epic" and open_children == 0 and key == "ref":
            notes.append(f"ref {b}: open epic with no open child; close it or name what is left")
        elif status != "closed" and key == "waiting_on" and single:
            notes.append(f"waiting_on {b}: still {status}")
    for tag in sorted(wanted.engine_tags | wanted.client_tags):
        if facts.tags.get(tag) and key == "waiting_on":
            notes.append(f"waiting_on {tag}: the tag exists on origin" + (", so this wait may be over" if single else ""))
    for repo, run_id in sorted(wanted.runs):
        if run_id in facts.runs and facts.runs[run_id][0] == "completed":
            conclusion = facts.runs[run_id][1] or "no conclusion"
            over = ", so this wait is over" if key == "waiting_on" and single else ""
            notes.append(f"{key} CI run {run_id}: completed, {conclusion}{over}")
    for repo, number in sorted(wanted.prs):
        state = facts.prs.get((repo, number))
        if state in ("MERGED", "CLOSED"):
            over = ", so this wait is over" if key == "waiting_on" and single else ""
            notes.append(f"{key} {repo}#{number}: {state.lower()}{over}")
    return notes


def verify(board: str, facts: Facts, now: datetime) -> Verdict:
    """Per thread, the lines whose claim the facts settle, as markdown, with how many lines could be
    checked at all. Lines that still hold are not listed."""
    out: list[str] = []
    checked = opaque = unresolved = 0
    for t in threads(board):
        notes: list[str] = []
        dues = [i for i, (k, _v) in enumerate(t.fields) if k == "due"]
        for i, (key, value) in enumerate(t.fields):
            if key in ("ref", "waiting_on"):
                w = facts_named(value)
                resolved = (any(b in facts.beads for b in w.beads)
                            or any(t in facts.tags for t in w.engine_tags | w.client_tags)
                            or any(r in facts.runs for _repo, r in w.runs) or any(p in facts.prs for p in w.prs))
                if resolved:
                    checked += 1
                    notes.extend(_line_notes(key, value, facts))
                elif w.count():
                    unresolved += 1
                elif value.lower() not in _UNSET:
                    opaque += 1
            elif key == "due" and i == dues[-1]:  # given twice, the last due: line counts
                when = parse_date(value)
                if when is not None and when <= now:
                    notes.append(f"due {value}: due now")
            elif key == "worktree":
                path = os.path.expanduser(value)  # verbatim; a slug or a relative id is not checked
                if os.path.isabs(path) and not os.path.exists(path):
                    notes.append(f"worktree {value}: no such path")
        if notes:
            out.append(f"- {t.title}")
            out.extend(f"  - {n}" for n in dict.fromkeys(notes))
    return Verdict(out, checked, opaque, unresolved)


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


def _child_of(row: dict, epic: str) -> bool:
    """An open row is the epic's child by its parent field, its id, or a dependency on the epic."""
    if row.get("parent") == epic or row["id"].startswith(epic + "."):
        return True
    deps = row.get("dependencies")
    return isinstance(deps, list) and any(
        (d.get("depends_on_id") if isinstance(d, dict) else d) == epic for d in deps)


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
    for i in ids:
        if i in by_id:
            out[i] = (str(by_id[i].get("status")), str(by_id[i].get("issue_type")),
                      sum(1 for r in open_rows if r["id"] != i and _child_of(r, i)))
    missing = [i for i in ids if i not in by_id]
    if missing:
        code, raw = run([*base, "--all", "--id", ",".join(missing)], timeout, cwd)
        if code != 0:
            return out, "some beads not checked: bd list --id failed or timed out"
        for r in _bd_rows(raw):
            if r["id"] in missing:
                # Not in the not-closed list: closed, or a status that list leaves out (hooked, pinned).
                out[r["id"]] = (str(r.get("status")), str(r.get("issue_type")), -1)
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


def resolve_run(repo: str, run_id: str, gh: str | None, cwd: str | None, timeout: float) -> tuple[str, str] | None:
    if not gh:
        return None
    code, out = run([gh, "run", "view", run_id, *(["-R", repo] if repo else []), "--json", "status,conclusion"], timeout, cwd)
    try:
        d = json.loads(out) if code == 0 else None
    except ValueError:
        d = None
    return (str(d.get("status") or ""), str(d.get("conclusion") or "")) if isinstance(d, dict) else None


def resolve_pr(repo: str, number: str, gh: str | None, cwd: str | None, timeout: float) -> str | None:
    if not gh or not repo:
        return None
    code, out = run([gh, "pr", "view", number, "-R", repo, "--json", "state"], timeout, cwd)
    try:
        d = json.loads(out) if code == 0 else None
    except ValueError:
        d = None
    return str(d.get("state") or "") if isinstance(d, dict) else None
