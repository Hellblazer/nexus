# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""The client collection-name parse scanner and its per-site ledger engine
(RDR-229 P1.1, nexus-p2m1x.1).

Split out of ``tests/test_collection_name_parse_census.py`` so that file stays
under its size ceiling. Nothing here is a test; the test files import it.

WHAT IT MATCHES. One AST walk of a Python file yields ``(lineno, shape)``
hits. The nine shapes RDR-204 Phase 3 built keep their names, so every older
assertion reads unchanged: ``split`` / ``rsplit`` / ``partition`` /
``rpartition`` (``"__"`` argument), ``startswith`` / ``endswith`` (a literal
collection prefix, bare or in a tuple/list), ``in`` / ``not_in`` (against the
literal ``"__"``) and ``primitive_call``. RDR-229 Technical Design item 4
widens the fifth and adds seven more:

- ``primitive_call`` is now a call to ANY function in the DERIVED helper set
  (:func:`derive_parser_helpers`), not the three typed in
  ``nexus.corpus._CANDIDATE_STRING_PRIMITIVES``: every ``corpus.py`` function
  that reaches a name parser through the module's own call graph.
- ``fstring_prefix``: ``startswith``/``endswith`` on an f-string with a
  literal part ending in ``__``, or on a local bound to one.
- ``const_prefix``: ``startswith``/``endswith`` on a module constant that
  holds a collection prefix (a tuple, a dict's keys, a tuple of pairs).
- ``loop_prefix``: ``startswith``/``endswith`` on a name bound by a ``for``
  loop or comprehension over such a constant, its ``.items()``/``.keys()``,
  or a literal tuple of prefixes; scoped per function.
- ``substring``: ``"__<x>__" in name``, the f-string form, or a local bound
  to it.
- ``glob_default``: a string constant ``"<type>__*"``.
- ``sql_prefix``: a SQL string (it holds SELECT, WHERE or FROM) with
  ``starts_with(.., '<type>__')``, ``LIKE '<type>..'`` or
  ``split_part(.., '__', ..)``.
- ``fnmatch``: every ``fnmatch`` / ``fnmatchcase`` call. Each is labelled in
  the ledger as a label pattern, a file path or routing.

``taxonomy__`` joined the prefix set (the routing census scans it).

A docstring, a comment, or a bare string expression never contributes a hit:
the walk matches ``ast.Call`` / ``ast.Compare`` nodes and skips string
statements.

THE LEDGER. Every hit is one site. A site carries one class from
:data:`CLASSES` (the RDR's five, the two non-parse labels for ``fnmatch``,
and an explicit ``holdover`` that must carry a note naming the step that
moves it). The ledger is a table of :class:`Site` entries, content-anchored
through ``tests._lint_line_anchor`` the way ``_EXCLUDED_SITES`` is: the entry
holds the site's own stripped source text plus its nearest preceding
non-blank line, and its current line number is re-derived on every run.
:func:`diff_ledger` returns the unlabelled live sites and the stale ledger
entries; each message names file, line and shape.
"""
from __future__ import annotations

import ast
import pathlib
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache

from tests._lint_line_anchor import resolve_anchor

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC = REPO_ROOT / "src" / "nexus"
CORPUS_REL = "src/nexus/corpus.py"

#: Collection-type / lifecycle prefixes a raw ``startswith``/``endswith``
#: call names when it parses a collection NAME. RDR-204's five plus
#: ``taxonomy__`` (RDR-229 P1.1: the routing census scans it).
TYPE_PREFIXES = frozenset(
    {"code__", "docs__", "rdr__", "knowledge__", "taxonomy__", "quarantine-"}
)
_CT_PREFIXES = ("code__", "docs__", "rdr__", "knowledge__", "taxonomy__")

SHAPES = (
    "split", "rsplit", "partition", "rpartition", "startswith", "endswith",
    "in", "not_in", "primitive_call", "fstring_prefix", "const_prefix",
    "loop_prefix", "substring", "glob_default", "sql_prefix", "fnmatch",
)

#: The RDR-229 Technical Design item 1 classes, then the two labels for a
#: ``fnmatch`` that is not a parse (item 4), then the explicit holdover.
PARSE_CLASSES = (
    "mint", "migration-source", "diagnostic", "routing", "uri-classifier",
)
NON_PARSE_CLASSES = ("label-pattern", "file-path")
CLASSES = (*PARSE_CLASSES, *NON_PARSE_CLASSES, "holdover")

_STR_SPLITTERS = ("split", "rsplit", "partition", "rpartition")
_GLOB_DEFAULT_RE = re.compile(r"^(?:code|docs|rdr|knowledge|taxonomy)__\*$")
_SQL_KEYWORD_RE = re.compile(r"\b(?:SELECT|WHERE|FROM)\b")
_SQL_PREFIX_RE = re.compile(
    r"(?i:starts_with\s*\([^)]*'(?:code|docs|rdr|knowledge|taxonomy)__')"
    r"|LIKE\s+'(?:code|docs|rdr|knowledge|taxonomy|quarantine)"
    r"|(?i:split_part\s*\([^)]*'__')"
    r"|'quarantine-'"
)


def _str_const(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_prefix_text(s: str) -> bool:
    return s.startswith(_CT_PREFIXES) or s.startswith("quarantine-")


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


def _fstring_ends_dunder(node: ast.AST) -> bool:
    """An f-string with a literal part ending in ``__``."""
    return any(lit.endswith("__") for lit in _fstring_literals(node))


def _fstring_dunder_both_ends(node: ast.AST) -> bool:
    """``f"__{x}__"``: the first literal opens and the last closes with ``__``."""
    if not isinstance(node, ast.JoinedStr) or len(node.values) < 3:
        return False
    first, last = node.values[0], node.values[-1]
    return (
        isinstance(first, ast.Constant) and str(first.value).startswith("__")
        and isinstance(last, ast.Constant) and str(last.value).endswith("__")
    )


def module_prefix_constants(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Module-level names whose value holds a collection prefix anywhere in it
    (a str, a tuple, a dict's keys, a tuple of pairs): name -> prefixes."""
    out: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
            continue
        found = sorted({
            s for x in ast.walk(node.value)
            if (s := _str_const(x)) is not None and _is_prefix_text(s)
        })
        if not found:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = tuple(found)
    return out


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


@lru_cache(maxsize=1)
def live_prefix_constants() -> dict[str, tuple[str, ...]]:
    """Every prefix-holding module constant under ``src/nexus``, by name (a
    constant is imported by name, so the name is the cross-file key)."""
    consts: dict[str, tuple[str, ...]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = _parse(path)
        if tree is not None:
            consts.update(module_prefix_constants(tree))
    return consts


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


def derive_parser_helpers(corpus_tree: ast.Module) -> frozenset[str]:
    """The fifth class's helper set, derived from ``corpus.py``'s own call
    graph (RDR-229 Technical Design item 4), never typed as a list.

    Roots are the parser definitions: a module-level function that applies a
    module-level compiled regex (``re.compile``), or that holds a raw
    ``"__"`` split / ``in`` test. Every function that names a root or an
    already-reached function (a bare name or an attribute) joins the set,
    to a fixpoint; a module-level ``alias = helper`` assignment follows its
    target. ``is_conformant_collection_name`` and
    ``parse_conformant_collection_name`` are roots because they apply
    ``_CONFORMANT_COLLECTION_RE``.
    """
    regexes: set[str] = set()
    aliases: dict[str, str] = {}
    for n in corpus_tree.body:
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
    funcs = _module_functions(corpus_tree)
    refs: dict[str, set[str]] = {}
    reach: set[str] = set()
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
    return derive_parser_helpers(tree)


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


def _scope_bindings(
    nodes: list[ast.AST], consts: dict[str, tuple[str, ...]],
) -> tuple[set[str], set[str], set[str]]:
    """(loop-bound prefix names, f-string-prefix locals, f-string-substring
    locals) in one scope."""
    loopvars: set[str] = set()
    fprefix: set[str] = set()
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
                loopvars.add(first.id)
        if isinstance(n, ast.Assign):
            names = {t.id for t in n.targets if isinstance(t, ast.Name)}
            if _fstring_ends_dunder(n.value):
                fprefix |= names
            if _fstring_dunder_both_ends(n.value):
                fsub |= names
    return loopvars, fprefix, fsub


def _startswith_shape(
    call: ast.Call, consts: dict[str, tuple[str, ...]],
    loopvars: set[str], fprefix: set[str],
) -> str | None:
    """The one shape a ``startswith``/``endswith`` call matches, by priority:
    a literal prefix, a prefix constant, a loop-bound name, an f-string."""
    elts: list[ast.AST] = []
    for arg in call.args:
        elts.extend(arg.elts if isinstance(arg, (ast.Tuple, ast.List)) else [arg])
    attr = call.func.attr  # type: ignore[attr-defined]
    if any((s := _str_const(e)) is not None and s in TYPE_PREFIXES for e in elts):
        return attr
    if any(isinstance(e, (ast.Name, ast.Attribute)) and _ref_name(e) in consts for e in elts):
        return "const_prefix"
    if any(isinstance(e, ast.Name) and e.id in loopvars for e in elts):
        return "loop_prefix"
    if any(
        _fstring_ends_dunder(e) or (isinstance(e, ast.Name) and e.id in fprefix)
        for e in elts
    ):
        return "fstring_prefix"
    return None


def _compare_shapes(node: ast.Compare, fsub: set[str]) -> list[str]:
    shapes: list[str] = []
    for op, comp in zip(node.ops, node.comparators):
        if not isinstance(op, (ast.In, ast.NotIn)):
            continue
        left = _str_const(node.left)
        right = _str_const(comp)
        if left == "__" or right == "__":
            shapes.append("not_in" if isinstance(op, ast.NotIn) else "in")
            continue
        for side in (node.left, comp):
            text = _str_const(side)
            if (
                (text is not None and len(text) > 4
                 and text.startswith("__") and text.endswith("__"))
                or _fstring_dunder_both_ends(side)
                or (isinstance(side, ast.Name) and side.id in fsub)
            ):
                shapes.append("substring")
                break
    return shapes


def scan_file(
    path: pathlib.Path,
    consts: dict[str, tuple[str, ...]] | None = None,
    helpers: frozenset[str] | None = None,
) -> list[tuple[int, str]]:
    """``(lineno, shape)`` for every collection-name-parse-shaped site in one
    file, BEFORE any exclusion. *consts* is the prefix-constant map (default:
    this file's own module-level constants); *helpers* is the derived helper
    set (default: derived from the live ``corpus.py``)."""
    text = _read(path)
    tree = _parse(path)
    if text is None or tree is None:
        return []
    lines = text.splitlines()
    consts = {**module_prefix_constants(tree), **(consts or {})}
    helpers = live_parser_helpers() if helpers is None else helpers
    hits: list[tuple[int, str]] = []
    docstrings = {
        id(n.value) for n in ast.walk(tree)
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
    }
    scopes: list[ast.AST] = [tree] + [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        nodes = _scope_nodes(scope)
        loopvars, fprefix, fsub = _scope_bindings(nodes, consts)
        for node in nodes:
            if isinstance(node, ast.Call):
                hits.extend(_call_hits(node, consts, loopvars, fprefix, helpers))
            elif isinstance(node, ast.Compare):
                hits.extend((node.lineno, s) for s in _compare_shapes(node, fsub))
    for node in ast.walk(tree):
        text = _str_const(node)
        if text is None or id(node) in docstrings:
            continue
        if _GLOB_DEFAULT_RE.match(text):
            hits.append((node.lineno, "glob_default"))
        elif _SQL_KEYWORD_RE.search(text) and (m := _SQL_PREFIX_RE.search(text)):
            hits.append((_sql_hit_line(node, m.group(0), lines), "sql_prefix"))
    return sorted(hits)


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
    node: ast.Call, consts: dict[str, tuple[str, ...]],
    loopvars: set[str], fprefix: set[str], helpers: frozenset[str],
) -> list[tuple[int, str]]:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else ""
    )
    if name in ("fnmatch", "fnmatchcase"):
        return [(node.lineno, "fnmatch")]
    if name in helpers:
        return [(node.lineno, "primitive_call")]
    if not isinstance(func, ast.Attribute):
        return []
    if name in _STR_SPLITTERS:
        if node.args and _str_const(node.args[0]) == "__":
            return [(node.lineno, name)]
        return []
    if name in ("startswith", "endswith"):
        shape = _startswith_shape(node, consts, loopvars, fprefix)
        return [(node.lineno, shape)] if shape else []
    return []


def raw_sites(
    src: pathlib.Path = SRC, repo_root: pathlib.Path = REPO_ROOT,
    consts: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, list[tuple[int, str]]]:
    """Every raw match under *src*, keyed by path relative to *repo_root*,
    with no exclusion applied. ``primitive_call`` hits inside ``corpus.py``
    are dropped: the helpers call one another by construction, and the
    coordinator's ruling (RDR-204 nexus-ft04v.26) scopes the fifth class to
    callers outside ``corpus.py``'s own bodies."""
    consts = live_prefix_constants() if consts is None else consts
    found: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(src.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(repo_root).as_posix()
        hits = scan_file(path, consts)
        if rel == CORPUS_REL:
            hits = [(ln, s) for ln, s in hits if s != "primitive_call"]
        if hits:
            found[rel] = hits
    return found


@dataclass(frozen=True)
class Site:
    """One ledger entry: a site's content anchor, shape, class and note.

    *anchor* is the site's stripped source text preceded by its nearest
    non-blank line (more lines when that is ambiguous). *n* is the number
    of hits of this shape on that line. A ``holdover`` must carry a *note*
    naming the step that moves it; other classes may.
    """
    rel: str
    anchor: tuple[str, ...]
    shape: str
    cls: str
    note: str = ""
    n: int = 1


def ledger_problems(sites: list[Site]) -> list[str]:
    """Structural defects in the ledger itself: an unknown class or shape,
    a holdover with no note, a duplicate (file, anchor, shape) entry."""
    problems: list[str] = []
    seen: set[tuple[str, tuple[str, ...], str]] = set()
    for s in sites:
        where = f"{s.rel} {s.anchor[-1]!r} [{s.shape}]"
        if s.cls not in CLASSES:
            problems.append(f"{where}: class {s.cls!r} is not one of {CLASSES}")
        if s.shape not in SHAPES:
            problems.append(f"{where}: shape {s.shape!r} is not one of {SHAPES}")
        if s.cls == "holdover" and not s.note.strip():
            problems.append(f"{where}: a holdover must carry a note naming the step that moves it")
        if s.shape == "fnmatch" and s.cls not in (*NON_PARSE_CLASSES, "routing"):
            problems.append(f"{where}: an fnmatch is a label-pattern, a file-path or routing, not {s.cls!r}")
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
