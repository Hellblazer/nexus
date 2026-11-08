#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# PEP 723 metadata: `uv run` reads the interpreter floor from here when the script
# is run by hand; hooks.json carries the same floor in its argv.
# /// script
# requires-python = ">=3.12"
# ///
"""Advise before an action that destroys shared state (nexus-qo7ie.36).

ONE table-driven PreToolUse hook for the machine-checkable bright lines of
the destructive-action guard rules. Each :class:`Row` in :data:`ROWS` names a
rule, the tools it watches, a predicate over the call and the advice text. A
call pays for a JSON parse and one substring hint test per row. Only when a
hint hits (``|``, ``rm``, ``grep``, ``find`` ... are common) does it also pay
for one split-and-tokenise pass over the command, shared by every row; a
predicate that needs the machine's state (``git worktree list``, ``pgrep``)
spawns under ONE shared deadline of :data:`DEADLINE_S` seconds, and once that
is spent the answer is "no match". The ``_lib`` framework (and its telemetry)
is imported only after a row has matched.

MODE. :data:`MODE` is ``"advise"``: a matched call gets the advice as
``additionalContext`` and NO permission decision (the ordinary permission flow
decides, exactly like ``_lib.warn``). The hook never denies in this mode. The
flip to ``"deny"`` is the one line below (or a per-row ``mode=``); the deny
path is implemented and pinned by tests but is not active. In deny mode the
usual ``# routing-allow: <reason>`` escape applies to Bash calls.

FALSE POSITIVES only advise, but the predicates are written to keep them rare:
the command is split on unquoted ``; & | newline``, each segment is tokenised
and unwrapped (env assignments, sudo/nohup/xargs, ssh/wsl/lab-remote/lab-run,
``bash -c '...'``), and a row looks at the command word, not at any substring.

NOT CARRIED, by design (not machine-checkable from the command text): which
branches are "yours" (ownership), whether a deletion target is alive, whether
a cloud-touching walk sits inside an announced window, and the brief clauses
for dispatched agents. Those live in prose. Also left out on purpose: a FOREGROUND pipe whose first
stage is nx, pytest, lab-run, lab-remote or mvnw (``pytest ... | tail`` and
``lab-remote ... | tail`` are everyday and were judged quiet; the incident was the background
form, and "filter at read time" lives in the nexus skill).

Stdlib only. Fail-open: a predicate that raises counts as no match.
"""
from __future__ import annotations

import io
import json
import os
import re
import stat
import sys
import time
from typing import Callable, NamedTuple

RULE_NAME = "destructive_action_advice"

#: Seconds, from the start of the call, that ALL spawns together may take.
DEADLINE_S = 2.0

#: "advise" (never denies) or "deny". The one-line flip.
MODE = "advise"

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit"})
_BASH = frozenset({"Bash"})

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_HERE)

# ---------------------------------------------------------------------------
# Command analysis (quote-aware split, tokenise, unwrap)
# ---------------------------------------------------------------------------


class Seg(NamedTuple):
    raw: str
    eff: tuple[str, ...]  # effective command tokens, command word first
    via: frozenset[str]  # prefix words skipped on the way (xargs, nohup, ...)

    @property
    def cmd(self) -> str:
        return os.path.basename(self.eff[0]) if self.eff else ""


class Pipe(NamedTuple):
    segs: tuple[Seg, ...]
    term: str  # what ends the pipeline: ; & && || newline or ""


def _split(text: str) -> list[tuple[str, str]]:
    """Split on unquoted ``; & | newline`` into ``(segment, separator)``."""
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < n:
                i += 1
                buf.append(text[i])
            elif c == quote:
                quote = ""
        elif c == "\\" and i + 1 < n:
            buf.append(c)
            i += 1
            buf.append(text[i])
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c in ";\n":
            out.append(("".join(buf), c))
            buf = []
        elif c == "|":
            sep = "||" if text[i + 1 : i + 2] == "|" else "|"
            i += len(sep) - 1
            out.append(("".join(buf), sep))
            buf = []
        elif c == "&":
            prev = text[i - 1] if i else ""
            nxt = text[i + 1 : i + 2]
            if prev in "<>" or nxt == ">":  # 2>&1, >&2, &>file
                buf.append(c)
            else:
                sep = "&" * 2 if nxt == "&" else "&"  # not a literal: the user-surface lint bans "and-and" strings
                i += len(sep) - 1
                out.append(("".join(buf), sep))
                buf = []
        else:
            buf.append(c)
        i += 1
    out.append(("".join(buf), ""))
    return out


