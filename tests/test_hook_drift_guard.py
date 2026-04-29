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


# RDR-089 follow-up (nexus-qeo8): the aspect-extraction enqueue hook
# is the document-grain analogue of the batch-chain guard above. The
# hook lives in `nexus.aspect_worker` (not in mcp_infra, to keep the
# infra module dependency-light), so the allow-list differs.
DOCUMENT_HOOK_GUARDED_NAMES = frozenset({
    "aspect_extraction_enqueue_hook",
})

DOCUMENT_HOOK_ALLOWED_FILES = frozenset({
    "src/nexus/aspect_worker.py",  # the definition
    "src/nexus/mcp/core.py",       # the single registration site
})


def _scan_file_for_hook_refs(
    path: pathlib.Path,
    *,
    guarded_names: frozenset[str] = GUARDED_NAMES,
    source_module: str = "nexus.mcp_infra",
) -> list[str]:
    """Return semantic references to guarded hook names in *path*.

    Counts:
      * ``from <source_module> import <hook>``
      * ``import <source_module> ... ; <source_module>.<hook>`` attribute access
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
        # from <source_module> import <hook>[, ...]
        if isinstance(node, ast.ImportFrom) and node.module == source_module:
            for alias in node.names:
                if alias.name in guarded_names:
                    refs.append(
                        f"line {node.lineno}: from {source_module} import {alias.name}"
                    )
        # <module>.<hook> attribute access
        elif isinstance(node, ast.Attribute) and node.attr in guarded_names:
            refs.append(f"line {node.lineno}: ...{node.attr} attribute access")
        # Bare-name reference (e.g. after `import <hook>`)
        elif isinstance(node, ast.Name) and node.id in guarded_names:
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


# ── Document-chain GUARDED_NAMES wave 2 (nexus-qeo8) ─────────────────────────


def test_document_hook_not_called_outside_aspect_worker_and_core() -> None:
    """RDR-089 follow-up drift guard: the document-grain hook
    ``aspect_extraction_enqueue_hook`` may only be referenced inside
    its definition module (``src/nexus/aspect_worker.py``) and the
    single registration site (``src/nexus/mcp/core.py``). Every
    other module fires through ``fire_post_document_hooks`` — the
    framework dispatches.

    The same anti-pattern caught by the batch-chain guard above:
    a future contributor who imports the hook directly to call it
    inline (e.g. to "make extraction synchronous in this one path")
    re-introduces the synchronous-extraction bottleneck the P1.3
    spike specifically retired. The fix is always to fire through
    the chain dispatcher, not to import the hook.
    """
    offenders: dict[str, list[str]] = {}
    for py in SRC_ROOT.rglob("*.py"):
        rel = py.relative_to(PROJECT_ROOT).as_posix()
        if rel in DOCUMENT_HOOK_ALLOWED_FILES:
            continue
        refs = _scan_file_for_hook_refs(
            py,
            guarded_names=DOCUMENT_HOOK_GUARDED_NAMES,
            source_module="nexus.aspect_worker",
        )
        if refs:
            offenders[rel] = refs

    if offenders:
        formatted = "\n".join(
            f"  {path}:\n    " + "\n    ".join(refs)
            for path, refs in sorted(offenders.items())
        )
        raise AssertionError(
            "RDR-089 follow-up drift guard: "
            "aspect_extraction_enqueue_hook may only be referenced "
            "inside src/nexus/aspect_worker.py (definition) and "
            "src/nexus/mcp/core.py (registration). New consumers "
            "should fire through fire_post_document_hooks. "
            "Offenders:\n" + formatted
        )


def test_document_hook_drift_guard_catches_synthetic_offender(
    tmp_path: pathlib.Path,
) -> None:
    """The drift-guard scanner correctly flags an ImportFrom of the
    aspect-worker hook in an arbitrary file. Pinned so this guard
    cannot regress to a no-op."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "from nexus.aspect_worker import aspect_extraction_enqueue_hook\n"
        "aspect_extraction_enqueue_hook('/p', 'knowledge__delos', '')\n"
    )
    refs = _scan_file_for_hook_refs(
        offender,
        guarded_names=DOCUMENT_HOOK_GUARDED_NAMES,
        source_module="nexus.aspect_worker",
    )
    assert refs, "scanner must flag a direct ImportFrom of the guarded hook"
    assert any("aspect_extraction_enqueue_hook" in r for r in refs)


