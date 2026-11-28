# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a commit's change to one file is, for the red-run report's fix pick (RDR-258 Phase 0, nexus-2jomg.1).

The report ties a red item to the commit in its fix range that touched the failing test's file. Taking the
first such commit misattributed two of the nine readings the Phase 0 step 3 hand check read: a commit whose
whole change to the test was one docstring line, and a commit whose only change to an allowlist test was an
entry for a file the same commit added. ``ChangeReader.analyse`` reads one commit's diff to one file and says
whether it can be the fix:

- a line of docstring or comment (found in the file at that commit, so a line inside a multi-line docstring
  counts) and a blank line are not a change to what the test checks;
- a hunk that only adds lines, every one of them a table row, one of which names a file the same commit adds
  (a whole path component match, the analysed file excluded), only declares that file to a table, so it was
  in flight with the commit and fixed nothing that was already red.

A commit qualifies when at least one hunk is neither. ``doc_only`` says every changed line was docstring,
comment or blank. ``code_lines`` is every other changed line.

Table edit (``table_only``). A change is a table edit when every code line it adds or removes is a row of a table
and it changes no statement or expression outside one. For Python that is decided with ``ast`` at both the old
and the new revision of each changed line: a row is a line within a List, Tuple, Set or Dict literal that is bound
to a name at any scope (module, class or function-local; the target may be a name, an attribute such as
``self.x`` or a subscript such as ``d[k]``; also under ``frozenset``, ``tuple``, ``list``, ``set`` or ``dict``) or
is an argument of a ``parametrize`` or ``fixture`` decorator on a function or a class, and the line of an integer
constant assignment at module or class level. A literal that is only compared or iterated (``assert x == {...}``,
``for k in (...)``) is bound to nothing and is code. Every element of such a literal is a row, a multi-line string element included. A string
that is not an element of a collection literal is code, whatever its length: ``probe = ("..." "...")`` assigned
to a name, or a string passed to a call, as are the keyword arguments of an ordinary call. That is the
probe-script case, a script held in a string. Python that does not parse, Java and other files fall back to
``is_table_line``, a regex over the line alone; in Java a ``static final`` integer constant assignment is a row too.

