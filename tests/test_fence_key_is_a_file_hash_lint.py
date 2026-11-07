# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w8dzp: the PDF/markdown fence key reaching the writer seams is the source file's sha256.

nexus-ypgbz stamps ``meta.content_hash`` with the fence key at ``doc_indexer._write_chunks_with_owner_rows``
and ``UploadRun.open_writer``, and the exporter reads ``meta.content_hash == index_content_hash`` as proof the
key is a file hash (``key_registered``, the same-as witness). That holds only while every key reaching those two
writers is ``doc_indexer._sha256`` of the file being indexed. This walks the call graph from each writer call in
``src/nexus`` back to where the key is made:

* every binding of the key's name in a function (plain, annotated, augmented, tuple, ``for``, ``with``, walrus or
  comprehension) must be a plain ``= _sha256(<p>)`` where ``<p>`` is a parameter of that function rebound, if at
  all, only to the same file (``p.resolve()``, ``Path(p)``): the file the function indexes;
* a key that arrives as a parameter is followed into every caller of that function (``X.submit(fn, ...)`` read as
  ``fn(...)``), and a parameter with no caller to follow fails;
* a writer, or a function on a key's path, referenced other than by a direct call (an alias, ``functools.partial``,
  a thread target) fails, since the walk cannot see through it, and so does a writer called outside any function;
* only the functions in ``_ORIGINS`` may make the key, each from its own file parameter, and a key parameter is
  never rebound;
* ``_sha256`` is defined once in ``src`` and is the file's sha256 (a behavioural check against hashlib), and is bound
  nowhere else: no parameter, ``import ... as``, ``for`` or ``with`` target, or assignment, at module level or in a
  function (``_sha256_strays``);
* a writer or a key-path function fetched by its string (``getattr(m, "n")``, ``m.__getattribute__("n")``) fails, like
  any other reference the walk cannot see through;
* only ``_STAMPERS`` call ``stamp_registered_content_hash``: a call at module level, the stamp fetched by its string,
  imported ``as`` another name, or referenced other than as a direct callee all fail (``_stamp_callers``).

Each check has a source-string fixture at the end of the file that it must catch, so its reach is itself tested.

Anything else turns this red: a future caller that salts, derives or re-points the key.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from nexus.doc_indexer import _sha256

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
#: The writers that stamp the key as the registration's content hash: name -> (positional index, keyword).
_WRITERS: dict[str, tuple[int | None, str]] = {
    "_write_chunks_with_owner_rows": (2, "content_hash"),
    "open_writer": (None, "content_hash"),
}
#: The executor hand-offs read as calls of their first argument.
_SUBMITS = ("submit",)
#: The only functions that make the key, each from the file it indexes. A new place that makes a key is added here by
#: hand, which is where a reviewer sees it.
_ORIGINS: dict[str, str] = {"_index_document": "file_path", "index_pdf": "pdf_path"}
#: The functions that stamp the key onto the registration; anything else calling the stamp is outside this walk.
_STAMPERS = {"_write_chunks_with_owner_rows", "open_writer"}


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(_SRC.rglob("*.py"))]


def _functions(modules) -> dict[str, list[tuple[Path, ast.FunctionDef]]]:
    out: dict[str, list[tuple[Path, ast.FunctionDef]]] = {}
    for path, tree in modules:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, []).append((path, node))
    return out


def _name_of(func: ast.expr) -> str | None:
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _callee(call: ast.Call) -> tuple[str | None, list[ast.expr]]:
    """The called function's name and its positional arguments; ``X.submit(fn, *args)`` reads as ``fn(*args)``."""
    name = _name_of(call.func)
    if name in _SUBMITS and call.args:
        return _name_of(call.args[0]), call.args[1:]
    return name, list(call.args)


def _argument(call: ast.Call, args: list[ast.expr], index: int | None, keyword: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == keyword:
            return kw.value
    if index is not None and index < len(args):
        return args[index]
    return None


def _params(fn: ast.FunctionDef) -> list[str]:
    return [a.arg for a in fn.args.posonlyargs + fn.args.args if a.arg not in ("self", "cls")]


def _binds(target: ast.expr, name: str) -> bool:
    return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target))


