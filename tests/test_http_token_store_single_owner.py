# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every ``HttpTokenStore`` in ``src/nexus`` is owned by one ``with`` block.

``HttpTokenStore._replace_client`` closes the old client at once and its heal
paths set the token or URL before the new client is built, with no lock. The
T1 scratch store needs parking, a lock and a rollback for the same swaps
because it is a process-wide singleton used from many threads (2026-11-21).
The token store gets away without them only because each caller owns it for
one ``with HttpTokenStore(...) as store:`` block in one thread. This pins that
contract: a construction outside a ``with`` item (stored on an object, kept in
a module global, handed to another thread) fails here, and the store then
needs the scratch store's treatment first.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"
#: Constructions found when this was written (2026-11-23): 12.
MIN_SITES = 10


def _constructions() -> list[tuple[str, int, bool]]:
    """``(path, line, owned_by_a_with_block)`` for every ``HttpTokenStore(...)`` call."""
    found: list[tuple[str, int, bool]] = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "http_token_store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        with_items = {
            id(item.context_expr)
            for node in ast.walk(tree) if isinstance(node, (ast.With, ast.AsyncWith))
            for item in node.items
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "HttpTokenStore":
                found.append((path.relative_to(SRC).as_posix(), node.lineno, id(node) in with_items))
    return found


def test_the_scan_is_not_vacuous() -> None:
    assert len(_constructions()) >= MIN_SITES


def test_every_token_store_is_owned_by_one_with_block() -> None:
    loose = [f"{path}:{line}" for path, line, owned in _constructions() if not owned]
    assert not loose, (
        "HttpTokenStore built outside a `with` block. Its client swaps assume one owner in one thread "
        "(see HttpTokenStore._replace_client); give it HttpScratchStore's parking and rollback first:\n  "
        + "\n  ".join(loose)
    )