def _tokens(raw: str) -> list[str]:
    try:
        import shlex  # noqa: PLC0415 -- lazy: only a tokenised command pays for it

        return shlex.split(raw, posix=True)
    except ValueError:  # unbalanced quoting: a rough split beats dropping the segment
        return raw.split()


_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PREFIX = frozenset(
    {"sudo", "nohup", "time", "exec", "command", "builtin", "nice", "ionice", "env",
     "setsid", "stdbuf", "caffeinate"}
)
_XARGS_VALUED = frozenset({"-n", "-I", "-P", "-L", "-s", "-d", "-E", "-a"})


def _unwrap(toks: list[str]) -> tuple[tuple[str, ...], frozenset[str]]:
    via: set[str] = set()
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        for opener in ("$(", "(", "{", "`"):
            if t.startswith(opener):
                t = t[len(opener):]
                break
        if not t:
            i += 1
            continue
        if _ASSIGN.match(t):
            i += 1
            continue
        base = os.path.basename(t)
        if base in _PREFIX:
            via.add(base)
            i += 1
            while i < n and toks[i].startswith("-"):
                i += 1
            continue
        if base == "timeout":
            via.add(base)
            i += 1
            while i < n and toks[i].startswith("-"):
                i += 1
            if i < n and re.fullmatch(r"[0-9.]+[smhd]?", toks[i]):
                i += 1
            continue
        if base == "xargs":
            via.add(base)
            i += 1
            while i < n and toks[i].startswith("-"):
                i += 2 if toks[i] in _XARGS_VALUED else 1
            continue
        if t != toks[i]:
            return (t, *toks[i + 1 :]), frozenset(via)
        break
    return tuple(toks[i:]), frozenset(via)


_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_SSH_VALUED = frozenset("-p -i -l -o -F -J -L -R -D -b -c -e -m -O -S -w -W -B -E -I -Q".split())
_WSL_VALUED = frozenset({"-d", "-u", "--distribution", "--user", "--cd"})


def _inner_commands(eff: tuple[str, ...]) -> list[str]:
    """Command text a wrapper segment carries (``bash -c``, ssh, wsl, lab-run)."""
    if not eff:
        return []
    c, rest = os.path.basename(eff[0]), list(eff[1:])
    if c in _SHELLS:
        for j, t in enumerate(rest):
            if re.fullmatch(r"-[a-z]*c[a-z]*", t) and j + 1 < len(rest):
                return [rest[j + 1]]
        return []
    if c == "eval":
        return [" ".join(rest)]
    if c in ("lab-remote", "lab-run"):
        return [" ".join(rest[rest.index("--") + 1 :])] if "--" in rest else []
    if c in ("ssh", "wsl", "wsl.exe"):
        valued = _SSH_VALUED if c == "ssh" else _WSL_VALUED
        if c != "ssh" and "--" in rest:
            return [" ".join(rest[rest.index("--") + 1 :])]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            i += 2 if rest[i] in valued else 1
        if c == "ssh":
            i += 1  # the host
        return [" ".join(rest[i:])] if i < len(rest) else []
    return []


_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")


def _strip_heredocs(text: str) -> tuple[str, list[str]]:
    """``(text without heredoc bodies, the bodies)``. A body is data fed to a command, not a command: the
    operator line stays (its redirect targets matter), the lines up to the closing tag go. An unclosed
    heredoc runs to the end, as in the shell."""
    if "<<" not in text:
        return text, []
    kept: list[str] = []
    bodies: list[str] = []
    pending: list[tuple[str, bool, list[str]]] = []
    for line in text.split("\n"):
        if pending:
            tag, dash, body = pending[0]
            if (line.lstrip("\t") if dash else line).rstrip() == tag:
                bodies.append("\n".join(body))
                pending.pop(0)
            else:
                body.append(line)
            continue
        kept.append(line)
        for m in _HEREDOC.finditer(line):
            pending.append((m.group(3), bool(m.group(1)), []))
    bodies.extend("\n".join(body) for _tag, _dash, body in pending)
    return "\n".join(kept), bodies


def _analyze(text: str, depth: int = 0, bodies: list[str] | None = None) -> tuple[list[Pipe], list[Pipe]]:
    """``(top-level pipelines, pipelines found inside wrappers)``. Heredoc bodies are cut first and
    appended to *bodies* when given."""
    text, found = _strip_heredocs(text)
    if bodies is not None:
        bodies.extend(found)
    top: list[Pipe] = []
    inner: list[Pipe] = []
    cur: list[Seg] = []
    for raw, sep in _split(text):
        if raw.strip():
            eff, via = _unwrap(_tokens(raw))
            cur.append(Seg(raw, eff, via))
            if depth < 3:
                for sub in _inner_commands(eff):
                    if sub.strip():
                        t2, i2 = _analyze(sub, depth + 1)
                        inner.extend(t2 + i2)
        if sep != "|" and cur:
            top.append(Pipe(tuple(cur), sep))
            cur = []
    if cur:
        top.append(Pipe(tuple(cur), ""))
    return top, inner