def _bindings(fn: ast.AST, name: str) -> list[tuple[ast.AST, ast.expr | None]]:
    """Every binding of *name* in *fn*: (node, the plain value bound, or None when it is not a plain assignment)."""
    out: list[tuple[ast.AST, ast.expr | None]] = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append((n, n.value))
                elif _binds(t, name):
                    out.append((n, None))
        elif isinstance(n, ast.AnnAssign) and _binds(n.target, name):
            out.append((n, n.value if isinstance(n.target, ast.Name) else None))
        elif isinstance(n, ast.NamedExpr) and n.target.id == name:
            out.append((n, n.value))
        elif isinstance(n, (ast.AugAssign, ast.For, ast.AsyncFor, ast.comprehension)) and _binds(n.target, name):
            out.append((n, None))
        elif isinstance(n, (ast.With, ast.AsyncWith)) and any(
                i.optional_vars is not None and _binds(i.optional_vars, name) for i in n.items):
            out.append((n, None))
        elif isinstance(n, ast.ExceptHandler) and n.name == name:
            out.append((n, None))
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and any((a.asname or a.name.split(".")[0]) == name
                                                                 for a in n.names):
            out.append((n, None))
        elif isinstance(n, ast.Delete) and any(_binds(t, name) for t in n.targets):
            out.append((n, None))
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name == name:
            out.append((n, None))
    return out


def _is_file_hash(fn: ast.FunctionDef, expr: ast.expr | None) -> bool:
    """``_sha256(<p>)`` in one of the _ORIGINS, where ``<p>`` is that function's file parameter, rebound if at all
    only to the same file."""
    if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "_sha256"
            and len(expr.args) == 1 and not expr.keywords and isinstance(expr.args[0], ast.Name)):
        return False
    source = expr.args[0].id
    if _ORIGINS.get(fn.name) != source or source not in _params(fn) + [a.arg for a in fn.args.kwonlyargs]:
        return False
    return all(value is not None and _same_file(value, source) for _node, value in _bindings(fn, source))


def _same_file(expr: ast.expr, name: str) -> bool:
    """``<name>.resolve()``, ``Path(<name>)`` or a chain of those: the same file under another spelling."""
    if isinstance(expr, ast.Name):
        return expr.id == name
    if isinstance(expr, ast.Call) and not expr.keywords:
        if isinstance(expr.func, ast.Attribute) and expr.func.attr in ("resolve", "absolute") and not expr.args:
            return _same_file(expr.func.value, name)
        if isinstance(expr.func, ast.Name) and expr.func.id == "Path" and len(expr.args) == 1:
            return _same_file(expr.args[0], name)
    return False


