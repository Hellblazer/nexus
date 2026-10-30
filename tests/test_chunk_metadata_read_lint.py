# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 0 (nexus-qyx35): a row read of a non-CHUNK metadata key is a finding.

A shared chunk row holds the last writer's value of every DOCUMENT, POSITION and LEGACY
key (RDR-236 key table, ``src/nexus/tables/chunk-metadata-keys.toml``). A reader that takes
one of them from ``metadata[...]`` or ``metadata.get(...)`` reads another document's value.
This lint finds those reads in ``src/nexus`` by AST and holds them to a RATCHET: the sites
that exist today are listed in ``tests/chunk_metadata_read_sites.toml`` (file, function, key,
count). The count may only fall: a site not listed is a finding naming the key's catalog
home, and a listed count above what the scan finds is a finding too (lower it, or delete the
entry) so the list shrinks as the readers move in Phase 2.

What a "read" is. A subscript load or ``.get(...)`` with a string-literal key, whose receiver is
a chunk-row metadata expression: a name in ``_ROW_NAMES`` (``metadata``, ``md``, ...), any
``<expr>.metadata`` attribute, or a local alias assigned in the same function from one of
those, from ``<expr>["metadata"]`` or from ``<expr>.get("metadata")`` (also through ``or``).
Blind spots, by construction: a row metadata dict bound to another name (``meta``, ``m``:
those names are also catalog document meta and ``entry.meta``, so they are not guessed at),
``"k" in metadata`` tests, ``.pop``, ``**metadata`` splats, and a key held in a variable.
The list is therefore a floor on the reads, not a census of every one.

