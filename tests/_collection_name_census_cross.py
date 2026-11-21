# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Cross-module parser roots for the client collection-name census
(RDR-229 P1.R fix round, nexus-p2m1x.5).

P1.1 derived its helper set from ``corpus.py`` alone, and only from the
functions that reach a regex or a raw ``"__"`` test. Two kinds of parser
therefore went uncounted: a ``corpus.py`` function whose parse is a prefix
test (``resolve_corpus``'s ``startswith(f"{corpus}__")``), and a parser that
lives in another module (``CollectionName.parse``). A caller of either read a
name's parts with no ledger entry. This module closes that, derived from the
code and never typed as a list.

A ROOT is a function whose body holds either

- a counted site of a NAME-DECOMPOSING shape (:data:`DECOMPOSING`: the shapes
  that take a part out of a name or test a computed ``<x>__`` boundary), after
  the exclusions the census already applies, or
- a call to a full parser (:data:`FULL_PARSERS`: ``parse_conformant_collection_name`` and
  ``candidate_attributes``), the parsers that return every segment.

Every call site of a root, in any module, is then one ``primitive_call`` site.
Roots found in ``corpus.py`` join its helper set (so the existing fixpoint and
by-name matching apply). Roots found anywhere else are matched through the
caller's imports, so a different function that happens to share a name is not
counted.

WHAT IT STILL CANNOT SEE (documented, not closed):

1. One level only outside ``corpus.py``: a caller of a root is a site, but
   the caller's own callers are not. (Inside ``corpus.py`` the fixpoint is
   transitive, as before.)
2. A function whose only counted sites are prefix ROUTING tests
   (``const_prefix``, ``loop_prefix``, literal ``startswith``, ``glob_default``,
   ``sql_prefix``, ``regex_prefix``) is not a root: it answers "which kind of
   collection", it does not return a part of the name, and its test is already
   one ledger site. Over a hundred call sites of such functions exist
   (``uri_for``, ``select_config``, ``_resolve_corpus_target`` and similar);
   counting them would put a routing wrapper's callers in the parse ledger.
   The same goes for a function whose only counted sites are calls to a
   helper other than ``parse_conformant_collection_name`` (a wrapper such as
   ``quarantine_collection_name``): its own call to the helper is a site, its
   callers are not.
3. An instance method (``self.f()``, ``obj.f()``) is not resolved, because the
   receiver's class is not knowable from the AST; only ``module.f``, ``f``
   imported by name, and ``Class.f`` are. A root that is an instance method
   is therefore never matched.
4. A call through a local alias (``g = resolve_corpus; g(x)``), ``getattr`` or
   a function passed as a value is invisible.
5. A function defined inside another function (not a module-level function or
   a method of a module-level class) is attributed to its enclosing function.