class Ctx:
    """One tool call, with the command analysis computed on first use."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.tool = str(payload.get("tool_name") or "")
        ti = payload.get("tool_input")
        self.tool_input: dict = ti if isinstance(ti, dict) else {}
        cmd = self.tool_input.get("command") if self.tool == "Bash" else ""
        self.command: str = cmd if isinstance(cmd, str) else ""
        fp = self.tool_input.get("file_path") if self.tool in _EDIT_TOOLS else ""
        self.file_path: str = fp if isinstance(fp, str) else ""
        self.bg: bool = self.tool_input.get("run_in_background") is True
        self.cwd: str = str(payload.get("cwd") or "") or os.getcwd()
        self.note = ""  # a predicate may set text that leads the advice (reset per row)
        self.deadline = time.monotonic() + DEADLINE_S  # shared by every spawn of this call
        self.worktrees: dict[str, int] = {}  # cwd -> worktree count, one lookup per cwd
        self.bodies: list[str] = []  # heredoc bodies of the command (data, never analysed as commands)
        self._analysis: tuple[list[Pipe], list[Pipe]] | None = None

    @property
    def subject(self) -> str:
        return self.command or self.file_path

    def _get(self) -> tuple[list[Pipe], list[Pipe]]:
        if self._analysis is None:
            self._analysis = _analyze(self.command, 0, self.bodies)
        return self._analysis

    @property
    def top(self) -> list[Pipe]:
        return self._get()[0]

    @property
    def pipes(self) -> list[Pipe]:
        top, inner = self._get()
        return top + inner



def _git_args(eff: tuple[str, ...]) -> tuple[list[str], str] | None:
    """``(args after git's global options, the -C directory)`` for a git segment."""
    if not eff or os.path.basename(eff[0]) not in {"git", "git.exe"}:
        return None
    i, chdir = 1, ""
    while i < len(eff):
        t = eff[i]
        if t == "-C" and i + 1 < len(eff):
            chdir = eff[i + 1]
            i += 2
        elif t in ("-c", "--git-dir", "--work-tree", "--namespace", "--exec-path") and i + 1 < len(eff):
            i += 2
        elif t.startswith("-"):
            i += 1
        else:
            break
    return list(eff[i:]), chdir


def _positional(args: list[str]) -> list[str]:
    out: list[str] = []
    after_dd = False
    for a in args:
        if after_dd or not a.startswith("-"):
            out.append(a)
        elif a == "--":
            after_dd = True
    return out


def _flag_cluster(args: list[str], letters: str) -> bool:
    """A short-option cluster (``-rf``) containing one of *letters*."""
    return any(re.fullmatch(r"-[A-Za-z]+", a) and any(ch in a[1:] for ch in letters) for a in args)


# ---------------------------------------------------------------------------
# Predicates (one per row)
# ---------------------------------------------------------------------------


def _pattern_kill(ctx: Ctx) -> bool:
    """pkill/killall; kill fed by $(pgrep|pidof|ps) or backticks; xargs kill after pgrep/ps."""
    for pipe in ctx.pipes:
        reader_seen = False
        for seg in pipe.segs:
            c = seg.cmd
            if c in ("pkill", "killall"):
                return True
            if c in ("pgrep", "pidof", "ps"):
                reader_seen = True
            elif c == "kill":
                if "xargs" in seg.via and reader_seen:
                    return True
                if re.search(r"(?:\$\(|`)\s*(?:pgrep|pidof|ps)\b", seg.raw):
                    return True
    return False


def _branch_delete(ctx: Ctx) -> bool:
    """git branch -d/-D/--delete with a glob, names from xargs / a substitution, or hidden output."""
    for pipe in ctx.pipes:
        for i, seg in enumerate(pipe.segs):
            parsed = _git_args(seg.eff)
            if not parsed or not parsed[0] or parsed[0][0] != "branch":
                continue
            opts = parsed[0][1:]
            if not ("--delete" in opts or _flag_cluster(opts, "dD")):
                continue
            if any(re.search(r"[*?\[]", t) for t in _positional(opts)):
                return True
            if "xargs" in seg.via:
                return True
            if re.search(r"(?:\$\(|`)\s*git\s+(?:branch|for-each-ref)\b", seg.raw):
                return True
            names = [t for t in _positional(opts) if not re.match(r"(?:\d*|&)[<>]", t)]  # not redirects
            if names and all(re.fullmatch(r"[\w./@+-]+", t) for t in names):
                continue  # every name is written out in the command: hiding the output loses nothing
            if i + 1 < len(pipe.segs) and pipe.segs[i + 1].cmd in ("wc", "head", "tail", "grep"):
                return True
            if re.search(r"(?:^|\s)(?:&>|1>|>)\s*/dev/null\b", seg.raw):
                return True
    return False


_STASH_WRITES = frozenset(
    {"push", "save", "pop", "apply", "drop", "clear", "branch", "create", "store"}
)


def _which(name: str) -> str | None:
    try:
        sys.path.insert(0, _SCRIPTS)
        import _exec_path  # noqa: PLC0415 -- stdlib-only helper beside the hooks

        return _exec_path.which_off_cwd(name)
    except Exception:  # noqa: BLE001 -- unresolvable: the predicate has no answer
        return None


def _spawn(ctx: Ctx, argv: list[str], cwd: str | None = None):
    """Run *argv* inside the call's one shared deadline. ``None`` when the deadline is spent, the run
    times out or cannot start: every caller reads that as "no match". stdin is /dev/null, never the
    hook's own pipe."""
    left = ctx.deadline - time.monotonic()
    if left <= 0:
        return None
    try:
        import subprocess  # noqa: PLC0415 -- lazy: only a predicate that spawns pays for it

        return subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=left,
        )
    except Exception:  # noqa: BLE001 -- undeterminable: no advice
        return None


