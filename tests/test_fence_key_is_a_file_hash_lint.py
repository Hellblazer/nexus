# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-w8dzp: the PDF/markdown fence key reaching the writer seams is the source file's sha256.

nexus-ypgbz stamps ``meta.content_hash`` with the fence key at ``doc_indexer._write_chunks_with_owner_rows``
and ``UploadRun.open_writer``, and the exporter reads ``meta.content_hash == index_content_hash`` as proof the
key is a file hash (``key_registered``, the same-as witness). That holds only while every key reaching those two
writers is ``_sha256(<file>)``. This walks the call graph from each writer call in ``src/nexus`` back to where the
key is made: a local assignment must be a ``_sha256(...)`` call and nothing else, and a key that arrives as a
parameter is followed into every caller of that function (``pool.submit(fn, ...)`` included). Anything else, or
a parameter with no caller to follow, fails: a future caller that salts or derives the key turns this red.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
#: The writers that stamp the key as the registration's content hash: name -> (positional index, keyword).
_WRITERS: dict[str, tuple[int | None, str]] = {
    "_write_chunks_with_owner_rows": (2, "content_hash"),
    "open_writer": (None, "content_hash"),
}


def _functions() -> dict[str, list[tuple[Path, ast.FunctionDef]]]:
    out: dict[str, list[tuple[Path, ast.FunctionDef]]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.setdefault(node.name, []).append((path, node))
    return out


def _callee(call: ast.Call) -> tuple[str | None, list[ast.expr]]:
    """The called function's name and its positional arguments, with ``X.submit(fn, *args)`` read as ``fn(*args)``."""
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
    if name == "submit" and call.args and isinstance(call.args[0], ast.Name):
        return call.args[0].id, call.args[1:]
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


def _is_file_hash(expr: ast.expr) -> bool:
    return (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "_sha256"
            and len(expr.args) == 1)


def _call_sites(functions, target: str):
    for name, defs in functions.items():
        for path, fn in defs:
            for call in ast.walk(fn):
                if isinstance(call, ast.Call):
                    callee, args = _callee(call)
                    if callee == target:
                        yield path, fn, call, args


def _trace(functions, path: Path, fn: ast.FunctionDef, expr: ast.expr | None, seen: set, problems: list[str]) -> int:
    """Check that *expr* in *fn* is a file hash; return how many ``_sha256`` origins were reached."""
    where = f"{path.relative_to(_SRC.parents[1])}:{getattr(expr, 'lineno', fn.lineno)} in {fn.name}"
    if expr is None:
        problems.append(f"{where}: the key argument is missing")
        return 0
    if _is_file_hash(expr):
        return 1
    if not isinstance(expr, ast.Name):
        problems.append(f"{where}: the key is {ast.unparse(expr)!r}, not _sha256(<file>) or a name traced to it")
        return 0
    assigned = [n.value for n in ast.walk(fn) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None
                and any(isinstance(t, ast.Name) and t.id == expr.id
                        for t in (n.targets if isinstance(n, ast.Assign) else [n.target]))]
    if assigned:
        return sum(_trace(functions, path, fn, value, seen, problems) for value in assigned)
    params = _params(fn)
    kwonly = [a.arg for a in fn.args.kwonlyargs]
    if expr.id not in params + kwonly:
        problems.append(f"{where}: the key {expr.id!r} is neither assigned here nor a parameter")
        return 0
    if (fn.name, expr.id) in seen:
        return 1
    seen.add((fn.name, expr.id))
    index = params.index(expr.id) if expr.id in params else None
    reached = 0
    for cpath, cfn, call, args in _call_sites(functions, fn.name):
        reached += _trace(functions, cpath, cfn, _argument(call, args, index, expr.id), seen, problems)
    if reached == 0:
        problems.append(f"{where}: the key arrives as parameter {expr.id!r} and no caller passes a file hash")
    return reached


def test_every_fence_key_at_the_writer_seams_is_a_file_sha256() -> None:
    functions = _functions()
    problems: list[str] = []
    sites = 0
    for writer, (index, keyword) in _WRITERS.items():
        for path, fn, call, args in _call_sites(functions, writer):
            sites += 1
            _trace(functions, path, fn, _argument(call, args, index, keyword), set(), problems)
    # Non-vacuity: the four seams counted on 2026-11-04 (three doc_indexer calls of the doc writer, one open_writer).
    assert sites >= 4, f"only {sites} writer call sites found; the walk no longer sees them"
    assert not problems, "\n".join(problems)
