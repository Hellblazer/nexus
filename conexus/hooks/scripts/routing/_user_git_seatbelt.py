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
``exec``, ``time``, ``nohup``, ``sudo``, ``nice`` and ``timeout``. A ``$(...)`` or backtick span is
one word to the command around it (so a flag after it is still that command's flag), and its body is
read the same way, recursively. The body of a heredoc whose delimiter is quoted (``<<'EOF'``) is text,
backticks included; the body of an unquoted one expands, so a substitution in it is still read. Quoted text and arguments to other programs are
never in command position, so ``echo "git reset --hard"`` and
``grep -rn 'git add' .`` pass. Global options ``-C <path>``, ``-c k=v``,
``--git-dir``, ``--work-tree`` and the like are skipped to reach the subcommand;
``-C`` also names the target checkout.

Accepted gaps (this is a seatbelt against accidents, not a boundary):
``bash -c '...'``, ``sh -c`` and ``eval`` strings, ``xargs git``, ``find -exec
git``, a heredoc fed to a shell, aliases, shell functions, scripts on disk, any
wrapper not named above, a long option written as an abbreviation (``--forc``
for ``--force``, which git accepts), a variable or ANSI-quoted word standing for
the command or a flag, text shlex cannot tokenise (it fails open), and a ``-C``
path written with Windows backslashes (read as shell escapes, so the checkout is
unreadable and a git-state row passes). A ``-C`` path that cannot be resolved
here (``$PWD``, ``~``, a substitution) reads the checkout the cwd names. Tests
pin the first four and the variable and ANSI forms.

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
_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "sudo", "nice", "timeout"})
#: Per wrapper, the options that take a value as the next word (``sudo -n`` is a flag, ``nice -n`` is not).
_WRAPPER_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "env": frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-r", "-t", "-U", "-D", "-R", "-T"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-s", "--signal", "-k", "--kill-after"}),
}
#: A command or backtick substitution, blanked to this word so it cannot split the command around it.
_SUBST = "NXSUBST"
#: ``<<-? ws* WORD`` (``'WORD'``, ``"WORD"`` and ``\WORD`` are the quoted forms: their body is text).
_HEREDOC_MARK_RE = re.compile(r"<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\([A-Za-z0-9_]+)|([A-Za-z0-9_.\-]+))")
#: What the word in command position is called when it is git (read, never spawned).
_GIT_NAMES = frozenset({"git", "git.exe"})
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


class _Heredoc(NamedTuple):
    delim: str
    strip_tabs: bool
    quoted: bool


def _heredoc_at(text: str, i: int) -> "tuple[_Heredoc, int] | None":
    """The heredoc whose ``<<WORD`` starts at *i*, and the index after the marker; None for ``<<<`` or no word."""
    if not text.startswith("<<", i) or text.startswith("<<<", i) or (i > 0 and text[i - 1] == "<"):
        return None
    m = _HEREDOC_MARK_RE.match(text, i)
    if m is None:
        return None
    quoted = m.group(2) is not None or m.group(3) is not None or m.group(4) is not None
    delim = m.group(2) or m.group(3) or m.group(4) or m.group(5)
    return _Heredoc(delim, m.group(1) == "-", quoted), m.end()


def _heredoc_body_end(text: str, pos: int, hd: _Heredoc) -> "tuple[int, int] | None":
    """``(start, end)`` of the terminator line at or after *pos*, or None when there is none."""
    n = len(text)
    while pos <= n:
        nl = text.find("\n", pos)
        end = n if nl < 0 else nl
        line = text[pos:end].rstrip("\r")
        if (line.lstrip("\t") if hd.strip_tabs else line) == hd.delim:
            return pos, end
        if nl < 0:
            return None
        pos = nl + 1
    return None


def _end_backtick(text: str, i: int) -> int:
    """Index of the backtick that closes the one at *i*, or the last index when it never closes."""
    j = i + 1
    while j < len(text) and text[j] != "`":
        j += 2 if text[j] == "\\" else 1
    return min(j, len(text) - 1)


