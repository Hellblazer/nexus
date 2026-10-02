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

import pytest

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
            "completion stamp as one protocol (nexus-z0o2p.13). Two "
            "pins back it: the journey "
            "(tests/integration/test_rdr223_index_document_journey.py) "
            "shows the writer's begin precedes its first write request, and "
            "this file's AST leg (test_cross_function_entries_call_the_owner_"
            "write_before_fire_batch) checks the function reaches the owner "
            "write before its first fire_batch."
        ),
        same_function=False,
    ),
    ("doc_indexer.py", "_index_pdf_incremental"): _Coverage(
        reason=(
            "producer 2 (nx index pdf, >128 chunks, incremental path): the "
            "fence begin is the first request of the combined writer "
            "(_write_chunks_with_owner_rows -> MultiBatchDocumentWriter."
            "_begin_fence), called before this function's fire_batch — "
            "cross-function by RDR-223 design (nexus-z0o2p.15). Two pins "
            "back it: tests/integration/test_rdr223_pdf_journey.py shows the "
            "writer's begin precedes its first write request, and this "
            "file's AST leg (test_cross_function_entries_call_the_owner_"
            "write_before_fire_batch) checks the function reaches the owner "
            "write before its first fire_batch."
        ),
        same_function=False,
    ),
    ("doc_indexer.py", "index_pdf"): _Coverage(
        reason=(
            "producer 3 (nx index pdf, <=128 chunks, small-doc inline "
            "path): the fence begin is the first request of the combined "
            "writer, called before this function's fire_batch — "
            "cross-function by RDR-223 design (nexus-z0o2p.15). Two pins "
            "back it: tests/integration/test_rdr223_pdf_journey.py shows the "
            "writer's begin precedes its first write request, and this "
            "file's AST leg (test_cross_function_entries_call_the_owner_"
            "write_before_fire_batch) checks the function reaches the owner "
            "write before its first fire_batch."
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
            "writer's design (RDR-223, nexus-z0o2p.11). Two pins back it: "
            "tests/integration/test_rdr223_pdf_journey.py shows the writer's "
            "begin precedes its first write request, and this file's AST leg "
            "(test_cross_function_entries_call_the_owner_write_before_"
            "fire_batch) checks that every call of _flag follows the writer's "
            "open/finish or sits in the dry-run branch."
        ),
        same_function=False,
    ),
    ("code_indexer.py", "index_code_file"): _Coverage(
        reason=(
            "producer 5 (nx index repo, code, per-file path for a file the "
            "ChunkBatcher rejected as oversize, a file with no catalog "
            "identity, or a non-service T3): _fence_begin called in this same "
            "function (nexus-vw594 F1); the oversize leg writes through "
            "MultiBatchDocumentWriter, whose own fence begin re-affirms it "
            "(nexus-z0o2p.14)."
        ),
        same_function=True,
    ),
    ("prose_indexer.py", "index_prose_file"): _Coverage(
        reason=(
            "producer 6 (nx index repo, prose/rdr, per-file path for a file "
            "the ChunkBatcher rejected as oversize, a file with no catalog "
            "identity, or a non-service T3): _fence_begin called in this same "
            "function (nexus-vw594 F1); the oversize leg writes through "
            "MultiBatchDocumentWriter, whose own fence begin re-affirms it "
            "(nexus-z0o2p.14)."
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
            "producer 9 (nx index repo, PDF path, per-file path for a file "
            "the ChunkBatcher rejected as oversize, a file with no catalog "
            "identity, or a non-service T3): _fence_begin called in this same "
            "function (nexus-vw594 F1); the oversize leg writes through "
            "MultiBatchDocumentWriter, whose own fence begin re-affirms it "
            "(nexus-z0o2p.14)."
        ),
        same_function=True,
    ),
    ("catalog/note_write.py", "fire_note_chains"): _Coverage(
        reason=(
            "producers 10-13 (MCP store_put, nx store put, nx memory promote, the "
            "recovery-bundle import; RDR-223 P2.2/.6/.7/.8, nexus-z0o2p.12/.16/"
            ".17/.18): the four note producers fire their batch chain through this "
            "one function, after nexus.catalog.note_write.put_note wrote the note. "
            "The fence begins in put_note (before its write_note); the completion "
            "stamp rides the note's one write_manifest_many request, and the batch "
            "chain skips manifest_write_batch_hook. Cross-function: put_note is the "
            "one function every note producer calls, and fire_note_chains raises "
            "for any outcome that is not a stored note, so it cannot fire for a "
            "write that did not land. "
            "test_note_producers_begin_the_fence_in_put_note_before_the_write proves "
            "put_note calls _fence_begin before write_note, and "
            "test_note_producers_write_before_they_fire proves each of the four "
            "producers calls put_note before fire_note_chains."
        ),
        same_function=False,
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
    same-function site as cross-function to dodge the AST proof above.

    ``catalog/note_write.py::fire_note_chains`` joined them at RDR-223 Phase 2
    (nexus-z0o2p.12/.16/.17/.18): it is the one place MCP ``store_put``, ``nx store put``,
    ``nx memory promote`` and the recovery-bundle import fire their batch chain, and the fence
    begins in ``note_write.put_note``, the one function every note producer calls first. That
    claim is proven below, not asserted. (Until the joint review of those four each producer
    carried its own entry; the firing was hand-copied four times.)"""
    cross = sorted(k for k, cov in _ALLOWLIST.items() if not cov.same_function)
    assert cross == [
        ("catalog/note_write.py", "fire_note_chains"),
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


#: The note producers whose fence begins in ``put_note`` (the ``fire_note_chains`` entry above).
_PUT_NOTE_CALLERS = (
    ("mcp/core.py", "store_put"),
    ("commands/store.py", "put_cmd"),
    ("commands/memory.py", "promote_cmd"),
    ("catalog/recovery_bundle.py", "_default_import_doc"),
)


def _call_names_in(rel_path: str, function: str) -> list[tuple[int, str]]:
    """``(line, callee-name)`` of every call in *function*, in source order."""
    tree = ast.parse((SRC_ROOT / rel_path).read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == function)
    return sorted(
        (c.lineno, c.func.id if isinstance(c.func, ast.Name) else c.func.attr)
        for c in ast.walk(fn)
        if isinstance(c, ast.Call) and isinstance(c.func, (ast.Name, ast.Attribute)))


@pytest.mark.parametrize("rel_path,function", _PUT_NOTE_CALLERS)
def test_note_producers_call_put_note(rel_path: str, function: str) -> None:
    """Each note producer writes its note through ``put_note`` (RDR-223 P2.2 nexus-z0o2p.12,
    P2.6 nexus-z0o2p.16, P2.7 nexus-z0o2p.17, P2.8 nexus-z0o2p.18)."""
    names = [n for _l, n in _call_names_in(rel_path, function)]
    assert "put_note" in names, f"{function} must write its note through note_write.put_note"


@pytest.mark.parametrize("rel_path,function", _PUT_NOTE_CALLERS)
def test_note_producers_write_before_they_fire(rel_path: str, function: str) -> None:
    """The ordering half of the fence claim, for every producer: the ``put_note`` call (whose
    ``_fence_begin`` precedes ``write_note``, proven below) comes BEFORE the call that fires the
    batch chain, and nothing fires by hand: a producer that fired ``fire_batch`` first, or fired it
    itself, would batch-complete a document whose fence never began."""
    calls = _call_names_in(rel_path, function)
    names = [n for _l, n in calls]
    assert "fire_note_chains" in names, f"{function} must fire its chains through fire_note_chains"
    assert names.index("put_note") < names.index("fire_note_chains"), (
        f"{function} must write (and so fence) before it fires the batch chain: {calls}")
    assert "fire_batch" not in names, f"{function} fires fire_batch itself: {calls}"
    # RDR-223 decision of 2026-09-30 (nexus-z0o2p.34): the stamp is LAST. put_note writes with no
    # stamp, the chains fire, and stamp_note sends it.
    assert "stamp_note" in names, f"{function} must stamp its note through note_write.stamp_note"
    assert names.index("fire_note_chains") < names.index("stamp_note"), (
        f"{function} must fire its chains before it stamps the note: {calls}")


def test_note_producers_begin_the_fence_in_put_note_before_the_write() -> None:
    """The proof behind the cross-function entries: ``put_note`` calls ``_fence_begin`` before it
    calls ``write_note`` (RDR-223 P2.2, nexus-z0o2p.12). :func:`test_note_producers_call_put_note`
    proves each producer calls ``put_note``."""
    note_write = ast.parse((SRC_ROOT / "catalog" / "note_write.py").read_text())
    put_note = next(
        n for n in ast.walk(note_write)
        if isinstance(n, ast.FunctionDef) and n.name == "put_note")
    calls = sorted(
        (c.lineno, c.func.id) for c in ast.walk(put_note)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        and c.func.id in (_FENCE_BEGIN_HELPERS | {"write_note"}))
    names = [name for _line, name in calls]
    assert "_fence_begin" in names and "write_note" in names, names
    assert names.index("_fence_begin") < names.index("write_note"), (
        f"put_note must begin the fence before it writes: {calls}")


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


# ── RDR-223: the cross-function entries reach the owner write before their fire_batch ─────
#
# The journeys pin that the WRITER's fence begin precedes its first data request. They do not pin
# that these functions call the writer BEFORE they fire the post-store hooks, which is what makes
# the hooks read stored chunks and what the cross-function justification above claims. Nothing
# checked it: reordering ``fire_batch`` above the write would leave every test green until a
# hook read a chunk that was not there. This leg checks it syntactically, per entry.

#: The calls that hand chunks to the owner write: the combined write helper, and the streaming
#: run's writer (opened, fed, finished).
_OWNER_WRITE_CALLS = frozenset({"_write_chunks_with_owner_rows", "open_writer", "add_batch", "finish"})

#: (file, function) -> how its fire_batch must be preceded. "direct": the fire_batch sits in the
#: function's own body, after a call in _OWNER_WRITE_CALLS. "nested": the fire_batch is in a nested
#: helper (``_flag``); every call of the helper must follow an owner-write call in its own
#: function, or sit under ``if dry_run`` (a dry run has no owner write by design).
_OWNER_WRITE_ENTRIES: dict[tuple[str, str], str] = {
    ("doc_indexer.py", "_index_document"): "direct",
    ("doc_indexer.py", "_index_pdf_incremental"): "direct",
    ("doc_indexer.py", "index_pdf"): "direct",
    ("pipeline_stages.py", "_flag"): "nested",
}


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else None)


def _owner_write_order_problems(tree: ast.Module, function: str, mode: str) -> list[str]:
    """Why *function*'s fire_batch is not provably preceded by an owner write (empty: it is)."""
    defs = [n for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function]
    if not defs:
        return [f"{function}() not found"]
    problems: list[str] = []

    def owner_lines(fn: ast.AST) -> list[int]:
        return sorted(c.lineno for c in ast.walk(fn)
                      if isinstance(c, ast.Call) and _call_name(c) in _OWNER_WRITE_CALLS)

    if mode == "direct":
        for fn in defs:
            fires = sorted(c.lineno for c in ast.walk(fn)
                           if isinstance(c, ast.Call) and _call_name(c) == "fire_batch")
            owners = owner_lines(fn)
            if not fires:
                problems.append(f"{function}() has no fire_batch")
            elif not owners:
                problems.append(f"{function}() never calls the owner write")
            elif owners[0] > fires[0]:
                problems.append(
                    f"{function}(): first fire_batch at line {fires[0]} precedes the first owner "
                    f"write at line {owners[0]}")
        return problems

    # nested helper: every call of it, in the function that encloses its definition.
    for helper in defs:
        outer = next((n for n in ast.walk(tree)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not helper
                      and any(c is helper for c in ast.walk(n))), None)
        if outer is None:
            problems.append(f"{function}() is not nested")
            continue
        dry: set[int] = set()
        for n in ast.walk(outer):
            if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == "dry_run":
                dry.update(id(x) for x in ast.walk(n))
        scopes = [outer] + [n for n in ast.walk(outer)
                            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not outer]
        calls = [c for c in ast.walk(outer)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == function]
        if not calls:
            problems.append(f"{function}() is never called")
        for c in calls:
            if id(c) in dry:
                continue
            scope = min((sc for sc in scopes if any(x is c for x in ast.walk(sc))),
                        key=lambda sc: sum(1 for _ in ast.walk(sc)))
            if not any(line < c.lineno for line in owner_lines(scope)):
                problems.append(
                    f"{function}() called at line {c.lineno} with no owner-write call before it in "
                    f"{scope.name}() and outside the dry_run branch")
    return problems


def test_cross_function_entries_call_the_owner_write_before_fire_batch() -> None:
    problems: list[str] = []
    for (rel, function), mode in _OWNER_WRITE_ENTRIES.items():
        tree = ast.parse((SRC_ROOT / rel).read_text(encoding="utf-8"), filename=rel)
        problems += [f"{rel}: {p}" for p in _owner_write_order_problems(tree, function, mode)]
    assert not problems, (
        "nexus-z0o2p.11/.15 (S6): a cross-function fence entry no longer provably writes its chunks "
        "with their owner rows before it fires the post-store hooks:\n  " + "\n  ".join(problems))


def test_owner_write_entries_are_exactly_the_rdr223_cross_function_entries() -> None:
    """The entries checked above are the cross-function entries that are owner-write callers (the
    ChunkBatcher closures are cross-function by another mechanism), and every one is still an
    allowlist entry, so neither list can drift from the other."""
    cross = {k for k, cov in _ALLOWLIST.items() if not cov.same_function}
    assert set(_OWNER_WRITE_ENTRIES) <= cross
    assert cross - set(_OWNER_WRITE_ENTRIES) == {
        ("indexer.py", "_fire_deferred_hooks"), ("indexer.py", "_fire_flush_grain_hooks"),
        # the note writer's entry (RDR-223 P2.2): proved by its own test above, not an owner-write
        # caller of the PDF/document kind this list checks
        ("catalog/note_write.py", "fire_note_chains")}


def test_owner_write_leg_kill_control_flags_a_fire_batch_ahead_of_the_write() -> None:
    """The scanner passes a write-then-hook body, and flags a hook-then-write body, a body with no
    write, and an ungated nested-helper call ahead of the writer; it passes the dry-run-guarded
    and finish-guarded helper calls."""
    good = "def f():\n    _write_chunks_with_owner_rows(a)\n    hooks.fire_batch(b)\n"
    bad = "def f():\n    hooks.fire_batch(b)\n    _write_chunks_with_owner_rows(a)\n"
    none = "def f():\n    hooks.fire_batch(b)\n"
    assert _owner_write_order_problems(ast.parse(good), "f", "direct") == []
    assert _owner_write_order_problems(ast.parse(bad), "f", "direct")
    assert _owner_write_order_problems(ast.parse(none), "f", "direct")
    nested_ok = (
        "def outer(dry_run):\n"
        "    def _flag(x):\n        hooks.fire_batch(x)\n"
        "    def _land():\n        w.finish()\n        _flag(1)\n"
        "    if dry_run:\n        _flag(2)\n"
        "    else:\n        run.open_writer()\n        _flag(3)\n")
    nested_bad = (
        "def outer(dry_run):\n"
        "    def _flag(x):\n        hooks.fire_batch(x)\n"
        "    _flag(1)\n    run.open_writer()\n")
    assert _owner_write_order_problems(ast.parse(nested_ok), "_flag", "nested") == []
    assert _owner_write_order_problems(ast.parse(nested_bad), "_flag", "nested")


# ── RDR-223 decision D2 (2026-09-30): the completion stamp is the LAST thing EVERY writer path does ──
#
# The stamp used to ride the write, so a process killed in a post-store hook (taxonomy assignment,
# aspect enqueue, catalog enrichment) left a document that read complete and never got them: the
# next run's staleness check skips a complete document with a matching hash. Sam extended the rule
# from the PDF and markdown paths to every writer path (T2 nexus/rdr-223-stamp-last-every-path-
# decision-2026-09-30, nexus-z0o2p.34): the oversize fallbacks, the ChunkBatcher flush, the four
# note writers and the .nxexp import. Nothing but a code reading enforced it, and a reorder keeps
# every test that does not kill a hook green. This leg checks it syntactically, per path: the
# stamp-sending call is the last thing the path does after its hooks, and the write that precedes it
# carries NO stamp (``defer_completion=True`` / ``stamp=False`` / ``complete=None`` /
# ``defer_completion`` on the import writer).

_HOOK_CALLS = frozenset({"fire_batch", "fire_document", "fire_single"})


@dataclass(frozen=True)
class _StampPath:
    """One writer path.

    *stamps* is ``(call name, expected number of call sites)`` for the call that sends the
    completion stamp; *writes* is ``(call name, keyword, expected number of call sites)`` for the
    owner write that must NOT stamp (each call must pass the keyword as the constant *want*), or
    ``None`` when the path's write is made elsewhere and *handover* names the call that must pass a
    keyword to its callee instead. *before* lists calls that must precede every stamp (the hooks and
    enrichment the stamp vouches for): within the statements before the stamp in its innermost
    ``try`` body (``before_in="try"``, falling back to the function body), in the statement list that
    holds the stamp (``"block"``, which keeps one branch's hook from vouching for a sibling branch's
    stamp) or anywhere earlier in the function (``"function"``). *no_hook_after* demands that no
    ``fire_*`` call follows a stamp in its innermost ``try`` body (or in the function).
    """

    rel: str
    function: str
    stamps: tuple[str, int]
    writes: tuple[str, str, object, int] | None = None
    handover: tuple[str, str] | None = None
    before: tuple[str, ...] = ()
    no_hook_after: bool = True
    before_in: str = "try"


_STAMP_LAST_ENTRIES: tuple[_StampPath, ...] = (
    # ── doc_indexer: markdown and PDF (decision D2 of 2026-09-30, the first scope) ──
    _StampPath("doc_indexer.py", "_index_document", ("complete", 1),
               writes=("_write_chunks_with_owner_rows", "defer_completion", True, 1),
               before=("fire_batch", "fire_document")),
    _StampPath("doc_indexer.py", "_index_pdf_incremental", ("complete", 1),
               writes=("_write_chunks_with_owner_rows", "defer_completion", True, 1),
               before=("fire_batch",)),
    # index_pdf holds two stamps: the small path's own, and the one it sends for the incremental
    # path after that path's fire_document; both follow the catalog enrichment.
    _StampPath("doc_indexer.py", "index_pdf", ("complete", 2),
               writes=("_write_chunks_with_owner_rows", "defer_completion", True, 1),
               before=("_register_in_catalog",)),
    # index_markdown stamps what _index_document handed back, after _catalog_markdown_hook.
    _StampPath("doc_indexer.py", "index_markdown", ("_stamp_last", 2),
               handover=("_index_document", "pending_stamp"),
               before=("_catalog_markdown_hook",), no_hook_after=False, before_in="block"),
    # ── the three oversize fallbacks of nx index repo (nexus-z0o2p.34) ──
    _StampPath("code_indexer.py", "index_code_file", ("complete_oversize_write", 1),
               writes=("write_oversize_file", "defer_completion", True, 1), before=("fire_document",)),
    _StampPath("prose_indexer.py", "index_prose_file", ("complete_oversize_write", 1),
               writes=("write_oversize_file", "defer_completion", True, 1), before=("fire_document",)),
    _StampPath("indexer.py", "_index_pdf_file", ("complete_oversize_write", 1),
               writes=("write_oversize_file", "defer_completion", True, 1), before=("fire_document",)),
    # The oversize writer itself: passes the flag through, and its stamp helper stamps exactly once.
    _StampPath("oversize_write.py", "complete_oversize_write", ("complete", 1)),
    # ── the four note writers: put_note writes with stamp=False, the producer fires, stamp_note stamps ──
    _StampPath("catalog/note_write.py", "put_note", ("stamp_note", 0),
               writes=("write_note", "stamp", False, 1)),
    _StampPath("mcp/core.py", "store_put", ("stamp_note", 1), before=("fire_note_chains",), no_hook_after=False,
               before_in="function"),
    _StampPath("commands/store.py", "put_cmd", ("stamp_note", 1), before=("fire_note_chains",), no_hook_after=False,
               before_in="function"),
    _StampPath("commands/memory.py", "promote_cmd", ("stamp_note", 1), before=("fire_note_chains",), no_hook_after=False,
               before_in="function"),
    _StampPath("catalog/recovery_bundle.py", "_default_import_doc", ("stamp_note", 1),
               before=("fire_note_chains",), no_hook_after=False,
               before_in="function"),
    # ── the .nxexp import: the writer defers, each page's stamps follow that page's chains ──
    _StampPath("exporter.py", "_ensure", ("complete_documents", 0),
               writes=("MultiDocumentImportWriter", "defer_completion", True, 1)),
    _StampPath("exporter.py", "flush", ("complete_documents", 1),
               before=("_fire_store_chains_grouped_by_doc",), no_hook_after=False),
    # ── the ChunkBatcher flush of nx index repo ──
    _StampPath("chunk_batcher.py", "_flush_batch", ("_on_batch_stamp", 1),
               before=("_on_batch_complete", "_invoke_callbacks"), no_hook_after=False,
               before_in="function"),
)


def _fn_defs(tree: ast.Module, name: str) -> list[ast.FunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


def _calls_named(fn: ast.AST, name: str) -> list[ast.Call]:
    return [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _call_name(c) == name]


def _const_kw(call: ast.Call, arg: str, want: object) -> bool:
    kws = [k for k in call.keywords if k.arg == arg]
    return len(kws) == 1 and isinstance(kws[0].value, ast.Constant) and kws[0].value.value is want


def _earlier_statements(fn: ast.AST, stamp: ast.Call, mode: str) -> list[ast.stmt]:
    """The statements that run before *stamp*, per *mode* (see :class:`_StampPath`); a statement that
    contains the stamp itself is never one of them."""
    if mode == "function":
        return [s for s in ast.walk(fn) if isinstance(s, ast.stmt) and s.lineno < stamp.lineno
                and not any(x is stamp for x in ast.walk(s))]
    lists: list[list[ast.stmt]] = []
    for node in ast.walk(fn):
        for attr in (("body",) if mode == "try" and isinstance(node, ast.Try) else
                     ("body", "orelse", "finalbody") if mode == "block" else ()):
            block = getattr(node, attr, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt) \
                    and any(x is stamp for s in block for x in ast.walk(s)):
                lists.append(block)
    if not lists:
        lists = [fn.body]
    block = min(lists, key=lambda b: sum(1 for s in b for _ in ast.walk(s)))
    return [s for s in block if s.lineno < stamp.lineno and not any(x is stamp for x in ast.walk(s))]


def _stamp_path_problems(tree: ast.Module, spec: _StampPath) -> list[str]:
    fns = _fn_defs(tree, spec.function)
    if not fns:
        return [f"{spec.function}() not found"]
    problems: list[str] = []
    name, want_stamps = spec.stamps
    for fn in fns:
        stamps = _calls_named(fn, name)
        if len(stamps) != want_stamps:
            problems.append(f"{spec.function}() has {len(stamps)} {name}() call(s), expected {want_stamps}")
        if spec.writes is not None:
            wname, kw, want, n_writes = spec.writes
            writes = _calls_named(fn, wname)
            if len(writes) != n_writes:
                problems.append(f"{spec.function}() has {len(writes)} {wname}() call(s), expected {n_writes}")
            for w in writes:
                if not _const_kw(w, kw, want):
                    problems.append(
                        f"{spec.function}(): {wname}() at line {w.lineno} does not pass {kw}={want!r}, "
                        "so the write stamps the document itself, ahead of the hooks")
        if spec.handover is not None:
            hname, kw = spec.handover
            for site in _calls_named(tree, hname):
                if not [k for k in site.keywords if k.arg == kw]:
                    problems.append(
                        f"{hname}() at line {site.lineno} does not pass {kw}=, so it stamps the "
                        "document itself, ahead of the caller's enrichment")
            if not _calls_named(fn, hname):
                problems.append(f"{spec.function}() never calls {hname}()")
        for stamp in stamps:
            earlier = _earlier_statements(fn, stamp, spec.before_in)
            for pre in spec.before:
                if not [c for stmt in earlier for c in _calls_named(stmt, pre)]:
                    problems.append(
                        f"{spec.function}(): {name}() at line {stamp.lineno} is not preceded by a {pre}() call "
                        f"(searched {spec.before_in!r})")
            if spec.no_hook_after:
                tries = [t for t in ast.walk(fn) if isinstance(t, ast.Try)
                         and any(x is stamp for stmt in t.body for x in ast.walk(stmt))]
                scope = (min(tries, key=lambda t: sum(1 for _ in ast.walk(t))).body if tries else fn.body)
                later = sorted(x.lineno for stmt in scope for x in ast.walk(stmt)
                               if isinstance(x, ast.Call) and _call_name(x) in _HOOK_CALLS and x.lineno > stamp.lineno)
                if later:
                    problems.append(
                        f"{spec.function}(): {name}() at line {stamp.lineno} precedes a hook fire at line {later[0]}")
    return problems


def _entry_tree(spec: _StampPath) -> ast.Module:
    return ast.parse((SRC_ROOT / spec.rel).read_text(encoding="utf-8"), filename=spec.rel)


def test_every_writer_path_stamps_complete_after_its_hooks() -> None:
    problems: list[str] = []
    for spec in _STAMP_LAST_ENTRIES:
        problems += [f"{spec.rel}: {p}" for p in _stamp_path_problems(_entry_tree(spec), spec)]
    assert not problems, (
        "RDR-223 D2 (2026-09-30): a writer path no longer stamps the document complete after its "
        "post-store hooks, so a kill in a hook would leave a complete document whose hooks never "
        "ran:\n  " + "\n  ".join(problems))


def test_the_stamp_last_gate_lists_every_writer_path() -> None:
    """Non-vacuity and completeness: the table names each of the writer paths of RDR-223 Technical
    Design (the list below is the RDR's own), so adding a path means adding it here."""
    listed = {(s.rel, s.function) for s in _STAMP_LAST_ENTRIES}
    for path in (
        ("doc_indexer.py", "_index_document"), ("doc_indexer.py", "_index_pdf_incremental"),
        ("doc_indexer.py", "index_pdf"), ("doc_indexer.py", "index_markdown"),
        ("code_indexer.py", "index_code_file"), ("prose_indexer.py", "index_prose_file"),
        ("indexer.py", "_index_pdf_file"), ("chunk_batcher.py", "_flush_batch"),
        ("mcp/core.py", "store_put"), ("commands/store.py", "put_cmd"),
        ("commands/memory.py", "promote_cmd"), ("catalog/recovery_bundle.py", "_default_import_doc"),
        ("exporter.py", "flush"),
    ):
        assert path in listed, f"the stamp-last gate does not list {path}"
    assert len(_STAMP_LAST_ENTRIES) >= 16


def test_the_streaming_pdf_pipeline_stamps_after_its_post_pass() -> None:
    """The streaming PDF path (the one D2 started from) stamps through ``writer.complete()`` after
    its metadata post-pass and ``fire_document``; pinned here so the table above is the whole set."""
    tree = ast.parse((SRC_ROOT / "pipeline_stages.py").read_text(encoding="utf-8"))
    completes = [c for fn in _fn_defs(tree, "pipeline_index_pdf") for c in _calls_named(fn, "complete")]
    assert completes, "pipeline_index_pdf no longer stamps with writer.complete()"


def _flush_wiring_problems(tree: ast.Module) -> list[str]:
    """The ChunkBatcher flush: ``_batch_flush``'s ``write_manifest_many`` is sent with ``complete=None``,
    ``_stamp_flush_documents`` sends ONE stamp-only ``append_many`` (a ``complete``, no ``chunks``),
    and the ChunkBatcher built by ``_run_index`` is handed it as ``on_batch_stamp``."""
    problems: list[str] = []
    (run_index,) = _fn_defs(tree, "_run_index")

    def retry_calls(fn: ast.AST, target: str) -> list[ast.Call]:
        return [c for c in _calls_named(fn, "_manifest_write_with_retry")
                if c.args and isinstance(c.args[0], ast.Attribute) and c.args[0].attr == target]

    (flush,) = _fn_defs(run_index, "_batch_flush")
    writes = retry_calls(flush, "write_manifest_many")
    if len(writes) != 1 or not _const_kw(writes[0], "complete", None):
        problems.append("_batch_flush's write_manifest_many must be sent with complete=None (the stamp follows the hooks)")
    (stamp,) = _fn_defs(run_index, "_stamp_flush_documents")
    sends = retry_calls(stamp, "append_manifest_many")
    if len(sends) != 1 or not [k for k in sends[0].keywords if k.arg == "complete"] \
            or [k for k in sends[0].keywords if k.arg == "chunks"]:
        problems.append("_stamp_flush_documents must send ONE append_manifest_many with complete= and no chunks=")
    if retry_calls(stamp, "write_manifest_many"):
        problems.append("_stamp_flush_documents must not write a manifest")
    batchers = _calls_named(run_index, "ChunkBatcher")
    wired = [k for c in batchers for k in c.keywords
             if k.arg == "on_batch_stamp" and isinstance(k.value, ast.Name) and k.value.id == "_stamp_flush_documents"]
    if len(batchers) != 1 or len(wired) != 1:
        problems.append("the ChunkBatcher built by _run_index must be given on_batch_stamp=_stamp_flush_documents")
    # A document the write failed in place must never be stamped: the write records failed_doc_ids
    # in _flush_failed_docs and the stamp consults (and consumes) it.
    for fn, role in ((flush, "_batch_flush records"), (stamp, "_stamp_flush_documents consults")):
        if not [n for n in ast.walk(fn) if isinstance(n, ast.Name) and n.id == "_flush_failed_docs"]:
            problems.append(f"{role} the flush's failed_doc_ids through _flush_failed_docs (a failed document must not be stamped)")
    # Consulting the dict is not enough: the failed set must FILTER the owed set. Dropping the
    # exclusion while keeping the pop leaves every name above present (nexus-ioauc, the Phase 2
    # gate's M3 mutation), so the assignment to ``owed`` must itself name the failed-document set.
    owed_assigns = [n for n in ast.walk(stamp)
                    if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "owed" for t in n.targets)]
    if len(owed_assigns) != 1 or not [
        n for n in ast.walk(owed_assigns[0].value) if isinstance(n, ast.Name) and n.id == "_write_failed"
    ]:
        problems.append("_stamp_flush_documents' owed-set filter must exclude _write_failed (a failed document must not be stamped)")
    return problems


def test_every_deferred_flush_write_carries_no_stamp_and_the_batcher_is_wired_to_the_stamp() -> None:
    problems = _flush_wiring_problems(ast.parse((SRC_ROOT / "indexer.py").read_text(encoding="utf-8")))
    assert not problems, "RDR-223 D2 (flush): " + "; ".join(problems)


def test_the_oversize_writer_passes_the_deferral_through_and_never_stamps_a_deferred_write() -> None:
    tree = ast.parse((SRC_ROOT / "oversize_write.py").read_text(encoding="utf-8"))
    (fn,) = _fn_defs(tree, "write_oversize_file")
    (writer,) = _calls_named(fn, "MultiBatchDocumentWriter")
    kws = [k for k in writer.keywords if k.arg == "defer_completion"]
    assert len(kws) == 1 and isinstance(kws[0].value, ast.Name) and kws[0].value.id == "defer_completion"
    assert not _calls_named(fn, "complete"), "write_oversize_file must leave the stamp to complete_oversize_write"


def test_the_note_writer_and_the_import_writer_defer_their_stamp_by_construction() -> None:
    """put_note hands the stamp to stamp_note (the write passes stamp=False, which write_note turns
    into a write_one_request with no content hash), and the import writer is built deferred."""
    tree = ast.parse((SRC_ROOT / "catalog" / "note_write.py").read_text(encoding="utf-8"))
    (write_note,) = _fn_defs(tree, "write_note")
    sends = _calls_named(write_note, "write_one_request")
    assert sends and all(
        any(k.arg == "content_hash" and isinstance(k.value, ast.IfExp) for k in c.keywords) for c in sends), (
        "write_note must send no content_hash when stamp=False")
    (stamp_note,) = _fn_defs(tree, "stamp_note")
    assert len(_calls_named(stamp_note, "complete_document")) == 1


# ── kill controls: the checker is red on a stamp ahead of a hook, a dropped stamp and a dropped deferral ──


def test_stamp_path_checker_kill_control_flags_each_defect() -> None:
    spec = _StampPath("x.py", "f", ("complete_oversize_write", 1),
                      writes=("write_oversize_file", "defer_completion", True, 1), before=("fire_document",))
    good = ("def f():\n    try:\n        p = write_oversize_file(defer_completion=True)\n"
            "        hooks.fire_document(d)\n        complete_oversize_write(p)\n    finally:\n        pass\n")
    early = good.replace("        hooks.fire_document(d)\n        complete_oversize_write(p)\n",
                         "        complete_oversize_write(p)\n        hooks.fire_document(d)\n")
    dropped = good.replace("        complete_oversize_write(p)\n", "        pass\n")
    no_defer = good.replace("(defer_completion=True)", "()")
    false_defer = good.replace("defer_completion=True", "defer_completion=False")
    assert _stamp_path_problems(ast.parse(good), spec) == []
    for label, text in (("early", early), ("dropped", dropped), ("no_defer", no_defer), ("false_defer", false_defer)):
        assert _stamp_path_problems(ast.parse(text), spec), label


def _real_text(rel: str) -> str:
    return (SRC_ROOT / rel).read_text(encoding="utf-8")


def _mutate_in_function(text: str, function: str, old: str, new: str) -> str:
    """*text* with the first *old* inside *function*'s own lines replaced by *new*; asserts it was there."""
    fn = next(n for n in ast.walk(ast.parse(text))
              if isinstance(n, ast.FunctionDef) and n.name == function)
    lines = text.splitlines(keepends=True)
    body = "".join(lines[fn.lineno - 1:fn.end_lineno])
    assert old in body, f"non-vacuity: {old!r} is not in {function}()"
    mutated = body.replace(old, new, 1)
    return "".join(lines[:fn.lineno - 1]) + mutated + "".join(lines[fn.end_lineno:])


def _spec(rel: str, function: str) -> _StampPath:
    return next(s for s in _STAMP_LAST_ENTRIES if (s.rel, s.function) == (rel, function))


#: (rel, function, label, old, new): a mutation of the REAL source that each path's checker must flag.
_MUTATIONS: tuple[tuple[str, str, str, str, str], ...] = (
    # doc_indexer (the first scope)
    ("doc_indexer.py", "index_pdf", "incremental-path stamp deleted", "_pending.complete()", "pass"),
    ("doc_indexer.py", "index_pdf", "small-path stamp deleted", "            pending.complete()", "            pass"),
    ("doc_indexer.py", "index_pdf", "small-path defer dropped", "defer_completion=True,", ""),
    ("doc_indexer.py", "index_pdf", "small-path enrichment dropped ahead of the stamp",
     "        _register_in_catalog(metadatas_list, len(metadatas_list))\n", ""),
    ("doc_indexer.py", "index_markdown", "markdown stamp dropped from the metadata branch",
     "            _catalog_markdown_hook(md_path, col_name, content_type, corpus, len(metadatas), base_path=base_path, source_uri=source_uri)\n            _stamp_last()\n",
     "            _catalog_markdown_hook(md_path, col_name, content_type, corpus, len(metadatas), base_path=base_path, source_uri=source_uri)\n"),
    ("doc_indexer.py", "index_markdown", "markdown hands over no pending stamp", "pending_stamp=_stamps,", ""),
    ("doc_indexer.py", "index_markdown", "markdown stamps ahead of its enrichment",
     "                _catalog_markdown_hook(md_path, col_name, content_type, corpus, count, base_path=base_path, source_uri=source_uri)\n                _stamp_last()\n",
     "                _stamp_last()\n                _catalog_markdown_hook(md_path, col_name, content_type, corpus, count, base_path=base_path, source_uri=source_uri)\n"),
    # the three oversize fallbacks
    ("code_indexer.py", "index_code_file", "oversize code: defer dropped", "force_re_embed=ctx.force_re_embed, defer_completion=True,", "force_re_embed=ctx.force_re_embed,"),
    ("code_indexer.py", "index_code_file", "oversize code: stamp deleted", "complete_oversize_write(_pending)", "pass"),
    ("prose_indexer.py", "index_prose_file", "oversize prose: defer dropped", "force_re_embed=ctx.force_re_embed, defer_completion=True,", "force_re_embed=ctx.force_re_embed,"),
    ("prose_indexer.py", "index_prose_file", "oversize prose: stamp deleted", "complete_oversize_write(_pending)", "pass"),
    ("indexer.py", "_index_pdf_file", "oversize pdf: defer dropped", "force_re_embed=force_re_embed, defer_completion=True,", "force_re_embed=force_re_embed,"),
    ("indexer.py", "_index_pdf_file", "oversize pdf: stamp deleted", "complete_oversize_write(_pending)", "pass"),
    # the four note writers
    ("catalog/note_write.py", "put_note", "put_note stamps with its write", "            stamp=False)", "            stamp=True)"),
    ("mcp/core.py", "store_put", "MCP store_put: stamp deleted", "outcome = stamp_note(outcome)", "pass"),
    ("commands/store.py", "put_cmd", "nx store put: stamp deleted", "outcome = stamp_note(outcome)", "pass"),
    ("commands/memory.py", "promote_cmd", "nx memory promote: stamp deleted", "outcome = stamp_note(outcome)", "pass"),
    ("catalog/recovery_bundle.py", "_default_import_doc", "recovery import: stamp deleted", "outcome = stamp_note(outcome)", "pass"),
    ("mcp/core.py", "store_put", "MCP store_put: stamp ahead of the chains",
     "fire_note_chains(outcome, content, hooks=_hooks)", "outcome = stamp_note(outcome)\n        fire_note_chains(outcome, content, hooks=_hooks)"),
    # the .nxexp import
    ("exporter.py", "_ensure", "import writer built without the deferral", "defer_completion=True,", ""),
    ("exporter.py", "flush", "import stamps deleted", "stamped = writer.complete_documents(result.landed)", "stamped = None"),
    # the ChunkBatcher flush
    ("chunk_batcher.py", "_flush_batch", "flush stamp deleted", "self._on_batch_stamp(", "(lambda *a: None)("),
)


@pytest.mark.parametrize("rel,function,label,old,new", _MUTATIONS, ids=[m[2] for m in _MUTATIONS])
def test_the_stamp_last_gate_is_red_when_the_real_source_loses_a_stamp_a_deferral_or_the_order(
    rel: str, function: str, label: str, old: str, new: str,
) -> None:
    """Mutate the REAL source of each writer path in memory and require red; the unmutated source is
    the green control (``test_every_writer_path_stamps_complete_after_its_hooks``)."""
    text = _real_text(rel)
    spec = _spec(rel, function)
    assert _stamp_path_problems(ast.parse(text), spec) == []
    mutated = _mutate_in_function(text, function, old, new)
    assert mutated != text, label
    assert _stamp_path_problems(ast.parse(mutated), spec), label


@pytest.mark.parametrize("label,old,new", [
    ("flush writes with the stamp", "                    complete=None,\n", "                    complete=complete_map or None,\n"),
    ("flush batcher not wired to the stamp", "            on_batch_stamp=_stamp_flush_documents,\n", ""),
    ("stamp ignores the failed documents", "            _write_failed = {_d for _d, _r in _full_docs if _flush_failed_docs.pop(_d, 0) is None}\n",
     "            _write_failed = set()\n"),
    ("owed filter drops the failed-document exclusion (nexus-ioauc, M3)",
     "                if _d in _complete_map and _d not in _write_failed\n",
     "                if _d in _complete_map\n"),
    ("stamp request carries chunks","                            complete={_d: (_h, _n) for _d, _h, _n in group},\n",
     "                            complete={_d: (_h, _n) for _d, _h, _n in group}, chunks=[],\n"),
])
def test_the_flush_wiring_leg_is_red_when_the_flush_stamps_with_its_write_or_is_not_wired(
    label: str, old: str, new: str,
) -> None:
    text = _real_text("indexer.py")
    assert _flush_wiring_problems(ast.parse(text)) == []
    assert old in text, f"non-vacuity: {label}"
    assert _flush_wiring_problems(ast.parse(text.replace(old, new, 1))), label
