# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 Phase 3 Step 2 (nexus-z0o2p.25): the dead chunk-write paths stay deleted.

The engine refuses an ownerless chunk write from P3.2, and these paths wrote chunks through
``upsert-chunks`` (or a Chroma-shaped ``col.upsert``/``col.add`` the service collection stub does
not have) with no owner row, from code no ``src``, ``scripts``, ``tests/e2e`` or ``conexus`` caller
reached:

* ``db/reconcile.verify_fill_collections`` / ``verify_fill_pg_source`` and their worker;
* ``db/embed_migrate``;
* ``nx t3 reidentify`` and ``db/t3_reidentify`` (its ``col.upsert`` is not on ``_ServiceCollectionStub``,
  so ``--no-dry-run`` raised ``AttributeError`` on every real install);
* ``scripts/migrate_art_papers.py`` (a one-off over the retired SQLite T2/catalog files and ``col.add``).

A pin that cannot fail proves nothing: each assertion below fails with the deleted file or symbol put back.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "nexus"

_DELETED_PATHS = (
    "src/nexus/db/embed_migrate.py",
    "src/nexus/db/t3_reidentify.py",
    "scripts/migrate_art_papers.py",
    "tests/test_embed_migrate.py",
    "tests/test_t3_reidentify.py",
    "tests/migration/test_vector_etl_pg_source.py",
)

_DELETED_RECONCILE_SYMBOLS = frozenset({
    "verify_fill_collections", "verify_fill_pg_source", "_verify_fill_one",
    "resolve_local_service_endpoint",
})

_DELETED_MODULES = ("nexus.db.embed_migrate", "nexus.db.t3_reidentify")


@pytest.mark.parametrize("rel", _DELETED_PATHS)
def test_deleted_file_stays_deleted(rel: str) -> None:
    assert not (REPO / rel).exists(), f"{rel} came back; RDR-223 P3.3 deleted it as a dead chunk-write path"


def test_reconcile_has_no_verify_fill_machinery() -> None:
    tree = ast.parse((SRC / "db" / "reconcile.py").read_text(encoding="utf-8"))
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    assert not (defined & _DELETED_RECONCILE_SYMBOLS), sorted(defined & _DELETED_RECONCILE_SYMBOLS)
    # The survivors the P4b gate pins, so a "delete the module" reading of this bead fails here too.
    assert {"iter_collection_chunks", "list_collection_names", "dim_for_model_token"} <= defined


_DELETED_LEAVES = tuple(m.rsplit(".", 1)[1] for m in _DELETED_MODULES)


def _imports_a_deleted_module(node: ast.AST) -> bool:
    """Absolute imports, RELATIVE imports (``from .embed_migrate import x``, ``from . import
    t3_reidentify``) and ``importlib.import_module("nexus.db.embed_migrate")`` / ``__import__`` strings."""
    if isinstance(node, ast.Import):
        names = [a.name for a in node.names]
        return any(n == m or n.startswith(m + ".") for n in names for m in _DELETED_MODULES)
    if isinstance(node, ast.ImportFrom):
        if node.level:
            leaves = ([node.module.split(".")[0]] if node.module else []) + [a.name for a in node.names]
            return any(leaf in _DELETED_LEAVES for leaf in leaves)
        if node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            return any(n == m or n.startswith(m + ".") for n in names for m in _DELETED_MODULES)
        return False
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name in ("import_module", "__import__"):
            return any(
                isinstance(a, ast.Constant) and isinstance(a.value, str)
                and any(a.value == m or a.value.startswith(m + ".") or a.value in _DELETED_LEAVES
                        for m in _DELETED_MODULES)
                for a in node.args
            )
    return False


def test_nothing_imports_the_deleted_modules() -> None:
    offenders: list[str] = []
    for root in (SRC, REPO / "scripts", REPO / "tests"):
        for py in root.rglob("*.py"):
            if py == Path(__file__):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            offenders.extend(f"{py.relative_to(REPO)}:{n.lineno}" for n in ast.walk(tree) if _imports_a_deleted_module(n))
    assert not offenders, offenders


@pytest.mark.parametrize("source", [
    "import nexus.db.embed_migrate",
    "from nexus.db import t3_reidentify",
    "from .embed_migrate import migrate_collection_safe",
    "from . import t3_reidentify",
    "import importlib; importlib.import_module('nexus.db.t3_reidentify')",
    "__import__('nexus.db.embed_migrate')",
])
def test_the_import_scan_sees_every_spelling(source: str) -> None:
    """The scan's own kill control: each spelling below must be caught, or the pin above is vacuous."""
    assert any(_imports_a_deleted_module(n) for n in ast.walk(ast.parse(source))), source


def test_t3_group_has_no_reidentify_command() -> None:
    from nexus.commands.t3 import t3

    assert "reidentify" not in t3.commands, sorted(t3.commands)
    assert t3.commands, "non-vacuity: the t3 group still registers its other verbs"