def _end_of_substitution(text: str, j: int) -> "tuple[int, bool]":
    """``(index after the ``)`` that closes the ``$(`` whose body starts at *j*, whether it closed)``.

    Reads the body the way the shell does for balance: quotes hide parentheses, a nested ``$(`` inside
    double quotes is its own span, a comment runs to its newline, and the body of a heredoc started on a
    line is skipped from that line's end to its terminator, so ``fix (partly`` in a message does not
    leave the span open. An unterminated span runs to the end of the text.
    """
    n = len(text)
    stack = ["plain"]  # per open paren: the quote state to return to when it closes
    state = "plain"
    pending: list[_Heredoc] = []
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if state == "sq":
            if ch == "'":
                state = "plain"
        elif state == "dq":
            if ch == '"':
                state = "plain"
            elif ch == "$" and text[j + 1:j + 2] == "(":
                stack.append("dq")
                state = "plain"
                j += 1
            elif ch == "`":
                j = _end_backtick(text, j)
        elif ch == "'":
            state = "sq"
        elif ch == '"':
            state = "dq"
        elif ch == "`":
            j = _end_backtick(text, j)
        elif ch == "$" and text[j + 1:j + 2] == "(":
            stack.append("plain")
            j += 1
        elif ch == "(":
            stack.append("plain")
        elif ch == ")":
            state = stack.pop()
            if not stack:
                return j + 1, True
        elif ch == "#" and text[j - 1] in " \t\n;&|(":
            nl = text.find("\n", j)
            j = n if nl < 0 else nl - 1
        elif ch == "<":
            found = _heredoc_at(text, j)
            if found is not None:
                pending.append(found[0])
                j = found[1] - 1
        elif ch == "\n" and pending:
            pos, last = j + 1, None
            for hd in pending:
                res = _heredoc_body_end(text, pos, hd)
                if res is None:
                    break
                last, pos = res[1], res[1] + 1
            pending = []
            if last is not None:
                j = last
        j += 1
    return n, False


def _substitutions_in_text(body: str) -> list[str]:
    """The ``$(...)`` and backtick bodies in text that expands but has no quoting (an unquoted heredoc body)."""
    found: list[str] = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\":
            i += 2
        elif c == "$" and body[i + 1:i + 2] == "(":
            end, closed = _end_of_substitution(body, i + 2)
            found.append(body[i + 2:end - 1 if closed else end])
            i = end
        elif c == "`":
            j = _end_backtick(body, i)
            found.append(body[i + 1:j])
            i = j + 1
        else:
            i += 1
    return found


def _split_substitutions(text: str) -> "tuple[str, list[str]]":
    """``(text with each top-level $(...) / backtick span blanked to one word, the span bodies)``.

    One pass over the quote state (an apostrophe inside ``"..."`` is not a single quote), comments (a
    ``#`` word runs to its newline and is not code) and heredocs: the body of a heredoc is dropped from the
    returned text, and an unquoted one still contributes the substitutions in it (they run); a quoted one
    (``<<'EOF'``, found only outside quotes) is text and contributes nothing. A nested span is part of its
    parent's body and is found when that body is read in turn, so the cost stays linear in the depth cap.
    """
    out: list[str] = []
    bodies: list[str] = []
    i, n = 0, len(text)
    state = "plain"
    pending: list[_Heredoc] = []
    while i < n:
        c = text[i]
        if c == "\\":
            out.append(text[i:i + 2])
            i += 2
        elif state == "sq":
            out.append(c)
            if c == "'":
                state = "plain"
            i += 1
        elif c == "$" and text[i + 1:i + 2] == "(":
            end, closed = _end_of_substitution(text, i + 2)
            bodies.append(text[i + 2:end - 1 if closed else end])
            out.append(_SUBST)
            i = end
        elif c == "`":
            j = _end_backtick(text, i)
            bodies.append(text[i + 1:j])
            out.append(_SUBST)
            i = j + 1
        elif state == "dq":
            out.append(c)
            if c == '"':
                state = "plain"
            i += 1
        elif c == "'":
            state = "sq"
            out.append(c)
            i += 1
        elif c == '"':
            state = "dq"
            out.append(c)
            i += 1
        elif c == "#" and (i == 0 or text[i - 1] in " \t\n;&|("):
            nl = text.find("\n", i)
            i = n if nl < 0 else nl
        elif c == "<" and (found := _heredoc_at(text, i)) is not None:
            pending.append(found[0])
            out.append(text[i:found[1]])
            i = found[1]
        elif c == "\n" and pending:
            pos, terms, last = i + 1, [], None
            for hd in pending:
                res = _heredoc_body_end(text, pos, hd)
                if res is None:
                    break
                if not hd.quoted:
                    bodies.extend(_substitutions_in_text(text[pos:res[0]]))
                terms.append(text[res[0]:res[1]])
                last, pos = res[1], res[1] + 1
            pending = []
            if last is None:
                out.append(c)
                i += 1
            else:
                out.append("\n" + "\n".join(terms))
                i = last
        else:
            out.append(c)
            i += 1
    return "".join(out), bodies


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
            value_flags = _WRAPPER_VALUE_FLAGS.get(base, frozenset())
            while i < len(words) and (words[i].startswith("-") or _ASSIGN_RE.match(words[i])):
                i += 2 if words[i] in value_flags else 1
            if base == "timeout":
                i += 1  # the duration
        else:
            return words[i + 1:] if base in _GIT_NAMES else None
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


