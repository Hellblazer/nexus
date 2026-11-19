# SPDX-License-Identifier: AGPL-3.0-or-later
"""The walk-site ratchet (RDR-241 Phase 1 Step 1, nexus-uwggq.1.1).

``src/nexus/tables/walk-sites.toml`` lists every function in ``src/nexus`` that
calls ``all_documents``, with how many calls it holds. This test parses every
``src/nexus/**/*.py`` with ``ast`` and holds the source and the table to an
exact match, so the count of whole-catalog walks cannot rise without an edit to
the table in the same change (the table is the review point; the lint runs after
a push to develop, so it records the rise rather than refusing it): a
call in an unlisted function, a listed function with no call, a ``calls`` value
that differs from the scan (up or down), and a ``class`` outside the closed set
each fail.

What the scan counts: an ``ast.Call`` whose callee is an attribute named
``all_documents`` (``cat.all_documents()``) or a bare name ``all_documents``.
``enumerate_all_documents`` and every other name are not counted; neither are
docstrings, comments or a ``def all_documents``. A call is keyed to the dotted
chain of its enclosing ``ClassDef`` / ``FunctionDef`` / ``AsyncFunctionDef``
names (``ManifestSectionLabelsRung._scan``; a nested function is
``outer.inner``); a call outside any function is keyed ``<module>``.

Blind spots, stated (RDR-241 section "The ratchet"):

- Only a call whose callee is ``all_documents`` is counted. A call through an
  alias or ``getattr`` (``f = cat.all_documents; f()``,
  ``getattr(cat, "all_documents")()``, ``from m import all_documents as ad``) and
  a bound method passed rather than called (``asyncio.to_thread(cat.all_documents,
  ...)``, ``functools.partial``, an executor ``submit``) are invisible.
- The scan counts calls, not their arguments: an existing call edited from a page
  (``limit=n``) to a whole read (no limit, or ``limit=0``) keeps its count, so the
  BOUNDED class is a label, not a check.
- A new caller of a helper that wraps a walk is not seen. The three wrappers
  (``enumerate_all_documents``, ``_all_entries``, ``_entries_of_type``) are
  listed as sites and their reasons name
  today's callers, but nothing checks those names: a new caller rests on review.
  The same holds for any other listed helper a new function calls.
- Whole-tenant shapes that are not ``all_documents`` calls are outside this
  scanner: ``find_all``'s grow-until-short loop, ``link_audit``'s page loop,
  ``bulk_unlink``'s dry-run count, and the read-then-slice of
  ``by_content_type``, ``by_corpus`` and an unscoped ``by_owner`` in
  ``catalog_list`` and ``catalog_search``.

A file that fails to parse is a failure, never a skip, and a scan that examines
no file or finds no call fails (the nexus-moht0 rule).
"""
from __future__ import annotations

import ast
import functools
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import pytest

from nexus.tables import walk_sites
from nexus.tables.load import load_table
from nexus.tables.walk_sites import CLASSES, WalkSite

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"
CALLEE = "all_documents"
MODULE_LEVEL = "<module>"


@dataclass(frozen=True)
class Scan:
    files: int
    parse_failures: tuple[str, ...]
    #: site key -> the ``path:line`` of each call in it, in source order
    calls: dict[str, tuple[str, ...]]

    @property
    def call_count(self) -> int:
        return sum(len(v) for v in self.calls.values())


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.stack: list[str] = []
        self.found: list[tuple[str, int]] = []

    def _scope(self) -> str:
        return ".".join(self.stack) if self.stack else MODULE_LEVEL

    def _enter(self, node: ast.AST, name: str, outside: Iterable[ast.AST]) -> None:
        # Decorators, defaults and base classes are evaluated in the enclosing scope.
        for child in outside:
            self.visit(child)
        self.stack.append(name)
        for child in node.body:  # type: ignore[attr-defined]
            self.visit(child)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        outside = [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]
        self._enter(node, node.name, outside)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        outside = [
            *node.decorator_list,
            *args.defaults,
            *(d for d in args.kw_defaults if d is not None),
        ]
        self._enter(node, node.name, outside)

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == CALLEE) or (
            isinstance(func, ast.Name) and func.id == CALLEE
        ):
            self.found.append((self._scope(), node.lineno))
        self.generic_visit(node)


