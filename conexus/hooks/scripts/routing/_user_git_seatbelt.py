# SPDX-License-Identifier: AGPL-3.0-or-later
"""The git guard's user mode: a small seatbelt for a repository that is not nexus.

``subagent_git_write_requires_orchestrator`` keeps its full structure-agnostic
rule for the maintainer (``_audience.maintainer``). For anyone else this module
denies only the forms a user would never want an agent to run unasked: they
destroy history or work that cannot be recovered. Commit, add, plain push,
pull, fetch, rebase, merge, cherry-pick, branch create, tag, ``reset --soft``
and ``--mixed`` and the like are not touched.

Two kinds of deny:

``text``
    Decided from the command alone, in any cwd, in any repo or none, and when a
    git call times out: force and destructive pushes, ``branch -D``/``-M``,
    reflog expire and delete, ``gc --prune``, ``git prune``, ``update-ref -d``,
    ``filter-branch``/``filter-repo``, ``stash drop``/``clear``,
    ``worktree remove --force``, and config writes of ``core.hooksPath``,
    ``core.fsmonitor``, ``core.sshCommand``, ``include.*``, ``includeIf.*`` or
    any ``--global``/``--system`` write.
``state``
    Denied only when the target checkout has something to lose, and allowed
    when the state cannot be read (no cwd, not a repo, a spawn that fails or
    times out): ``checkout`` with a pathspec or ``-f``, ``switch -f``/
    ``--discard-changes``, ``restore`` of the working tree, ``reset --hard``/
    ``--merge``/``--keep``, ``rm -f``, ``stash`` (push), and ``clean`` whose own
    dry run lists anything.

How a command is read: the raw text is tokenised with ``shlex`` in
``punctuation_chars`` mode, after unquoted newlines become ``;`` and heredoc
bodies and comments are dropped. A ``git`` word counts when it sits in command
position: at the start, after ``; & | ( ) {``, after ``then do else elif if
while until !``, after ``VAR=x`` assignments and after ``env``, ``command``,
``exec``, ``time``, ``nohup`` and ``sudo``. ``$(...)`` and backtick bodies are
read the same way, recursively. Quoted text and arguments to other programs are
never in command position, so ``echo "git reset --hard"`` and
``grep -rn 'git add' .`` pass. Global options ``-C <path>``, ``-c k=v``,
``--git-dir``, ``--work-tree`` and the like are skipped to reach the subcommand;
``-C`` also names the target checkout.

Accepted gaps (this is a seatbelt against accidents, not a boundary):
``bash -c '...'``, ``sh -c`` and ``eval`` strings, ``xargs git``, ``find -exec
git``, a heredoc fed to a shell, aliases, shell functions, scripts on disk, and
text shlex cannot tokenise (it fails open). Tests pin the first four.

Stdlib only.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from typing import NamedTuple

_MAX_DEPTH = 5
_GIT_TIMEOUT = 5

_SEPARATOR_CHARS = frozenset(";&|()")
_LEADING_WORDS = frozenset({"then", "do", "else", "elif", "!", "{", "}", "if", "while", "until", "time"})
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "sudo", "nice"})
#: Wrapper options that take a value as the next word.
_WRAPPER_VALUE_FLAGS = frozenset({"-u", "-n", "-C", "-g", "-a", "-S", "-p", "-U", "-h", "-r", "-t"})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Git global options that take a value as the next word (``--x=v`` forms need no skip).
_GIT_VALUE_GLOBALS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--super-prefix", "--config-env"})

_DANGEROUS_CONFIG_KEYS = frozenset({"core.hookspath", "core.fsmonitor", "core.sshcommand"})
_CONFIG_READ_FLAGS = frozenset({
    "--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l",
    "--get-color", "--get-colorbool",
})
_CONFIG_WRITE_FLAGS = frozenset({
    "--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section",
    "--edit", "-e",
})
_CONFIG_VALUE_FLAGS = frozenset({"-f", "--file", "--blob", "--default", "-t", "--type", "--comment"})
_STASH_SAFE = frozenset({"pop", "apply", "list", "show", "branch", "create", "store"})


class Hit(NamedTuple):
    """One denied form. *kind* is ``"text"`` or ``"state"``."""

    label: str
    kind: str
    untracked: bool = False
    clean_args: "tuple[str, ...] | None" = None


# --------------------------------------------------------------------------
# Reading the command
# --------------------------------------------------------------------------


def _preprocess(text: str) -> str:
    """Unquoted newlines to ``;``; heredoc bodies, comments and continuations dropped."""
    out: list[str] = []
    pending: list[tuple[str, bool]] = []
    i, n = 0, len(text)
    sq = dq = False
    while i < n:
        c = text[i]
        if sq:
            out.append(c)
            sq = c != "'"
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if text[i + 1] == "\n":
                i += 2
                continue
            if text[i + 1] == "\r" and text[i + 2:i + 3] == "\n":
                i += 3
                continue
            out.append(c + text[i + 1])
            i += 2
            continue
        if dq:
            out.append(c)
            dq = c != '"'
            i += 1
            continue
        if c == "'":
            sq = True
        elif c == '"':
            dq = True
        elif c == "#" and (not out or out[-1] in " \t;&|(\n"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif c == "<" and text.startswith("<<", i) and not text.startswith("<<<", i):
            m = re.match(r"<<(-?)[ \t]*(['\"]?)([A-Za-z0-9_.\-]+)\2", text[i:])
            if m:
                pending.append((m.group(3), m.group(1) == "-"))
                out.append(m.group(0))
                i += m.end()
                continue
        elif c == "\n":
            i += 1
            while pending:
                delim, strip_tabs = pending.pop(0)
                while i < n:
                    j = text.find("\n", i)
                    line = text[i:] if j < 0 else text[i:j]
                    i = n if j < 0 else j + 1
                    line = line.rstrip("\r")
                    if (line.lstrip("\t") if strip_tabs else line) == delim:
                        break
            out.append(";")
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _tokens(text: str) -> "list[str] | None":
    """shlex tokens of one preprocessed text, or None when it cannot be tokenised."""
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        return None


def _substitutions(text: str) -> list[str]:
    """Bodies of ``$(...)`` and backtick substitutions (also inside double quotes)."""
    found: list[str] = []
    i, n = 0, len(text)
    sq = False
    while i < n:
        c = text[i]
        if sq:
            sq = c != "'"
        elif c == "\\":
            i += 1
        elif c == "'":
            sq = True
        elif c == "$" and text[i + 1:i + 2] == "(":
            depth, j = 1, i + 2
            while j < n and depth:
                if text[j] == "\\":
                    j += 1
                elif text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                j += 1
            found.append(text[i + 2:j - 1] if depth == 0 else text[i + 2:])
        elif c == "`":
            j = i + 1
            while j < n and text[j] != "`":
                j += 2 if text[j] == "\\" else 1
            found.append(text[i + 1:j])
            i = j
        i += 1
    return found


def _split_commands(tokens: list[str]) -> list[list[str]]:
    commands: list[list[str]] = [[]]
    for tok in tokens:
        if tok and set(tok) <= _SEPARATOR_CHARS:
            commands.append([])
        else:
            commands[-1].append(tok)
    return [c for c in commands if c]


def _git_invocation(words: list[str]) -> "list[str] | None":
    """The words after ``git`` when *words* is a git command, else None."""
    i = 0
    while i < len(words):
        w = words[i]
        base = os.path.basename(w.replace("\\", "/")).lower()
        if w in _LEADING_WORDS or _ASSIGN_RE.match(w):
            i += 1
        elif base in _WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or _ASSIGN_RE.match(words[i])):
                i += 2 if words[i] in _WRAPPER_VALUE_FLAGS else 1
        else:
            return words[i + 1:] if base in ("git", "git.exe") else None
    return None


def _split_globals(args: list[str]) -> "tuple[list[str], str | None, list[str]]":
    """``(globals' -C paths, subcommand, args after it)``; subcommand None when absent."""
    dash_c: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GIT_VALUE_GLOBALS:
            if a == "-C" and i + 1 < len(args):
                dash_c.append(args[i + 1])
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return dash_c, a, args[i + 1:]
    return dash_c, None, []


# --------------------------------------------------------------------------
# The forms
# --------------------------------------------------------------------------


def _parts(args: list[str], value_flags: frozenset[str] = frozenset()) -> "tuple[list[str], list[str], list[str]]":
    """``(flags, positionals, words after --)``."""
    flags: list[str] = []
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return flags, pos, args[i + 1:]
        if a.startswith("-") and a != "-":
            flags.append(a)
            if a in value_flags:
                i += 1
        else:
            pos.append(a)
        i += 1
    return flags, pos, []


def _short(flags: list[str], letters: str) -> bool:
    """True when a single-dash cluster contains any of *letters*."""
    return any(f[1:2] != "-" and any(ch in f[1:] for ch in letters) for f in flags)


def _long(flags: list[str], *names: str) -> bool:
    return any(f.split("=", 1)[0] in names for f in flags)


def _long_prefix(flags: list[str], prefix: str) -> bool:
    return any(f.startswith(prefix) for f in flags)


def _config_hit(args: list[str]) -> "Hit | None":
    flags, pos, _ = _parts(args, _CONFIG_VALUE_FLAGS)
    if pos and pos[0] in ("get", "list"):
        return None
    if pos and pos[0] in ("set", "unset", "rename-section", "remove-section", "edit"):
        write, key = True, (pos[1] if len(pos) > 1 else "")
    elif any(f in _CONFIG_READ_FLAGS for f in flags):
        return None
    elif any(f in _CONFIG_WRITE_FLAGS for f in flags):
        write, key = True, (pos[0] if pos else "")
    else:
        write, key = len(pos) >= 2, (pos[0] if pos else "")
    if not write:
        return None
    if _long(flags, "--global", "--system"):
        return Hit("config --global/--system", "text")
    k = key.lower()
    if k in _DANGEROUS_CONFIG_KEYS or k.startswith(("include.", "includeif.")):
        return Hit(f"config {key}", "text")
    return None


def _checkout_hit(args: list[str], base: "str | None") -> "Hit | None":
    flags, pos, after = _parts(args)
    if _short(flags, "f") or _long(flags, "--force"):
        return Hit("checkout -f", "state")
    if after or (pos and pos[0] == "."):
        return Hit("checkout of a path", "state")
    creating = _short(flags, "bB") or _long(flags, "--orphan", "--track", "--detach", "--no-track")
    names = pos[1:] if len(pos) > 1 else pos
    if base and not creating and any(os.path.lexists(os.path.join(base, p)) for p in names):
        return Hit("checkout of a path", "state")
    return None


def _form(sub: str, args: list[str], base: "str | None") -> "Hit | None":
    """The D1 form *sub* + *args* is, or None when it is allowed."""
    flags, pos, after = _parts(args)
    first = pos[0] if pos else ""
    if sub == "push":
        refs = pos + after
        if (
            _short(flags, "fd")
            or _long_prefix(flags, "--force")
            or _long(flags, "--delete", "--mirror", "--prune")
            or any(r.startswith(("+", ":")) for r in refs)
        ):
            return Hit("push --force/--delete/--mirror", "text")
    elif sub == "branch":
        force = _short(flags, "f") or _long(flags, "--force")
        if (
            _short(flags, "DM")
            or (_short(flags, "dm") and force)
            or (_long(flags, "--delete", "--move") and force)
        ):
            return Hit("branch -D/-M", "text")
    elif sub == "reflog":
        if first in ("expire", "delete"):
            return Hit(f"reflog {first}", "text")
    elif sub == "gc":
        if _long_prefix(flags, "--prune"):
            return Hit("gc --prune", "text")
    elif sub == "prune":
        return Hit("prune", "text")
    elif sub == "update-ref":
        if _short(flags, "d") or _long(flags, "--delete"):
            return Hit("update-ref -d", "text")
    elif sub in ("filter-branch", "filter-repo"):
        return Hit(sub, "text")
    elif sub == "stash":
        if first in ("drop", "clear"):
            return Hit(f"stash {first}", "text")
        if first not in _STASH_SAFE:
            return Hit("stash", "state", untracked=_short(flags, "ua") or _long(flags, "--include-untracked", "--all"))
    elif sub == "worktree":
        if first == "remove" and (_short(flags, "f") or _long(flags, "--force")):
            return Hit("worktree remove --force", "text")
    elif sub == "config":
        return _config_hit(args)
    elif sub == "checkout":
        return _checkout_hit(args, base)
    elif sub == "switch":
        if _short(flags, "f") or _long(flags, "--force", "--discard-changes"):
            return Hit("switch -f", "state")
    elif sub == "restore":
        staged = _short(flags, "S") or _long(flags, "--staged")
        worktree = _short(flags, "W") or _long(flags, "--worktree")
        if not staged or worktree:
            return Hit("restore", "state")
    elif sub == "reset":
        if _long(flags, "--hard", "--merge", "--keep"):
            return Hit("reset --hard", "state")
    elif sub == "rm":
        if _short(flags, "f") or _long(flags, "--force"):
            return Hit("rm -f", "state")
    elif sub == "clean":
        if not (_short(flags, "n") or _long(flags, "--dry-run")):
            return Hit("clean", "state", clean_args=tuple(args))
    return None


# --------------------------------------------------------------------------
# Git state
# --------------------------------------------------------------------------


def _run_git(target: str, *args: str) -> "subprocess.CompletedProcess[str] | None":
    """Run git in *target*; None on any failure (missing git, timeout, not a repo)."""
    try:
        import _exec_path  # noqa: PLC0415 -- parent dir is on sys.path for the hook

        git = _exec_path.which_off_cwd("git")
        if git is None:
            return None
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        done = subprocess.run(
            [git, *args], cwd=target, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=_GIT_TIMEOUT, env=env,
        )
    except Exception:  # noqa: BLE001 -- unreadable state passes a state form
        return None
    return done if done.returncode == 0 else None


def _has_something_to_lose(hit: Hit, target: str) -> bool:
    """True only when git proves the checkout has something to lose."""
    if hit.clean_args is not None:
        done = _run_git(target, "clean", "-n", *hit.clean_args)
    else:
        done = _run_git(target, "status", "--porcelain", "-uall" if hit.untracked else "-uno")
    return bool(done and done.stdout.strip())


def _target_dir(cwd: "str | None", dash_c: list[str]) -> "str | None":
    if not cwd or not os.path.isabs(cwd):
        return None
    target = cwd
    for p in dash_c:
        target = os.path.normpath(p if os.path.isabs(p) else os.path.join(target, p))
    return target if os.path.isdir(target) else None


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _scan(text: str, cwd: "str | None", depth: int) -> "Hit | None":
    if depth > _MAX_DEPTH:
        return None
    for body in _substitutions(text):
        hit = _scan(body, cwd, depth + 1)
        if hit is not None:
            return hit
    tokens = _tokens(_preprocess(text))
    if tokens is None:
        return None
    for words in _split_commands(tokens):
        args = _git_invocation(words)
        if args is None:
            continue
        dash_c, sub, rest = _split_globals(args)
        if sub is None:
            continue
        target = _target_dir(cwd, dash_c)
        hit = _form(sub, rest, target)
        if hit is None:
            continue
        if hit.kind == "text":
            return hit
        if target is not None and _has_something_to_lose(hit, target):
            return hit
    return None


def decide(command: str, cwd: "str | None") -> "Hit | None":
    """The denied form *command* contains for a user's repository, or None."""
    return _scan(command, cwd, 0)


def deny_text(hit: Hit) -> str:
    """The short deny: what was stopped, why, and what stays allowed."""
    return (
        f"git {hit.label} was stopped: it can destroy history or uncommitted work that "
        "cannot be recovered. If you mean it, ask the user to run it. Commit, add, push, "
        "pull, fetch, rebase, merge, branch and tag operations, reset --soft and --mixed "
        "and read-only commands are not blocked."
    )