def _without_quiet(args: list[str]) -> list[str]:
    """*args* with ``-q`` / ``--quiet`` removed: a quiet dry run lists nothing, so it proves nothing."""
    out: list[str] = []
    for i, a in enumerate(args):
        if a == "--":
            out.extend(args[i:])
            break
        if a == "--quiet":
            continue
        if a.startswith("-") and not a.startswith("--") and "q" in a:
            a = "-" + a[1:].replace("q", "")
            if a == "-":
                continue
        out.append(a)
    return out


def _homes() -> list[str]:
    homes = [os.path.expanduser("~")]
    try:
        import pwd  # noqa: PLC0415 -- POSIX only; the ImportError arm is the non-POSIX branch

        homes.append(pwd.getpwuid(os.getuid()).pw_dir)
    except Exception:  # noqa: BLE001 -- no passwd entry or not POSIX
        pass
    return [h for h in homes if h and h != "~"]


def _is_hostwide_config_file(path: str) -> bool:
    """True when *path* names the account's ``~/.gitconfig`` or ``~/.config/git/config``, or ``/etc/gitconfig``."""
    p = path.strip()
    for prefix in ("~/", "$HOME/", "${HOME}/"):
        if p.startswith(prefix):
            return p[len(prefix):] in (".gitconfig", ".config/git/config")
    if p.replace("\\", "/").lower() == "/etc/gitconfig":
        return True
    norm = os.path.normcase(os.path.normpath(p))
    return any(
        norm == os.path.normcase(os.path.join(home, *rel))
        for home in _homes()
        for rel in ((".gitconfig",), (".config", "git", "config"))
    )


def _config_file_args(args: list[str]) -> list[str]:
    files: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-f", "--file") and i + 1 < len(args):
            files.append(args[i + 1])
            i += 1
        elif a.startswith("--file="):
            files.append(a[len("--file="):])
        i += 1
    return files


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
    if any(_is_hostwide_config_file(f) for f in _config_file_args(args)):
        return Hit("config --file <host-wide file>", "text")
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
            return Hit("clean", "state", clean_args=tuple(_without_quiet(args)))
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
            [git, "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *args],
            cwd=target, capture_output=True, text=True,
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
        if _SUBST in p or "$" in p or "`" in p or p.startswith("~"):
            continue  # cannot be resolved here: read the checkout the cwd names
        target = os.path.normpath(p if os.path.isabs(p) else os.path.join(target, p))
    return target if os.path.isdir(target) else None


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def _scan(text: str, cwd: "str | None", depth: int) -> "Hit | None":
    if depth > _MAX_DEPTH:
        return None
    blanked, bodies = _split_substitutions(text)
    for body in bodies:
        hit = _scan(body, cwd, depth + 1)
        if hit is not None:
            return hit
    tokens = _tokens(_preprocess(blanked))
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