def _worktree_count(ctx: Ctx, cwd: str) -> int:
    """Worktrees of the repository at *cwd* (looked up once per cwd); 0 when undeterminable."""
    if cwd in ctx.worktrees:
        return ctx.worktrees[cwd]
    count = 0
    exe = _which("git")
    if exe is not None:
        proc = _spawn(ctx, [exe, "worktree", "list", "--porcelain"], cwd)
        if proc is not None and proc.returncode == 0:
            count = sum(1 for line in proc.stdout.splitlines() if line.startswith("worktree "))
    ctx.worktrees[cwd] = count
    return count


def _stash(ctx: Ctx) -> bool:
    """git stash (bare, or a writing sub-verb) while the repository has more than one worktree."""
    for pipe in ctx.pipes:
        for seg in pipe.segs:
            parsed = _git_args(seg.eff)
            if not parsed or not parsed[0] or parsed[0][0] != "stash":
                continue
            args, chdir = parsed
            sub = args[1] if len(args) > 1 else ""
            if sub and not sub.startswith("-") and sub not in _STASH_WRITES:
                continue  # list / show: reads
            cwd = os.path.join(ctx.cwd, chdir) if chdir else ctx.cwd
            if _worktree_count(ctx, cwd) > 1:
                return True
    return False


_SHELL_SCRIPT = (".sh", ".bash")


def _is_shell_script(path: str) -> bool:
    if path.endswith(_SHELL_SCRIPT):
        return True
    if os.path.splitext(path)[1]:
        return False
    try:  # an extensionless REGULAR file: a bash/sh shebang makes it a shell script
        if not stat.S_ISREG(os.stat(path).st_mode):
            return False  # opening a FIFO blocks until a writer shows up
        with open(path, "rb") as fh:
            head = fh.readline(128)
    except OSError:
        return False
    return bool(re.match(rb"#!\s*\S*(?:bash|/sh|env\s+(?:bash|sh))\b", head))


def _script_targets(ctx: Ctx) -> list[str]:
    if ctx.tool in _EDIT_TOOLS:
        return [ctx.file_path] if ctx.file_path and _is_shell_script(ctx.file_path) else []
    targets: list[str] = []
    for pipe in ctx.top:
        for seg in pipe.segs:
            if seg.cmd == "sed" and any(
                t == "--in-place" or re.fullmatch(r"-[A-Za-z]*i[A-Za-z]*(?:\.\w+)?", t) for t in seg.eff[1:]
            ):
                targets += [t for t in seg.eff[1:] if t.endswith(_SHELL_SCRIPT)]
        for seg in pipe.segs:
            for m in re.finditer(r"(?:>>?|\btee(?:\s+-a)?)\s*(['\"]?)([^\s'\"|;&<>]+\.(?:sh|bash))(?![\w.-])\1", seg.raw):
                targets.append(m.group(2))
    return targets