The legacy-fallback module. The note TTL rules read the row until the keyless-note count is
zero (RDR-236 RF-8), so ``src/nexus/catalog/note_ttl.py`` is exempt whole
(``LEGACY_FALLBACK_MODULES``). There is no other fallback module; creating one would mean
moving the reads, which is Phase 2.
"""
from __future__ import annotations

import ast
import collections
import functools
import pathlib
import tomllib

import pytest

from nexus.tables import chunk_metadata_keys as ckm

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"
SITES_FILE = REPO_ROOT / "tests" / "chunk_metadata_read_sites.toml"

#: Bare names that hold one chunk row's metadata dict in this code base.
_ROW_NAMES = frozenset({"metadata", "md", "row_meta", "chunk_meta", "chunk_metadata", "meta_row"})

Site = tuple[str, str, str, int]  # relative path, function qualname, key, line


def _is_metadata_source(node: ast.AST) -> bool:
    """True for ``x.metadata``, ``x["metadata"]``, ``x.get("metadata"...)``, or an ``or`` of them."""
    if isinstance(node, ast.BoolOp):
        return any(_is_metadata_source(v) for v in node.values)
    if isinstance(node, ast.Attribute):
        return node.attr == "metadata"
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "metadata"
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "metadata"
        )
    return False


class _Scanner(ast.NodeVisitor):
    def __init__(self, relpath: str, keys: frozenset[str]) -> None:
        self.relpath = relpath
        self.keys = keys
        self.stack: list[str] = []
        self.aliases: list[set[str]] = [set()]
        self.sites: list[Site] = []

    def _scope(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.aliases.append(set())
        self.generic_visit(node)
        self.aliases.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and _is_metadata_source(node.value):
            self.aliases[-1].add(node.targets[0].id)
        self.generic_visit(node)

    def _is_row_metadata(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in _ROW_NAMES or node.id in self.aliases[-1]
        return isinstance(node, ast.Attribute) and node.attr == "metadata"

    def _hit(self, key: object, node: ast.AST) -> None:
        if isinstance(key, str) and key in self.keys:
            self.sites.append((self.relpath, ".".join(self.stack) or "<module>", key, node.lineno))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant) and self._is_row_metadata(node.value):
            self._hit(node.slice.value, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and self._is_row_metadata(f.value)
        ):
            self._hit(node.args[0].value, node)
        self.generic_visit(node)


def scan_source(source: str, relpath: str, keys: frozenset[str] | None = None) -> list[Site]:
    """The row reads of a non-CHUNK key in *source*."""
    scanner = _Scanner(relpath, keys if keys is not None else ckm.non_chunk_row_keys())
    scanner.visit(ast.parse(source))
    return scanner.sites


@functools.cache
def _scan_repo() -> tuple[tuple[Site, ...], int]:
    """(sites outside the exempt module, files scanned): one AST pass over ``src/nexus`` per session."""
    sites: list[Site] = []
    files = 0
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        files += 1
        if rel in ckm.LEGACY_FALLBACK_MODULES:
            continue
        sites.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return tuple(sites), files


def _observed() -> dict[tuple[str, str, str], int]:
    counts: collections.Counter[tuple[str, str, str]] = collections.Counter()
    for rel, func, key, _line in _scan_repo()[0]:
        counts[(rel, func, key)] += 1
    return dict(counts)


def _listed() -> dict[tuple[str, str, str], int]:
    doc = tomllib.loads(SITES_FILE.read_text(encoding="utf-8"))
    out: dict[tuple[str, str, str], int] = {}
    for site, keys in doc["sites"].items():
        rel, _, func = site.partition("::")
        for key, count in keys.items():
            out[(rel, func, key)] = count
    return out


def _remedy(key: str) -> str:
    row = ckm.row_for(key)
    return (
        f"{key} is a {row.cls} key (remedy {row.remedy}); read it from: {row.home}. "
        f"Moved by: {row.moved_by}."
    )


# --------------------------------------------------------------------------
# The ratchet over the repository


def test_no_row_read_of_a_non_chunk_key_outside_the_list():
    observed, listed = _observed(), _listed()
    new = {k: n for k, n in observed.items() if n > listed.get(k, 0)}
    assert not new, (
        "A chunk-row metadata read of a non-CHUNK key that is not on the list (RDR-236). "
        "A shared chunk row holds the last writer's value; read the catalog owner instead.\n"
        + "\n".join(f"  {rel}::{func}: {_remedy(key)} (found {n}, listed {listed.get(k, 0)})"
                    for k, n in sorted(new.items()) for rel, func, key in [k])
    )


def test_the_list_only_shrinks():
    observed, listed = _observed(), _listed()
    stale = {k: n for k, n in listed.items() if n > observed.get(k, 0)}
    assert not stale, (
        "tests/chunk_metadata_read_sites.toml lists reads that are gone (or fewer). The count "
        "may only fall: lower it or delete the entry in the commit that moved the reader.\n"
        + "\n".join(f"  {rel}::{func} {key}: listed {n}, found {observed.get(k, 0)}"
                    for k, n in sorted(stale.items()) for rel, func, key in [k])
    )


def test_the_scan_examined_enough_to_mean_something():
    sites, files = _scan_repo()
    assert files >= 300, f"scanned only {files} files under src/nexus"
    assert len(sites) >= 60, f"found only {len(sites)} reads; the receiver rules stopped matching"
    assert len({s[0] for s in sites}) >= 10


def test_the_exempt_module_is_real_and_would_otherwise_be_flagged():
    for rel in ckm.LEGACY_FALLBACK_MODULES:
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} is named the legacy-fallback module but does not exist"
        assert scan_source(path.read_text(encoding="utf-8"), rel), (
            f"{rel} has no row read of a non-CHUNK key; drop it from LEGACY_FALLBACK_MODULES"
        )


def test_every_listed_key_is_a_non_chunk_key():
    keys = ckm.non_chunk_row_keys()
    assert {k for (_r, _f, k) in _listed()} <= keys


# --------------------------------------------------------------------------
# Non-vacuity: the scanner finds what it claims, on synthetic source


def _keys(src: str) -> list[str]:
    return [k for (_r, _f, k, _l) in scan_source(src, "x.py")]


def test_scanner_flags_subscript_and_get_on_a_metadata_name():
    assert _keys("def f(metadata):\n    a = metadata['title']\n    b = metadata.get('tags', '')\n") == ["title", "tags"]


def test_scanner_flags_a_dot_metadata_attribute_and_names_the_function():
    sites = scan_source("class C:\n    def m(self, r):\n        return r.metadata.get('bib_year')\n", "x.py")
    assert sites == [("x.py", "C.m", "bib_year", 3)]


def test_scanner_follows_a_local_alias_of_the_metadata_attribute():
    src = "def f(hit):\n    meta = hit.metadata or {}\n    return meta.get('ttl_days')\n"
    assert _keys(src) == ["ttl_days"]
    src2 = "def f(r):\n    m = r.get('metadata') or {}\n    return m['indexed_at']\n"
    assert _keys(src2) == ["indexed_at"]


def test_scanner_ignores_chunk_row_keys_and_stores_and_other_receivers():
    src = (
        "def f(metadata, entry, other):\n"
        "    metadata['title'] = 1\n"            # a write
        "    a = metadata.get('chunk_text_hash')\n"   # a CHUNK key
        "    b = metadata.get('quarantined_at')\n"    # a ROW key
        "    c = entry.meta.get('title')\n"           # not a row metadata expression
        "    d = other['title']\n"
        "    e = metadata.get(some_variable)\n"
    )
    assert _keys(src) == []


def test_an_alias_does_not_leak_out_of_its_function():
    src = "def f(r):\n    meta = r.metadata\n\ndef g(meta):\n    return meta.get('title')\n"
    assert _keys(src) == []
