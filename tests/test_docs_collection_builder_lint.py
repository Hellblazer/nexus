# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-p2m1x.28: ``src/nexus`` builds a ``docs__<corpus>`` name in one place only.

A corpus-derived docs name is ``docs__<corpus>__<model>__v1``; a corpus shaped like a repo owner segment (``1-20``)
builds the docs collection of the repo whose owner tumbler is ``1.20``. ``nexus.corpus.docs_leaf_fallback_collection_name``
refuses that shape, so it is the only function allowed to put a variable after a literal ``docs__``. A second builder
(``commands/dt.py`` had one) is a second place the refusal is missing.

WHAT IT MATCHES. An f-string whose first part is a literal starting ``docs__`` followed by a formatted value, a
``"docs__" + x`` concatenation, and a ``"docs__%s"`` / ``"docs__{}"`` template, anywhere under ``src/nexus``. The
allowed home is that one function. Non-vacuity: the allowed home must itself be found, or the scan is not reading it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"
_BUILDER = ("corpus.py", "docs_leaf_fallback_collection_name")


def _is_docs_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("docs__")


def _builds_docs_name(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        parts = node.values
        return bool(parts) and _is_docs_literal(parts[0]) and any(isinstance(p, ast.FormattedValue) for p in parts[1:])
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _is_docs_literal(node.left)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.startswith("docs__") and ("%s" in node.value or "{}" in node.value)
    return False


def _sites() -> list[tuple[str, str, int]]:
    """(file relative to src/nexus, enclosing function name or '', line) for every docs-name build."""
    found: list[tuple[str, str, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(_SRC).as_posix()

        def visit(node: ast.AST, fn: str) -> None:
            for child in ast.iter_child_nodes(node):
                inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
                if _builds_docs_name(child):
                    found.append((rel, inner, child.lineno))
                visit(child, inner)

        visit(tree, "")
    return found


def test_only_the_leaf_fallback_builder_puts_a_variable_after_a_literal_docs_prefix():
    sites = _sites()
    home = [s for s in sites if (s[0], s[1]) == _BUILDER]
    assert home, f"non-vacuity: the builder {_BUILDER} was not found among {len(sites)} site(s); the scan is blind"
    stray = [f"src/nexus/{f}:{ln} in {fn or '<module>'}" for f, fn, ln in sites if (f, fn) != _BUILDER]
    assert not stray, (
        "a docs__<variable> name built outside nexus.corpus.docs_leaf_fallback_collection_name, which refuses a corpus "
        "shaped like a repo owner segment (nexus-p2m1x.28); route it through that builder:\n  " + "\n  ".join(stray)
    )