def _live_runner(ctx: Ctx, path: str) -> int:
    """Pid of a live shell executing the script at *path* (by basename), or 0.

    ``pgrep -f`` finds candidates (read-only, ~20 ms); each is then confirmed
    with ``ps`` as a shell whose arguments name the script, or the script run
    directly, so a ``less run.sh`` or ``grep x run.sh`` does not count.
    """
    pgrep, ps = _which("pgrep"), _which("ps")
    if pgrep is None or ps is None:
        return 0
    base = os.path.basename(path)
    found = _spawn(ctx, [pgrep, "-f", "--", re.escape(base)])
    if found is None:
        return 0
    me = {os.getpid(), os.getppid()}
    for word in found.stdout.split():
        if not word.isdigit() or int(word) in me:
            continue
        shown = _spawn(ctx, [ps, "-ww", "-p", word, "-o", "args="])
        if shown is None:
            return 0  # the deadline is spent
        args = shown.stdout.split()
        if not args:
            continue
        head = os.path.basename(args[0])
        if head == base or (
            head in _SHELLS and any(os.path.basename(t) == base for t in args[1:] if not t.startswith("-"))
        ):
            return int(word)
    return 0


def _running_script_edit(ctx: Ctx) -> bool:
    for path in _script_targets(ctx):
        pid = _live_runner(ctx, path)
        if pid:
            ctx.note = f"{os.path.basename(path)} is being run by pid {pid}. "
            return True
    return False


_MASKERS = frozenset({"tail", "head", "grep", "wc", "tee"})


def _redirects_stdout(raw: str) -> bool:
    cleaned = re.sub(r"\d*>&[\d-]*", "", raw)
    cleaned = re.sub(r"(?<![0-9&])2>>?\s*\S+", "", cleaned)
    return ">" in cleaned


def _bg_pipe(ctx: Ctx) -> bool:
    """A background job whose last pipeline stage is tail/head/grep/wc/tee and whose own output is not redirected."""
    top = ctx.top
    cands = [p for p in top if p.term == "&" or (p.segs and "nohup" in p.segs[0].via)]
    if ctx.bg and top:
        cands.append(top[-1])
    for pipe in cands:
        if len(pipe.segs) < 2 or pipe.segs[-1].cmd not in _MASKERS:
            continue
        if any(_redirects_stdout(s.raw) for s in pipe.segs[:-1]):
            continue
        return True
    return False


_BROAD_ROOT = re.compile(
    r"^(?:/|~|\$HOME|\$\{HOME\}"
    r"|/(?:Users|home)(?:/[^/]+)?"
    r"|/Volumes(?:/[^/]+)?"
    r"|/Volumes/[^/]+/tmp"
    r"|/mnt/[^/]+/Users(?:/[^/]+)?"
    r"|(?:/private)?/var/folders)$"
)


def _is_broad_root(path: str) -> bool:
    p = path.replace("\\", "/")
    p = p.rstrip("/") or "/"
    if "nx.noindex" in p.split("/"):
        return True  # a test volume's run tree: 65k+ entries, walked once for 25 minutes
    return bool(_BROAD_ROOT.match(p)) or p == os.path.expanduser("~").rstrip("/")


