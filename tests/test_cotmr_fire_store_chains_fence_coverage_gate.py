# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-cotmr / nexus-tafjk: the ``fire_store_chains`` fence-coverage
tripwire.

``tests/test_vw594_fence_coverage_gate.py`` (nexus-vw594 F4) AST-enumerates
every ``.fire_batch(`` call site and requires an ``_ALLOWLIST`` entry with a
fence-coverage reason. It structurally CANNOT see the CLI store-path
producers: they call ``hooks.fire_store_chains(...)``, a DIFFERENT method
name, and ``fire_store_chains``'s own internal ``self.fire_batch(...)`` call
lives inside ``hook_registry.py``, which F4's tripwire explicitly excludes as
"the dispatch mechanism, not a producer." That blind spot is exactly how the
nexus-tafjk gap shipped invisibly: commit f55435eb's F4 allowlist entry for
``("mcp/core.py", "store_put")`` claimed "MCP store_put / nx store put"
coverage, but the CLI half of that claim (``commands/store.py::put_cmd``)
was never independently checked because no tripwire ever looked at
``fire_store_chains(`` call sites at all.

This is the sibling gate for that surface, same idiom as F4:

1. AST-enumerate every ``.fire_store_chains(`` call site under
   ``src/nexus/`` (excluding ``hook_registry.py`` — same dispatch-mechanism
   exclusion F4 uses; the ``ThreadLocalHookRegistry.fire_store_chains``
   pass-through call lives there).
2. Require each site's (file, enclosing-function) pair to be in
   ``_ALLOWLIST`` with a non-empty, specific reason — an unlisted site
   fails loud, exactly the class of gap this file exists to close.
3. For allowlist entries claiming fence coverage (``fenced=True``),
   independently AST-verify that the enclosing function calls
   ``_fence_begin`` — mirroring F4's own same-function proof rather than
   trusting the registry's prose.
4. Non-vacuity: the allowlist is neither empty nor stale.
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: The dispatch mechanism itself, not a producer — same exclusion
#: rationale as test_vw594_fence_coverage_gate.py's _EXCLUDED_FILES.
_EXCLUDED_FILES = frozenset({"hook_registry.py"})

_FENCE_BEGIN_HELPERS = frozenset({"_fence_begin", "_fence_begin_many"})


@dataclass(frozen=True)
class _Coverage:
    reason: str
    fenced: bool


# (relative-path-from-src-nexus, enclosing-function-name) -> coverage record.
_ALLOWLIST: dict[tuple[str, str], _Coverage] = {
    ("commands/store.py", "put_cmd"): _Coverage(
        reason=(
            "nx store put: _fence_begin called in this same function "
            "before db.put; manifest_complete rides the fire_store_chains "
            "call at the tail, mirroring MCP core.py::store_put's F2 "
            "pattern verbatim (nexus-cotmr)."
        ),
        fenced=True,
    ),
    ("commands/memory.py", "promote_cmd"): _Coverage(
        reason=(
            "nx memory promote: _fence_begin called in this same function "
            "before t3.put; manifest_complete rides the fire_store_chains "
            "call at the tail, mirroring MCP core.py::store_put's F2 "
            "pattern verbatim (nexus-cotmr)."
        ),
        fenced=True,
    ),
    ("exporter.py", "_fire_store_chains_grouped_by_doc"): _Coverage(
        reason=(
            "nx store import: KNOWN, NAMED residual gap (nexus-tafjk "
            "DEFER-LOUD, out of nexus-cotmr's stated scope). Import "
            "batches are grouped by doc_id but each group can carry "
            "MULTIPLE chunks per document (unlike store put / promote's "
            "single-chunk shape), so the F2 single-chunk pattern does not "
            "directly transplant — it would need a content_hash computed "
            "over the whole group's chunk set, not a single chunk. Not "
            "fenced here; the group's catalog_doc_id key IS available if "
            "a future bead does this properly."
        ),
        fenced=False,
    ),
    ("catalog/recovery_bundle.py", "_default_import_doc"): _Coverage(
        reason=(
            "nx catalog import (recovery bundle, nexus-xn3fr review-fold): "
            "_fence_begin called in this same function before t3.put when a "
            "catalog row was minted; manifest_complete rides the "
            "fire_store_chains call at the tail, mirroring MCP "
            "core.py::store_put's F2 pattern. Single-chunk by construction "
            "(single_chunk_manifest_metadata), so the F2 shape transplants "
            "directly, unlike exporter.py's multi-chunk groups."
        ),
        fenced=True,
    ),
    ("commands/collection.py", "_reembed_collection"): _Coverage(
        reason=(
            "collection re-embed: this call passes catalog_doc_id='' "
            "UNCONDITIONALLY — it re-embeds EXISTING chunks across many "
            "documents in one page, not a single document write, so there "
            "is no single document identity to fence against. Structurally "
            "exempt, not merely unfenced."
        ),
        fenced=False,
    ),
}


@dataclass(frozen=True)
class _Site:
    rel_path: str
    function: str
    lineno: int


def _py_files() -> list[pathlib.Path]:
    return [
        p for p in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts and p.name not in _EXCLUDED_FILES
    ]


def _enclosing_function_name(stack: list[ast.AST]) -> str:
    for node in reversed(stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def _find_fire_store_chains_sites(tree: ast.Module, rel_path: str) -> list[_Site]:
    sites: list[_Site] = []
    stack: list[ast.AST] = []

    class _Visitor(ast.NodeVisitor):
        def generic_visit(self, node: ast.AST) -> None:
            stack.append(node)
            super().generic_visit(node)
            stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "fire_store_chains":
                sites.append(_Site(
                    rel_path=rel_path,
                    function=_enclosing_function_name(stack),
                    lineno=node.lineno,
                ))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def _function_calls_fence_begin(tree: ast.Module, function_name: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name:
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id in _FENCE_BEGIN_HELPERS
                ):
                    return True
    return False


def _all_sites() -> list[_Site]:
    sites: list[_Site] = []
    for path in _py_files():
        rel = str(path.relative_to(SRC_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites.extend(_find_fire_store_chains_sites(tree, rel))
    return sites


def test_every_fire_store_chains_producer_is_accounted_for() -> None:
    """Every fire_store_chains( call site under src/nexus/ is in
    _ALLOWLIST, fenced or explicitly, reasonedly not. An unlisted site is
    the nexus-tafjk class of gap: a new CLI store-path producer shipping
    with no fence coverage AND no tripwire to catch the omission."""
    sites = _all_sites()
    offenders = [s for s in sites if (s.rel_path, s.function) not in _ALLOWLIST]
    assert not offenders, (
        "nexus-cotmr fire_store_chains coverage gate: call site(s) with no "
        "_ALLOWLIST entry (add one — fenced=True with proof, or fenced=False "
        "with a specific reason why not):\n  "
        + "\n  ".join(f"{s.rel_path}:{s.lineno} in {s.function}()" for s in offenders)
    )


def test_allowlist_is_non_vacuous() -> None:
    assert len(_ALLOWLIST) >= 4, (
        f"allowlist has only {len(_ALLOWLIST)} entries — expected at least "
        "4 (one per known fire_store_chains call site); a shrunk allowlist "
        "likely means a call site was silently dropped from tracking."
    )
    for key, cov in _ALLOWLIST.items():
        assert cov.reason.strip(), f"{key}: allowlist entry with an empty reason"
        assert len(cov.reason.strip()) > 20, (
            f"{key}: allowlist reason too short to be a real justification "
            f"({cov.reason!r})"
        )


def test_allowlist_has_no_stale_entries() -> None:
    found_keys = {(s.rel_path, s.function) for s in _all_sites()}
    stale = sorted(set(_ALLOWLIST) - found_keys)
    assert not stale, (
        "nexus-cotmr fire_store_chains coverage gate: stale allowlist "
        f"entries (no longer call fire_store_chains — rename/move/removal?): {stale}"
    )


def test_fenced_allowlist_entries_are_proven() -> None:
    """For every allowlist entry claiming fenced=True, independently
    verify via AST that the named function really does call
    _fence_begin — the registry's prose is not trusted blindly.

    KILL CONTROL: commenting out the ``_fence_begin(...)`` call in
    ``commands/store.py``'s ``put_cmd`` (or ``commands/memory.py``'s
    ``promote_cmd``) turns this test RED for that specific entry while
    leaving every other test in this file green — verified manually
    during implementation, 2026-08-06."""
    trees: dict[str, ast.Module] = {}

    def _tree_for(rel_path: str) -> ast.Module:
        if rel_path not in trees:
            trees[rel_path] = ast.parse(
                (SRC_ROOT / rel_path).read_text(encoding="utf-8"),
                filename=rel_path,
            )
        return trees[rel_path]

    unproven = []
    for (rel_path, function), cov in _ALLOWLIST.items():
        if not cov.fenced:
            continue
        if not _function_calls_fence_begin(_tree_for(rel_path), function):
            unproven.append(f"{rel_path}:{function}()")
    assert not unproven, (
        "nexus-cotmr: allowlist entries claim fence coverage but no "
        f"_fence_begin call was found in that function: {unproven}"
    )


def test_unfenced_entries_are_the_documented_minimum() -> None:
    """Pins the exact set of deliberately-unfenced entries so a future
    author cannot quietly reclassify a real gap as 'structurally exempt'
    to dodge the AST proof above without review noticing the count/set
    change."""
    unfenced = sorted(k for k, cov in _ALLOWLIST.items() if not cov.fenced)
    assert unfenced == [
        ("commands/collection.py", "_reembed_collection"),
        ("exporter.py", "_fire_store_chains_grouped_by_doc"),
    ], (
        "unfenced allowlist entries changed — keep this to the documented "
        f"minimum, or fence the new site instead: {unfenced}"
    )
