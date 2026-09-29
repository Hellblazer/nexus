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
    slug = "code__arcaneum-2ad2825c__voyage-code-3__v1"
    sibling = "docs__arcaneum-2ad2825c__voyage-context-3__v1"
    assert _sibling_names(monkeypatch, slug, [sibling], row_owner="arcaneum-2ad2825c") == [sibling]
    assert _sibling_names(monkeypatch, slug, [sibling], row_owner="1-15") == []


def test_nothing_in_src_calls_list_sibling_collections() -> None:
    """The reason the loss above is harmless. A new caller must decide the sibling rule."""
    callers: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
                if name == "list_sibling_collections":
                    callers.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert callers == [], (
        f"list_sibling_collections has callers now: {callers}. nexus-6pbwx made its "
        "row-owner match miss a repaired slug collection's slug siblings; decide the rule "
        "for the new caller (see the function's docstring), then update this pin."
    )
