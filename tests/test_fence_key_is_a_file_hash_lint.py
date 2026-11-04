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
  a thread target) fails, since the walk cannot see through it;
* ``_sha256`` is defined once in ``src`` and is the file's sha256 (a behavioural check against hashlib).

Anything else turns this red: a future caller that salts, derives or re-points the key.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
#: The writers that stamp the key as the registration's content hash: name -> (positional index, keyword).
_WRITERS: dict[str, tuple[int | None, str]] = {
    "_write_chunks_with_owner_rows": (2, "content_hash"),
    "open_writer": (None, "content_hash"),
}
#: The executor hand-offs read as calls of their first argument.
_SUBMITS = ("submit",)


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


def _bindings(fn: ast.FunctionDef, name: str) -> list[tuple[ast.AST, ast.expr | None]]:
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
    return out


def _is_file_hash(fn: ast.FunctionDef, expr: ast.expr | None) -> bool:
    """``_sha256(<p>)`` where ``<p>`` is a parameter of *fn* that *fn* never rebinds: the file it indexes."""
    if not (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "_sha256"
            and len(expr.args) == 1 and not expr.keywords and isinstance(expr.args[0], ast.Name)):
        return False
    source = expr.args[0].id
    if source not in _params(fn) + [a.arg for a in fn.args.kwonlyargs]:
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
        self.seen: set[tuple[Path, str, str]] = set()
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
        if key in self.seen:
            return 1
        self.seen.add(key)
        self.on_path.add(fn.name)
        index = params.index(expr.id) if expr.id in params else None
        reached = sum(self.trace(cpath, cfn, _argument(call, args, index, expr.id))
                      for cpath, cfn, call, args in self.call_sites(fn.name))
        if reached == 0:
            self.problems.append(f"{where}: the key arrives as parameter {expr.id!r} and no caller passes a file hash")
        return reached

    def indirect_references(self, names: set[str]) -> list[str]:
        """References to *names* that are not a direct call nor ``submit``'s first argument."""
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
                if isinstance(node, ast.alias) and node.name in names and node.asname:
                    out.append(f"{path.relative_to(_SRC.parents[1])}: {node.name} is imported as {node.asname}")
        return out


def test_every_fence_key_at_the_writer_seams_is_the_indexed_file_s_sha256() -> None:
    walk = _Walk(_modules())
    sites = 0
    for writer, (index, keyword) in _WRITERS.items():
        for path, fn, call, args in walk.call_sites(writer):
            sites += 1
            walk.trace(path, fn, _argument(call, args, index, keyword))
    # Non-vacuity: the four seams counted on 2026-11-04 (three doc_indexer calls of the doc writer, one open_writer).
    assert sites >= 4, f"only {sites} writer call sites found; the walk no longer sees them"
    problems = walk.problems + walk.indirect_references(set(_WRITERS) | walk.on_path)
    assert not problems, "\n".join(problems)


def test_sha256_is_defined_once_and_hashes_the_file(tmp_path: Path) -> None:
    """The name the walk accepts must be the one function, and it must be the file's sha256."""
    defs = [(p, n) for p, tree in _modules() for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_sha256"]
    assert [p.name for p, _ in defs] == ["doc_indexer.py"], defs
    from nexus.doc_indexer import _sha256

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 300)
    assert _sha256(sample) == hashlib.sha256(sample.read_bytes()).hexdigest()
