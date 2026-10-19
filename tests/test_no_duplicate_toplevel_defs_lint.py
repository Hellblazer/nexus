# SPDX-License-Identifier: AGPL-3.0-or-later
"""No module in ``src/nexus`` binds one top-level function or class name twice.

A second ``def`` of a name at module level silently replaces the first for every caller, including callers
written above it. On 2026-10-19 nexus-dxsjc added a compact ``_span`` to ``health.py`` 380 lines below the
existing ``_span``, and the reaper and per-collection-index doctor rows began printing ``9.0h`` where they
said ``9 hours``. ruff's F811 does not catch this shape: it fires only when the first definition is unused,
and this one had callers.

What counts as a rebind of a def'd name: another ``def``/``class``, an assignment (``_span = ...``), or an
import (``from x import _span``), directly in the module body; and a second method of one name directly in a
class body. A ``def`` inside ``if``/``try`` (a platform or optional-import variant) is a deliberate
alternative, not a shadow, and so are ``@overload`` stubs and ``@<f>.register`` / ``@<p>.setter``-style
decorated variants, which name the first on purpose.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"

_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _is_variant(node: ast.AST) -> bool:
    """An ``@overload`` stub or a ``@x.register`` / ``@x.setter`` style variant, which reuses a name on purpose."""
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Name) and target.id == "overload":
            return True
        if isinstance(target, ast.Attribute) and target.attr in {"overload", "register", "setter", "getter", "deleter"}:
            return True
    return False


def _bound_names(node: ast.stmt) -> list[str]:
    match node:
        case ast.Assign(targets=targets):
            return [t.id for t in targets if isinstance(t, ast.Name)]
        case ast.AnnAssign(target=ast.Name(id=name), value=value) if value is not None:
            return [name]
        case ast.Import(names=aliases) | ast.ImportFrom(names=aliases):
            return [(a.asname or a.name).split(".")[0] for a in aliases]
    return []


def _dupes_in(body: list[ast.stmt], scope: str) -> list[tuple[str, int, int]]:
    defined: dict[str, int] = {}
    dupes: list[tuple[str, int, int]] = []
    for node in body:
        if isinstance(node, _DEFS):
            if _is_variant(node):
                continue
            if node.name in defined:
                dupes.append((f"{scope}{node.name}", defined[node.name], node.lineno))
            else:
                defined[node.name] = node.lineno
            if isinstance(node, ast.ClassDef):
                dupes.extend(_dupes_in(node.body, f"{scope}{node.name}."))
        elif not scope:
            for name in _bound_names(node):
                if name in defined:
                    dupes.append((name, defined[name], node.lineno))
    return dupes


def duplicate_toplevel_defs(source: str) -> list[tuple[str, int, int]]:
    """``(name, first_line, later_line)`` for each def'd name the module or a class body binds again later."""
    return _dupes_in(ast.parse(source).body, "")


def test_the_check_flags_each_shadowing_shape_and_spares_deliberate_variants() -> None:
    shadow = "def _span(s):\n    return 1\n\ndef row():\n    return _span(1)\n\ndef _span(s):\n    return 2\n"
    assert duplicate_toplevel_defs(shadow) == [("_span", 1, 7)]
    assert duplicate_toplevel_defs("def f(): pass\nf = None\n") == [("f", 1, 2)]
    assert duplicate_toplevel_defs("def f(): pass\nfrom m import f\n") == [("f", 1, 2)]
    assert duplicate_toplevel_defs("class C:\n    def m(self): pass\n    def m(self): pass\n") == [("C.m", 2, 3)]
    guarded = "import sys\nif sys.platform == 'win32':\n    def f(): pass\nelse:\n    def f(): pass\n"
    assert duplicate_toplevel_defs(guarded) == []
    overloads = "from typing import overload\n@overload\ndef f(x: int) -> int: ...\n@overload\ndef f(x: str) -> str: ...\ndef f(x): return x\n"
    assert duplicate_toplevel_defs(overloads) == []
    prop = "class C:\n    @property\n    def v(self): return 1\n    @v.setter\n    def v(self, x): pass\n"
    assert duplicate_toplevel_defs(prop) == []
    assert duplicate_toplevel_defs("x = 1\nx = 2\ndef g(): pass\n") == []


def test_no_module_in_src_redefines_a_top_level_name() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 200, f"scanned only {len(files)} files under {SRC}; the sweep is vacuous"
    found = [
        f"{path.relative_to(SRC.parent)}: {name} bound at line {first} and again at line {second}"
        for path in files
        for name, first, second in duplicate_toplevel_defs(path.read_text(encoding="utf-8"))
    ]
    assert not found, "a later binding replaces the earlier def for every caller:\n" + "\n".join(found)
