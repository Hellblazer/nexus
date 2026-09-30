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
            "producer 1 (nx index md / nx index rdr / nx dt index text): the "
            "fence begin is the first request of the combined writer "
            "(_write_chunks_with_owner_rows -> MultiBatchDocumentWriter."
            "_begin_fence), which this function calls before its "
            "fire_batch — cross-function by RDR-223 design, since the "
            "writer owns the begin, the chunk+owner writes and the "
            "completion stamp as one protocol (nexus-z0o2p.13). Ordering "
            "is pinned by tests/integration/"
            "test_rdr223_index_document_journey.py (begin precedes the "
            "first write request)."
        ),
        same_function=False,
    ),
    ("doc_indexer.py", "_index_pdf_incremental"): _Coverage(
        reason=(
            "producer 2 (nx index pdf, >128 chunks, incremental path): the "
            "fence begin is the first request of the combined writer "
            "(_write_chunks_with_owner_rows -> MultiBatchDocumentWriter."
            "_begin_fence), called before this function's fire_batch — "
            "cross-function by RDR-223 design (nexus-z0o2p.15). Ordering "
            "is pinned by tests/integration/test_rdr223_pdf_journey.py "
            "(begin precedes the first write request)."
        ),
        same_function=False,
    ),
    ("doc_indexer.py", "index_pdf"): _Coverage(
        reason=(
            "producer 3 (nx index pdf, <=128 chunks, small-doc inline "
            "path): the fence begin is the first request of the combined "
            "writer, called before this function's fire_batch — "
            "cross-function by RDR-223 design (nexus-z0o2p.15). Ordering "
            "is pinned by tests/integration/test_rdr223_pdf_journey.py."
        ),
        same_function=False,
    ),
    ("pipeline_stages.py", "_flag"): _Coverage(
        reason=(
            "producer 4 (nx index pdf, streaming threshold): uploader_loop's "
            "nested _flag helper fires the batch hooks. The fence "
            "begin is the first request of the document's multi-batch "
            "writer (UploadRun.open_writer -> MultiBatchDocumentWriter), "
            "which uploader_loop feeds and whose sent batches are the only "
            "ones fire_batch is called for — cross-function by the "
            "writer's design (RDR-223, nexus-z0o2p.11). Ordering is pinned "
            "by tests/integration/test_rdr223_pdf_journey.py."
        ),
        same_function=False,
    ),
    ("commands/store.py", "put_cmd"): _Coverage(
        reason=(
            "producer 10, nx store put, split-note path (nexus-spujb): a "
            "note written as several chunks fires fire_batch inline; "
            "_fence_begin is called in this same function before the first "
            "piece is written. A one-piece note still rides "
            "fire_store_chains."
        ),
        same_function=True,
    ),
    ("catalog/recovery_bundle.py", "_default_import_doc"): _Coverage(
        reason=(
            "recovery-bundle import, split-note path (nexus-spujb): same "
            "shape as nx store put; _fence_begin is called in this same "
            "function before the first piece is written."
        ),
        same_function=True,
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
    not inline) — plus the pre-existing streaming-pipeline stage split, plus
    ``_index_document`` since RDR-223 (nexus-z0o2p.13), and the two
    non-streaming PDF paths (nexus-z0o2p.15), whose begin is the
    first request of the combined chunk+owner writer they call.
    Pins the count so a future author cannot quietly reclassify a
    same-function site as cross-function to dodge the AST proof above."""
    cross = sorted(k for k, cov in _ALLOWLIST.items() if not cov.same_function)
    assert cross == [
        ("doc_indexer.py", "_index_document"),
        ("doc_indexer.py", "_index_pdf_incremental"),
        ("doc_indexer.py", "index_pdf"),
        ("indexer.py", "_fire_deferred_hooks"),
        ("indexer.py", "_fire_flush_grain_hooks"),
        ("pipeline_stages.py", "_flag"),
    ], (
        "cross-function allowlist entries changed — this is the escape "
        f"hatch from AST proof, keep it to the documented minimum: {cross}"
    )


# ── RDR-223 (nexus-z0o2p.10): the multi-batch writer's fence leg ──────────────
#
# The gate above only sees fire_batch producers. Every path migrated onto the combined write
# writes through ``write_document(...)`` / ``MultiBatchDocumentWriter(...)`` instead, and the
# writer's fence (begin -> snapshot -> stamp) is only on when the caller passes ``content_hash``.
# Without it a multi-request document leaves a stale ``complete`` stamp on a half-replaced
# manifest, so the writer refuses a second request when ``content_hash`` is None. A caller that
# forgets the argument would then fail at RUNTIME, on the first large document. This leg makes it
# fail here instead, naming the call site.
#
# The rule is syntactic and deliberately blunt: every call under src/nexus must pass a
# ``content_hash=`` keyword that is not the literal ``None``. A ``**kwargs`` spread does not count
# (it proves nothing). A caller that genuinely writes ONE request per document and has no content
# hash (a note) is listed below, by (file, enclosing function), with the reason.

_WRITER_CALLABLES = frozenset({"write_document", "MultiBatchDocumentWriter"})

#: (relative-path-from-src-nexus, enclosing-function-name) -> why this caller may go unfenced.
#: Empty until a caller needs it; each entry needs a specific reason.
_UNFENCED_WRITER_CALLERS: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class _WriterSite:
    rel_path: str
    function: str
    lineno: int
    callee: str
    fenced: bool


def _find_writer_sites(tree: ast.Module, rel_path: str) -> list[_WriterSite]:
    sites: list[_WriterSite] = []
    stack: list[ast.AST] = []

    class _Visitor(ast.NodeVisitor):
        def generic_visit(self, node: ast.AST) -> None:
            stack.append(node)
            super().generic_visit(node)
            stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name in _WRITER_CALLABLES:
                fenced = any(
                    kw.arg == "content_hash"
                    and not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
                    for kw in node.keywords
                )
                sites.append(_WriterSite(
                    rel_path=rel_path, function=_enclosing_function_name(stack),
                    lineno=node.lineno, callee=name, fenced=fenced))
            self.generic_visit(node)

    _Visitor().visit(tree)
    return sites


def _all_writer_sites() -> list[_WriterSite]:
    sites: list[_WriterSite] = []
    for path in _py_files():
        rel = str(path.relative_to(SRC_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites.extend(_find_writer_sites(tree, rel))
    return sites


def test_every_multi_batch_writer_caller_passes_a_content_hash() -> None:
    unfenced = [
        s for s in _all_writer_sites()
        if not s.fenced and (s.rel_path, s.function) not in _UNFENCED_WRITER_CALLERS
    ]
    assert not unfenced, (
        "nexus-z0o2p.10: write_document / MultiBatchDocumentWriter called without a "
        "content_hash= keyword (or with the literal None), so the index-run fence is off and a "
        "multi-request document would leave a stale 'complete' stamp on a half-replaced "
        "manifest. Pass content_hash=, or, for a caller that provably writes one request per "
        "document with no content hash, add (file, function) to _UNFENCED_WRITER_CALLERS with "
        "the reason: "
        + ", ".join(f"{s.rel_path}:{s.function}():{s.lineno} ({s.callee})" for s in unfenced)
    )


def test_writer_leg_is_non_vacuous() -> None:
    """The scan must actually see the writer's own construction site (``write_document`` builds a
    ``MultiBatchDocumentWriter``), or a renamed callable would make the leg pass on nothing."""
    sites = _all_writer_sites()
    assert any(
        s.rel_path == "catalog/multi_batch_write.py" and s.function == "write_document"
        and s.callee == "MultiBatchDocumentWriter" and s.fenced
        for s in sites
    ), f"writer construction site not found by the scan: {sites}"


def test_unfenced_writer_allowlist_entries_are_real_and_reasoned() -> None:
    sites = {(s.rel_path, s.function) for s in _all_writer_sites()}
    stale = sorted(k for k in _UNFENCED_WRITER_CALLERS if k not in sites)
    assert not stale, f"_UNFENCED_WRITER_CALLERS entries with no call site: {stale}"
    weak = sorted(k for k, why in _UNFENCED_WRITER_CALLERS.items() if len(why.strip()) < 30)
    assert not weak, f"_UNFENCED_WRITER_CALLERS entries need a specific reason: {weak}"


def test_writer_leg_kill_control_flags_the_omissions() -> None:
    """The scanner flags a missing content_hash, a literal None and a **kwargs spread, and passes
    a real value."""
    src = (
        "def a():\n    write_document(cat, batches, doc_id=d, collection=c)\n"
        "def b():\n    write_document(cat, batches, doc_id=d, collection=c, content_hash=None)\n"
        "def c():\n    MultiBatchDocumentWriter(cat, **opts)\n"
        "def d():\n    mod.write_document(cat, batches, content_hash=h)\n"
    )
    sites = {s.function: s.fenced for s in _find_writer_sites(ast.parse(src), "x.py")}
    assert sites == {"a": False, "b": False, "c": False, "d": True}