def _find_parts(eff: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """``(roots, expression tokens)`` of a find segment."""
    i = 1
    while i < len(eff) and eff[i] in ("-H", "-L", "-P", "-E", "-X", "-d", "-s", "-x"):
        i += 1
    roots: list[str] = []
    while i < len(eff) and not (eff[i].startswith("-") or eff[i] in ("!", "(", "\\(")):
        roots.append(eff[i])
        i += 1
    return roots, list(eff[i:])


def _maxdepth(expr: list[str]) -> int | None:
    for j, t in enumerate(expr[:-1]):
        if t == "-maxdepth" and expr[j + 1].isdigit():
            return int(expr[j + 1])
    return None


def _whole_disk_scan(ctx: Ctx) -> bool:
    """grep -r / rg / mdfind / find rooted at /, ~, the home or volume roots (find: unless -maxdepth <= 2)."""
    for pipe in ctx.pipes:
        for seg in pipe.segs:
            c, args = seg.cmd, list(seg.eff[1:])
            if c in ("grep", "egrep", "fgrep", "rg"):
                recursive = c == "rg" or "--recursive" in args or "--dereference-recursive" in args or _flag_cluster(args, "rR")
                if recursive and any(_is_broad_root(a) for a in _positional(args)):
                    return True
            elif c == "mdfind":
                if "-onlyin" not in args:
                    return True
                j = args.index("-onlyin")
                if j + 1 < len(args) and _is_broad_root(args[j + 1]):
                    return True
            elif c == "find":
                roots, expr = _find_parts(seg.eff)
                depth = _maxdepth(expr)
                if any(_is_broad_root(r) for r in roots) and (depth is None or depth > 2):
                    return True
    return False


#: A home directory's ``tmp`` is shared the moment two agents are told to use ``~/tmp/<prefix>-*``
#: (the 2026-10-18 ``rm -rf ~/tmp/nx129-*`` took a sibling's scripts and logs with it).
_HOME_TMP = (
    r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/]+|/home/[^/]+|"
    + re.escape(os.path.expanduser("~").replace("\\", "/"))
    + r")/tmp"
)
_SHARED_ROOT = re.compile(
    r"^(?:(?:/private)?/var/folders/[^/]+/[^/]+/T|/private/tmp|/tmp|(?:/private)?/var/folders|/dev/shm"
    r"|\$\{?TMPDIR\}?|" + _HOME_TMP + r")(?:/(?P<rest>.*))?$"
)
_PYTEST_OF = re.compile(r"(?:^|/)pytest-of-[^/]+(?:/(?P<rest>.*))?$")
_GLOB = re.compile(r"[*?\[]")
_TIME_TESTS = ("-newer", "-mtime", "-mmin", "-ctime", "-cmin", "-atime", "-amin")


def _shared_rests(path: str) -> list[str]:
    """What follows a shared temp root (or a pytest-of-* basetemp) in *path*."""
    p = path.replace("\\", "/").rstrip("/")
    rests: list[str] = []
    m = _SHARED_ROOT.match(p)
    if m:
        rests.append(m.group("rest") or "")
    m = _PYTEST_OF.search(p)
    if m:
        rests.append(m.group("rest") or "")
    return rests


def _shared_glob_delete(ctx: Ctx) -> bool:
    """rm -r with a glob in the first component under a shared temp root; find there with -delete/-exec rm and a time test."""
    for pipe in ctx.pipes:
        for seg in pipe.segs:
            args = list(seg.eff[1:])
            if seg.cmd == "rm" and ("--recursive" in args or _flag_cluster(args, "rR")):
                for target in _positional(args):
                    if any(_GLOB.search(r.split("/")[0]) for r in _shared_rests(target)):
                        return True
            elif seg.cmd == "find":
                roots, expr = _find_parts(seg.eff)
                deletes = "-delete" in expr or any(
                    t in ("-exec", "-execdir") and expr[j + 1 : j + 2] == ["rm"] for j, t in enumerate(expr)
                )
                if not (deletes and any(t.startswith(_TIME_TESTS) for t in expr)):
                    continue
                for root in roots:
                    p = root.replace("\\", "/").rstrip("/")
                    m = _SHARED_ROOT.match(p)
                    if (m and not m.group("rest")) or _PYTEST_OF.search(p):
                        return True
    return False


_SESSION_START_CALL = re.compile(r"(?<!\w)session_start\s*\(|\bentry\.main\s*\(")
_HOOK_MODULE = re.compile(r"nexus\._hook_runtime|nexus\.hooks\.session_start")
_MAIN_CALL = re.compile(r"\b(?:main|session_start)\s*\(")
_REAL_HOME = re.compile(r"^(?:\$HOME|\$\{HOME\}|~)(?:/.*)?$")


def _scratch_assignment(command: str, name: str, real_marker: str) -> bool:
    for m in re.finditer(rf"(?:^|[\s;&|(]){name}=(\"[^\"]*\"|'[^']*'|\S+)", command):
        val = m.group(1).strip("\"'")
        if val and not _REAL_HOME.match(val) and val != os.path.expanduser("~") and real_marker not in val:
            return True
    return False


def _runs_hook_code(code: str) -> bool:
    """Python source that CALLS a hook entry: ``session_start(...)``, ``entry.main(...)``, or ``main(...)``
    out of an imported hook module. An import with no call runs no verb."""
    if _SESSION_START_CALL.search(code):
        return True
    return bool(_HOOK_MODULE.search(code) and _MAIN_CALL.search(code))


