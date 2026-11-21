# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-229 P2.7 (nexus-p2m1x.12): ``list_sibling_collections`` is gone.

nexus-6pbwx showed its name matcher (``__<row owner_id>__`` inside collection names,
or a ``-<hash8>`` suffix) missed a repaired slug collection's slug siblings, and the
pin here kept that harmless by checking nothing in ``src`` called it. RDR-229 deletes
the function instead: nothing called it, and siblings are found by row tuple
(``(content_type, owner_id)`` through the catalog list route, RDR-210). This file pins
that it stays deleted: no definition, no re-export, no reference anywhere in ``src``.
"""
from __future__ import annotations

import ast
import pathlib

import nexus.registry as registry
import nexus.repo_identity as repo_identity

SRC = pathlib.Path(__file__).parent.parent / "src" / "nexus"

_NAME = "list_sibling_collections"


def _mentions(tree: ast.AST) -> list[int]:
    """Line numbers of every way source can reach the function: a call, a bare name,
    an attribute (``repo_identity.list_sibling_collections``), a from-import alias or
    a ``def`` of that name."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == _NAME:
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == _NAME:
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            lines.extend(node.lineno for a in node.names if a.name == _NAME)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == _NAME:
            lines.append(node.lineno)
    return lines


def test_the_function_is_gone_from_repo_identity() -> None:
    assert not hasattr(repo_identity, _NAME)
    assert _NAME not in repo_identity.__all__


def test_the_registry_shim_no_longer_names_it() -> None:
    assert _NAME not in registry._RELOCATED_HELPERS
    assert _NAME not in registry.__all__
    # The shim's __getattr__ must not forward the name to anything.
    try:
        getattr(registry, _NAME)
    except AttributeError:
        pass
    else:  # pragma: no cover - the failure path
        raise AssertionError(f"nexus.registry still resolves {_NAME}")


def test_nothing_in_src_references_list_sibling_collections() -> None:
    """No definition, call, import, attribute or string re-export remains in ``src``."""
    refs: list[str] = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in _mentions(ast.parse(text)):
            refs.append(f"{path.relative_to(SRC)}:{line}")
        # A string mention (an ``__all__`` entry, a shim set) is the shape the AST scan
        # ignores, and the one the registry shim used.
        for no, raw in enumerate(text.splitlines(), 1):
            if f'"{_NAME}"' in raw or f"'{_NAME}'" in raw:
                refs.append(f"{path.relative_to(SRC)}:{no} (string)")
    assert refs == [], (
        f"{_NAME} is referenced again: {refs}. RDR-229 P2.7 deleted it; siblings are "
        "found by row tuple (RDR-210), not by a name matcher."
    )


def test_the_reference_scan_sees_calls_attributes_imports_and_definitions() -> None:
    """Non-vacuity: the scan flags each shape it claims to, so a clean tree means something."""
    for src in (
        "list_sibling_collections(a, b)",
        "mod.list_sibling_collections",
        "from nexus.repo_identity import list_sibling_collections",
        "from nexus.repo_identity import list_sibling_collections as lsc",
        "def list_sibling_collections(a): pass",
    ):
        assert _mentions(ast.parse(src)), src
    assert _mentions(ast.parse("something_else(x)")) == []
    # The src walk must actually see files, or a clean result proves nothing.
    assert len(list(SRC.rglob("*.py"))) > 100