class _Walk:
    def __init__(self, modules) -> None:
        self.functions = _functions(modules)
        self.modules = modules
        self.problems: list[str] = []
        self.done: dict[tuple[Path, str, str], int] = {}
        self.in_progress: set[tuple[Path, str, str]] = set()
        self.on_path: set[str] = set()

    def call_sites(self, target: str):
        done: set[int] = set()
        for defs in self.functions.values():
            for path, fn in defs:
                for call in ast.walk(fn):
                    if isinstance(call, ast.Call) and id(call) not in done:
                        callee, args = _callee(call)
                        if callee == target:
                            done.add(id(call))
                            yield path, fn, call, args

    def trace(self, path: Path, fn: ast.FunctionDef, expr: ast.expr | None) -> int:
        """Check that *expr* in *fn* is the file's hash; return how many ``_sha256`` origins were reached."""
        where = f"{path.relative_to(_SRC.parents[1])}:{getattr(expr, 'lineno', fn.lineno)} in {fn.name}"
        if expr is None:
            self.problems.append(f"{where}: the key argument is missing")
            return 0
        if _is_file_hash(fn, expr):
            return 1
        if not isinstance(expr, ast.Name):
            self.problems.append(f"{where}: the key is {ast.unparse(expr)!r}, not _sha256(<the file parameter>)")
            return 0
        bindings = _bindings(fn, expr.id)
        params_all = _params(fn) + [a.arg for a in fn.args.kwonlyargs]
        if bindings and expr.id in params_all:
            # A key that arrives as a parameter and is also rebound ("compute it if missing") would let a caller's
            # value through on one branch; the walk refuses the shape instead of following both.
            self.problems.append(f"{where}: the key parameter {expr.id!r} is also rebound in {fn.name}")
            return 0
        if bindings:
            reached = 0
            for node, value in bindings:
                if value is None:
                    self.problems.append(f"{where}: {ast.unparse(node).splitlines()[0]!r} rebinds the key")
                elif _is_file_hash(fn, value):
                    reached += 1
                else:
                    self.problems.append(f"{where}: the key is bound to {ast.unparse(value)!r}")
            return reached
        params = _params(fn)
        if expr.id not in params + [a.arg for a in fn.args.kwonlyargs]:
            self.problems.append(f"{where}: the key {expr.id!r} is neither assigned here nor a parameter")
            return 0
        key = (path, fn.name, expr.id)
        if key in self.done:
            return self.done[key]  # reached by another path of a diamond: the same answer
        if key in self.in_progress:
            return 0  # a cycle reaches nothing by itself; the callers outside it must
        self.in_progress.add(key)
        self.on_path.add(fn.name)
        index = params.index(expr.id) if expr.id in params else None
        reached = sum(self.trace(cpath, cfn, _argument(call, args, index, expr.id))
                      for cpath, cfn, call, args in self.call_sites(fn.name))
        self.in_progress.discard(key)
        self.done[key] = reached
        if reached == 0:
            self.problems.append(f"{where}: the key arrives as parameter {expr.id!r} and no caller passes a file hash")
        return reached

    def indirect_references(self, names: set[str]) -> list[str]:
        """References to *names* that are not a direct call nor ``submit``'s first argument, and ``getattr``
        of one by its string name."""
        out: list[str] = []
        for path, tree in self.modules:
            allowed: set[int] = set()
            for call in ast.walk(tree):
                if isinstance(call, ast.Call):
                    allowed.add(id(call.func))
                    if _name_of(call.func) in _SUBMITS and call.args:
                        allowed.add(id(call.args[0]))
            for node in ast.walk(tree):
                name = (node.id if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                        else node.attr if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) else None)
                if name in names and id(node) not in allowed:
                    out.append(f"{path.relative_to(_SRC.parents[1])}:{node.lineno}: {name} is referenced other than "
                               "by a direct call, which the walk cannot follow")
                if isinstance(node, ast.Call) and _reflects(node, names):
                    out.append(f"{path.relative_to(_SRC.parents[1])}:{node.lineno}: a writer or key-path function is "
                               "reached by getattr, which the walk cannot follow")
                if isinstance(node, ast.alias) and node.name in names and node.asname:
                    out.append(f"{path.relative_to(_SRC.parents[1])}: {node.name} is imported as {node.asname}")
        return out


def _writer_problems(modules) -> tuple[int, list[str]]:
    """(writer call sites seen, problems) for *modules*: the whole writer-seam walk."""
    walk = _Walk(modules)
    sites = 0
    for writer, (index, keyword) in _WRITERS.items():
        for path, fn, call, args in walk.call_sites(writer):
            sites += 1
            walk.trace(path, fn, _argument(call, args, index, keyword))
    problems = walk.problems + walk.indirect_references(set(_WRITERS) | walk.on_path)
    # A writer called outside any function (module level, a class body) is not a site the walk saw.
    for path, tree in walk.modules:
        anywhere = sum(1 for c in ast.walk(tree) if isinstance(c, ast.Call) and _callee(c)[0] in _WRITERS)
        in_functions = len({id(c) for fns in walk.functions.values() for fpath, fn in fns if fpath == path
                            for c in ast.walk(fn) if isinstance(c, ast.Call) and _callee(c)[0] in _WRITERS})
        if anywhere and not in_functions >= anywhere:
            problems.append(f"{path.relative_to(_SRC.parents[1])}: a writer is called outside any function")
    return sites, problems