def _runs_hook_verb(seg: Seg, bodies: list[str]) -> bool:
    """This segment, by its own command word and arguments, runs a hook verb (never a mention of one)."""
    toks = seg.eff
    if not toks or any(os.path.basename(t) == "pytest" for t in toks):
        return False  # inside pytest's fence, or not a runner at all
    c = seg.cmd
    if c == "nx-hook":
        return True
    if c in {"uv", "uvx"}:
        rest = [t for t in toks[1:] if t != "run"]
        first = next((t for t in rest if not t.startswith("-")), "")
        if os.path.basename(first) == "nx-hook":
            return True
    if c in {"uv", "uvx"} or c.startswith("python"):
        if "-m" in toks[:-1] and toks[toks.index("-m") + 1].startswith("nexus._hook_runtime"):
            return True
        if "-c" in toks[:-1] and _runs_hook_code(toks[toks.index("-c") + 1]):
            return True
        if "<<" in seg.raw and any(_runs_hook_code(b) for b in bodies):
            return True
    return False


def _hook_verb_by_hand(ctx: Ctx) -> bool:
    """A real nx-hook / hook-entry / session_start() call outside pytest, without a scratch HOME and NEXUS_CONFIG_DIR."""
    pipes = ctx.pipes  # fills ctx.bodies
    if not any(_runs_hook_verb(s, ctx.bodies) for p in pipes for s in p.segs):
        return False
    scratch = _scratch_assignment(ctx.command, "HOME", ".config/nexus") and _scratch_assignment(
        ctx.command, "NEXUS_CONFIG_DIR", ".config/nexus"
    )
    return not scratch


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

_SRC = "feedback_destructive_action_guards"


class Row(NamedTuple):
    id: str
    source: str  # rule source: memory file section + census R-number(s)
    tools: frozenset[str]
    predicate: Callable[[Ctx], bool]
    advice: str
    hints: tuple[str, ...] | None = None  # cheap gate for Bash: one must occur in the command
    mode: str | None = None  # None = :data:`MODE`


ROWS: tuple[Row, ...] = (
    Row(
        "pattern_kill",
        f"{_SRC} / 'No pattern kills' (R32; R34 pattern forms)",
        _BASH,
        _pattern_kill,
        "pkill, killall and kill-by-pgrep end every process the pattern matches, on the laptop as on a shared "
        "host: a laptop `pkill -f` has been used to end an agent's own queued run, and it cannot tell that run "
        "from a peer's. A single SIGTERM to a pid picked out of `ps` by its command line has ended another "
        "session's lab-remote run. Signal only a pid you started yourself (`$!` or your own background task "
        "id), found by its parent pid, and check it with `ps -p <pid> -o pid,ppid,args` before `kill <pid>`. "
        "Never take a pid from ps or pgrep by its command line.",
        hints=("kill",),
    ),
    Row(
        "branch_bulk_delete",
        f"{_SRC} / 'feedback_never_glob_delete_branches' (R8, R10)",
        _BASH,
        _branch_delete,
        "Deleting branches by glob, from xargs or a command substitution, or with the output hidden can remove "
        "other sessions' branches with no record of the names (216 branches were lost once; a branch's reflog "
        "dies with it). Delete only branch names you can name as yours, one `git branch -D <name>` at a time, "
        "and keep the output visible.",
        hints=("branch",),
    ),
    Row(
        "stash_with_worktrees",
        f"{_SRC} / 'feedback_no_git_stash_in_worktrees' (R12)",
        _BASH,
        _stash,
        "This repository has more than one worktree, and `refs/stash` lives in the shared git dir, so "
        "`git stash pop` can apply a peer's entry. For a revert-and-retest control copy the file aside "
        "(`cp f /tmp/x; git checkout -- f; <test>; cp /tmp/x f`) or write the old version with "
        "`git show HEAD:f > f`.",
        hints=("stash",),
    ),
    Row(
        "bg_pipe_masks_exit",
        f"{_SRC} / 'feedback_never_edit_running_script' (R23)",
        _BASH,
        _bg_pipe,
        "A background job piped into tail, head, grep, wc or tee reports the last stage's exit status (0) and "
        "shows no progress until it ends. Redirect the job to a log and keep its status: "
        "`cmd > run.log 2>&1; ec=$?; ...; exit $ec`, then tail the log.",
        hints=("|",),
    ),
    Row(
        "whole_disk_scan",
        f"{_SRC} / 'feedback_no_full_disk_scans' (R14)",
        _BASH,
        _whole_disk_scan,
        "A recursive search rooted at /, the home directory, /Users, /Volumes or a Windows profile walks "
        "hundreds of thousands of files (one such grep ran 25 minutes). Scope the scan to the directories the "
        "work wrote to, with -maxdepth on shared roots, and prefer `find <dir> -maxdepth N -name ...` to a "
        "content grep.",
        hints=("grep", "rg", "mdfind", "find"),
    ),
    Row(
        "shared_glob_delete",
        f"{_SRC} / 'feedback_never_glob_delete_shared_state' (R1)",
        _BASH,
        _shared_glob_delete,
        "Deleting by glob or by modification time under a shared temp root removes other sessions' live "
        "directories (pytest basetemps, test databases). Delete only paths your own commands created, by exact "
        "name, inside a private directory of your own (a TMPDIR you made), and check what is alive first "
        "(`ps`, `ipcs -m`).",
        hints=("rm", "find"),
    ),
    Row(
        "hook_verb_by_hand",
        f"{_SRC} / 'feedback_never_call_real_session_start_by_hand' (R26)",
        _BASH,
        _hook_verb_by_hand,
        "Running a real hook verb by hand (nx-hook, the hook entry's main, session_start) writes the live "
        "session marker for the calling Claude pid and runs the verb's side effects. Measure or trace inside "
        "pytest, through a renderer that writes nothing, or with HOME and NEXUS_CONFIG_DIR both pointed at a "
        "scratch directory.",
        hints=("_hook_runtime", "session_start", "entry.main", "nx-hook"),
    ),
    Row(
        "running_script_edit",
        f"{_SRC} / 'feedback_never_edit_running_script' (R22)",
        _BASH | _EDIT_TOOLS,
        _running_script_edit,
        "A live shell is executing this script. bash reads a script by byte offset, so editing it in place "
        "corrupts the run. Wait for the process to exit, or write the new version to another file and move it "
        "into place afterwards.",
        hints=(".sh", ".bash"),
    ),
)