Python files are read with ``ast`` and ``tokenize`` (a file that does not parse falls back to ``#`` lines);
Java files with a small scanner for ``//``, ``/* */`` and text blocks. Other files: a line starting ``#`` or ``//``.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_HUNK_RE: re.Pattern[str] = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

# --- the regex reading of a line: the fallback for Java, other files and Python that does not parse ---------

#: A diff line that is data, not logic: a string, tuple, list or dict entry, a comment, a blank, or a bare
#: key/value. An assertion, control flow or definition line is not.
_LOGIC_RE: re.Pattern[str] = re.compile(
    r"^\s*(assert|def|class|import|from|return|if|elif|else|for|while|with|raise|yield|try|except|"
    r"@|async)\b")
_TABLE_RE: re.Pattern[str] = re.compile(
    r"""^\s*(#.*|["'(\[{].*|[\w./:-]+\s*[:=]\s*["'(\[{\d].*|[\w./-]+,?|[)\]}],?)?\s*$""")
#: A row's end: a comma, optionally after closing brackets and optionally followed by a comment.
_ROW_END_RE: re.Pattern[str] = re.compile(r"[)\]}]*,\s*(#.*)?$")
_JAVA_CODE_RE: re.Pattern[str] = re.compile(
    r"^\s*(if|for|while|switch|return|throw|try|catch|assert\w*|class|interface|import|package|public|private|"
    r"protected|static|final|void|@)\b|[;{]\s*$")


def is_table_line(line: str) -> bool:
    """Data, not logic. A string literal with a ``\\n`` escape and no trailing comma is code in an implicit
    concatenation (a probe script), not an entry; ``"...\\n",`` and ``"...\\n"),  # note`` are rows."""
    code_in_string = (line.lstrip()[:1] in ("'", '"') and "\\n" in line and not _ROW_END_RE.search(line))
    return not _LOGIC_RE.match(line) and not code_in_string and bool(_TABLE_RE.match(line))


#: ``private static final int EXPECTED_RECORD_TYPES = 105;``: the Java counterpart of a Python integer constant.
_JAVA_INT_CONST_RE: re.Pattern[str] = re.compile(
    r"^\s*(?:(?:public|private|protected)\s+)?static\s+final\s+(?:int|long|short|byte)\s+\w+\s*=\s*-?\d[\d_]*[lL]?\s*;"
    r"\s*(?://.*)?$")


def _java_row(line: str) -> bool:
    return bool(_JAVA_INT_CONST_RE.match(line)) or is_table_line(line)


# --- comment and docstring lines ------------------------------------------------------------------------------


def python_doc_lines(text: str) -> set[int]:
    """1-based numbers of the comment-only lines and string-statement (docstring) lines of Python source.

    Comments come from ``tokenize``, so a ``#`` inside a triple-quoted string is not one. Source that does not
    parse falls back to every line starting with ``#``.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {n for n, ln in enumerate(text.splitlines(), 1) if ln.lstrip().startswith("#")}
    out: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str) and node.end_lineno is not None):
            out.update(range(node.lineno, node.end_lineno + 1))
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT and tok.line.lstrip().startswith("#"):
                out.add(tok.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def java_doc_lines(text: str) -> set[int]:
    """1-based numbers of the lines of Java source that hold comment text and no code."""
    out: set[int] = set()
    state = "code"                     # code | block | string | text_block | char
    for n, line in enumerate(text.splitlines(), 1):
        has_code, has_comment = False, state == "block"
        i = 0
        while i < len(line):
            two, c = line[i:i + 2], line[i]
            if state == "block":
                has_comment = True
                if two == "*/":
                    state, i = "code", i + 1
            elif state == "text_block":
                has_code = True
                if line.startswith('"""', i):
                    state, i = "code", i + 2
            elif state in ("string", "char"):
                has_code = True
                if c == "\\":
                    i += 1
                elif c == ('"' if state == "string" else "'"):
                    state = "code"
            elif two == "//":
                has_comment = True
                break
            elif two == "/*":
                state, has_comment, i = "block", True, i + 1
            elif line.startswith('"""', i):
                state, has_code, i = "text_block", True, i + 2
            elif c in "\"'":
                state, has_code = ("string" if c == '"' else "char"), True
            elif not c.isspace():
                has_code = True
            i += 1
        if state in ("string", "char"):
            state = "code"             # an unterminated literal ends with its line
        if has_comment and not has_code:
            out.add(n)
    return out


def doc_lines(path: str, text: str) -> set[int]:
    """Lines of ``text`` that are comment or docstring, by the language of ``path``."""
    if path.endswith(".py"):
        return python_doc_lines(text)
    if path.endswith(".java"):
        return java_doc_lines(text)
    return {n for n, ln in enumerate(text.splitlines(), 1) if ln.lstrip().startswith(("#", "//"))}


# --- table lines of Python source (ast) ------------------------------------------------------------------------

_COLLECTIONS = (ast.List, ast.Tuple, ast.Set, ast.Dict)
_COLLECTION_CALLS: frozenset[str] = frozenset({"frozenset", "tuple", "list", "set", "dict"})


#: Decorators whose literal arguments are tables: ``pytest.mark.parametrize(..., [rows])``, ``pytest.fixture(params=[...])``.
_TABLE_DECORATORS: frozenset[str] = frozenset({"parametrize", "fixture"})


def _decorator_name(func: ast.AST) -> str:
    return func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""


def _spans(node: ast.AST) -> range:
    return range(node.lineno, (node.end_lineno or node.lineno) + 1)  # type: ignore[attr-defined]


def python_table_lines(text: str) -> set[int] | None:
    """Lines that are rows of a table in Python source, or None when it does not parse (see the module docstring)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    table: set[int] = set()

    def value(node: ast.AST | None) -> None:
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _COLLECTION_CALLS
                and len(node.args) == 1):
            node = node.args[0]
        if isinstance(node, _COLLECTIONS):
            table.update(_spans(node))

    def constants(body: list[ast.stmt]) -> None:
        for st in body:
            if isinstance(st, ast.ClassDef):
                constants(st.body)
            elif isinstance(st, (ast.Assign, ast.AnnAssign)):
                v = st.value.operand if isinstance(st.value, ast.UnaryOp) else st.value
                if isinstance(v, ast.Constant) and type(v.value) is int:
                    table.update(_spans(st))

    constants(tree.body)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                if isinstance(dec, ast.Call) and _decorator_name(dec.func) in _TABLE_DECORATORS:
                    for a in [*dec.args, *(k.value for k in dec.keywords)]:
                        value(a)
    return table


# --- one commit's change to one file ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One commit's change to one file."""

    code_lines: tuple[str, ...]      # changed lines that are not docstring, comment or blank
    qualifies: bool                  # some hunk is neither doc-only nor a self-declaring table entry
    doc_only: bool                   # a change was found and every changed line is docstring, comment or blank
    table_only: bool | None          # every code line is a table row; None when there is no code line


@dataclass(frozen=True)
class _Rev:
    """A file at one revision: its docstring and comment lines, and its table lines when it is parsable Python."""

    doc: set[int]
    table: set[int] | None

    def is_row(self, n: int, text: str, data: Callable[[str], bool] = is_table_line) -> bool:
        return (n in self.table) if self.table is not None else data(text)


def parse_hunks(diff: str) -> list[tuple[list[tuple[int, str]], list[tuple[int, str]]]]:
    """``-U0`` diff text as (removed, added) lists of (line number, text), one pair per hunk."""
    hunks: list[tuple[list[tuple[int, str]], list[tuple[int, str]]]] = []
    old = new = 0
    for ln in diff.splitlines():
        m = _HUNK_RE.match(ln)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            hunks.append(([], []))
        elif not hunks:
            continue                   # the header before the first hunk
        elif ln.startswith("-"):
            hunks[-1][0].append((old, ln[1:]))
            old += 1
        elif ln.startswith("+"):
            hunks[-1][1].append((new, ln[1:]))
            new += 1
    return hunks


def _names_added_file(line: str, added: tuple[str, ...]) -> bool:
    """Whether the line names one of the added paths, by whole path components (a trailing run of them)."""
    for path in added:
        parts = path.split("/")
        for k in range(len(parts)):
            if re.search(rf"(?<![\w.-]){re.escape('/'.join(parts[k:]))}(?![\w.-])", line):
                return True
    return False


def _java_data_line(line: str) -> bool:
    return not _JAVA_CODE_RE.search(line)


class ChangeReader:
    """Reads and memoises ``Change`` values out of a git repository (``git`` raises on a failure)."""

    def __init__(self, repo: Path, git: Callable[..., str]) -> None:
        self.repo = repo
        self._git = git
        self._changes: dict[tuple[str, str], Change] = {}
        self._added: dict[str, tuple[str, ...]] = {}
        self._revs: dict[tuple[str, str], _Rev] = {}

    def _rev(self, rev: str, path: str) -> _Rev:
        """The file at ``rev``; an absent file (an added file, a root commit) reads as empty."""
        if (rev, path) not in self._revs:
            try:
                text = self._git(self.repo, "show", f"{rev}:{path}")
            except RuntimeError:
                text = ""
            self._revs[rev, path] = _Rev(doc_lines(path, text),
                                         python_table_lines(text) if path.endswith(".py") else None)
        return self._revs[rev, path]

    def added_paths(self, sha: str) -> tuple[str, ...]:
        """Paths of the files ``sha`` adds."""
        if sha not in self._added:
            out = self._git(self.repo, "show", "--no-color", "--no-renames", "--diff-filter=A",
                            "--name-only", "--format=", sha)
            self._added[sha] = tuple(out.split())
        return self._added[sha]

    def analyse(self, sha: str, path: str) -> Change:
        key = (sha, path)
        if key not in self._changes:
            diff = self._git(self.repo, "show", "--no-color", "--no-renames", "-U0", "--format=",
                             sha, "--", path)
            hunks = parse_hunks(diff)
            if not hunks:
                self._changes[key] = Change((), False, False, None)
                return self._changes[key]
            old, new = self._rev(f"{sha}^", path), self._rev(sha, path)
            added = tuple(p for p in self.added_paths(sha) if p != path)
            java = path.endswith(".java")
            data = _java_data_line if java else is_table_line
            row = _java_row if java else is_table_line
            code: list[str] = []
            rows: list[bool] = []
            qualifies = False
            for removed, adds in hunks:
                kept_rm = [(n, t) for n, t in removed if n not in old.doc and t.strip()]
                kept_add = [(n, t) for n, t in adds if n not in new.doc and t.strip()]
                code += [t for _, t in kept_rm + kept_add]
                rows += [old.is_row(n, t, row) for n, t in kept_rm] + [new.is_row(n, t, row) for n, t in kept_add]
                declares = (bool(added) and not kept_rm and bool(kept_add)
                            and all(new.is_row(n, t, data) for n, t in kept_add)
                            and any(_names_added_file(t, added) for _, t in kept_add))
                if (kept_rm or kept_add) and not declares:
                    qualifies = True
            self._changes[key] = Change(tuple(code), qualifies, not code, all(rows) if rows else None)
        return self._changes[key]