def test_every_fence_key_at_the_writer_seams_is_the_indexed_file_s_sha256() -> None:
    sites, problems = _writer_problems(_modules())
    # Non-vacuity: the four seams counted on 2026-11-04 (three doc_indexer calls of the doc writer, one open_writer).
    assert sites >= 4, f"only {sites} writer call sites found; the walk no longer sees them"
    assert not problems, "\n".join(problems)


def _reflects(call: ast.Call, names) -> bool:
    """``getattr(obj, "<name>")``, ``obj.__getattribute__("<name>")`` or ``object.__getattribute__(obj, "<name>")``
    with a *name* in *names*: the function is fetched by its string, which the walk cannot follow."""
    fn = _name_of(call.func)
    if fn == "getattr":
        candidates = call.args[1:2]
    elif fn == "__getattribute__":
        candidates = call.args[:2]
    else:
        return False
    return any(isinstance(a, ast.Constant) and a.value in names for a in candidates)


def _stamp_callers(modules) -> set[str]:
    """The names of the functions that call the stamp, plus ``<module>:<file>`` for a call outside any function, and
    ``<getattr>``, ``<alias>`` or ``<reference>`` ``:<file>`` for a stamp fetched by its string, imported under another
    name, or named other than as the callee of a direct call: shapes whose caller this scan cannot name."""
    out: set[str] = set()
    for path, tree in modules:
        callees = {id(c.func) for c in ast.walk(tree) if isinstance(c, ast.Call)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _reflects(node, {"stamp_registered_content_hash"}):
                out.add(f"<getattr>:{path.name}")
            elif isinstance(node, ast.alias) and node.name == "stamp_registered_content_hash" and node.asname:
                out.add(f"<alias>:{path.name}")
            elif (isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)
                  and _name_of(node) == "stamp_registered_content_hash" and id(node) not in callees):
                out.add(f"<reference>:{path.name}")
        in_functions: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for c in ast.walk(node):
                    if isinstance(c, ast.Call) and _name_of(c.func) == "stamp_registered_content_hash":
                        in_functions.add(id(c))
                        out.add(node.name)
        if any(isinstance(c, ast.Call) and _name_of(c.func) == "stamp_registered_content_hash"
               and id(c) not in in_functions for c in ast.walk(tree)):
            out.add(f"<module>:{path.name}")
    return out


def test_only_the_two_seams_stamp_the_key() -> None:
    """The walk covers the writers that stamp; a new caller of the stamp is a seam it does not know."""
    callers = _stamp_callers(_modules())
    assert callers == _STAMPERS, callers


def _sha256_strays(modules) -> list[str]:
    """Every way ``_sha256`` is bound in *modules* other than the one def and an import of it from doc_indexer."""
    others: list[str] = []
    for p, tree in modules:
        for n in ast.walk(tree):
            if isinstance(n, ast.arg) and n.arg == "_sha256":
                others.append(f"{p.name}:{n.lineno} (a parameter)")
            elif isinstance(n, ast.ClassDef) and n.name == "_sha256":
                others.append(f"{p.name}:{n.lineno} (a class)")
        # _bindings over the whole module reaches every function, class body and the module level alike.
        for node, _value in _bindings(tree, "_sha256"):
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").endswith("doc_indexer") and not any(
                        a.asname == "_sha256" and a.name != "_sha256" for a in node.names):
                    continue  # importing the one def is fine
            others.append(f"{p.name}:{getattr(node, 'lineno', 0)}")
    return others


def test_sha256_is_defined_once_and_hashes_the_file(tmp_path: Path) -> None:
    """The name the walk accepts must be the one function, and it must be the file's sha256."""
    defs = [(p, n) for p, tree in _modules() for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_sha256"]
    assert [p.name for p, _ in defs] == ["doc_indexer.py"], defs
    # Nor is the name bound any other way (a lambda, a parameter, an import as, a for or with target).
    others = _sha256_strays(_modules())
    assert not others, others
    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 300)
    assert _sha256(sample) == hashlib.sha256(sample.read_bytes()).hexdigest()


