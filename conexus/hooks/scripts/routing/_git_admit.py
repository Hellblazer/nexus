# SPDX-License-Identifier: AGPL-3.0-or-later
"""Segment-level admission for the subagent git-write guard (nexus-5y47c).

``subagent_git_write_requires_orchestrator`` denies a subagent's Bash call when a
standalone ``git`` token is followed anywhere by a write-verb word. That rule is
structure-agnostic on purpose, and it over-matched: 97% of 2,199 denials in three
weeks were not the harm it exists for (``git merge-base A $m``, ``git stash list |
head``, ``grep push scripts/git-push-develop.sh``, ``cd <worktree> && git add``,
``git fetch``, a scratch ``git init``). Sam ruled six fixes on 2026-11-25; this
module is the admission half of them. It only ever ADDS a pass; a command it does not admit
reaches the unchanged guard, which decides as before.

How it stays safe. ``analyze`` first reads the command with a STRICT lexer: a
command that holds anything the lexer does not understand (a substitution, a
heredoc, a backslash, a brace or subshell, a redirect to a file, a comment, a
non-ASCII word outside quotes) is not admitted, so the segments the lexer returns
are exactly the commands the shell runs, and each unquoted word is what the shell
sees. Then every segment must be one of:

* a git command in an allowlisted READ-ONLY shape (``status``, ``log``, ``stash
  list``, ``merge-base``, ``branch --list``...): its arguments may spell verb
  words, since git does not run them;
* a non-git command from a short list that cannot run its arguments (``grep``,
  ``cat``, ``echo``, ``ls``...): its arguments may spell verb words too;
* ``git fetch`` without a refspec destination, ``--prune`` or ``-f``;
* an allowlisted write shape aimed at a linked worktree git proves (``git -C
  <root> commit ...``, ``cd <root> && git add ...``, an own-branch ``push``), at
  a detached worktree created outside every repository, or at a scratch
  repository the same command creates under a temporary directory;
* anything else, which must pass the UNCHANGED guard on its own text.

Text that can reach an interpreter is never exempt: a producer of literal text
(``echo``, ``printf``) or a segment whose text the guard would deny taints the
rest of its pipeline, and a segment of the last kind after the taint is refused.
A variable bound earlier in the command is read back into later segments before
they are scanned, so ``X='git checkout f'; sh -c "$X"`` denies.

Fail mode: every refusal path returns ``None`` and the caller falls through. The
module holds no state and never touches the filesystem except through the
callables it is given (``Hooks``) and the two pure path checks below.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any, Callable, NamedTuple

MAX_COMMAND = 20000
MAX_SEGMENTS = 120
MAX_WORD = 4096
_MAX_SUBST = 8192

#: Per verb, EVERY flag the worktree write shapes admit, mapped to whether the flag
#: takes a value. An allowlist: git accepts any unambiguous prefix of a long option,
#: so a refusal list leaks (review round 1 of the ``git -C`` carve-out, 2026-11-19).
#: Nothing admitted runs a command, discards uncommitted work or moves state other
#: worktrees share. nexus-5y47c adds only long spellings of flags already admitted
#: (``--quiet``, ``--verbose``, ``--dry-run``), ``merge --no-commit`` and ``-F``, ``commit
#: --dry-run`` and ``cherry-pick --no-edit``.
DASH_C_FLAGS: dict[str, dict[str, bool]] = {
    "add": {"-A": False, "--all": False, "-u": False, "--update": False,
            "-N": False, "--intent-to-add": False, "-v": False, "--verbose": False,
            "-n": False, "--dry-run": False, "--": False},
    "commit": {"-m": True, "--message": True, "-F": True, "--file": True,
               "-a": False, "--all": False, "--amend": False, "--no-edit": False,
               "--allow-empty": False, "-s": False, "--signoff": False,
               "--fixup": True, "--squash": True, "-q": False, "--quiet": False,
               "--dry-run": False, "--": False},
    "rm": {"--cached": False, "-r": False, "-q": False, "--quiet": False, "--": False},
    "mv": {"-k": False, "-v": False, "--verbose": False},
    "switch": {"-c": True, "--create": True, "--detach": False, "-q": False,
               "--quiet": False},
    "reset": {"--soft": False, "--mixed": False, "-q": False, "--quiet": False,
              "--": False},
    "rebase": {"--onto": True, "--continue": False, "--autosquash": False,
               "--no-autosquash": False, "-r": False, "--rebase-merges": False,
               "--keep-base": False, "-q": False, "--quiet": False},
    "cherry-pick": {"--continue": False, "-x": False, "-n": False, "--no-commit": False,
                    "--no-edit": False, "--allow-empty": False, "-m": True, "--mainline": True},
    "revert": {"--continue": False, "-n": False, "--no-commit": False,
               "--no-edit": False, "-m": True, "--mainline": True},
    "merge": {"--ff-only": False, "--no-ff": False, "--ff": False,
              "--no-edit": False, "--continue": False, "--no-commit": False,
              "--squash": False, "-m": True, "-F": True, "--file": True, "-q": False,
              "--quiet": False},
    "branch": {},  # create only: `git branch <name> [<start>]`
}

#: Read-only git subcommands: git reads, and a word among the arguments is data.
_RO_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "show", "blame", "rev-parse", "rev-list", "ls-files",
    "ls-tree", "ls-remote", "cat-file", "merge-base", "merge-tree", "describe",
    "shortlog", "name-rev", "for-each-ref", "show-ref", "diff-tree", "diff-index",
    "diff-files", "grep", "count-objects", "whatchanged", "show-branch",
    "check-ignore", "check-attr", "var", "verify-commit", "verify-tag", "version",
    "help", "cherry", "range-diff", "get-tar-commit-id",
})
#: ``git branch`` options that only list (a value flag names the word after it).
_BRANCH_LIST_FLAGS: dict[str, bool] = {
    "-a": False, "--all": False, "-r": False, "--remotes": False, "-v": False,
    "-vv": False, "--verbose": False, "--list": False, "-l": False,
    "--show-current": False, "--no-color": False, "--color": False,
    "--no-column": False, "--column": False, "-i": False, "--ignore-case": False,
    "--no-abbrev": False, "--abbrev": False, "--contains": True, "--no-contains": True,
    "--merged": True, "--no-merged": True, "--points-at": True, "--sort": True,
    "--format": True,
}
_FETCH_FLAGS: dict[str, bool] = {
    "-q": False, "--quiet": False, "-v": False, "--verbose": False, "--all": False,
    "-t": False, "--tags": False, "--no-tags": False, "-n": False,
    "--unshallow": False, "--no-recurse-submodules": False, "--depth": True,
}
_CLONE_FLAGS: dict[str, bool] = {
    "-q": False, "--quiet": False, "--depth": True, "-b": True, "--branch": True,
    "--single-branch": False, "--no-single-branch": False, "--no-checkout": False,
    "-n": False, "--no-tags": False, "--bare": False,
}
_PUSH_FLAGS: dict[str, bool] = {
    "-u": False, "--set-upstream": False, "-q": False, "--quiet": False,
    "-v": False, "--verbose": False, "-n": False, "--dry-run": False,
}
_INIT_FLAGS: dict[str, bool] = {
    "-q": False, "--quiet": False, "-b": True, "--initial-branch": True, "--bare": False,
}
_WORKTREE_ADD_FLAGS: dict[str, bool] = {"--detach": False, "-q": False, "--quiet": False}

#: Commands that cannot run what they are given, so a verb word in their arguments
#: is text. ``echo`` and ``printf`` produce their arguments as output, which makes
#: them taint a pipeline (see ``analyze``). ``sed``, ``awk``, ``find``, ``xargs``,
#: ``env``, ``tee``, ``sort -o``, ``rg --pre`` and every interpreter stay out.
_INERT_HEADS = frozenset({
    "grep", "egrep", "fgrep", "cat", "head", "tail", "wc", "ls", "echo", "printf",
    "cut", "tr", "pwd", "basename", "dirname", "realpath", "readlink", "stat",
    "file", "test", "true", "false", ":", "nl", "date", "sleep", "diff", "cmp",
    "sha256sum", "shasum", "md5", "md5sum", "du", "which", "mkdir",
})
_TEXT_PRODUCERS = frozenset({"echo", "printf"})
#: Words that start a command again, or close a compound, or open a loop.
_INTRO = frozenset({"if", "while", "until", "then", "do", "else", "elif", "!", "time"})
_CLOSERS = frozenset({"fi", "done"})
#: Assignment names that change how a later command resolves or what it runs.
_BAD_ASSIGN_RE = re.compile(
    r"(?:GIT_.*|LD_.*|DYLD_.*|PYTHON.*|NODE_.*|XDG_.*|SSH_.*|GPG_.*|BASH_ENV|ENV|"
    r"PATH|IFS|HOME|SHELL|ZDOTDIR|CDPATH|PAGER|EDITOR|VISUAL|TMPDIR|"
    r"PERL.*|RUBY.*|CLASSPATH|JAVA_TOOL_OPTIONS)"
)
_CONFIG_WORD_RE = re.compile(r"(?<![\w.-])config(?![\w.-])")
_AS_IF_PIPED_TO_SH = " | sh"
_EXEC_RE = re.compile(
    r"(?<![\w.-])(?:sh|bash|zsh|dash|ksh|csh|tcsh|fish|pwsh|powershell|xargs|eval|"
    r"source|parallel|python[\d.]*|perl|ruby|node(?:js)?|deno|bun|php|lua|[gmn]?awk|"
    r"sed|osascript|expect|tclsh|groovy|jshell|irb|swift|emacs|vim|nvim|gdb|lldb)(?![\w.-])"
)
_DOT_SOURCE_RE = re.compile(r"(?:^|[;&|(\n])\s*\.\s")
_GIT_ENV_RE = re.compile(r"(?<![A-Za-z0-9_])GIT_[A-Z_]+")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_UNQ_SAFE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:@,+=%-^~"
)
_GLOB = frozenset("*?[]")
_SYSTEM_BINS = frozenset({"/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"})
_PROTECTED_BRANCHES = frozenset({"develop", "main", "master"})
_BRANCH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
_REMOTE_REF_PREFIX = "refs/heads/"


class Check(NamedTuple):
    """A git segment the caller must still run the config and side-effect rules on."""
    text: str
    worktree: bool
    root: str | None


class Admission(NamedTuple):
    kind: str
    checks: tuple[Check, ...]


class Hooks(NamedTuple):
    """What the module cannot decide itself. All are pure from its point of view."""
    legacy_denies: Callable[[str], bool]
    is_wt_root: Callable[[str], bool]
    branch_of: Callable[[str], str | None]
    safe_config_key: Callable[[str], bool]
    #: does the text hold a write-verb word, whatever stands before it (the unchanged verb list)?
    has_verb_word: Callable[[str], bool]
    #: does the text hold ``git`` as a command word (the unchanged arming rule)?
    has_git_word: Callable[[str], bool]


class _Refuse(Exception):
    """The command is not admitted."""


class _Subst(NamedTuple):
    """A ``$(...)`` the lexer read: its own segments, and its text."""
    segs: tuple["_RawSeg", ...]
    raw: str


class _RawWord(NamedTuple):
    #: ``(kind, text)``: ``u`` unquoted literal, ``s`` single-quoted, ``d`` double-quoted
    #: literal, ``v`` unquoted variable name, ``V`` quoted variable name, ``g`` glob char,
    #: ``c`` a command substitution (text is a ``_Subst``).
    parts: tuple[tuple[str, Any], ...]


class _RawSeg(NamedTuple):
    words: tuple[_RawWord, ...]
    raw: str
    op: str  # the operator that FOLLOWS: && || ; \n | or "" at the end
    #: ``(operator, target word)`` per file redirect; ``/dev/null`` and fd copies are dropped.
    redirects: tuple[tuple[str, _RawWord], ...] = ()
    #: the segments of a ``( ... )`` group, which runs in a subshell of its own.
    sub: tuple["_RawSeg", ...] | None = None


class _Word(NamedTuple):
    text: str
    lit: bool  # fully literal: no unresolved variable, glob or leading tilde
    #: no unquoted ``~`` or ``^``: zsh reads them as expansion and glob syntax. A write shape
    #: takes only plain words, so `'HEAD~1'` is written quoted, as the deny text has always said.
    plain: bool = True


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

_MAX_SUBST_DEPTH = 3
_FD_DUP_RE = re.compile(r">&\d(?![\w])")
_VAR_BRACE_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _lex(command: str, i: int = 0, depth: int = 0, in_subst: bool = False) -> tuple[list[_RawSeg], int]:
    """The segments of *command* from *i*, and the index after the last character read.
    With *in_subst* the read stops after an unquoted ``)`` (the end of a ``$(...)``)."""
    n = len(command)
    segs: list[_RawSeg] = []
    words: list[_RawWord] = []
    redirects: list[tuple[str, _RawWord]] = []
    parts: list[tuple[str, Any]] = []
    pending: list[str] = []  # the redirect operator whose target word is next
    subshell: tuple[_RawSeg, ...] | None = None
    seg_start = i

    def end_word() -> None:
        nonlocal parts
        if parts:
            word = _RawWord(tuple(parts))
            parts = []
            if subshell is not None and not pending:
                raise _Refuse  # a word after `( ... )`
            if pending:
                redirects.append((pending.pop(), word))
            else:
                words.append(word)

    def end_seg(op: str, op_len: int) -> None:
        nonlocal words, redirects, seg_start, i, subshell
        end_word()
        if pending:
            raise _Refuse  # a redirect with no target
        segs.append(_RawSeg(tuple(words), command[seg_start:i].strip(), op, tuple(redirects), subshell))
        words, redirects, subshell = [], [], None
        i += op_len
        seg_start = i
        if op in ("&&", "||", "|"):  # a newline after a binary operator continues the line
            while i < n and command[i] in " \t\n":
                i += 1
            seg_start = i
        if len(segs) > MAX_SEGMENTS:
            raise _Refuse

    def add(kind: str, text: str) -> None:
        if parts and parts[-1][0] == kind and kind in ("u", "s", "d"):
            parts[-1] = (kind, parts[-1][1] + text)
        else:
            parts.append((kind, text))

    while i < n:
        c = command[i]
        if c in " \t":
            end_word()
            i += 1
        elif c == "\n":
            end_seg("\n", 1)
        elif c == ";":
            if command[i + 1:i + 2] in (";", "&"):
                raise _Refuse
            end_seg(";", 1)
        elif c == "&":
            nxt = command[i + 1:i + 2]
            if nxt == "&":
                end_seg("&&", 2)
            elif nxt == ">":
                end_word()
                i += 3 if command[i + 2:i + 3] == ">" else 2
                pending.append("&>")
            else:
                raise _Refuse
        elif c == "|":
            nxt = command[i + 1:i + 2]
            if nxt == "|":
                end_seg("||", 2)
            elif nxt == "&":
                raise _Refuse
            else:
                end_seg("|", 1)
        elif c in "<>":
            if len(parts) == 1 and parts[0][0] == "u" and parts[0][1].isdigit() and not pending:
                parts = []  # the fd of `2>&1`
            else:
                end_word()
            if pending:
                raise _Refuse
            fd = _FD_DUP_RE.match(command, i)
            if fd:
                i = fd.end()
            elif command[i:i + 2] in ("<<", "<(", ">(", "<&", ">|") or command[i:i + 2] == ">&":
                raise _Refuse
            elif c == "<":
                pending.append("<")
                i += 1
            else:
                pending.append(">>" if command[i + 1:i + 2] == ">" else ">")
                i += 2 if command[i + 1:i + 2] == ">" else 1
        elif c == "(" and not words and not parts and not pending and subshell is None:
            if depth >= _MAX_SUBST_DEPTH or command[i + 1:i + 2] == "(":
                raise _Refuse
            inner, i = _lex(command, i + 1, depth + 1, True)
            subshell = tuple(inner)
        elif c == ")" and in_subst:
            end_word()
            if pending:
                raise _Refuse
            segs.append(_RawSeg(tuple(words), command[seg_start:i].strip(), "", tuple(redirects), subshell))
            return segs, i + 1
        elif c == "'":
            j = command.find("'", i + 1)
            if j < 0:
                raise _Refuse
            body = command[i + 1:j]
            if "\x00" in body:
                raise _Refuse
            parts.append(("s", body))
            i = j + 1
        elif c == '"':
            i = _lex_dquote(command, i + 1, parts, depth)
        elif c == "$":
            i = _lex_var(command, i, parts, "v", depth)
        elif c in _GLOB:
            parts.append(("g", c))
            i += 1
        elif c in _UNQ_SAFE:
            if c == "=" and not parts and command[i + 1:i + 2].isalnum():
                raise _Refuse  # zsh `=cmd`
            add("u", c)
            i += 1
        else:
            # `#`, backslash, backtick, braces, parentheses, `!`, CR, non-ASCII...
            raise _Refuse
    if in_subst:
        raise _Refuse  # no closing parenthesis
    end_word()
    if pending:
        raise _Refuse
    if words or parts or redirects or subshell is not None:
        segs.append(_RawSeg(tuple(words), command[seg_start:].strip(), "", tuple(redirects), subshell))
    return segs, i


def _lex_var(command: str, i: int, parts: list[tuple[str, Any]], kind: str, depth: int) -> int:
    """Read ``$NAME``, ``${NAME}``, ``$?`` or ``$(...)`` at *i*; return the index past it."""
    nxt = command[i + 1:i + 2]
    if nxt == "(":
        if depth >= _MAX_SUBST_DEPTH or command[i + 2:i + 3] == "(":
            raise _Refuse  # too deep, or `$((` arithmetic
        segs, end = _lex(command, i + 2, depth + 1, True)
        parts.append(("c", _Subst(tuple(segs), command[i + 2:end - 1])))
        return end
    if nxt == "{":
        m = _VAR_BRACE_RE.match(command, i + 1)
        if not m:
            raise _Refuse
        parts.append((kind, m.group(1)))
        return m.end()
    m = _NAME_RE.match(command, i + 1)
    if m:
        after = command[m.end():m.end() + 2]
        if after[:1] == "[" or (after[:1] == ":" and after[1:2].isalpha()):
            raise _Refuse  # zsh reads `$x[1]` as a subscript and `$x:h` as a modifier
        parts.append((kind, m.group()))
        return m.end()
    if nxt == "?":
        parts.append((kind, "?"))
        return i + 2
    raise _Refuse


def _lex_dquote(command: str, i: int, parts: list[tuple[str, Any]], depth: int) -> int:
    """Read a double-quoted string whose opening quote ends just before *i*. A backslash
    is read as the shell reads it there: it escapes only ``$``, a backtick, ``"`` and
    ``\\``; before any other character it stays. (A backslash-newline is refused.)"""
    n = len(command)
    buf: list[str] = []

    def flush() -> None:
        if buf:
            parts.append(("d", "".join(buf)))
            buf.clear()

    start_len = len(parts)
    while i < n:
        c = command[i]
        if c == '"':
            flush()
            if len(parts) == start_len:
                parts.append(("d", ""))  # "" is an empty word, not nothing
            return i + 1
        if c in "`\x00":
            raise _Refuse
        if c == "\\":
            nxt = command[i + 1:i + 2]
            if nxt in ("$", '"', "\\"):
                buf.append(nxt)
                i += 2
            elif nxt in ("\n", "\r", "`", ""):
                raise _Refuse
            else:
                buf.append("\\" + nxt)
                i += 2
            continue
        if c == "$":
            flush()
            i = _lex_var(command, i, parts, "V", depth)
            continue
        buf.append(c)
        i += 1
    raise _Refuse


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

Env = dict[str, tuple[str, bool]]


def _resolve(word: _RawWord, env: Env) -> _Word:
    out: list[str] = []
    lit = True
    plain = True
    for idx, (kind, text) in enumerate(word.parts):
        if kind in ("u", "s", "d"):
            out.append(text)
            if kind == "u" and ("~" in text or "^" in text):
                plain = False
            if kind == "u" and idx == 0 and text.startswith("~"):
                lit = False
        elif kind == "g":
            out.append(text)
            lit = False
        elif kind == "c":
            out.append("$(" + text.raw + ")")
            lit = False
        else:
            bound = env.get(text)
            if bound is None:
                out.append("$" + text)
                lit = False
                continue
            tmpl, certain = bound
            out.append(tmpl)
            if not certain or "$" in tmpl:
                lit = False
            elif kind == "v" and (not tmpl or re.search(r"[\s*?\[\]~]", tmpl)):
                lit = False  # an unquoted expansion may split or glob
    text_out = "".join(out)
    if len(text_out) > MAX_WORD:
        raise _Refuse
    return _Word(text_out, lit, plain)


def _render_raw(raw: str, env: Env) -> str:
    """*raw* with every variable bound earlier in the command read back, so text
    carried in a variable is scanned where it is used."""
    if "$" not in raw or not env:
        return raw

    def sub(m: re.Match[str]) -> str:
        name = m.group(1) or m.group(2)
        bound = env.get(name)
        return bound[0] if bound is not None else m.group(0)

    out = re.sub(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))", sub, raw)
    if len(out) > _MAX_SUBST:
        raise _Refuse
    return out


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def _temp_roots() -> tuple[str, ...]:
    roots = {os.path.realpath(p) for p in ("/tmp", "/var/tmp", "/private/tmp", "/private/var/tmp")}
    try:
        roots.add(os.path.realpath(tempfile.gettempdir()))
    except Exception:  # noqa: BLE001 -- no temp dir: only the fixed roots
        pass
    return tuple(roots)


def under_temp_root(path: str) -> bool:
    real = os.path.realpath(path)
    return any(real != root and real.startswith(root.rstrip(os.sep) + os.sep) for root in _temp_roots())


def outside_every_repo(path: str) -> bool:
    """True when neither *path* nor any ancestor holds a ``.git`` entry (the path
    need not exist yet; its nearest existing ancestor decides through the walk)."""
    cur = os.path.realpath(path)
    while True:
        if os.path.lexists(os.path.join(cur, ".git")):
            return False
        parent = os.path.dirname(cur)
        if parent == cur:
            return True
        cur = parent


def _clean_abs(word: _Word) -> str | None:
    """The word as an absolute path with no ``..`` and no ``.`` segment, or None."""
    if not word.lit or not word.text or not os.path.isabs(word.text):
        return None
    if any(seg in ("..", ".") for seg in re.split(r"[\\/]", word.text)):
        return None
    return os.path.normpath(word.text)


def _fresh_scratch_dir(path: str) -> bool:
    """A directory a command may turn into a scratch repository: under a temporary
    root, outside every repository, and not already a repository."""
    return (
        under_temp_root(path)
        and outside_every_repo(path)
        and not os.path.lexists(os.path.join(path, ".git"))
    )


# ---------------------------------------------------------------------------
# Flag walks
# ---------------------------------------------------------------------------


def _flags_ok(allowed: dict[str, bool], rest: list[_Word]) -> bool:
    """True when every option word is exactly one *allowed* lists. The word after a
    value flag is its value, whatever it holds; after ``--`` every word is a path."""
    expect_value = False
    for w in rest:
        if expect_value:
            expect_value = False
            continue
        if not w.lit:
            return False
        t = w.text
        if not t.startswith("-") or t == "-":
            continue
        name, eq, _value = t.partition("=")
        if name not in allowed or (eq and not (name.startswith("--") and allowed[name])):
            return False
        expect_value = allowed[name] and not eq
        if t == "--":
            return True
    return not expect_value


def _split_positionals(
    allowed: dict[str, bool], rest: list[_Word],
) -> tuple[list[_Word], list[_Word]] | None:
    """``(flags, positionals)`` for *rest*, or None when a flag is not in *allowed*
    or a word is not literal. A value flag consumes the next word into the flags."""
    flags: list[_Word] = []
    pos: list[_Word] = []
    expect_value = False
    for w in rest:
        if expect_value:
            flags.append(w)
            expect_value = False
            continue
        if not w.lit:
            return None
        t = w.text
        if t.startswith("-") and t != "-":
            name, eq, _v = t.partition("=")
            if name not in allowed or (eq and not (name.startswith("--") and allowed[name])):
                return None
            flags.append(w)
            expect_value = allowed[name] and not eq
        else:
            pos.append(w)
    return None if expect_value else (flags, pos)


# ---------------------------------------------------------------------------
# Git segments
# ---------------------------------------------------------------------------


class _Res(NamedTuple):
    kind: str
    check: Check | None = None
    created: str | None = None


def _parse_git(words: list[_Word]) -> tuple[_Word | None, int] | None:
    """``(-C word or None, index of the subcommand word)``, or None when a global
    option other than ``-C`` (once), ``--no-pager``, ``-P`` or ``--no-optional-locks``
    stands before the subcommand."""
    c_word: _Word | None = None
    i = 1
    while i < len(words):
        t = words[i].text
        if not words[i].lit:
            return None
        if t == "-C":
            if c_word is not None or i + 1 >= len(words):
                return None
            c_word = words[i + 1]
            i += 2
        elif t in ("--no-pager", "-P", "--no-optional-locks"):
            i += 1
        elif t.startswith("-"):
            return None
        else:
            return c_word, i
    return None


def _branch_read_only(args: list[_Word]) -> bool:
    listing = False
    expect_value = False
    for w in args:
        if expect_value:
            expect_value = False
            continue
        if not w.lit:
            return False
        t = w.text
        if t.startswith("-"):
            name, eq, _v = t.partition("=")
            if name not in _BRANCH_LIST_FLAGS:
                return False
            if name in ("--list", "-l", "--show-current", "--contains", "--no-contains",
                        "--merged", "--no-merged", "--points-at"):
                listing = True
            expect_value = _BRANCH_LIST_FLAGS[name] and not eq
        elif not listing:
            return False  # `git branch <name>` creates a branch
    return not expect_value


def _ro_shape(sub: str, args: list[_Word]) -> bool:
    if sub in _RO_SUBCOMMANDS:
        if sub == "grep":
            return not any(w.lit and (w.text.startswith("-O") or w.text.startswith("--open-files-in-pager"))
                           for w in args)
        return True
    if sub == "stash":
        return bool(args) and args[0].lit and args[0].text in ("list", "show")
    if sub == "worktree":
        return bool(args) and args[0].lit and args[0].text == "list"
    if sub == "branch":
        return _branch_read_only(args)
    return False


def _fetch_shape(args: list[_Word]) -> bool:
    """``git fetch`` with only flags that write nothing but ``refs/remotes/*`` and no
    refspec destination: a word after the remote may not hold ``:`` or start with ``+``."""
    split = _split_positionals(_FETCH_FLAGS, args)
    if split is None:
        return False
    _flags, pos = split
    for idx, w in enumerate(pos):
        if w.text.startswith("+") or (idx > 0 and ":" in w.text):
            return False
    return True


def _push_shape(args: list[_Word], branch: str | None) -> bool:
    """``push [-u] origin <HEAD|branch|branch:branch|HEAD:branch>`` where ``branch`` is
    the one the worktree has checked out and is not ``develop``, ``main`` or ``master``."""
    if branch is None or branch in _PROTECTED_BRANCHES or not _BRANCH_NAME_RE.fullmatch(branch):
        return False
    split = _split_positionals(_PUSH_FLAGS, args)
    if split is None:
        return False
    _flags, pos = split
    if len(pos) != 2 or pos[0].text != "origin":
        return False
    spec = pos[1].text
    if spec.startswith("+"):
        return False
    src, colon, dst = spec.partition(":")
    if src not in ("HEAD", branch):
        return False
    if colon:
        if dst.startswith(_REMOTE_REF_PREFIX):
            dst = dst[len(_REMOTE_REF_PREFIX):]
        if dst != branch:
            return False
    return True


def _handle_git(
    words: list[_Word], raw: str, *, ctx: tuple[str, str] | None, created: set[str],
    hooks: Hooks, writes_ok: bool,
) -> _Res | None:
    parsed = _parse_git(words)
    if parsed is None:
        return None
    c_word, i = parsed
    sub = words[i].text
    if not words[i].lit:
        return None
    args = words[i + 1:]

    if _ro_shape(sub, args) or (sub == "fetch" and _fetch_shape(args)):
        return _Res("read", Check(raw, False, None))
    if not writes_ok or not all(w.plain for w in words[1:]):
        return None

    c_path: str | None = None
    if c_word is not None:
        c_path = _clean_abs(c_word)
        if c_path is None:
            return None

    if sub == "worktree":
        return _worktree_add(args, raw)
    if sub == "clone":
        return _clone(args)
    if sub == "init":
        return _init(args, c_path, ctx)

    target: tuple[str, str] | None
    if c_path is not None:
        if c_path in created:
            target = ("scratch", c_path)
        elif hooks.is_wt_root(c_path):
            target = ("wt", c_path)
        else:
            target = None
    else:
        target = ctx
    if target is None:
        return None
    kind, root = target
    if kind == "scratch":
        if root not in created:
            return None  # only a repository this very command created
        if sub == "config":
            ok = len(args) == 2 and args[0].lit and not args[0].text.startswith("-") and hooks.safe_config_key(
                args[0].text
            )
            return _Res("scratch") if ok else None
        if sub in DASH_C_FLAGS and _flags_ok(DASH_C_FLAGS[sub], args):
            return _Res("scratch")
        return None
    # a linked worktree git proved
    if sub == "push":
        # No config walk: the shape names the checked-out branch and nothing else, so
        # the only config a `-u` can write is that branch's own upstream.
        return _Res("push") if _push_shape(args, hooks.branch_of(root)) else None
    if sub in DASH_C_FLAGS and _flags_ok(DASH_C_FLAGS[sub], args):
        tail = " ".join(a.text for a in args)
        if "git" in tail and hooks.legacy_denies(tail):
            return None  # a message that spells a git write: pass it with -F <file>
        return _Res("worktree", Check(raw, True, root))
    return None


def _worktree_add(args: list[_Word], raw: str) -> _Res | None:
    """``worktree add --detach <absolute path outside every repository> [<commit>]``."""
    if not args or not args[0].lit or args[0].text != "add":
        return None
    split = _split_positionals(_WORKTREE_ADD_FLAGS, args[1:])
    if split is None:
        return None
    flags, pos = split
    if not any(f.text == "--detach" for f in flags) or not 1 <= len(pos) <= 2:
        return None
    path = _clean_abs(pos[0])
    if path is None or not outside_every_repo(path):
        return None
    if len(pos) == 2 and not _BRANCH_NAME_RE.fullmatch(pos[1].text):
        return None
    return _Res("detached", Check(raw, False, None))


def _init(args: list[_Word], c_path: str | None, ctx: tuple[str, str] | None) -> _Res | None:
    """``git init`` of one fresh scratch directory: the positional, or ``-C``, or the
    directory a ``cd <scratch> &&`` before it named."""
    split = _split_positionals(_INIT_FLAGS, args)
    if split is None:
        return None
    _flags, pos = split
    if len(pos) > 1 or (pos and c_path is not None):
        return None
    if pos:
        path = _clean_abs(pos[0])
    elif c_path is not None:
        path = c_path
    elif ctx is not None and ctx[0] == "scratch":
        path = ctx[1]
    else:
        return None
    if path is None or not _fresh_scratch_dir(path):
        return None
    return _Res("scratch", None, path)


def _clone(args: list[_Word]) -> _Res | None:
    split = _split_positionals(_CLONE_FLAGS, args)
    if split is None:
        return None
    _flags, pos = split
    if len(pos) != 2:
        return None
    dest = _clean_abs(pos[1])
    if dest is None or not _fresh_scratch_dir(dest):
        return None
    return _Res("scratch", None, dest)


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def _first_plain(word: _RawWord) -> str | None:
    """The text of a word that is one unquoted literal, else None."""
    if len(word.parts) == 1 and word.parts[0][0] == "u":
        return word.parts[0][1]
    return None


def analyze(command: str, hooks: Hooks) -> Admission | None:
    """The admission verdict for *command*, or None (the caller falls through)."""
    if not command or len(command) > MAX_COMMAND:
        return None
    try:
        segs, _end = _lex(command)
        if not segs:
            return None
        return _Run(command, hooks).finish(segs)
    except _Refuse:
        return None


def _bare_command(head: _Word) -> str:
    """The command name when the head is a bare name or sits in a system bin
    directory; ``./git``, ``/tmp/x/grep`` and the like answer the empty string."""
    if not head.lit:
        return ""
    if "/" not in head.text:
        return head.text
    folder, _, name = head.text.rpartition("/")
    return name if folder in _SYSTEM_BINS else ""


_SED_PRINT_RE = re.compile(r"\d+(?:,(?:\d+|\$))?p(?:;\d+(?:,(?:\d+|\$))?p)*")


def _sed_print_only(args: list[_Word]) -> bool:
    """``sed -n '<N[,M]>p' [file...]``: a line-range print, which runs nothing and writes nothing."""
    return (
        len(args) >= 2 and args[0].text == "-n" and args[0].lit and args[1].lit
        and _SED_PRINT_RE.fullmatch(args[1].text) is not None
        and all(a.lit and not a.text.startswith("-") for a in args[2:])
    )


def _redirect_kinds(seg: _RawSeg, env: Env) -> tuple[bool, bool]:
    """``(writes a file, reads a file)`` for the redirects of *seg*; ``/dev/null`` is neither."""
    writes = reads = False
    for op, w in seg.redirects:
        t = _resolve(w, env)
        if t.lit and t.text == "/dev/null":
            continue
        if op == "<":
            reads = True
        else:
            writes = True
    return writes, reads


def _substitutions(words: Any) -> list[_Subst]:
    return [text for w in words for kind, text in w.parts if kind == "c"]


class _Run:
    """One command's analysis: the variables, the repositories it created, the
    git segments the caller still has to check, and the kinds of write it holds."""

    def __init__(self, command: str, hooks: Hooks) -> None:
        self.hooks = hooks
        self.writes_ok = _GIT_ENV_RE.search(command) is None
        self.git_in_command = hooks.has_git_word(command)
        self.created: set[str] = set()
        self.checks: list[Check] = []
        self.kinds: set[str] = set()
        self.substitutions = 0
        self.executor = False  # a segment that can run text: an interpreter, a shell, xargs

    def finish(self, segs: list[_RawSeg]) -> Admission:
        self.run(segs, {}, None)
        if self.executor:
            # The unchanged guard withholds every exemption from a command that holds an
            # interpreter: verb text and the interpreter may meet through an environment
            # variable or a file this reading does not follow. Kept, so none of the
            # bypass families that name one is loosened.
            raise _Refuse
        for kind in ("push", "worktree", "detached", "scratch"):
            if kind in self.kinds:
                return Admission(kind, tuple(self.checks))
        return Admission("read", tuple(self.checks))

    def _cd_context(self, path: str | None) -> tuple[str, str] | None:
        if path is None:
            return None
        if self.hooks.is_wt_root(path):
            return ("wt", path)
        if path in self.created or _fresh_scratch_dir(path):
            return ("scratch", path)
        return None

    def _subst(self, subs: list[_Subst], env: Env, ctx: tuple[str, str] | None) -> None:
        """Analyse each ``$(...)`` in its own copy of the shell: it sees the variables and
        the directory the outer command has, and leaves neither changed."""
        for sub in subs:
            self.substitutions += 1
            if self.substitutions > 20:
                raise _Refuse
            self.run(list(sub.segs), dict(env), ctx)

    def run(self, segs: list[_RawSeg], env: Env, ctx0: tuple[str, str] | None) -> None:
        hooks = self.hooks
        ctx = ctx0  # a subshell or substitution starts in the directory its outer command is in
        base = ctx0  # where a command after `;`, a newline or `||` is: the cwd a guarded cd set
        keep_ctx = ctx0 is not None
        pending_ctx: tuple[str, str] | None = None
        depth = 0
        tainted = False
        prev_op = ""
        for k, seg in enumerate(segs):
            # The directory a `cd <root> &&` set holds along the `&&` run; a `;`, a newline
            # or `||` may follow a failed cd, so the next command is back at the start cwd.
            if prev_op in ("", ";", "\n", "||") and not keep_ctx:
                ctx = base
            keep_ctx = False
            piped_in = prev_op == "|"
            piped = piped_in or seg.op == "|"
            if not piped_in:
                tainted = False
            if seg.sub is not None:  # `( ... )`: its own copy of the shell
                if piped or seg.redirects:
                    raise _Refuse
                self.run(list(seg.sub), dict(env), ctx)
                prev_op = seg.op
                continue
            words = list(seg.words)
            while words:  # keywords that start a command again, or open or close a compound
                kw = _first_plain(words[0])
                if kw in ("if", "while", "until"):
                    depth += 1
                elif kw in _CLOSERS:
                    depth -= 1
                    if depth < 0 or len(words) > 1 or seg.op == "|" or seg.redirects:
                        raise _Refuse
                elif kw not in _INTRO:
                    break
                words.pop(0)
            self._subst(_substitutions(words) + _substitutions(w for _o, w in seg.redirects), env, ctx)
            if not words:
                prev_op = seg.op
                continue
            if _first_plain(words[0]) == "for":
                name = _first_plain(words[1]) if len(words) > 1 else None
                if (
                    len(words) < 4 or name is None or _NAME_RE.fullmatch(name) is None
                    or _first_plain(words[2]) != "in" or piped or seg.redirects
                ):
                    raise _Refuse
                env[name] = (" ".join(_resolve(w, env).text for w in words[3:]), False)
                depth += 1
                prev_op = seg.op
                continue
            assigns: list[tuple[str, _RawWord]] = []
            bad_assign = False
            while words and words[0].parts and words[0].parts[0][0] == "u":
                first = words[0].parts[0][1]
                m = _ASSIGN_RE.match(first)
                if not m:
                    break
                if _BAD_ASSIGN_RE.fullmatch(m.group(1)):
                    bad_assign = True
                value_parts = words[0].parts[1:]
                if first[m.end():]:
                    value_parts = (("u", first[m.end():]),) + value_parts
                assigns.append((m.group(1), _RawWord(value_parts or (("u", ""),))))
                words.pop(0)
            raw = _render_raw(seg.raw, env)
            if not words:  # a pure assignment
                if piped or bad_assign or _redirect_kinds(seg, env) != (False, False):
                    raise _Refuse
                conditional = prev_op not in ("", ";", "\n") or depth > 0
                for name, value in assigns:
                    w = _resolve(value, env)
                    env[name] = (w.text, w.lit and not conditional)
                prev_op = seg.op
                continue
            rw = [_resolve(w, env) for w in words]
            head = rw[0]
            name = _bare_command(head)
            writes_file, reads_file = _redirect_kinds(seg, env)

            if name == "cd":
                if piped or len(rw) != 2 or rw[1].text.startswith("-") or writes_file or reads_file:
                    raise _Refuse
                base = None  # after a cd the cwd is the new one or, if it failed, the old one
                new_ctx = self._cd_context(_clean_abs(rw[1])) if depth == 0 else None
                if new_ctx is not None and seg.op == "&&":
                    ctx, keep_ctx = new_ctx, True
                else:
                    ctx = None
                    nxt = segs[k + 1] if k + 1 < len(segs) else None
                    if (
                        new_ctx is not None and seg.op == "||" and nxt is not None and nxt.words
                        and _first_plain(nxt.words[0]) in ("exit", "return") and nxt.op in (";", "\n", "")
                    ):
                        pending_ctx = new_ctx  # `cd <root> || exit 1`: the next command runs only in it
                prev_op = seg.op
                continue
            if name == "exit" and pending_ctx is not None:
                ctx, base, keep_ctx, pending_ctx = pending_ctx, pending_ctx, True, None
                prev_op = seg.op
                continue

            res: _Res | None = None
            if bad_assign:
                res = None  # an env change that alters the command: the unchanged guard decides
            elif name == "git":
                res = _handle_git(
                    rw, raw, ctx=ctx, created=self.created, hooks=hooks, writes_ok=self.writes_ok,
                )
                if res is not None and res.created:
                    self.created.add(res.created)
            elif name == "printf" and any(a.text == "-v" for a in rw[1:]):
                raise _Refuse  # `printf -v V ...` sets a variable the later commands read
            elif name in _INERT_HEADS or (name == "sed" and _sed_print_only(rw[1:])):
                res = _Res("read")
            if res is not None and writes_file and name in _TEXT_PRODUCERS:
                res = None  # echo and printf write their own words: never into a file
            # Would the unchanged guard deny this segment if its output went to a shell? Then its
            # text (a verb word, a config read) may become a command, so it is neither written to
            # a file nor left upstream of a command in the same pipeline.
            carries = name in _TEXT_PRODUCERS or hooks.legacy_denies(raw + _AS_IF_PIPED_TO_SH)
            if res is not None:
                if res.check is not None:
                    self.checks.append(res.check)
                self.kinds.add(res.kind)
                if carries and writes_file:
                    raise _Refuse
                if piped and carries:
                    tainted = True
                prev_op = seg.op
                continue
            # anything else must pass the unchanged guard on its own text
            if piped_in and tainted:
                raise _Refuse
            # `export V=stash`, `declare -x V=stash`, `env V=stash cmd`: read the value back
            # wherever `$V` is used later, as for a plain assignment (maybe-bound).
            for extra in words[1:]:
                first = extra.parts[0] if extra.parts else ("", "")
                am = _ASSIGN_RE.match(first[1]) if first[0] == "u" else None
                if am:
                    rest = ((("u", first[1][am.end():]),) if first[1][am.end():] else ()) + extra.parts[1:]
                    env[am.group(1)] = (_resolve(_RawWord(rest or (("u", ""),)), env).text, False)
            if hooks.legacy_denies(raw):
                raise _Refuse
            if (
                self.git_in_command and not hooks.has_git_word(raw)
                and hooks.has_verb_word(" ".join(w.text for w in rw[1:]))
            ):
                # A verb word among the arguments of a command that is not git, beside a git word elsewhere: an alias,
                # a symlink or a copy of git (`alias g=git`, `ln -s /usr/bin/git /tmp/g`) makes
                # that command git, and only the two words together say so. The unchanged guard
                # reads the whole text for this, so it decides.
                raise _Refuse
            if name == "git" and _CONFIG_WORD_RE.search(raw):
                # The config rule judges a read by the whole command (a `$` anywhere makes it
                # unprovable), which a segment cannot see: the unchanged guard decides alone.
                raise _Refuse
            if carries and writes_file:
                raise _Refuse
            if piped and carries:
                tainted = True
            if _EXEC_RE.search(raw) or _DOT_SOURCE_RE.search(raw):
                self.executor = True
            prev_op = seg.op
        if depth != 0:
            raise _Refuse