# ── nexus-9099 + nexus-jgzl: T3-write CLI parity guard ─────────────────────
#
# RDR-095 established symmetric-fire for the bulk indexer modules
# (test_every_cli_ingest_site_fires_both_chains above). nexus-9099
# discovered that ``nx store put``, ``nx memory promote``, and
# ``nx store import`` shipped without firing any chain — three CLI
# T3-write paths that the bulk-indexer guard couldn't reach because
# they live in ``commands/`` and ``exporter.py``, not the indexer
# files. The fix wires those three paths through ``fire_store_chains``
# (mcp_infra). This guard ensures any future CLI T3-write path also
# fires the chains: a function that calls ``X.put(collection=...)`` or
# ``X.upsert_chunks_with_embeddings(collection_name=...)`` in
# ``src/nexus/commands/`` or ``src/nexus/exporter.py`` must also call
# ``fire_store_chains`` in the same function body.
#
# The kwarg-shape heuristic is what distinguishes T3 writes from T2
# writes:
#   * ``T3Database.put`` is always called with ``collection=`` (T3
#     ChromaDB collection name).
#   * ``T2Database.put`` is called with ``project=`` (project namespace).
#   * ``T3Database.upsert_chunks_with_embeddings`` always takes
#     ``collection_name=``.
# So matching on ``X.put(collection=...)`` plus the upsert variant
# catches every T3 write and skips T2 writes without an allow-list.

T3_WRITE_PARITY_FILES: list[str] = [
    # All commands/*.py modules can host CLI T3 writes; scan them all
    # so a future contributor adding a new write command is caught.
    *sorted(
        str(p.relative_to(PROJECT_ROOT))
        for p in (SRC_ROOT / "commands").glob("*.py")
        if p.name != "__init__.py"
    ),
    # Library module that backs ``nx store import``.
    "src/nexus/exporter.py",
]


def _function_writes_to_t3(func: ast.AST) -> bool:
    """True if *func* contains a call shaped like a T3 write.

    Recognised shapes:

    * ``X.put(collection=…)`` — the T3Database.put signature.
      T2Database.put uses ``project=…`` and is correctly skipped.
    * ``X.upsert_chunks_with_embeddings(collection_name=…)`` — bulk
      import write; the only caller is exporter.import_collection.
    """
    for sub in ast.walk(func):
        if not isinstance(sub, ast.Call):
            continue
        if not isinstance(sub.func, ast.Attribute):
            continue
        attr = sub.func.attr
        kwarg_names = {kw.arg for kw in sub.keywords if kw.arg}
        if attr == "put" and "collection" in kwarg_names:
            return True
        if (
            attr == "upsert_chunks_with_embeddings"
            and "collection_name" in kwarg_names
        ):
            return True
    return False


def _function_calls_fire_store_chains(func: ast.AST) -> bool:
    """True if *func* contains a Call to ``fire_store_chains``."""
    for sub in ast.walk(func):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Name) and sub.func.id == "fire_store_chains":
            return True
        if (
            isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "fire_store_chains"
        ):
            return True
    return False


def test_every_cli_t3_write_function_fires_store_chains() -> None:
    """Every function in commands/*.py and exporter.py that writes to T3
    must also call ``fire_store_chains`` in the same function body.

    nexus-9099 (RDR-095 follow-up): the bulk-indexer drift guard
    (``test_every_cli_ingest_site_fires_both_chains``) doesn't cover
    CLI command modules. This guard closes that gap so any future
    write path gets caught at CI time, not at silent-corruption time.
    """
    offenders: list[str] = []
    for rel in T3_WRITE_PARITY_FILES:
        path = PROJECT_ROOT / rel
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _function_writes_to_t3(node):
                continue
            if not _function_calls_fire_store_chains(node):
                offenders.append(f"{rel}::{node.name}")

    assert not offenders, (
        "nexus-9099 + RDR-095 invariant: every CLI/library function that writes "
        "to T3 (via X.put(collection=...) or X.upsert_chunks_with_embeddings) "
        "must call fire_store_chains in the same function so the post-store "
        "hook chains fire from CLI ingest paths. Offenders:\n  "
        + "\n  ".join(offenders)
    )


def test_t3_write_helper_detects_known_shapes() -> None:
    """Sanity: the helper recognises the canonical T3-write call shapes
    and skips T2-write call shapes. If this test ever flips, the
    parity guard above silently stops catching regressions.
    """
    snippets = {
        # Should be detected (T3 write):
        "t3_put_kw": ("def f():\n    db.put(collection=c, content=x)\n", True),
        "t3_put_self_kw": (
            "def f():\n    self._t3.put(collection=c, content=x)\n", True,
        ),
        "upsert_kw": (
            "def f():\n    db.upsert_chunks_with_embeddings("
            "collection_name=c, ids=ids, documents=docs)\n",
            True,
        ),
        # Should NOT be detected (T2 write or unrelated):
        "t2_put_kw": (
            "def f():\n    db.put(project=p, title=t, content=x)\n", False,
        ),
        "no_call": ("def f():\n    pass\n", False),
        "positional_put": (
            "def f():\n    db.put(c, x)\n", False,
        ),
    }
    for name, (src, expected) in snippets.items():
        tree = ast.parse(src)
        func = tree.body[0]
        actual = _function_writes_to_t3(func)
        assert actual is expected, (
            f"_function_writes_to_t3 mis-classified {name!r}: "
            f"expected {expected}, got {actual}"
        )
