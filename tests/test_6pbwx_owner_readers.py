# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-6pbwx: readers that used to see a repo collection's name-derived owner.

The engine now rewrites a legacy slug-named code/docs/rdr collection's
``owner_id`` to its documents' owner segment. ``list_sibling_collections``
matches ``__<row owner_id>__`` inside collection names, so a repaired slug
collection no longer finds its slug-named siblings. That is harmless only
while nothing calls it; this pins both facts.
"""
from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock

SRC = pathlib.Path(__file__).parent.parent / "src" / "nexus"


def _sibling_names(monkeypatch, name: str, others: list[str], row_owner: str) -> list[str]:
    monkeypatch.setattr(
        "nexus.mcp_infra.get_collection_row",
        lambda n: {"name": n, "owner_id": row_owner, "content_type": "code"},
    )
    colls = []
    for n in others:
        m = MagicMock()
        m.name = n
        colls.append(m)
    monkeypatch.setattr("nexus.db.http_vector_client.live_collection_rows", lambda _client: colls)
    from nexus.repo_identity import list_sibling_collections

    return list_sibling_collections(name, MagicMock())


def test_a_repaired_slug_collection_no_longer_matches_its_slug_siblings(monkeypatch) -> None:
    """The behaviour the docstring records. If this starts failing because the matcher
    learned the name's segment too, update the docstring and drop the caller pin below."""
    slug = "code__arcaneum-2ad2825c__bge-base-en-v15-768__v1"
    sibling = "docs__arcaneum-2ad2825c__bge-base-en-v15-768__v1"
    assert _sibling_names(monkeypatch, slug, [sibling], row_owner="arcaneum-2ad2825c") == [sibling]
    assert _sibling_names(monkeypatch, slug, [sibling], row_owner="1-15") == []


#: The only files allowed to mention the function: its definition, and the
#: registry's re-export (an import plus an ``__all__`` string, neither a use).
_ALLOWED_MENTIONS = {"repo_identity.py", "registry.py"}
_NAME = "list_sibling_collections"


def _mentions(tree: ast.AST) -> list[int]:
    """Line numbers of every way source can reach the function: a call, a bare name,
    an attribute (``repo_identity.list_sibling_collections``) or a from-import alias."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == _NAME:
            lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == _NAME:
            lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            lines.extend(node.lineno for a in node.names if a.name == _NAME)
    return lines


def test_nothing_in_src_references_list_sibling_collections() -> None:
    """The reason the loss above is harmless. A new caller, or a new alias for the
    function, must decide the sibling rule."""
    refs: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name in _ALLOWED_MENTIONS:
            continue
        for line in _mentions(ast.parse(path.read_text(encoding="utf-8"))):
            refs.append(f"{path.relative_to(SRC)}:{line}")
    assert refs == [], (
        f"list_sibling_collections is referenced now: {refs}. nexus-6pbwx made its "
        "row-owner match miss a repaired slug collection's slug siblings; decide the rule "
        "for the new caller (see the function's docstring), then update this pin."
    )


def test_the_reference_scan_sees_calls_attributes_and_imports() -> None:
    """Non-vacuity: the scan flags each shape it claims to, so a clean tree means something."""
    for src in (
        "list_sibling_collections(a, b)",
        "mod.list_sibling_collections",
        "from nexus.repo_identity import list_sibling_collections",
        "from nexus.repo_identity import list_sibling_collections as lsc",
    ):
        assert _mentions(ast.parse(src)), src
    assert _mentions(ast.parse("something_else(x)")) == []
    # registry.py re-exports the function lazily, by string only (module __getattr__ and
    # __all__), which the scan deliberately ignores; the allow-list entry is real.
    registry = (SRC / "registry.py").read_text(encoding="utf-8")
    assert f'"{_NAME}"' in registry
    assert _mentions(ast.parse(registry)) == []