# --- self-tests: each shape the walk must catch, on a source string, so the lint's reach is itself tested ---------------

_CLEAN = '''
def _index_document(file_path):
    key = _sha256(file_path)
    _write_chunks_with_owner_rows(1, 2, key)
'''


def _fixture(source: str):
    return [(_SRC / "fixture.py", ast.parse(source))]


def test_the_clean_fixture_passes_every_check() -> None:
    modules = _fixture(_CLEAN)
    sites, problems = _writer_problems(modules)
    assert (sites, problems) == (1, [])
    assert _sha256_strays(modules) == []
    assert _stamp_callers(modules) == set()


def test_a_writer_reached_by_getattr_is_caught() -> None:
    source = _CLEAN + '''
def sneaky(mod):
    getattr(mod, "_write_chunks_with_owner_rows")(1, 2, "salted")
'''
    _sites, problems = _writer_problems(_fixture(source))
    assert any("getattr" in p for p in problems), problems


@pytest.mark.parametrize("source", [
    "def f(_sha256):\n    return _sha256\n",  # a parameter
    "def f(*, _sha256=None):\n    return _sha256\n",  # a keyword-only parameter
    "def f(*_sha256):\n    return _sha256\n",  # a vararg
    "import hashlib as _sha256\n",  # import as, at module level
    "def f():\n    import hashlib as _sha256\n    return _sha256\n",  # import as, in a function
    "def f(xs):\n    for _sha256 in xs:\n        pass\n",  # a for target
    "def f(p):\n    with open(p) as _sha256:\n        pass\n",  # a with target
    "def f():\n    _sha256 = lambda p: 'x'\n",  # an assignment
], ids=["param", "kwonly", "vararg", "import-as-module", "import-as-fn", "for", "with", "lambda"])
def test_a_stray_binding_of_sha256_is_caught(source: str) -> None:
    assert _sha256_strays(_fixture(source)), source


def test_importing_the_one_def_is_not_a_stray() -> None:
    assert _sha256_strays(_fixture("from nexus.doc_indexer import _sha256\n")) == []


def test_a_module_level_stamp_call_is_caught() -> None:
    source = _CLEAN + "\nstamp_registered_content_hash(1, 2)\n"
    assert _stamp_callers(_fixture(source)) == {"<module>:fixture.py"}


def test_a_writer_reached_by_dunder_getattribute_is_caught() -> None:
    source = _CLEAN + '''
def sneaky(mod):
    mod.__getattribute__("_write_chunks_with_owner_rows")(1, 2, "salted")
'''
    _sites, problems = _writer_problems(_fixture(source))
    assert any("getattr" in p for p in problems), problems


def test_a_stamp_reached_by_getattr_is_caught() -> None:
    source = 'def sneaky(mod):\n    getattr(mod, "stamp_registered_content_hash")(1, 2)\n'
    assert _stamp_callers(_fixture(source)) == {"<getattr>:fixture.py"}


def test_a_stamp_reached_by_dunder_getattribute_is_caught() -> None:
    source = 'def sneaky(mod):\n    mod.__getattribute__("stamp_registered_content_hash")(1, 2)\n'
    assert _stamp_callers(_fixture(source)) == {"<getattr>:fixture.py"}


def test_a_stamp_imported_as_an_alias_and_called_is_caught() -> None:
    source = ("from nexus.x import stamp_registered_content_hash as stamp\n\n"
              "def sneaky():\n    stamp(1, 2)\n")
    assert _stamp_callers(_fixture(source)) == {"<alias>:fixture.py"}


def test_a_stamp_referenced_without_a_call_is_caught() -> None:
    source = "def sneaky(mod):\n    fn = mod.stamp_registered_content_hash\n    fn(1, 2)\n"
    assert _stamp_callers(_fixture(source)) == {"<reference>:fixture.py"}
