# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tests that exist at one rev of a git repository (RDR-258 Phase 0 step 2, nexus-2jomg.1).

``Inventory.judge`` says whether a candidate row's test is still there, by following the real
definition chain at the rev and never by a repo-wide name search, so a test that moved to another
file is absent at its old node id.

Python (a row with a path): parse the file with ``ast``, find the enclosing class named by the node
id (``A::B`` for a nested one), and look for the test in THAT class body: a ``def``, an ``async
def`` or an assignment, also under an ``if``/``try``/``with`` of the body. Not there: walk the
class's bases, a class of that name in the same file first and else any ``class <Base>`` in the
repo's Python files, to a depth of ``MAX_DEPTH``. A base from the standard library is skipped. A
base that cannot be resolved (a third-party class, a cycle's cap) errs toward present and the
verdict carries ``unresolved base <name>``. A module-level test is a top-level def or assignment.
A bare name (no path) takes the same walk, from the files that define its class, or from the
files that define the name for a test with no class.

Java: ``<Class>.java`` declares the method (comments and strings stripped; a call site is not a
declaration), else the class's ``extends`` chain is walked through ``<Base>.java``.

Renames are not followed. ``same_name_at_rev`` lists where else the name is defined, for a reader to
decide; it never moves a row.
"""

from __future__ import annotations

import ast
import builtins
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

#: How many bases up a test is looked for before the walk gives up as unresolved.
MAX_DEPTH: int = 5
#: Most names listed in one ``same_name_at_rev``.
HINT_CAP: int = 10

Row = dict[str, Any]
Verdict = tuple[str | None, str | None]       # (absent reason, reason a present row is only probably present)


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed in {repo}: {out.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return out.stdout.decode("utf-8", "replace")


def _flat(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """The statements of a body, looking through ``if``, ``try`` and ``with`` blocks but not into defs."""
    for st in body:
        yield st
        if isinstance(st, ast.If):
            yield from _flat(st.body)
            yield from _flat(st.orelse)
        elif isinstance(st, (ast.With, ast.AsyncWith)):
            yield from _flat(st.body)
        elif isinstance(st, ast.Try):
            for part in (st.body, st.orelse, st.finalbody, *(h.body for h in st.handlers)):
                yield from _flat(part)


def _binds(st: ast.stmt, name: str) -> bool:
    if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return st.name == name
    if isinstance(st, ast.Assign):
        return any(isinstance(n, ast.Name) and n.id == name for t in st.targets for n in ast.walk(t))
    if isinstance(st, ast.AnnAssign):
        return isinstance(st.target, ast.Name) and st.target.id == name
    return False


def _defines(body: Sequence[ast.stmt], name: str) -> bool:
    return any(_binds(st, name) for st in _flat(body))


def _class_in(body: Sequence[ast.stmt], name: str) -> ast.ClassDef | None:
    return next((st for st in _flat(body) if isinstance(st, ast.ClassDef) and st.name == name), None)


def _classes_in(body: Sequence[ast.stmt], name: str) -> list[ast.ClassDef]:
    """Every definition of the class, for one defined under both arms of an ``if``."""
    return [st for st in _flat(body) if isinstance(st, ast.ClassDef) and st.name == name]


def _deep(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """``_flat`` plus the bodies of ``for``, ``while`` and ``match``, which the walk does not follow."""
    for st in _flat(body):
        yield st
        if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            yield from _deep(st.body)
            yield from _deep(st.orelse)
        elif isinstance(st, ast.Match):
            for case in st.cases:
                yield from _deep(case.body)


def _imports(body: Sequence[ast.stmt], name: str) -> bool:
    for st in _flat(body):
        if isinstance(st, ast.Import) and any((a.asname or a.name.split(".")[0]) == name for a in st.names):
            return True
        if isinstance(st, ast.ImportFrom) and any((a.asname or a.name) == name for a in st.names):
            return True
    return False


def _unresolved_binding(body: Sequence[ast.stmt], name: str, *, is_class: bool, module: bool) -> list[str]:
    """Notes for the ways ``name`` can be bound that the walk cannot follow: an import, a star import
    (module level only), an assignment of a class name (``TestFac = make()``), a def or class under a loop."""
    notes: list[str] = []
    if _imports(body, name):
        notes.append(f"unresolved binding import {name}")
    if module and any(isinstance(st, ast.ImportFrom) and any(a.name == "*" for a in st.names) for st in _flat(body)):
        notes.append(f"unresolved binding star-import {name}")
    if is_class and any(_binds(st, name) for st in _flat(body)):
        notes.append(f"unresolved binding assignment {name}")
    flat = {id(st) for st in _flat(body)}
    hit = (lambda st: isinstance(st, ast.ClassDef) and st.name == name) if is_class else (lambda st: _binds(st, name))
    if any(id(st) not in flat and hit(st) for st in _deep(body)):
        notes.append(f"unresolved binding loop {name}")
    return notes


def _py_named(body: Sequence[ast.stmt], name: str, prefix: list[str]) -> Iterator[str]:
    """``prefix::Class::name`` for each definition of ``name`` at module or class level."""
    for st in _flat(body):
        if isinstance(st, ast.ClassDef):
            yield from _py_named(st.body, name, [*prefix, st.name])
        elif _binds(st, name):
            yield "::".join([*prefix, name])


_JAVA_TOKENS = re.compile(r'"""[\s\S]*?"""|//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'')
_JAVA_NOT_A_TYPE = frozenset(
    "return new throw else case assert yield instanceof do goto extends implements import package "
    "public protected private static final abstract synchronized default native strictfp transient "
    "volatile if for while switch catch try".split())
_JAVA_NOT_A_METHOD = frozenset("if for while switch catch synchronized return new throw assert".split())
_ANGLES = re.compile(r"<[^<>]*>")


def _java_strip(src: str) -> str:
    return _JAVA_TOKENS.sub(" ", src)


def _java_decls(src: str) -> set[str]:
    """Names declared as methods: ``<type> name(...) {`` or ``... throws``, never a call site."""
    s = _java_strip(src)
    out: set[str] = set()
    for m in re.finditer(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(", s):
        name = m.group(1)
        if name in _JAVA_NOT_A_METHOD:
            continue
        before = s[:m.start()].rstrip()
        if not before:
            continue
        word = re.search(r"[\w$]+$", before)
        if before[-1] not in ">]" and (word is None or word.group() in _JAVA_NOT_A_TYPE):
            continue
        depth, i = 0, m.end() - 1
        while i < len(s):
            depth += (s[i] == "(") - (s[i] == ")")
            i += 1
            if depth == 0:
                break
        if re.match(r"\s*(\{|throws\b)", s[i:]):
            out.add(name)
    return out


_JAVA_LANG = frozenset("Object Runnable Comparable AutoCloseable Cloneable Iterable CharSequence Thread "
                       "Exception RuntimeException Error Throwable Record Enum".split())


def _jdk_type(src: str, name: str) -> bool:
    """Whether ``name`` is a ``java.lang`` type or imported by name from ``java.`` or ``javax.``."""
    imported = re.search(rf"^\s*import\s+javax?\.[\w.]*\.{re.escape(name)}\s*;", _java_strip(src), re.M)
    return name in _JAVA_LANG or imported is not None


def _java_bases(src: str, cls: str) -> list[str]:
    """Simple names the top-level class ``cls`` extends or implements (several for an interface)."""
    s = _java_strip(src)
    m = re.search(rf"\b(?:class|interface|enum|record)\s+{re.escape(cls)}(?![\w$])([^{{;]*)", s)
    if not m:
        return []
    head = m.group(1)
    while _ANGLES.search(head):
        head = _ANGLES.sub("", head)
    ext = re.search(r"\bextends\s+([\w$.,\s]+?)(?=\bimplements\b|\bpermits\b|$)", head)
    imp = re.search(r"\bimplements\s+([\w$.,\s]+?)(?=\bpermits\b|$)", head)
    names = [n for g in (ext, imp) if g for n in g.group(1).split(",")]
    return [n.strip().rsplit(".", 1)[-1] for n in names if n.strip()]


class Inventory:
    """The tests at one rev of ``repo``; see the module docstring for what counts as present."""

    def __init__(self, repo: Path, rev: str = "HEAD") -> None:
        self.repo = repo
        try:
            self.rev = _git(repo, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").strip()
        except RuntimeError as exc:
            raise RuntimeError(f"cannot resolve rev {rev!r} in {repo}: {exc}") from exc
        self.files = frozenset(f for f in _git(repo, "ls-tree", "-r", "-z", "--name-only", self.rev).split("\0") if f)
        if not self.files:
            raise RuntimeError(f"no files at {self.rev[:12]} in {repo}")
        self._src: dict[str, str] = {}
        self._tree: dict[str, ast.Module | None] = {}
        self._decls: dict[str, set[str]] = {}
        self._java: dict[str, list[str]] = {}
        for f in sorted(self.files):
            if f.endswith(".java"):
                self._java.setdefault(f.rsplit("/", 1)[-1][:-5], []).append(f)

    # -- reading ----------------------------------------------------------------------------

    def _source(self, path: str) -> str:
        """The file at the rev; a path the tree lists but git cannot show (a gitlink) raises."""
        if path not in self._src:
            self._src[path] = _git(self.repo, "show", f"{self.rev}:{path}")
        return self._src[path]

    def _ast(self, path: str) -> ast.Module | None:
        if path not in self._tree:
            try:
                self._tree[path] = ast.parse(self._source(path))
            except (SyntaxError, ValueError):
                self._tree[path] = None
        return self._tree[path]

    def _java_names(self, path: str) -> set[str]:
        if path not in self._decls:
            self._decls[path] = _java_decls(self._source(path))
        return self._decls[path]

    def _grep(self, pattern: str, glob: str) -> list[str]:
        """Paths at the rev with a line matching the extended regex ``pattern`` (sorted)."""
        r = subprocess.run(["git", "-C", str(self.repo), "grep", "-l", "-z", "-E", "-e", pattern,
                            self.rev, "--", glob], capture_output=True, check=False)
        if r.returncode not in (0, 1):                  # 1 is "no match"
            raise RuntimeError(f"git grep failed in {self.repo}: {r.stderr.decode('utf-8', 'replace')[:200]}")
        return sorted(f.split(":", 1)[1] for f in r.stdout.decode("utf-8", "replace").split("\0") if f)

    @staticmethod
    def _py_def_pattern(name: str) -> str:
        n = re.escape(name)
        return rf"^[[:space:]]*((async[[:space:]]+)?def[[:space:]]+{n}[[:space:]]*\(|{n}[[:space:]]*(:[^=]*)?=)"

    def _py_class_files(self, cls: str) -> list[str]:
        n = re.escape(cls)
        return self._grep(rf"^[[:space:]]*(class[[:space:]]+{n}([^[:alnum:]_]|$)|{n}[[:space:]]*(:[^=]*)?=)", "*.py")

    # -- python -----------------------------------------------------------------------------

    def _stdlib_base(self, tree: ast.Module, base: ast.expr) -> bool:
        while isinstance(base, ast.Subscript):
            base = base.value
        root = base
        while isinstance(root, ast.Attribute):
            root = root.value
        if not isinstance(root, ast.Name):
            return False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if (a.asname or a.name.split(".")[0]) == root.id:
                        return a.name.split(".")[0] in sys.stdlib_module_names
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if (a.asname or a.name) == root.id:
                        return node.level == 0 and (node.module or "").split(".")[0] in sys.stdlib_module_names
        return isinstance(base, ast.Name) and hasattr(builtins, base.id)

    def _py_find(self, path: str, classes: Sequence[str], name: str, depth: int,
                 seen: set[tuple[str, tuple[str, ...]]]) -> tuple[bool, list[str]]:
        """(found, unresolved notes) for ``name`` in the class chain ``classes`` of ``path`` and its bases."""
        key = (path, tuple(classes))
        if key in seen:
            return False, []
        seen.add(key)
        tree = self._ast(path)
        if tree is None:
            return False, [f"unparseable {path}"]
        notes: list[str] = []
        scopes: list[tuple[Sequence[ast.stmt], ast.ClassDef | None]] = [(tree.body, None)]
        for c in classes:
            nxt: list[tuple[Sequence[ast.stmt], ast.ClassDef | None]] = []
            for body, _ in scopes:
                defs = _classes_in(body, c)
                nxt += [(d.body, d) for d in defs]
                if not defs:
                    notes += _unresolved_binding(body, c, is_class=True, module=body is tree.body)
            scopes = nxt
            if not scopes:
                return False, notes
        if any(_defines(body, name) for body, _ in scopes):
            return True, []
        for body, cls in scopes:
            notes += _unresolved_binding(body, name, is_class=False, module=cls is None)
        for _, cls in scopes:
            if cls is not None:
                found, sub = self._py_bases(tree, path, cls, name, depth, seen)
                if found:
                    return True, []
                notes += sub
        return False, notes

    def _py_bases(self, tree: ast.Module, path: str, cls: ast.ClassDef, name: str, depth: int,
                  seen: set[tuple[str, tuple[str, ...]]]) -> tuple[bool, list[str]]:
        notes: list[str] = []
        for base in cls.bases:
            if self._stdlib_base(tree, base):
                continue
            target = base
            while isinstance(target, ast.Subscript):
                target = target.value
            bname = target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else None
            if bname is None:
                notes.append("unresolved base <expression>")
                continue
            if depth >= MAX_DEPTH:
                notes.append(f"base chain deeper than {MAX_DEPTH} at {cls.name}")
                continue
            homes = [path] if _class_in(tree.body, bname) else self._py_class_files(bname)
            if not homes:
                notes.append(f"unresolved base {bname}")
            for home in homes:
                found, sub = self._py_find(home, [bname], name, depth + 1, seen)
                if found:
                    return True, []
                notes += sub
        return False, notes

    def _py_verdict(self, found: bool, notes: list[str]) -> Verdict:
        if found:
            return None, None
        return (None, ", ".join(sorted(set(notes)))) if notes else ("test gone", None)

    def _judge_python(self, base: str) -> Verdict:
        parts = base.split("::")
        if len(parts) > 1 and parts[0].endswith(".py"):
            if parts[0] not in self.files:
                return "file gone", None
            return self._py_verdict(*self._py_find(parts[0], parts[1:-1], parts[-1], 0, set()))
        *classes, name = base.split(".")
        found, notes = False, []
        homes = self._py_class_files(classes[0]) if classes else self._grep(self._py_def_pattern(name), "*.py")
        for home in homes:
            ok, sub = self._py_find(home, classes, name, 0, set())
            found, notes = found or ok, notes + sub
        return self._py_verdict(found, notes)

    # -- java -------------------------------------------------------------------------------

    def _java_find(self, cls: str, method: str, depth: int, seen: set[str]) -> tuple[bool, list[str]]:
        if cls in seen:
            return False, []
        seen.add(cls)
        notes: list[str] = []
        for path in self._java.get(cls, []):
            if method in self._java_names(path):
                return True, []
            for base in _java_bases(self._source(path), cls):
                if _jdk_type(self._source(path), base):
                    continue
                if depth >= MAX_DEPTH:
                    notes.append(f"base chain deeper than {MAX_DEPTH} at {cls}")
                elif base not in self._java:
                    notes.append(f"unresolved base {base}")
                else:
                    found, sub = self._java_find(base, method, depth + 1, seen)
                    if found:
                        return True, []
                    notes += sub
        return False, notes

    def _judge_java(self, base: str) -> Verdict:
        cls, _, method = base.partition("::")
        if cls not in self._java:
            return "file gone", None
        return self._py_verdict(*self._java_find(cls, method, 0, set()))

    # -- api --------------------------------------------------------------------------------

    def lineage_warning(self) -> str | None:
        """A line when the rev is not an ancestor of ``origin/develop`` (else ``develop``), or neither exists."""
        for ref in ("origin/develop", "develop"):
            tip = subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet", "--end-of-options",
                                  f"{ref}^{{commit}}"], capture_output=True, check=False)
            if tip.returncode != 0:
                continue
            r = subprocess.run(["git", "-C", str(self.repo), "merge-base", "--is-ancestor", self.rev, ref],
                               capture_output=True, check=False)
            if r.returncode not in (0, 1):
                raise RuntimeError(f"git merge-base failed in {self.repo}: {r.stderr.decode('utf-8', 'replace')[:200]}")
            if r.returncode == 1:
                return f"inventory rev {self.rev[:12]} is not an ancestor of {ref}, so it may not survive a merge"
            return None
        return f"neither origin/develop nor develop exists in {self.repo}: cannot tell whether {self.rev[:12]} survives a merge"

    def judge(self, row: Row) -> Verdict:
        """``(absent_reason, present_reason)``.

        ``absent_reason`` is ``"file gone"`` or ``"test gone"`` and then the row is absent;
        ``present_reason`` is set on a present row only when an unresolvable base made it
        "probably present": ``unresolved base <name>``, or ``unresolved binding <form> <name>`` for an
        import, a star import, an assignment of a class name or a def under a loop.
        """
        base = str(row["nodeid"]).partition("[")[0]
        return self._judge_java(base) if row["kind"] == "java" else self._judge_python(base)

    def absent_reason(self, row: Row) -> str | None:
        return self.judge(row)[0]

    def same_name_at_rev(self, row: Row) -> list[str]:
        """Where the row's test name is defined at the rev, for a reader to judge a move; never decides."""
        base = str(row["nodeid"]).partition("[")[0]
        out: set[str] = set()
        if row["kind"] == "java":
            cls, _, method = base.partition("::")
            for path in self._grep(rf"(^|[^[:alnum:]_$]){re.escape(method)}[[:space:]]*\(", "*.java"):
                if method in self._java_names(path):
                    out.add(f"{path.rsplit('/', 1)[-1][:-5]}::{method}")
            for path in self._java.get(cls, []):
                out.update(f"{cls}::{d}" for d in self._java_names(path) if d.startswith(method) and d != method)
        else:
            name = re.split(r"::|\.", base)[-1]
            for path in self._grep(self._py_def_pattern(name), "*.py"):
                tree = self._ast(path)
                if tree is not None:
                    out.update(_py_named(tree.body, name, [path]))
        return sorted(out)[:HINT_CAP]