"""
from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from tests import _collection_name_census_scan as _scan
from tests._collection_name_census_exclusions import EXCLUDED_SITES
from tests._lint_line_anchor import resolve_ledger

#: Shapes that take a part out of a collection name or test a computed
#: ``<x>__`` boundary. Routing shapes (a literal or constant prefix test) are
#: not in it, see "WHAT IT STILL CANNOT SEE" 2.
DECOMPOSING = frozenset({
    "fstring_prefix", "concat_prefix", "split", "rsplit", "partition",
    "rpartition", "dunder_find", "strip_prefix", "substring", "hash_suffix",
    "in", "not_in",
})

#: The parsers that return every segment of a name (both live in ``legacy_names`` since RDR-229 P2.2).
FULL_PARSERS = frozenset({"parse_conformant_collection_name", "candidate_attributes"})

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class Roots:
    """Roots outside ``corpus.py``: ``(dotted module, name)`` for a function,
    ``(dotted module, class, name)`` for a method of a module-level class."""
    functions: frozenset[tuple[str, str]] = frozenset()
    methods: frozenset[tuple[str, str, str]] = frozenset()


def dotted(rel: str) -> str:
    """``src/nexus/a/b.py`` -> ``nexus.a.b``; a package ``__init__`` is the package."""
    parts = pathlib.PurePosixPath(rel).with_suffix("").parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _top_level_defs(tree: ast.Module) -> list[tuple[str | None, str, int, int]]:
    """``(class or None, name, first line, last line)`` of every module-level
    function and every method of a module-level class."""
    out: list[tuple[str | None, str, int, int]] = []
    for n in tree.body:
        if isinstance(n, _FUNCS):
            out.append((None, n.name, n.lineno, n.end_lineno or n.lineno))
        elif isinstance(n, ast.ClassDef):
            out.extend(
                (n.name, m.name, m.lineno, m.end_lineno or m.lineno)
                for m in n.body if isinstance(m, _FUNCS)
            )
    return out


@lru_cache(maxsize=1)
def excluded_hit_keys() -> frozenset[tuple[str, int, str]]:
    """``(rel, line, shape)`` of every ``EXCLUDED_SITES`` entry, resolved the
    way the census test resolves them."""
    resolved, _problems = resolve_ledger(
        _scan.REPO_ROOT,
        [(rel, content, (content, shape)) for rel, content, shape in EXCLUDED_SITES],
    )
    return frozenset((rel, ln, shape) for rel, ln, (_c, shape) in resolved)


def _root_spans(
    path: pathlib.Path, rel: str, consts: dict[str, tuple[str, ...]],
    dunders: frozenset[str], excluded: frozenset[tuple[str, int, str]],
) -> list[tuple[str | None, str]]:
    """``(class or None, name)`` of every root defined in one file."""
    tree = _scan._parse(path)
    if tree is None:
        return []
    defs = _top_level_defs(tree)
    hit_lines = {
        ln for ln, shape in _scan.scan_file(path, consts, frozenset(), dunders)
        if shape in DECOMPOSING and (rel, ln, shape) not in excluded
    }
    call_lines = {
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and (
            (isinstance(n.func, ast.Name) and n.func.id in FULL_PARSERS)
            or (isinstance(n.func, ast.Attribute) and n.func.attr in FULL_PARSERS)
        )
    }
    lines = hit_lines | call_lines
    return [
        (cls, name) for cls, name, lo, hi in defs
        if any(lo <= ln <= hi for ln in lines)
    ]


@lru_cache(maxsize=1)
def root_hit_exclusions() -> frozenset[tuple[str, int, str]]:
    """THE ROOT RULE, in one place (RDR-229 P2.7): a function is a parser root iff
    it holds at least one counted PARSE-CLASS site now. The hits that are not root
    evidence are the ``EXCLUDED_SITES`` and every ledger site classed with a
    non-parse label (``label-pattern``, ``file-path``): RDR Technical Design item 1
    names five parse classes, and a label the user typed or a source path takes no
    part out of a collection name, so a function holding only such sites is not a
    parser and its callers are not counted. Derived from the ledger on every scan,
    never a list; a site the ledger does not label is evidence (and fails the
    census as unlabelled)."""
    from tests._collection_name_census_ledger import LEDGER  # noqa: PLC0415 - the ledger imports the scanner

    resolved, _problems = _scan.resolve_sites(_scan.REPO_ROOT, list(LEDGER))
    non_parse = {key for key, site in resolved.items() if site.cls in _scan.NON_PARSE_CLASSES}
    return excluded_hit_keys() | frozenset(non_parse)


@lru_cache(maxsize=1)
def corpus_roots() -> frozenset[str]:
    """Names of the ``corpus.py`` functions that are roots in their own right
    (they join the helper set; the fixpoint then adds whatever reaches them)."""
    rel = _scan.CORPUS_REL
    consts, dunders = _scan._live_tree_constants()
    return frozenset(
        name for cls, name in _root_spans(
            _scan.REPO_ROOT / rel, rel, consts, dunders, root_hit_exclusions(),
        ) if cls is None
    )


def derive_roots(
    src: pathlib.Path, repo_root: pathlib.Path,
    consts: dict[str, tuple[str, ...]], dunders: frozenset[str],
) -> Roots:
    """Every root outside ``corpus.py``, across *src*."""
    excluded = root_hit_exclusions()
    funcs: set[tuple[str, str]] = set()
    methods: set[tuple[str, str, str]] = set()
    for path in _scan._py_files(src):
        rel = path.relative_to(repo_root).as_posix()
        if rel in (_scan.CORPUS_REL, _scan.LEGACY_NAMES_REL):
            continue
        mod = dotted(rel)
        for cls, name in _root_spans(path, rel, consts, dunders, excluded):
            if cls is None:
                funcs.add((mod, name))
            else:
                methods.add((mod, cls, name))
    return Roots(frozenset(funcs), frozenset(methods))


@lru_cache(maxsize=1)
def live_roots() -> Roots:
    """:func:`derive_roots` over the live tree, once per process."""
    consts, dunders = _scan._live_tree_constants()
    return derive_roots(_scan.SRC, _scan.REPO_ROOT, consts, dunders)


def _chain(node: ast.AST) -> list[str] | None:
    """``a.b.c`` as ``["a", "b", "c"]``; ``None`` when the base is not a name."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def src_rel(path: pathlib.Path) -> str:
    """*path* as ``src/<package>/...``: its parts after the last ``src``
    directory (a fixture tree is rooted the same way as the live one)."""
    parts = path.parts
    i = len(parts) - 1 - parts[::-1].index("src")
    return "/".join(parts[i:])


def call_matcher(
    path: pathlib.Path, tree: ast.Module, roots: Roots,
) -> Callable[[ast.Call], bool]:
    """A predicate: is this call a call to a root, resolved through this
    file's imports (and its own module-level functions and classes)?"""
    here = dotted(src_rel(path))
    package = here if path.name == "__init__.py" else here.rpartition(".")[0]
    from_binds: dict[str, tuple[str, str]] = {}
    mod_aliases: dict[str, str] = {}
    local_funcs = {n.name for n in tree.body if isinstance(n, _FUNCS)}
    local_classes = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                up = package.split(".")[: len(package.split(".")) - (n.level - 1)]
                base = ".".join([*up, *([base] if base else [])])
            for a in n.names:
                from_binds[a.asname or a.name] = (base, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.asname:
                    mod_aliases[a.asname] = a.name
                else:
                    mod_aliases[a.name.split(".")[0]] = a.name.split(".")[0]

    def is_root_call(call: ast.Call) -> bool:
        parts = _chain(call.func)
        if parts is None:
            return False
        head, *rest = parts
        if not rest:
            if head in from_binds:
                return from_binds[head] in roots.functions
            return head in local_funcs and (here, head) in roots.functions
        last, middle = rest[-1], rest[:-1]
        if head in from_binds:
            base_mod, sym = from_binds[head]
            if not middle and (base_mod, sym, last) in roots.methods:
                return True
            if len(middle) == 1 and (f"{base_mod}.{sym}", middle[0], last) in roots.methods:
                return True
            return (".".join([base_mod, sym, *middle]), last) in roots.functions
        if head in mod_aliases:
            if middle and (".".join([mod_aliases[head], *middle[:-1]]), middle[-1], last) in roots.methods:
                return True
            return (".".join([mod_aliases[head], *middle]), last) in roots.functions
        if head in local_classes and not middle:
            return (here, head, last) in roots.methods
        return False

    return is_root_call