def scan_tree(root: Path) -> Scan:
    """Parse every ``*.py`` under *root*; key each ``all_documents`` call by
    ``<path under root>::<qualified function>``."""
    files = 0
    failures: list[str] = []
    calls: dict[str, list[str]] = {}
    for path in sorted(root.rglob("*.py")):
        files += 1
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            failures.append(f"{rel}: {type(exc).__name__}: {exc}")
            continue
        visitor = _Visitor(rel)
        visitor.visit(tree)
        for scope, line in visitor.found:
            calls.setdefault(f"{rel}::{scope}", []).append(f"{rel}:{line}")
    return Scan(files=files, parse_failures=tuple(failures), calls={k: tuple(v) for k, v in calls.items()})


def problems(scan: Scan, sites: Iterable[WalkSite]) -> list[str]:
    """Every way the scan and the table disagree; empty means they match."""
    out: list[str] = []
    if scan.files == 0:
        out.append("the scan examined no files: nothing was checked")
    if scan.files and scan.call_count == 0:
        out.append(f"the scan examined {scan.files} files and found no {CALLEE} call: nothing was checked")
    out.extend(f"parse failure (a failure, not a skip): {f}" for f in scan.parse_failures)
    listed = {s.key: s for s in sites}
    for key, locs in scan.calls.items():
        site = listed.get(key)
        if site is None:
            out.append(f"{locs[0]}: {CALLEE} call in {key}, which walk-sites.toml does not list")
        elif site.calls != len(locs):
            out.append(f"{key}: the table says calls = {site.calls}, the scan found {len(locs)} ({', '.join(locs)})")
    for key in listed:
        if key not in scan.calls:
            out.append(f"{key}: listed in walk-sites.toml but no {CALLEE} call found there")
    out.extend(
        f"{s.key}: class {s.cls!r} is not one of {CLASSES}" for s in listed.values() if s.cls not in CLASSES
    )
    return out


def _site(key: str, calls: int = 1, cls: str = "BULK JOB") -> WalkSite:
    return WalkSite(key=key, cls=cls, calls=calls, reason="planted")


