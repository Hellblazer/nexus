# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The client collection-name parse scanner and its per-site ledger engine
(RDR-229 P1.1, nexus-p2m1x.1).

Split out of ``tests/test_collection_name_parse_census.py`` so that file stays
under its size ceiling. Nothing here is a test; the test files import it.

WHAT IT MATCHES. One AST walk of a Python file yields ``(lineno, shape)``
hits. The shapes RDR-204 Phase 3 built keep their names, so every older
assertion reads unchanged: ``split`` / ``rsplit`` / ``partition`` /
``rpartition`` (a ``"__"`` argument), ``startswith`` / ``endswith`` (a literal
collection prefix, bare or in a tuple/list), ``in`` / ``not_in`` (against the
literal ``"__"``) and ``primitive_call``. RDR-229 Technical Design item 4
widens the fifth and adds the rest:

- ``primitive_call`` is a call to ANY function in the DERIVED helper set
  (:func:`derive_parser_helpers`): every ``corpus.py`` function that reaches a
  name parser through the module's own call graph, or itself holds a
  name-decomposing site; and, since the P1.R fix round, a call to a ROOT in
  any other module (``tests/_collection_name_census_cross.py``: a function
  that holds such a site or calls ``parse_conformant_collection_name``, matched
  through the caller's imports). The three typed in
  ``nexus.corpus._CANDIDATE_STRING_PRIMITIVES`` are its non-vacuity floor.
- ``fstring_prefix``: ``startswith``/``endswith`` on an f-string with a
  literal part ending in ``__``, or on a local bound to one.
- ``concat_prefix``: the same on ``<expr> + "__"``, ``"%s__" % x`` or
  ``"{}__".format(x)``, or a local bound to one.
- ``const_prefix``: on a module constant that holds a collection prefix (a
  tuple, a dict's keys, a tuple of pairs, or a comprehension/generator over
  prefix-ending f-strings). Constants are collected across every file under
  the scanned tree and matched by name, imported or not.
- ``loop_prefix``: on a name bound by a ``for`` loop or comprehension over such
  a constant, its ``.items()``/``.keys()``, or a literal tuple of prefixes;
  scoped per function.
- ``local_prefix``: on a local bound to a prefix literal or a tuple/list of
  them.
  Locals bind through Assign, AnnAssign, walrus and tuple-unpack.
- ``strip_prefix``: ``removeprefix`` / ``removesuffix`` / ``.replace`` whose
  first argument is a collection prefix (a literal, or any shape above).
- ``substring``: ``"__<x>__" in name``, the f-string form, or a local bound to
  it. A Python dunder name (``__init__``, ``__pycache__``: :data:`_PY_DUNDERS`)
  is not a collection segment and never matches.
- ``dunder_find``: ``.find`` / ``.index`` / ``.count`` / ``.rfind`` /
  ``.rindex`` of ``"__"``.
- ``hash_suffix``: ``rsplit("-", ..)`` on a receiver that names a collection,
  and ``endswith(f"-{..}")`` in a function holding such an ``rsplit``: the
  legacy two-segment name's 8-character hash suffix.
- ``endswith`` also matches the literal ``"__"`` (a family-prefix test).
- ``split``/``in``/... also take ``sep="__"`` and a name bound to the string
  ``"__"``.
- ``glob_default``: a string constant ``"<type>__*"``.
- ``sql_prefix``: a SQL string (it holds SELECT, WHERE or FROM) with
  ``starts_with(.., '<type>__')``, ``LIKE '<type>..'`` or
  ``split_part(.., '__', ..)``.
- ``regex_prefix``: ``re.compile`` of a literal that opens on a collection
  prefix (``quarantine-``, ``code__``): a regex parse of a name outside
  ``corpus.py``.
- ``fnmatch``: every ``fnmatch`` / ``fnmatchcase`` call. Each is labelled in
  the ledger as a label pattern, a file path or routing.

``taxonomy__`` joined the prefix set (the routing census scans it).

A docstring, a comment, or a bare string expression never contributes a hit.

KNOWN LIMITS (documented, not closed). The cross-module root derivation has
its own list of what it cannot see (``tests/_collection_name_census_cross.py``:
one level outside ``corpus.py``; callers of a routing-only function or of a
wrapper that only calls a helper; instance methods; aliases and ``getattr``).
Beyond it, the scan cannot see a parse spelled another way: a name read with
slicing (``name[len(prefix):]``) or ``re.match`` on a non-literal pattern, a
prefix built in a helper in another module and passed in, or code under
``scripts/``. (6) SQL is matched in the case the
scan's patterns spell: ``SELECT/WHERE/FROM`` upper case, ``LIKE`` upper case.
(7) An aliased import (``from fnmatch import fnmatch as fm``; a helper imported
under another name) is not followed; a call is matched by its spelled name.
(8) A nested ``def`` closing over an outer loop variable or binding does not
see it: bindings are scoped to the function that makes them. (12) A constant
is matched by NAME across the whole tree, so a generic name (``_REGISTRY``,
``_DEFAULTS``) that holds a prefix in one file and something else in another
matches in both; and a prefix reached only through a call result, a
``getattr`` or a dict lookup is invisible. ``scripts/`` is not scanned.
The hash-suffix leg needs a receiver spelled with ``collection`` in it, so
``suffix.rsplit("-", 1)`` on a local derived from a collection name
(``commands/catalog.py:1945``, the per-file recovery) is not matched.
Also not matched: a prefix collection wrapped or built at the use site
(``startswith(tuple(CONST))``, ``startswith(tuple(f"{t}__" for t in T))``, a
local list or generator of such f-strings); a class-level prefix constant
(``self.PFX``, the collector reads module bodies only); ``CONST + owner``
(only a literal ending in ``__`` counts as concat); ``.replace("__", ...)``;
``rpartition("-")`` on a collection; and a regex parse of a collection name
outside ``corpus.py`` (``doc/ref_scanner.py``). Over-matches fail loud, not
silent: ``.count("__")`` on non-name text, or a dunder not in ``_PY_DUNDERS``.

THE LEDGER. Every hit is one site. A site carries one class from
:data:`CLASSES` (the RDR's five, the two non-parse labels for ``fnmatch``,
and ``holdover``). A ``holdover`` is a site no final class fits yet; its note
must name the Phase 2 step (``P2.n``) that moves it, and the census test
checks that. The ledger is a table of :class:`Site` entries, content-anchored
through ``tests._lint_line_anchor``: the entry holds the site's own stripped
source text plus its nearest preceding non-blank line, and its current line
number is re-derived on every run. :func:`diff_ledger` returns the unlabelled
live sites and the stale ledger entries; each message names file, line and
shape.
"""
from __future__ import annotations

import ast
import pathlib
import re
import types
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from tests._collection_name_census_notes import LABEL_TYPED
from tests._lint_line_anchor import resolve_anchor

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"
CORPUS_REL = "src/nexus/corpus.py"
#: The parser definitions and the migration-source floor (RDR-229 P2.2): the parser roots live here,
#: and a call inside it is the floor, never a site. Every other name read is a classified census caller.
LEGACY_NAMES_REL = "src/nexus/catalog/rows/legacy_names.py"

#: Collection-type / lifecycle prefixes a raw ``startswith``/``endswith``
#: call names when it parses a collection NAME. RDR-204's five plus
#: ``taxonomy__`` (RDR-229 P1.1: the routing census scans it).
TYPE_PREFIXES = frozenset(
    {"code__", "docs__", "rdr__", "knowledge__", "taxonomy__", "quarantine-"}
)
_CT_PREFIXES = ("code__", "docs__", "rdr__", "knowledge__", "taxonomy__")

SHAPES = (
    "split", "rsplit", "partition", "rpartition", "startswith", "endswith",
    "in", "not_in", "primitive_call", "fstring_prefix", "concat_prefix",
    "const_prefix", "loop_prefix", "local_prefix", "strip_prefix", "substring",
    "dunder_find", "hash_suffix", "glob_default", "sql_prefix", "regex_prefix",
    "fnmatch",
)

#: The RDR-229 Technical Design item 1 classes, then the two labels for a
#: ``fnmatch`` that is not a parse (item 4), then the explicit holdover.
PARSE_CLASSES = (
    "mint", "migration-source", "diagnostic", "routing", "uri-classifier",
)
NON_PARSE_CLASSES = ("label-pattern", "file-path")
CLASSES = (*PARSE_CLASSES, *NON_PARSE_CLASSES, "holdover")

_STR_SPLITTERS = ("split", "rsplit", "partition", "rpartition")
_FIND_CALLS = ("find", "index", "count", "rfind", "rindex")
_STRIP_CALLS = ("removeprefix", "removesuffix", "replace")
_GLOB_DEFAULT_RE = re.compile(r"^(?:code|docs|rdr|knowledge|taxonomy)__\*$")
#: A regex compiled from a literal that opens on a collection prefix
#: (``re.compile(r"^quarantine-(?P<origin>.+)$")``): a name parser that is
#: neither a split nor a ``startswith``.
_REGEX_PREFIX_RE = re.compile(
    r"^\^?(?:quarantine-|(?:code|docs|rdr|knowledge|taxonomy)__)"
)
_SQL_KEYWORD_RE = re.compile(r"\b(?:SELECT|WHERE|FROM)\b")
_SQL_PREFIX_RE = re.compile(
    r"(?i:starts_with\s*\([^)]*'(?:code|docs|rdr|knowledge|taxonomy)__')"
    r"|LIKE\s+'(?:code|docs|rdr|knowledge|taxonomy|quarantine)"
    r"|(?i:split_part\s*\([^)]*'__')"
    r"|'quarantine-'"
)

#: Python dunder names a ``"__x__" in name`` test is about, never a collection
#: owner segment: the object/type/module protocol names and the usual module
#: and package dunders.
_PY_DUNDERS = frozenset(
    {n for n in (*dir(object), *dir(type), *dir(types.ModuleType)) if n.startswith("__")}
    | {"__pycache__", "__main__", "__all__", "__version__", "__path__",
       "__file__", "__name__", "__spec__", "__slots__", "__dict__",
       "__module__", "__init_subclass__", "__post_init__", "__enter__",
       "__exit__", "__iter__", "__len__", "__call__", "__getitem__"}
)


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_prefix_text(s: str) -> bool:
    return s.startswith(_CT_PREFIXES) or s.startswith("quarantine-")


def _ends_dunder_prefix(lit: str) -> bool:
    """A literal that ends a collection prefix (``"__"``, ``"x__"``), not a
    whole dunder name (``"__init__"``)."""
    return lit.endswith("__") and not (len(lit) > 4 and lit.startswith("__"))


def _ref_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("items", "keys", "values")
    ):
        return _ref_name(node.func.value)
    return None


def _fstring_literals(node: ast.AST) -> list[str]:
    if not isinstance(node, ast.JoinedStr):
        return []
    return [str(v.value) for v in node.values if isinstance(v, ast.Constant)]


def _fstring_dunder_both_ends(node: ast.AST) -> bool:
    """``f"__{x}__"``: the first literal opens and the last closes with ``__``."""
    if not isinstance(node, ast.JoinedStr) or len(node.values) < 3:
        return False
    first, last = node.values[0], node.values[-1]
    return (
        isinstance(first, ast.Constant) and str(first.value).startswith("__")
        and isinstance(last, ast.Constant) and str(last.value).endswith("__")
    )


def _dunder_expr_shape(node: ast.AST) -> str | None:
    """The shape of a computed prefix: an f-string with a literal part ending
    in ``__`` (``fstring_prefix``), or ``<expr> + "__"``, ``"%s__" % x``,
    ``"{}__".format(x)`` (``concat_prefix``). ``None`` otherwise."""
    if any(_ends_dunder_prefix(lit) for lit in _fstring_literals(node)):
        return "fstring_prefix"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if any(
            (s := _str_const(side)) is not None and _ends_dunder_prefix(s)
            for side in (node.left, node.right)
        ):
            return "concat_prefix"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        s = _str_const(node.left)
        if s is not None and _ends_dunder_prefix(s):
            return "concat_prefix"
    if (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        s = _str_const(node.func.value)
        if s is not None and _ends_dunder_prefix(s):
            return "concat_prefix"
    return None


def _value_prefix_shape(node: ast.AST) -> str | None:
    """The shape a LOCAL bound to *node* gives a later ``startswith``."""
    s = _str_const(node)
    if s is not None:
        return "local_prefix" if s in TYPE_PREFIXES else None
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        for e in node.elts:
            es = _str_const(e)
            if es is not None and es in TYPE_PREFIXES:
                return "local_prefix"
        return None
    return _dunder_expr_shape(node)


def module_prefix_constants(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Module-level names whose value holds a collection prefix anywhere in it
    (a str, a tuple, a dict's keys, a tuple of pairs, or a comprehension over
    prefix-ending f-strings): name -> prefixes."""
    out: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        found = sorted({
            s for x in ast.walk(node.value)
            if (s := _str_const(x)) is not None and _is_prefix_text(s)
        })
        if not found and any(
            _dunder_expr_shape(x) is not None for x in ast.walk(node.value)
        ):
            found = ["<computed>__"]
        if not found:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = tuple(found)
    return out


def module_dunder_names(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound to the string ``"__"``."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if _str_const(node.value) == "__":
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return frozenset(names)


def _read(path: pathlib.Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _parse(path: pathlib.Path) -> ast.Module | None:
    text = _read(path)
    if text is None:
        return None
    try:
        return ast.parse(text)
    except SyntaxError:
        return None


def _py_files(src: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in sorted(src.rglob("*.py")) if "__pycache__" not in p.parts]


def tree_constants(
    src: pathlib.Path,
) -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    """Every prefix-holding module constant and every ``"__"`` name under
    *src*, by name (a constant is imported by name, so the name is the
    cross-file key)."""
    consts: dict[str, tuple[str, ...]] = {}
    dunders: set[str] = set()
    for path in _py_files(src):
        tree = _parse(path)
        if tree is not None:
            consts.update(module_prefix_constants(tree))
            dunders |= module_dunder_names(tree)
    return consts, frozenset(dunders)


@lru_cache(maxsize=1)
def _live_tree_constants() -> tuple[dict[str, tuple[str, ...]], frozenset[str]]:
    return tree_constants(SRC)


def live_prefix_constants() -> dict[str, tuple[str, ...]]:
    return _live_tree_constants()[0]


def _module_functions(tree: ast.Module) -> dict[str, ast.AST]:
    return {
        n.name: n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_raw_parse_call(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in _STR_SPLITTERS and node.args:
            return _str_const(node.args[0]) == "__"
    if isinstance(node, ast.Compare):
        return any(
            isinstance(op, (ast.In, ast.NotIn))
            and "__" in (_str_const(node.left), _str_const(c))
            for op, c in zip(node.ops, node.comparators)
        )
    return False


def derive_parser_helpers(
    corpus_tree: ast.Module, extra_roots: frozenset[str] = frozenset(),
    legacy_tree: ast.Module | None = None,
) -> frozenset[str]:
    """The fifth class's helper set, derived from the call graph of ``legacy_names.py`` (where the
    parser definitions live since RDR-229 P2.2) and ``corpus.py`` (whose functions reach them),
    never typed as a list (RDR-229 Technical Design item 4).

    Roots are the parser definitions: a module-level function that applies a
    module-level compiled regex (``re.compile``), or that holds a raw
    ``"__"`` split / ``in`` test. Every function that names a root or an
    already-reached function (a bare name or an attribute) joins the set,
    to a fixpoint; a module-level ``alias = helper`` assignment follows its
    target. ``is_conformant_collection_name`` and
    ``parse_conformant_collection_name`` are roots because they apply
    ``_CONFORMANT_COLLECTION_RE``. *extra_roots* are roots found by their
    sites rather than by a regex or a raw ``"__"`` test (a function whose parse
    is ``startswith(f"{x}__")``: ``resolve_corpus``), derived by the cross
    module.
    """
    regexes: set[str] = set()
    aliases: dict[str, str] = {}
    funcs: dict[str, ast.AST] = {}
    for tree in (corpus_tree, *([legacy_tree] if legacy_tree is not None else [])):
        for n in tree.body:
            if not isinstance(n, ast.Assign):
                continue
            v = n.value
            if (
                isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                and v.func.attr == "compile"
                and isinstance(v.func.value, ast.Name) and v.func.value.id == "re"
            ):
                regexes |= {t.id for t in n.targets if isinstance(t, ast.Name)}
            elif isinstance(v, ast.Name):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        aliases[t.id] = v.id
        funcs.update(_module_functions(tree))
    refs: dict[str, set[str]] = {}
    reach: set[str] = {n for n in extra_roots if n in funcs}
    for name, fn in funcs.items():
        names = {
            x.id if isinstance(x, ast.Name) else x.attr
            for x in ast.walk(fn) if isinstance(x, (ast.Name, ast.Attribute))
        }
        refs[name] = names
        if names & regexes or any(_is_raw_parse_call(x) for x in ast.walk(fn)):
            reach.add(name)
    changed = True
    while changed:
        changed = False
        for name, names in refs.items():
            if name not in reach and names & reach:
                reach.add(name)
                changed = True
        for alias, target in aliases.items():
            if target in reach and alias not in reach:
                reach.add(alias)
                changed = True
    return frozenset(reach)


@lru_cache(maxsize=1)
def live_parser_helpers() -> frozenset[str]:
    tree = _parse(REPO_ROOT / CORPUS_REL)
    assert tree is not None, f"{CORPUS_REL} does not parse"
    legacy_tree = _parse(REPO_ROOT / LEGACY_NAMES_REL)
    assert legacy_tree is not None, f"{LEGACY_NAMES_REL} does not parse"
    from tests import _collection_name_census_cross as _cross  # noqa: PLC0415 - the cross module imports this one

    return derive_parser_helpers(tree, _cross.corpus_roots(), legacy_tree)


def _scope_nodes(scope: ast.AST) -> list[ast.AST]:
    """Every node in *scope*'s own body, not entering a nested function."""
    out: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        n = stack.pop()
        out.append(n)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        stack.extend(ast.iter_child_nodes(n))
    return out


def _bind(binds: dict[str, str], target: ast.AST, value: ast.AST) -> None:
    """Record what *value* gives the local(s) in *target*: a plain name, or a
    tuple-unpack matched pairwise against a tuple/list value."""
    if isinstance(target, ast.Name):
        shape = _value_prefix_shape(value)
        if shape is not None:
            binds.setdefault(target.id, shape)
    elif (
        isinstance(target, (ast.Tuple, ast.List))
        and isinstance(value, (ast.Tuple, ast.List))
        and len(target.elts) == len(value.elts)
    ):
        for t, v in zip(target.elts, value.elts):
            _bind(binds, t, v)


def _scope_bindings(
    nodes: list[ast.AST], consts: dict[str, tuple[str, ...]],
) -> tuple[dict[str, str], set[str]]:
    """``({local name: shape}, f-string-substring locals)`` in one scope. A
    local binds through ``for``/comprehension over a prefix source
    (``loop_prefix``), Assign, AnnAssign, walrus and tuple-unpack."""
    binds: dict[str, str] = {}
    fsub: set[str] = set()
    for n in nodes:
        gens: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(n, (ast.For, ast.AsyncFor)):
            gens = [(n.target, n.iter)]
        elif isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            gens = [(g.target, g.iter) for g in n.generators]
        for target, it in gens:
            prefixy = _ref_name(it) in consts or (
                isinstance(it, (ast.Tuple, ast.List, ast.Set))
                and any(
                    (s := _str_const(e)) is not None and _is_prefix_text(s)
                    for e in it.elts
                )
            )
            if not prefixy:
                continue
            first = (
                target.elts[0]
                if isinstance(target, ast.Tuple) and target.elts else target
            )
            if isinstance(first, ast.Name):
                binds.setdefault(first.id, "loop_prefix")
        if isinstance(n, ast.Assign):
            for t in n.targets:
                _bind(binds, t, n.value)
            names = {t.id for t in n.targets if isinstance(t, ast.Name)}
            if _fstring_dunder_both_ends(n.value):
                fsub |= names
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            _bind(binds, n.target, n.value)
            if isinstance(n.target, ast.Name) and _fstring_dunder_both_ends(n.value):
                fsub.add(n.target.id)
        elif isinstance(n, ast.NamedExpr):
            _bind(binds, n.target, n.value)
            if _fstring_dunder_both_ends(n.value):
                fsub.add(n.target.id)
    return binds, fsub


def _expr_prefix_shape(
    e: ast.AST, consts: dict[str, tuple[str, ...]], binds: dict[str, str],
    *, literal_ok: bool,
) -> str | None:
    """The prefix shape one argument expression has, ``None`` for none. A
    literal collection prefix returns ``"literal"`` when *literal_ok*."""
    if isinstance(e, ast.NamedExpr):
        e = e.value
    s = _str_const(e)
    if s is not None:
        return "literal" if literal_ok and s in TYPE_PREFIXES else None
    if isinstance(e, (ast.Name, ast.Attribute)):
        if _ref_name(e) in consts:
            return "const_prefix"
        if isinstance(e, ast.Name) and e.id in binds:
            return binds[e.id]
        return None
    return _dunder_expr_shape(e)


def _arg_shapes(
    call: ast.Call, consts: dict[str, tuple[str, ...]], binds: dict[str, str],
    first_only: bool = False,
) -> list[str]:
    elts: list[ast.AST] = []
    args = call.args[:1] if first_only else call.args
    for arg in args:
        elts.extend(arg.elts if isinstance(arg, (ast.Tuple, ast.List)) else [arg])
    shapes = [
        sh for e in elts
        if (sh := _expr_prefix_shape(e, consts, binds, literal_ok=True))
    ]
    return shapes


def _startswith_shape(
    call: ast.Call, consts: dict[str, tuple[str, ...]], binds: dict[str, str],
) -> str | None:
    """The one shape a ``startswith``/``endswith`` call matches: a literal
    prefix (the method's own name), else the first prefix shape found."""
    attr = call.func.attr  # type: ignore[attr-defined]
    shapes = _arg_shapes(call, consts, binds)
    if "literal" in shapes:
        return attr
    if attr == "endswith" and any(_str_const(a) == "__" for a in call.args):
        return attr
    return shapes[0] if shapes else None


def _is_dunder_arg(node: ast.AST, dunders: frozenset[str]) -> bool:
    return _str_const(node) == "__" or (
        isinstance(node, ast.Name) and node.id in dunders
    )


def _compare_shapes(
    node: ast.Compare, fsub: set[str], dunders: frozenset[str],
) -> list[str]:
    shapes: list[str] = []
    for op, comp in zip(node.ops, node.comparators):
        if not isinstance(op, (ast.In, ast.NotIn)):
            continue
        if _is_dunder_arg(node.left, dunders) or _is_dunder_arg(comp, dunders):
            shapes.append("not_in" if isinstance(op, ast.NotIn) else "in")
            continue
        for side in (node.left, comp):
            text = _str_const(side)
            if (
                (text is not None and len(text) > 4
                 and text.startswith("__") and text.endswith("__")
                 and text not in _PY_DUNDERS)
                or _fstring_dunder_both_ends(side)
                or (isinstance(side, ast.Name) and side.id in fsub)
            ):
                shapes.append("substring")
                break
    return shapes


def _mentions_collection(node: ast.AST) -> bool:
    return any(
        (isinstance(x, ast.Name) and "collection" in x.id.lower())
        or (isinstance(x, ast.Attribute) and "collection" in x.attr.lower())
        for x in ast.walk(node)
    )


def _hash_suffix_fstring(call: ast.Call) -> bool:
    """``endswith(f"-{...}")``: an f-string opening with a hyphen."""
    if not call.args:
        return False
    a = call.args[0]
    return (
        isinstance(a, ast.JoinedStr) and bool(a.values)
        and isinstance(a.values[0], ast.Constant)
        and str(a.values[0].value).startswith("-")
    )


def _sql_hit_line(node: ast.AST, matched: str, lines: list[str]) -> int:
    """The physical line of the SQL pattern inside a (possibly multi-line)
    string. Counting newlines in the value is off by one when the literal
    opens with a backslash-newline, so find the matched text in the source."""
    first = matched.splitlines()[0]
    end = getattr(node, "end_lineno", None) or node.lineno  # type: ignore[attr-defined]
    for ln in range(node.lineno, end + 1):  # type: ignore[attr-defined]
        if first in lines[ln - 1]:
            return ln
    return node.lineno  # type: ignore[attr-defined]


def _call_hits(
    node: ast.Call, consts: dict[str, tuple[str, ...]], binds: dict[str, str],
    helpers: frozenset[str], dunders: frozenset[str], has_hash_rsplit: bool,
) -> list[tuple[int, str]]:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else ""
    )
    if name in ("fnmatch", "fnmatchcase"):
        return [(node.lineno, "fnmatch")]
    if name in helpers:
        return [(node.lineno, "primitive_call")]
    if name == "compile" and node.args:
        pattern = _str_const(node.args[0])
        if pattern is not None and _REGEX_PREFIX_RE.match(pattern):
            return [(node.lineno, "regex_prefix")]
    if not isinstance(func, ast.Attribute):
        return []
    if name in _STR_SPLITTERS:
        sep = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg == "sep"), None,
        )
        if sep is not None and _is_dunder_arg(sep, dunders):
            return [(node.lineno, name)]
        if (
            name == "rsplit" and sep is not None and _str_const(sep) == "-"
            and _mentions_collection(func.value)
        ):
            return [(node.lineno, "hash_suffix")]
        return []
    if name in _FIND_CALLS:
        if node.args and _is_dunder_arg(node.args[0], dunders):
            return [(node.lineno, "dunder_find")]
        return []
    if name in _STRIP_CALLS:
        if _arg_shapes(node, consts, binds, first_only=True):
            return [(node.lineno, "strip_prefix")]
        return []
    if name in ("startswith", "endswith"):
        if name == "endswith" and has_hash_rsplit and _hash_suffix_fstring(node):
            return [(node.lineno, "hash_suffix")]
        shape = _startswith_shape(node, consts, binds)
        return [(node.lineno, shape)] if shape else []
    return []


def scan_file(
    path: pathlib.Path,
    consts: dict[str, tuple[str, ...]] | None = None,
    helpers: frozenset[str] | None = None,
    dunders: frozenset[str] | None = None,
    roots: object | None = None,
) -> list[tuple[int, str]]:
    """``(lineno, shape)`` for every collection-name-parse-shaped site in one
    file, BEFORE any exclusion. *roots* is the cross-module root set
    (``tests._collection_name_census_cross.Roots``); a call to one is a
    ``primitive_call``. *consts* is the prefix-constant map (this
    file's own module-level constants are always added), *helpers* the
    derived helper set (default: derived from the live ``corpus.py``) and
    *dunders* the names bound to ``"__"`` (this file's own are added)."""
    text = _read(path)
    tree = _parse(path)
    if text is None or tree is None:
        return []
    lines = text.splitlines()
    consts = {**module_prefix_constants(tree), **(consts or {})}
    dunders = module_dunder_names(tree) | (dunders or frozenset())
    helpers = live_parser_helpers() if helpers is None else helpers
    root_call = None
    if roots is not None:
        from tests import _collection_name_census_cross as _cross  # noqa: PLC0415 - the cross module imports this one

        root_call = _cross.call_matcher(path, tree, roots)  # type: ignore[arg-type]
    hits: list[tuple[int, str]] = []
    docstrings = {
        id(n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    scopes: list[ast.AST] = [tree]
    scopes.extend(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    for scope in scopes:
        nodes = _scope_nodes(scope)
        binds, fsub = _scope_bindings(nodes, consts)
        has_hash_rsplit = any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "rsplit" and n.args
            and _str_const(n.args[0]) == "-" and _mentions_collection(n.func.value)
            for n in nodes
        )
        for node in nodes:
            if isinstance(node, ast.Call):
                found = _call_hits(
                    node, consts, binds, helpers, dunders, has_hash_rsplit,
                )
                if not found and root_call is not None and root_call(node):
                    found = [(node.lineno, "primitive_call")]
                hits.extend(found)
            elif isinstance(node, ast.Compare):
                hits.extend(
                    (node.lineno, s) for s in _compare_shapes(node, fsub, dunders)
                )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant):
            continue
        text_value = _str_const(node)
        if text_value is None or id(node) in docstrings:
            continue
        if _GLOB_DEFAULT_RE.match(text_value):
            hits.append((node.lineno, "glob_default"))
        elif _SQL_KEYWORD_RE.search(text_value) and (m := _SQL_PREFIX_RE.search(text_value)):
            hits.append((_sql_hit_line(node, m.group(0), lines), "sql_prefix"))
    return sorted(hits)


@lru_cache(maxsize=1)
def _live_raw_sites() -> dict[str, list[tuple[int, str]]]:
    return _raw_sites_uncached()


def raw_sites(
    src: pathlib.Path = SRC, repo_root: pathlib.Path = REPO_ROOT,
    consts: dict[str, tuple[str, ...]] | None = None,
    helpers: frozenset[str] | None = None,
) -> dict[str, list[tuple[int, str]]]:
    """The live scan is computed once per process and handed out as a copy;
    any other argument scans afresh. See :func:`_raw_sites_uncached`."""
    if src == SRC and repo_root == REPO_ROOT and consts is None and helpers is None:
        return {rel: list(hits) for rel, hits in _live_raw_sites().items()}
    return _raw_sites_uncached(src, repo_root, consts, helpers)


def _raw_sites_uncached(
    src: pathlib.Path = SRC, repo_root: pathlib.Path = REPO_ROOT,
    consts: dict[str, tuple[str, ...]] | None = None,
    helpers: frozenset[str] | None = None,
) -> dict[str, list[tuple[int, str]]]:
    """Every raw match under *src*, keyed by path relative to *repo_root*,
    with no exclusion applied. Prefix constants and ``"__"`` names are
    collected from *src* itself, so a constant defined in one file is seen in
    another. ``primitive_call`` hits inside ``corpus.py`` are dropped: the
    helpers call one another by construction, and the coordinator's ruling
    (RDR-204 nexus-ft04v.26) scopes the fifth class to callers outside
    ``corpus.py``'s own bodies; ``legacy_names.py``, the floor since RDR-229
    P2.2, holds the parser definitions, so no hit inside it is a site.

    Raises ``ValueError`` naming any file that does not parse: a file the
    walk cannot read is a hole in the census, never a quiet zero."""
    if consts is None:
        tree_consts, dunders = (
            _live_tree_constants() if src == SRC else tree_constants(src)
        )
    else:
        tree_consts, dunders = consts, tree_constants(src)[1]
    unparsed = [
        p.relative_to(repo_root).as_posix() for p in _py_files(src)
        if _parse(p) is None
    ]
    if unparsed:
        raise ValueError(f"census: file(s) did not parse: {unparsed}")
    from tests import _collection_name_census_cross as _cross  # noqa: PLC0415 - the cross module imports this one

    roots = (
        _cross.live_roots() if src == SRC and repo_root == REPO_ROOT and consts is None
        else _cross.derive_roots(src, repo_root, tree_consts, dunders)
    )
    found: dict[str, list[tuple[int, str]]] = {}
    for path in _py_files(src):
        rel = path.relative_to(repo_root).as_posix()
        hits = scan_file(path, tree_consts, helpers, dunders, roots)
        if rel == CORPUS_REL:
            hits = [(ln, s) for ln, s in hits if s != "primitive_call"]
        elif rel == LEGACY_NAMES_REL:
            hits = []  # the parsers' own bodies: the floor, whatever shape they spell
        if hits:
            found[rel] = hits
    return found


@dataclass(frozen=True)
class Site:
    """One ledger entry: a site's content anchor, shape, class and note.

    *anchor* is the site's stripped source text preceded by its nearest
    non-blank line (more when that is ambiguous). *n* is the number
    of hits of this shape on that line. A ``holdover`` must carry a *note*
    naming the Phase 2 step (``P2.n``) that moves it; other classes may.
    """
    rel: str
    anchor: tuple[str, ...]
    shape: str
    cls: str
    note: str = ""
    n: int = 1


_STEP_RE = re.compile(r"\bP2\.[0-9]+\b")


def _is_typed_label_site(s: Site) -> bool:
    """The one non-fnmatch ``label-pattern``: RDR item 1 names five parse classes, and a match on a label the user typed is none of them."""
    return s.cls == "label-pattern" and s.note == LABEL_TYPED


def ledger_problems(sites: list[Site]) -> list[str]:
    """Structural defects in the ledger itself: an unknown class or shape,
    a holdover whose note names no ``P2.n`` step, an fnmatch labelled as
    something it is not, a non-fnmatch carrying a fnmatch-only label, a
    duplicate (file, anchor, shape) entry. ``label-pattern`` is an fnmatch label,
    and also fits a non-fnmatch site that matches a label the user typed (P2.7's
    ``resolve_corpus`` fallback and its callers) when the site carries a note."""
    problems: list[str] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for s in sites:
        where = f"{s.rel} {s.anchor[-1]!r} [{s.shape}]"
        if s.cls not in CLASSES:
            problems.append(f"{where}: class {s.cls!r} is not one of {CLASSES}")
        if s.shape not in SHAPES:
            problems.append(f"{where}: shape {s.shape!r} is not one of {SHAPES}")
        if s.cls == "holdover" and not _STEP_RE.search(s.note):
            problems.append(f"{where}: a holdover's note must name the Phase 2 step (P2.n) that moves it")
        if s.shape == "fnmatch" and s.cls not in (*NON_PARSE_CLASSES, "routing"):
            problems.append(f"{where}: an fnmatch is a label-pattern, a file-path or routing, not {s.cls!r}")
        if s.shape != "fnmatch" and s.cls in NON_PARSE_CLASSES and not _is_typed_label_site(s):
            problems.append(
                f"{where}: {s.cls!r} is for an fnmatch; a {s.shape} site is a parse class "
                "(the one exception is a label-pattern carrying the LABEL_TYPED note)"
            )
        key = (s.rel, s.anchor, s.shape)
        if key in seen:
            problems.append(f"{where}: duplicate ledger entry")
        seen.add(key)
    return problems


def resolve_sites(
    repo_root: pathlib.Path, sites: list[Site],
) -> tuple[dict[tuple[str, int, str], Site], list[str]]:
    """Resolve every entry's anchor to its current line. Returns
    ``({(rel, lineno, shape): site}, problems)``; a STALE or AMBIGUOUS
    anchor is a problem, never a silent skip."""
    resolved: dict[tuple[str, int, str], Site] = {}
    problems: list[str] = []
    for s in sites:
        lineno, err = resolve_anchor(repo_root, s.rel, s.anchor)
        if err or lineno is None:
            problems.append(f"{s.rel} [{s.shape}] {s.anchor!r} -> {err}")
            continue
        key = (s.rel, lineno, s.shape)
        if key in resolved:
            problems.append(f"{s.rel}:{lineno} [{s.shape}]: two ledger entries resolve to one site")
            continue
        resolved[key] = s
    return resolved, problems


def live_counter(raw: dict[str, list[tuple[int, str]]]) -> Counter[tuple[str, int, str]]:
    return Counter(
        (rel, ln, shape) for rel, hits in raw.items() for ln, shape in hits
    )


def diff_ledger(
    live: Counter[tuple[str, int, str]],
    resolved: dict[tuple[str, int, str], Site],
) -> tuple[list[str], list[str]]:
    """``(unlabelled, stale)``. *unlabelled* names every live site with no
    ledger entry, or more hits than its entry counts; *stale* names every
    ledger entry whose site is gone or has fewer hits. Each message carries
    the file, the line and the shape."""
    unlabelled: list[str] = []
    stale: list[str] = []
    for key in sorted(live.keys() | resolved.keys()):
        rel, lineno, shape = key
        have = live.get(key, 0)
        want = resolved[key].n if key in resolved else 0
        if have > want:
            unlabelled.append(
                f"{rel}:{lineno} shape={shape}"
                + ("" if want == 0 else f" ({have} hits, ledger counts {want})")
                + (": UNLABELLED, add a ledger entry with a class" if want == 0 else "")
            )
        elif have < want:
            stale.append(f"{rel}:{lineno} shape={shape}: ledger counts {want}, live {have}")
    return unlabelled, stale
