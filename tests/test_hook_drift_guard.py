# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-095 drift guard: registered batch hooks must only be referenced
inside ``src/nexus/mcp_infra.py`` (definitions) and ``src/nexus/mcp/core.py``
(the single registration site).

The guard catches the original debt-accretion pattern from RDR-070 + RDR-086:
new per-document batch enrichment landing as hardcoded calls in five CLI
indexer files. Every other module fires through ``fire_post_store_batch_hooks``;
no third file imports or calls the hooks directly.

If this test fails on a future commit, the fix is almost always to register
a new batch hook via ``register_post_store_batch_hook`` in ``mcp/core.py``
rather than importing the hook function in the new module.
"""
from __future__ import annotations

import ast
import pathlib

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = PROJECT_ROOT / "src" / "nexus"

GUARDED_NAMES = frozenset({
    "taxonomy_assign_batch_hook",
    "chash_dual_write_batch_hook",
})

ALLOWED_FILES = frozenset({
    "src/nexus/mcp_infra.py",      # the definitions
    "src/nexus/mcp/core.py",       # the single registration site
})


def _scan_file_for_hook_refs(path: pathlib.Path) -> list[str]:
    """Return semantic references to guarded hook names in *path*.

    Counts:
      * ``from nexus.mcp_infra import <hook>``
      * ``import nexus.mcp_infra ... ; mcp_infra.<hook>`` attribute access
      * Bare-name references where the hook was already imported

    Excludes docstrings, comments, and string literals (which can mention
    the hook by name without coupling to it).
    """
    try:
        source = path.read_text()
    except (UnicodeDecodeError, OSError):
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    refs: list[str] = []

    for node in ast.walk(tree):
        # from nexus.mcp_infra import taxonomy_assign_batch_hook[, ...]
        if isinstance(node, ast.ImportFrom) and node.module == "nexus.mcp_infra":
            for alias in node.names:
                if alias.name in GUARDED_NAMES:
                    refs.append(
                        f"line {node.lineno}: from nexus.mcp_infra import {alias.name}"
                    )
        # mcp_infra.taxonomy_assign_batch_hook attribute access
        elif isinstance(node, ast.Attribute) and node.attr in GUARDED_NAMES:
            refs.append(f"line {node.lineno}: ...{node.attr} attribute access")
        # Bare-name reference (e.g. after `import taxonomy_assign_batch_hook`)
        elif isinstance(node, ast.Name) and node.id in GUARDED_NAMES:
            refs.append(f"line {node.lineno}: bare reference to {node.id}")

    return refs


def test_batch_hooks_not_called_outside_mcp_infra() -> None:
    """RDR-095 drift guard: no source file outside the allowlist may
    semantically reference taxonomy_assign_batch_hook or
    chash_dual_write_batch_hook. Every other module fires through
    fire_post_store_batch_hooks.
    """
    offenders: dict[str, list[str]] = {}
    for py in SRC_ROOT.rglob("*.py"):
        rel = py.relative_to(PROJECT_ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        refs = _scan_file_for_hook_refs(py)
        if refs:
            offenders[rel] = refs

    if offenders:
        formatted = "\n".join(
            f"  {path}:\n    " + "\n    ".join(refs)
            for path, refs in sorted(offenders.items())
        )
        raise AssertionError(
            "RDR-095 drift guard: registered batch hooks may only be "
            "referenced inside src/nexus/mcp_infra.py (definitions) and "
            "src/nexus/mcp/core.py (registration). New consumers should "
            "register via register_post_store_batch_hook in mcp/core.py "
            "and fire through fire_post_store_batch_hooks. Offenders:\n"
            f"{formatted}"
        )


def test_drift_guard_catches_synthetic_offender(tmp_path: pathlib.Path) -> None:
    """The drift guard's scanner correctly flags an ImportFrom of a
    guarded hook in an arbitrary file. Pinned so the guard cannot
    regress to a no-op.
    """
    offender = tmp_path / "offender.py"
    offender.write_text(
        "from nexus.mcp_infra import taxonomy_assign_batch_hook\n"
        "taxonomy_assign_batch_hook([], 'c', [], None, None)\n"
    )
    refs = _scan_file_for_hook_refs(offender)
    assert refs, "scanner must flag a direct ImportFrom of a guarded hook"
    assert any("taxonomy_assign_batch_hook" in r for r in refs)


def test_drift_guard_ignores_docstring_mentions(tmp_path: pathlib.Path) -> None:
    """The drift guard's scanner ignores string-literal mentions of hook
    names (so docstrings, comments, and log keys cannot trigger false
    positives).
    """
    benign = tmp_path / "benign.py"
    benign.write_text(
        '"""Mentions taxonomy_assign_batch_hook in a docstring."""\n'
        "# also chash_dual_write_batch_hook in a comment\n"
        "MSG = 'see chash_dual_write_batch_hook for details'\n"
    )
    refs = _scan_file_for_hook_refs(benign)
    assert refs == [], (
        "scanner must ignore docstring/comment/string-literal mentions"
    )


# ── Symmetric-fire invariant: every CLI ingest site calls BOTH chains ────────


CLI_SITE_FILES = [
    "src/nexus/indexer.py",
    "src/nexus/code_indexer.py",
    "src/nexus/prose_indexer.py",
    "src/nexus/pipeline_stages.py",
    "src/nexus/doc_indexer.py",
]


def test_every_cli_ingest_site_fires_both_chains() -> None:
    """Both `fire_post_store_batch_hooks` AND `fire_post_store_hooks` must
    be invoked from every CLI indexer module. Pins the symmetric-fire
    coverage: a single-doc consumer registered via
    register_post_store_hook (e.g. RDR-089 aspect extraction) is visible
    from MCP store_put AND from every CLI ingest path.

    The test asserts a Call node count rather than text presence so
    docstring or comment mentions do not satisfy the invariant. A future
    contributor who removes either fire on a CLI site fails CI here.
    """
    offenders: dict[str, list[str]] = {}
    for rel in CLI_SITE_FILES:
        path = PROJECT_ROOT / rel
        tree = ast.parse(path.read_text(), filename=str(path))
        batch_calls = 0
        single_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn_name = None
            if isinstance(node.func, ast.Name):
                fn_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fn_name = node.func.attr
            if fn_name == "fire_post_store_batch_hooks":
                batch_calls += 1
            elif fn_name == "fire_post_store_hooks":
                single_calls += 1
        problems: list[str] = []
        if batch_calls == 0:
            problems.append("missing fire_post_store_batch_hooks call")
        if single_calls == 0:
            problems.append("missing fire_post_store_hooks call")
        if batch_calls != single_calls:
            problems.append(
                f"chain-call mismatch: {batch_calls} batch fires vs "
                f"{single_calls} single-doc fires (must match)"
            )
        if problems:
            offenders[rel] = problems

    assert not offenders, (
        "RDR-095 symmetric-fire invariant: every CLI indexer site must "
        "call both fire_post_store_batch_hooks and fire_post_store_hooks "
        "the same number of times. Offenders:\n  "
        + "\n  ".join(f"{p}: {ps}" for p, ps in sorted(offenders.items()))
    )


# ── RDR-089 document-chain call-site presence ───────────────────────────────


# Expected fire counts per module for the document-grain chain.
# Total: 8 fire-statement instances across 7 modules (seven logical
# document boundaries — doc_indexer.py:index_pdf has two branch tails,
# accounting for the off-by-one between site count and module count).
# Mirrors CLI_SITE_FILES above plus mcp/core.py:store_put.
DOCUMENT_HOOK_FIRE_SITES: dict[str, int] = {
    "src/nexus/indexer.py": 1,             # _index_pdf_file (nx index repo PDF path)
    "src/nexus/doc_indexer.py": 3,         # _index_document + index_pdf x2
    "src/nexus/code_indexer.py": 1,        # index_code_file
    "src/nexus/prose_indexer.py": 1,       # index_prose_file
    "src/nexus/pipeline_stages.py": 1,     # pipeline_index_pdf (post _catalog_pdf_hook)
    "src/nexus/mcp/core.py": 1,            # store_put
}


def _count_fire_calls(tree: ast.AST, fn_name: str) -> int:
    """Count direct Call nodes whose callee is `fn_name` (matched by Name
    or Attribute attr).
    """
    count = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == fn_name:
            count += 1
        elif isinstance(node.func, ast.Attribute) and node.func.attr == fn_name:
            count += 1
    return count


def test_every_cli_ingest_site_fires_document_hook() -> None:
    """RDR-089 P0.2 wiring invariant: each CLI ingest module + the MCP
    store_put boundary must call ``fire_post_document_hooks`` exactly
    the documented number of times. Total: 7 fire-statement instances
    across 6 modules.

    The call counts are pinned per-module so a future contributor who
    drops a fire site (or accidentally double-fires inside a chunk
    loop) fails CI here, not at runtime.

    Either the alias name (``fire_post_document_hooks``) or the
    privatised import name (``_fire_post_document_hooks`` in
    ``mcp/core.py``) counts toward the total — they are the same
    callable.
    """
    offenders: dict[str, str] = {}
    for rel, expected in sorted(DOCUMENT_HOOK_FIRE_SITES.items()):
        path = PROJECT_ROOT / rel
        tree = ast.parse(path.read_text(), filename=str(path))
        actual = (
            _count_fire_calls(tree, "fire_post_document_hooks")
            + _count_fire_calls(tree, "_fire_post_document_hooks")
        )
        if actual != expected:
            offenders[rel] = f"expected {expected} call(s), got {actual}"

    assert not offenders, (
        "RDR-089 document-chain call-site invariant: each ingest "
        "module must fire fire_post_document_hooks the documented "
        "number of times. If this fails, the wiring drifted from the "
        "P0.2 fire-site map. Offenders:\n  "
        + "\n  ".join(f"{p}: {ps}" for p, ps in sorted(offenders.items()))
    )


def test_mcp_store_put_calls_document_hook_synchronously() -> None:
    """RDR-089 load-bearing contract (audit F1): the call to
    ``fire_post_document_hooks`` from ``mcp/core.py:store_put`` must be
    a *plain sync* invocation. No ``await``, no ``asyncio.to_thread``
    wrapping. ``store_put`` is ``def``, not ``async def``; FastMCP
    wraps sync ``@mcp.tool()`` bodies in worker threads at the
    framework level. Routing through ``async`` here would silently
    drop the returned coroutine — exactly the original RDR-089 defect.

    Pin via AST inspection: walk every ``Call`` node whose callee
    targets ``fire_post_document_hooks`` (or its alias), then assert no
    enclosing parent is an ``await`` or an ``asyncio.to_thread`` call.
    """
    rel = "src/nexus/mcp/core.py"
    path = PROJECT_ROOT / rel
    tree = ast.parse(path.read_text(), filename=str(path))

    # Build child→parent map for ancestry checks.
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    def _is_doc_hook_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in {
                "fire_post_document_hooks", "_fire_post_document_hooks",
            }
        if isinstance(node.func, ast.Attribute):
            return node.func.attr in {
                "fire_post_document_hooks", "_fire_post_document_hooks",
            }
        return False

    def _is_to_thread_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        # asyncio.to_thread(...) — Attribute access
        if isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread":
            return True
        if isinstance(node.func, ast.Name) and node.func.id == "to_thread":
            return True
        return False

    offending_calls: list[str] = []
    for node in ast.walk(tree):
        if not _is_doc_hook_call(node):
            continue
        # Walk up from this call to ensure no ancestor is await or to_thread.
        cur: ast.AST | None = node
        line = getattr(node, "lineno", 0)
        depth = 0
        while cur is not None and depth < 20:
            cur = parents.get(id(cur))
            if cur is None:
                break
            if isinstance(cur, ast.Await):
                offending_calls.append(
                    f"line {line}: fire_post_document_hooks wrapped in await"
                )
                break
            if _is_to_thread_call(cur):
                offending_calls.append(
                    f"line {line}: fire_post_document_hooks routed through "
                    f"asyncio.to_thread"
                )
                break
            depth += 1

    assert not offending_calls, (
        "RDR-089 sync-all-the-way-down contract (audit F1): "
        "fire_post_document_hooks at MCP store_put must be a plain "
        "synchronous call. await / asyncio.to_thread wrapping silently "
        "drops the dispatch (store_put is `def`, not `async def`). "
        "Offenders:\n  " + "\n  ".join(offending_calls)
    )