def _plant(root: Path, rel: str, source: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


# --------------------------------------------------------------------------
# Planted trees: the scanner and the comparison are not vacuous


def test_planted_call_in_an_unlisted_function_is_reported_with_its_path_and_line(tmp_path: Path) -> None:
    _plant(tmp_path, "pkg/mod.py", "def listed(cat):\n    return cat.all_documents()\n\n\ndef sneaky(cat):\n    x = 1\n    return cat.all_documents()\n")
    scan = scan_tree(tmp_path)
    found = problems(scan, [_site("pkg/mod.py::listed")])
    assert len(found) == 1, found
    assert "pkg/mod.py:7" in found[0] and "pkg/mod.py::sneaky" in found[0]


def test_planted_row_with_no_call_fails(tmp_path: Path) -> None:
    _plant(tmp_path, "mod.py", "def real(cat):\n    return cat.all_documents()\n")
    found = problems(scan_tree(tmp_path), [_site("mod.py::real"), _site("mod.py::gone")])
    assert found == ["mod.py::gone: listed in walk-sites.toml but no all_documents call found there"]


@pytest.mark.parametrize("claimed", [1, 3])
def test_planted_row_whose_calls_differs_from_the_scan_fails_up_or_down(tmp_path: Path, claimed: int) -> None:
    _plant(tmp_path, "mod.py", "def f(cat):\n    a = cat.all_documents()\n    b = cat.all_documents()\n    return a, b\n")
    found = problems(scan_tree(tmp_path), [_site("mod.py::f", calls=claimed)])
    assert len(found) == 1 and "calls = %d" % claimed in found[0] and "found 2" in found[0]


def test_planted_class_outside_the_closed_set_fails(tmp_path: Path) -> None:
    _plant(tmp_path, "mod.py", "def f(cat):\n    return cat.all_documents()\n")
    found = problems(scan_tree(tmp_path), [_site("mod.py::f", cls="WHATEVER")])
    assert len(found) == 1 and "WHATEVER" in found[0]


def test_empty_tree_fails_as_a_scan_that_examined_nothing(tmp_path: Path) -> None:
    found = problems(scan_tree(tmp_path), [])
    assert found and "examined no files" in found[0]


def test_tree_with_files_but_no_call_fails_as_vacuous(tmp_path: Path) -> None:
    _plant(tmp_path, "mod.py", "def f():\n    return 1\n")
    found = problems(scan_tree(tmp_path), [])
    assert found and "found no all_documents call" in found[0]


def test_unparseable_file_is_a_failure_not_a_skip(tmp_path: Path) -> None:
    _plant(tmp_path, "good.py", "def f(cat):\n    return cat.all_documents()\n")
    _plant(tmp_path, "bad.py", "def broken(:\n")
    scan = scan_tree(tmp_path)
    assert len(scan.parse_failures) == 1 and scan.parse_failures[0].startswith("bad.py")
    assert any("parse failure" in p for p in problems(scan, [_site("good.py::f")]))


def test_nested_function_method_and_module_level_calls_are_keyed(tmp_path: Path) -> None:
    _plant(
        tmp_path,
        "deep/mod.py",
        "ROWS = all_documents()\n"
        "\n"
        "def outer(cat):\n"
        "    def inner():\n"
        "        return cat.all_documents()\n"
        "    return inner\n"
        "\n"
        "class Rung:\n"
        "    async def _scan(self, cat):\n"
        "        return cat.all_documents()\n"
        "\n"
        "    def other(self, cat):\n"
        "        return enumerate_all_documents(cat) + cat.enumerate_all_documents()\n",
    )
    scan = scan_tree(tmp_path)
    assert set(scan.calls) == {"deep/mod.py::<module>", "deep/mod.py::outer.inner", "deep/mod.py::Rung._scan"}
    assert scan.call_count == 3  # enumerate_all_documents is a different name, not counted


# --------------------------------------------------------------------------
# The shipped table


def test_table_loads_and_its_classes_are_the_closed_set() -> None:
    rows = walk_sites.sites()
    assert rows, "walk-sites.toml has no rows"
    assert {r.cls for r in rows} <= set(CLASSES)
    assert all(r.key.count("::") == 1 and r.calls >= 1 and r.reason.strip() for r in rows)


def test_a_planted_table_with_an_unknown_class_is_refused_at_load(tmp_path: Path) -> None:
    toml = tmp_path / "t.toml"
    toml.write_text(
        '[table]\nid = "t"\nkind = "state-machine"\n'
        '[dimensions.site]\ndomain = ["a.py::f"]\n'
        '[[row]]\nid = "a.py::f"\nmatch = { site = "a.py::f" }\n'
        'emit = { class = "NOPE", calls = 1, reason = "x" }\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="NOPE"):
        walk_sites.sites_from_table(load_table(toml))


@functools.cache
def _live_scan() -> Scan:
    return scan_tree(SRC_ROOT)


def test_live_source_and_table_agree() -> None:
    scan = _live_scan()
    sys.stdout.write(
        f"walk-site scan: files examined {scan.files}, parse failures {len(scan.parse_failures)}, "
        f"call expressions {scan.call_count}, sites {len(scan.calls)}, "
        f"files with a call {len({k.split('::')[0] for k in scan.calls})}\n"
    )
    found = problems(scan, walk_sites.sites())
    assert not found, "walk-sites.toml and src/nexus disagree:\n  " + "\n  ".join(found)
