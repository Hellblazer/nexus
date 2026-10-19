# SPDX-License-Identifier: AGPL-3.0-or-later
"""No module in ``src/nexus`` defines one top-level function or class name twice.

A second ``def`` of a name at module level silently replaces the first for every caller, including callers
written above it. On 2026-10-19 nexus-dxsjc added a compact ``_span`` to ``health.py`` 380 lines below the
existing ``_span``, and the reaper and per-collection-index doctor rows began printing ``9.0h`` where they
said ``9 hours``. ruff's F811 does not catch this shape: it fires only when the first definition is unused,
and this one had callers.

Only statements directly in the module body count; a ``def`` inside ``if``/``try`` (a platform or
optional-import variant) is a deliberate alternative, not a shadow.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"


def duplicate_toplevel_defs(source: str) -> list[tuple[str, int, int]]:
    """``(name, first_line, second_line)`` for each top-level def/class name defined again later."""
    seen: dict[str, int] = {}
    dupes: list[tuple[str, int, int]] = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name in seen:
                dupes.append((node.name, seen[node.name], node.lineno))
            else:
                seen[node.name] = node.lineno
    return dupes


def test_the_check_flags_a_shadowing_def_and_spares_a_guarded_variant() -> None:
    shadow = "def _span(s):\n    return 1\n\ndef row():\n    return _span(1)\n\ndef _span(s):\n    return 2\n"
    assert duplicate_toplevel_defs(shadow) == [("_span", 1, 7)]
    guarded = "import sys\nif sys.platform == 'win32':\n    def f(): pass\nelse:\n    def f(): pass\n"
    assert duplicate_toplevel_defs(guarded) == []


def test_no_module_in_src_redefines_a_top_level_name() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 200, f"scanned only {len(files)} files under {SRC}; the sweep is vacuous"
    found = [
        f"{path.relative_to(SRC.parent)}: {name} defined at line {first} and again at line {second}"
        for path in files
        for name, first, second in duplicate_toplevel_defs(path.read_text(encoding="utf-8"))
    ]
    assert not found, "a later top-level def replaces the earlier one for every caller:\n" + "\n".join(found)
