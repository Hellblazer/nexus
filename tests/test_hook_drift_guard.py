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
