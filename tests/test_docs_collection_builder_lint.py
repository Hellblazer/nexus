# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-p2m1x.28: ``src/nexus`` builds a ``docs__<corpus>`` name in one place only.

A corpus-derived docs name is ``docs__<corpus>__<model>__v1``; a corpus shaped like a repo owner segment (``1-20``)
builds the docs collection of the repo whose owner tumbler is ``1.20``. ``nexus.corpus.docs_leaf_fallback_collection_name``
refuses that shape, so it is the only function allowed to put a variable after a literal ``docs__``. A second builder
(``commands/dt.py`` had one) is a second place the refusal is missing.

WHAT IT MATCHES. Anywhere under ``src/nexus``: an f-string with a literal part ending in ``docs__`` that a formatted
value follows (first or not), a ``"docs__" + x`` concatenation or ``%`` format, a ``"docs__%s"`` / ``"docs__{}"`` /
``"docs__{corpus}".format(...)`` template, and ``"__".join(["docs", ...])``. The allowed home is that one function; the
refusal's own message (``owner_segment_guard.require_corpus_name``) names the collection it refuses and builds nothing.
Non-vacuity: the allowed home must itself be found, or the scan is not reading it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"
_BUILDER = ("corpus.py", "docs_leaf_fallback_collection_name")
# The refusal's own message names the docs collection it refuses (``docs__{segment}__<model>__v1``): text, no name built.
_MESSAGE_SITES = {("owner_segment_guard.py", "require_corpus_name")}


def _is_docs_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("docs__")


def _builds_docs_name(node: ast.AST) -> bool:
    if isinstance(node, ast.JoinedStr):
        # An f-string with a literal part ENDING in ``docs__`` that a formatted value follows, wherever in it.
        parts = node.values
        return any(
            isinstance(p, ast.Constant) and isinstance(p.value, str) and p.value.endswith("docs__")
            and i + 1 < len(parts) and isinstance(parts[i + 1], ast.FormattedValue)
            for i, p in enumerate(parts)
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _is_docs_literal(node.left)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # A template: ``"docs__%s"``, ``"docs__%(c)s"``, ``"docs__{}"``, ``"docs__{corpus}".format(...)``.
        return node.value.startswith("docs__") and any(t in node.value for t in ("%s", "%(", "{"))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "join":
        # ``"__".join(["docs", corpus, ...])``: a list or tuple whose first element is the literal ``docs``.
        sep, args = node.func.value, node.args
        return (
            isinstance(sep, ast.Constant) and sep.value == "__" and len(args) == 1
            and isinstance(args[0], (ast.List, ast.Tuple)) and bool(args[0].elts)
            and isinstance(args[0].elts[0], ast.Constant) and args[0].elts[0].value == "docs"
        )
    return False


def _sites_in(source: str, rel: str) -> list[tuple[str, str, int]]:
    """(file, enclosing function name or '', line) for every docs-name build in *source*."""
    found: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if _builds_docs_name(child):
                found.append((rel, inner, child.lineno))
            visit(child, inner)

    visit(ast.parse(source), "")
    return found


def _sites() -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for path in sorted(_SRC.rglob("*.py")):
        found += _sites_in(path.read_text(encoding="utf-8"), path.relative_to(_SRC).as_posix())
    return found


@pytest.mark.parametrize(
    "source",
    [
        'x = f"docs__{c}__v1"',
        'x = f"{p}docs__{c}"',
        'x = "docs__" + c',
        'x = "docs__%s__v1" % c',
        'x = "docs__{}__v1".format(c)',
        'x = "docs__{corpus}__v1".format(corpus=c)',
        'x = "__".join(["docs", c, m])',
        'x = "__".join(("docs", c))',
    ],
)
def test_the_scan_sees_every_shape_it_claims_to(source):
    assert _sites_in(source, "x.py"), source


@pytest.mark.parametrize(
    "source",
    ['x = "docs__default"', 'x = f"docs__only"', 'x = "__".join(["code", c])', 'x = "__".join(parts)',
     'x = "{}__v1".format(c)'],
)
def test_the_scan_leaves_a_fixed_name_and_other_joins_alone(source):
    assert not _sites_in(source, "x.py"), source


def test_only_the_leaf_fallback_builder_puts_a_variable_after_a_literal_docs_prefix():
    sites = _sites()
    home = [s for s in sites if (s[0], s[1]) == _BUILDER]
    assert home, f"non-vacuity: the builder {_BUILDER} was not found among {len(sites)} site(s); the scan is blind"
    stray = [f"src/nexus/{f}:{ln} in {fn or '<module>'}" for f, fn, ln in sites if (f, fn) != _BUILDER and (f, fn) not in _MESSAGE_SITES]
    assert not stray, (
        "a docs__<variable> name built outside nexus.corpus.docs_leaf_fallback_collection_name, which refuses a corpus "
        "shaped like a repo owner segment (nexus-p2m1x.28); route it through that builder:\n  " + "\n  ".join(stray)
    )