# ---------------------------------------------------------------------------
# Decision and emission
# ---------------------------------------------------------------------------


class Hit(NamedTuple):
    row: Row
    note: str


def decide(payload: dict) -> list[Hit]:
    """The rows *payload* matches. Never raises: a broken predicate is no match."""
    ctx = Ctx(payload)
    hits: list[Hit] = []
    subject = ctx.subject
    for row in ROWS:
        if ctx.tool not in row.tools:
            continue
        if ctx.tool == "Bash" and row.hints is not None and not any(h in subject for h in row.hints):
            continue
        ctx.note = ""
        try:
            matched = row.predicate(ctx)
        except Exception:  # noqa: BLE001 -- fail open
            matched = False
        if matched:
            hits.append(Hit(row, ctx.note))
    return hits


def row_mode(row: Row, mode: str | None = None) -> str:
    """The mode *row* runs in: its own, else *mode*, else :data:`MODE`; anything unknown is advise."""
    m = row.mode or mode or MODE
    return m if m in ("advise", "deny") else "advise"


def render(hits: list[Hit]) -> str:
    return "\n\n".join(f"destructive-action advice [{h.row.id}]: {h.note}{h.row.advice}" for h in hits)


def emit(_lib, payload: dict, hits: list[Hit], mode: str | None = None) -> None:
    """Log each hit, then advise (never deny in advise mode) or deny."""
    ctx = Ctx(payload)
    escaped = bool(ctx.command) and _lib.should_skip_for_reason(ctx.command)
    denying = [h for h in hits if row_mode(h.row, mode) == "deny"]
    for h in hits:
        if h in denying:
            outcome = "escape" if escaped else "deny"
        else:
            outcome = f"advise_{h.row.id}"
        _lib.log_routing_event(
            rule=RULE_NAME, outcome=outcome, tool_name=ctx.tool, command_fragment=ctx.subject,
            escape_reason=_lib.extract_escape_reason(ctx.command) if escaped and h in denying else "",
        )
    text = render(hits)
    if denying and not escaped:
        _lib.deny(
            text + " (An escape, `# routing-allow: <reason>` of 8+ characters, exists for this guard, "
            "but only on the user's explicit instruction.)",
            summary=f"destructive-action guard: {', '.join(h.row.id for h in denying)}",
        )
    if denying and escaped and len(denying) == len(hits):
        _lib.pass_through()
    _lib.warn(text)


def main(mode: str | None = None) -> None:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            return
        hits = decide(payload)
    except Exception:  # noqa: BLE001 -- fail open, silently
        return
    if not hits:
        return  # fast path: no _lib import, no output (no decision)
    sys.path.insert(0, _HERE)
    import _lib  # noqa: E402,PLC0415 -- only a matched call pays for the framework

    sys.stdin = io.StringIO(raw)
    def body(p: dict) -> None:
        emit(_lib, p, hits, mode)

    _lib.run_hook(body, fail_closed=False, rule_name=RULE_NAME)


if __name__ == "__main__":
    main()
