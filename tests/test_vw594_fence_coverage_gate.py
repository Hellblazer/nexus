# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-vw594 F4: the RUNFENCE coverage tripwire, mechanically enforced.

The investigation memo (T2 ``nx memory get -p nexus -t
"vw594-investigation-2026-08-04"``) found the fence under-installed: of the
10 producer call sites that fire the post-store batch-hook chain
(``HookRegistry.fire_batch(...)``, the ONLY path ``manifest_write_batch_hook``
— and therefore the index-run fence's completion ride — can ever see), only
4 called ``_fence_begin`` before F1/F2 landed. A docs-only fix degrades to
"hope" for the next producer (#11); this test is the mechanical tripwire —
the fence analogue of ``tests/daemon/test_lifecycle_gate.py`` (RDR-149 P6).

It does NOT try to verify fence coverage generically for arbitrary future
code (that would require whole-program dataflow analysis). It:

1. AST-enumerates every ``<expr>.fire_batch(`` call site under ``src/nexus/``
   (excluding ``hook_registry.py`` itself, which is the dispatch mechanism,
   not a producer).
2. Requires each site's (file, enclosing-function) pair to be an entry in
   ``_ALLOWLIST`` below, with a non-empty, specific reason — a call site
   that is not listed FAILS the test. This is the actual tripwire: a new
   producer #11 that fires the batch chain without ever being reasoned
   about here fails loud, exactly the class of gap this bead exists to
   close.
3. For allowlist entries claiming SAME-FUNCTION coverage (``same_function=
   True`` — the call to ``_fence_begin``/``_fence_begin_many`` lives in the
   identical enclosing function as the ``fire_batch`` call), independently
   AST-verifies that claim rather than trusting the registry's prose. Two
   sites (the ChunkBatcher flush-grain closures) are documented as
   CROSS-function coverage instead — their fence begin fires from a sibling
   closure via ``ChunkBatcher``'s ``on_batch_begin`` callback, not inline —
   and are not re-verified generically (verifying an arbitrary cross-
   function call graph is out of scope; the two cross-function entries are
   named explicitly and kept to the minimum the real architecture needs, per
   the design memo's own "allowlist asserted non-empty-by-reason, never a
   blanket exemption" instruction).
4. Asserts the allowlist itself is neither empty nor accidentally stale
   (every entry must correspond to a call site actually found in the
   codebase right now) — the non-vacuity requirement.

KILL CONTROL (documented per the task's TDD contract): reverting any ONE of
the F1/F2 same-function ``_fence_begin`` calls this gate depends on (e.g.
commenting out the ``_fence_begin(...)`` call in ``code_indexer.py``'s
``index_code_file``) turns ``test_same_function_allowlist_entries_are_proven``
RED for that entry, without touching this file. Adding a brand-new
``fire_batch(`` call site anywhere under ``src/nexus/`` with no matching
allowlist entry turns ``test_every_fire_batch_producer_is_fenced`` RED. Both
verified manually during implementation, 2026-08-04 (see the developer's T1
scratch write-back for the exact revert/restore transcript).
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: The dispatch mechanism itself — NOT a producer. HookRegistry.fire_batch's
#: own body, and ThreadLocalHookRegistry's pass-through delegation, both
#: contain literal `.fire_batch(` text but neither originates a batch; they
#: forward one a caller already fired.
_EXCLUDED_FILES = frozenset({"hook_registry.py"})

_FENCE_BEGIN_HELPERS = frozenset({"_fence_begin", "_fence_begin_many"})


@dataclass(frozen=True)
class _Coverage:
    reason: str
    same_function: bool


# (relative-path-from-src-nexus, enclosing-function-name) -> coverage record.
# One entry per real producer in the investigation memo's producer table
# (T2 nexus/vw594-investigation-2026-08-04 §2).
_ALLOWLIST: dict[tuple[str, str], _Coverage] = {
    ("doc_indexer.py", "_index_document"): _Coverage(
        reason=(
            "producer 1 (nx index md / nx dt index text): _fence_begin "
            "called in this same function before the fire_batch upsert "
            "(nexus-5xn3k.3)."
        ),
        same_function=True,
    ),
    ("doc_indexer.py", "_index_pdf_incremental"): _Coverage(
        reason=(
            "producer 2 (nx index pdf, >128 chunks, incremental path): "
            "_fence_begin called in this same function (nexus-5xn3k.3)."
        ),
        same_function=True,
    ),
    ("doc_indexer.py", "index_pdf"): _Coverage(
        reason=(
            "producer 3 (nx index pdf, <=128 chunks, small-doc inline "
            "path): _fence_begin called in this same function "
            "(nexus-5xn3k.3)."
        ),
        same_function=True,
    ),
    ("pipeline_stages.py", "uploader_loop"): _Coverage(
        reason=(
            "producer 4 (nx index pdf, streaming threshold): fence begin "
            "is stamped by the SIBLING stage function pipeline_index_pdf "
            "in the same module, before uploader_loop's thread starts "
            "consuming the upload queue — cross-function by the streaming "
            "pipeline's stage-split architecture (extractor/chunker/"
            "embedder/uploader running as separate stage functions), not "
            "same-function (nexus-5xn3k.3)."
        ),
        same_function=False,
    ),
    ("code_indexer.py", "index_code_file"): _Coverage(
        reason=(
            "producer 5 (nx index repo, code, legacy per-file fallback "
            "when the ChunkBatcher rejects the file or is absent): "
            "_fence_begin called in this same function (nexus-vw594 F1)."
        ),
        same_function=True,
    ),
    ("prose_indexer.py", "index_prose_file"): _Coverage(
        reason=(
            "producer 6 (nx index repo, prose/rdr, legacy per-file "
            "fallback): _fence_begin called in this same function "
            "(nexus-vw594 F1)."
        ),
        same_function=True,
    ),
    ("indexer.py", "_fire_deferred_hooks"): _Coverage(
        reason=(
            "producer 8 (ChunkBatcher FILE-grain deferred callback, "
            "wired as on_file_complete): no fence call of its own by "
            "design — begin/complete happen ONCE PER FLUSH via the "
            "sibling on_batch_begin/on_batch_complete callbacks "
            "(_fire_flush_grain_begin / _fire_flush_grain_hooks below), "
            "not once per file (nexus-vw594 F1 — the historical :3631 "
            "cost objection this design preserves)."
        ),
        same_function=False,
    ),
    ("indexer.py", "_fire_flush_grain_hooks"): _Coverage(
        reason=(
            "producer 7 (ChunkBatcher FLUSH-grain aggregate, the "
            "repo-index hot path, wired as on_batch_complete): begin is "
            "stamped by the SIBLING on_batch_begin callback "
            "(_fire_flush_grain_begin) which ChunkBatcher fires BEFORE "
            "the network upload; this function only carries the "
            "completion ride via manifest_complete= (nexus-vw594 F1)."
        ),
        same_function=False,
    ),
    ("indexer.py", "_index_pdf_file"): _Coverage(
        reason=(
            "producer 9 (nx index repo, PDF path, legacy per-file "
            "fallback): _fence_begin called in this same function "
            "(nexus-vw594 F1)."
        ),
        same_function=True,
    ),
    ("mcp/core.py", "store_put"): _Coverage(
        reason=(
            "producer 10 (MCP store_put / nx store put): _fence_begin "
            "called in this same function before t3.put; manifest_complete "
            "rides the existing fire_batch call (nexus-vw594 F2)."
        ),
        same_function=True,
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


def _find_fire_batch_sites(tree: ast.Module, rel_path: str) -> list[_Site]:
    sites: list[_Site] = []
    stack: list[ast.AST] = []

    class _Visitor(ast.NodeVisitor):
        def generic_visit(self, node: ast.AST) -> None:
            stack.append(node)
            super().generic_visit(node)
            stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "fire_batch":
                sites.append(_Site(
                    rel_path=rel_path,
                    function=_enclosing_function_name(stack),
                    lineno=node.lineno,
                ))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def _function_calls_fence_begin(tree: ast.Module, function_name: str) -> bool:
    """True iff SOME function named *function_name* anywhere in *tree*
    contains a Call to one of ``_FENCE_BEGIN_HELPERS`` in its body
    (nested defs included, matching how the enclosing-function walk above
    also descends into nested closures)."""
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
        sites.extend(_find_fire_batch_sites(tree, rel))
    return sites


def test_every_fire_batch_producer_is_fenced() -> None:
    """Every fire_batch( call site under src/nexus/ is accounted for in
    _ALLOWLIST. An unlisted site is EXACTLY the nexus-vw594 coverage-gap
    class: a new producer shipping without anyone reasoning about its
    fence coverage."""
    sites = _all_sites()
    offenders = [s for s in sites if (s.rel_path, s.function) not in _ALLOWLIST]
    assert not offenders, (
        "nexus-vw594 F4 fence coverage gate: fire_batch( call site(s) with "
        "no _ALLOWLIST entry (add one with a specific reason, or wire "
        "_fence_begin / _fence_begin_many before the call):\n  "
        + "\n  ".join(f"{s.rel_path}:{s.lineno} in {s.function}()" for s in offenders)
    )


def test_allowlist_is_non_vacuous() -> None:
    """The allowlist itself must not be empty, and every reason must be a
    specific, non-empty string — a bare catch-all entry would be the exact
    vacuity the design memo warned against."""
    assert len(_ALLOWLIST) >= 8, (
        f"allowlist has only {len(_ALLOWLIST)} entries — expected at least "
        "8 (one per known producer, per the investigation memo's producer "
        "table); a shrunk allowlist likely means a producer was silently "
        "dropped from tracking, not that fewer producers exist."
    )
    for key, cov in _ALLOWLIST.items():
        assert cov.reason.strip(), f"{key}: allowlist entry with an empty reason"
        assert len(cov.reason.strip()) > 20, (
            f"{key}: allowlist reason too short to be a real justification "
            f"({cov.reason!r})"
        )


def test_allowlist_has_no_stale_entries() -> None:
    """Every allowlist entry must correspond to a fire_batch( site that
    actually exists right now — a stale entry (function renamed / moved /
    no longer calls fire_batch) silently weakens the gate exactly like an
    over-broad entry would."""
    found_keys = {(s.rel_path, s.function) for s in _all_sites()}
    stale = sorted(set(_ALLOWLIST) - found_keys)
    assert not stale, (
        "nexus-vw594 F4 fence coverage gate: stale allowlist entries (no "
        f"longer call fire_batch — rename/move/removal?): {stale}"
    )


def test_same_function_allowlist_entries_are_proven() -> None:
    """For every allowlist entry claiming SAME-FUNCTION coverage
    (same_function=True), independently verify via AST that the named
    function really does call _fence_begin / _fence_begin_many — the
    registry's prose is not trusted blindly for the cases cheap enough to
    check mechanically.

    KILL CONTROL: commenting out any one of the F1/F2 same-function
    _fence_begin call sites (code_indexer.py/index_code_file,
    prose_indexer.py/index_prose_file, indexer.py/_index_pdf_file, or the
    pre-existing doc_indexer.py sites) turns this test RED for that
    specific entry while leaving every other test in this file green —
    verified manually 2026-08-04 (developer T1 scratch write-back has the
    transcript) by temporarily deleting the code_indexer.py _fence_begin
    call and re-running this test alone.
    """
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
        if not cov.same_function:
            continue
        if not _function_calls_fence_begin(_tree_for(rel_path), function):
            unproven.append(f"{rel_path}:{function}()")
    assert not unproven, (
        "nexus-vw594 F4: allowlist entries claim same-function fence "
        "coverage but no _fence_begin/_fence_begin_many call was found in "
        f"that function: {unproven}"
    )


def test_cross_function_entries_are_the_documented_minimum() -> None:
    """Exactly the two ChunkBatcher flush-grain closures use cross-function
    coverage (their begin fires from a sibling on_batch_begin callback,
    not inline) — plus the pre-existing streaming-pipeline stage split.
    Pins the count so a future author cannot quietly reclassify a
    same-function site as cross-function to dodge the AST proof above."""
    cross = sorted(k for k, cov in _ALLOWLIST.items() if not cov.same_function)
    assert cross == [
        ("indexer.py", "_fire_deferred_hooks"),
        ("indexer.py", "_fire_flush_grain_hooks"),
        ("pipeline_stages.py", "uploader_loop"),
    ], (
        "cross-function allowlist entries changed — this is the escape "
        f"hatch from AST proof, keep it to the documented minimum: {cross}"
    )
