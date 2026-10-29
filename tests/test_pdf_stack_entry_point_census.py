# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every PDF entry point sits behind the stack gate. nexus-2k66t.4.2.

The review of cb36b89de found ``nx collection reindex`` calling ``index_pdf``
with no gate, after it had purged the collection: the one entry point the
per-command tests did not know to name. This census reads the source instead of
the tests: every non-test CALL of ``index_pdf``, ``batch_index_pdfs``,
``_index_pdf_file`` or ``PDFExtractor`` (under any import alias) outside the
three modules that implement PDF indexing must be inside a function that
reaches ``pdf_stack`` (``ensure_or_exit``, ``ensure``, ``require`` or the repo
walk's ``_hold_pdfs_without_stack``), or be on the reasoned allowlist below,
whose reason is itself checked: the named gating function must exist and
contain the gate. A new call site fails this test until it is gated or reasoned.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

_WATCHED = frozenset({"index_pdf", "batch_index_pdfs", "_index_pdf_file", "PDFExtractor"})

#: The modules that ARE the PDF pipeline. ``doc_indexer.index_pdf`` carries the
#: library backstop (``pdf_stack.require`` before any registration, pinned
#: below) and ``PDFExtractor.extract`` requires the stack in every mode.
_IMPLEMENTATION = frozenset({"doc_indexer.py", "pipeline_stages.py", "pdf_extractor.py"})

_GATE_ATTRS = frozenset({"ensure_or_exit", "ensure", "require"})
_GATE_NAMES = frozenset({"_hold_pdfs_without_stack"})

#: (file relative to src/nexus, function) -> (function in the same file that
#: holds the gate, why). The site is reached only through the gating function.
_ALLOWED: dict[tuple[str, str], tuple[str, str]] = {
    ("commands/dt.py", "_index_record"): (
        "index_cmd",
        "the per-record worker of `nx dt index`; index_cmd settles the stack once before the "
        "first record, and `nx dt capture` reaches it only through ctx.invoke(index_cmd)",
    ),
}


def _callee(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _has_gate(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in _GATE_ATTRS and isinstance(f.value, ast.Name) and f.value.id == "pdf_stack":
            return True
        if isinstance(f, ast.Name) and f.id in _GATE_NAMES:
            return True
    return False


def _functions(tree: ast.AST) -> dict[str, ast.AST]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


class _Sites(ast.NodeVisitor):
    def __init__(self, watched: frozenset[str]) -> None:
        self.watched = set(watched)
        self.stack: list[ast.AST] = []
        self.sites: list[tuple[int, str, ast.AST | None]] = []

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in _WATCHED and alias.asname:
                self.watched.add(alias.asname)

    def _fn(self, node: ast.AST) -> None:
        self.stack.append(node)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _fn
    visit_AsyncFunctionDef = _fn

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee(node)
        if name in self.watched:
            outer = self.stack[0] if self.stack else None
            self.sites.append((node.lineno, name, outer))
        self.generic_visit(node)


def _scan() -> list[tuple[str, int, str, str, bool]]:
    """``(file, line, callee, outermost function, gated)`` for every call site."""
    out: list[tuple[str, int, str, str, bool]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if path.name in _IMPLEMENTATION and "/" not in rel:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # An alias imported in one function is visible to a scan of the whole file;
        # collect every alias first, then look for the calls.
        collector = _Sites(frozenset(_WATCHED))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                collector.visit_ImportFrom(node)
        visitor = _Sites(frozenset(collector.watched))
        visitor.visit(tree)
        for line, name, outer in visitor.sites:
            fn_name = getattr(outer, "name", "<module>")
            out.append((rel, line, name, fn_name, outer is not None and _has_gate(outer)))
    return out


def test_every_pdf_entry_point_is_behind_the_stack_gate_or_on_the_reasoned_allowlist() -> None:
    sites = _scan()
    ungated = [
        f"{rel}:{line} {name}() in {fn}"
        for rel, line, name, fn, gated in sites
        if not gated and (rel, fn) not in _ALLOWED
    ]
    assert not ungated, (
        "PDF entry point(s) with no pdf_stack gate in the enclosing function (nexus-2k66t.4.2). "
        "Call pdf_stack.ensure_or_exit(...) before anything is written, or add a reasoned "
        "entry to _ALLOWED:\n  " + "\n  ".join(ungated)
    )


def test_the_census_is_not_vacuous() -> None:
    """It must find the entry points it exists to police: if a refactor renames
    the verbs and the scan finds nothing, the first test passes over nothing."""
    sites = _scan()
    files = {rel for rel, *_ in sites}
    assert len(sites) >= 8, sites
    assert {"commands/index.py", "commands/dt.py", "commands/collection.py", "indexer.py"} <= files
    assert any(name == "PDFExtractor" for _r, _l, name, _f, _g in sites), "PDFExtractor( in commands/index.py"
    # the alias route: index_pdf_cmd imports index_pdf as _index_pdf_raw and wraps it
    assert any(rel == "commands/index.py" and name == "_index_pdf_raw" for rel, _l, name, _f, _g in sites)


def test_every_allowlist_entry_still_matches_a_site_and_its_gate_is_real() -> None:
    sites = _scan()
    present = {(rel, fn) for rel, _l, _n, fn, _g in sites}
    for (rel, fn), (gate_fn, why) in _ALLOWED.items():
        assert (rel, fn) in present, f"stale allowlist entry {(rel, fn)}: no such call site any more"
        assert why
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        gating = _functions(tree).get(gate_fn)
        assert gating is not None, f"{rel}: gating function {gate_fn} is gone"
        assert _has_gate(gating), f"{rel}:{gate_fn} no longer calls pdf_stack; {fn}'s allowlist reason is false"


def test_a_scan_over_a_known_ungated_call_flags_it(tmp_path: Path) -> None:
    """The scanner itself: an ungated call is seen as ungated, a gated one as gated."""
    src = (
        "from nexus.doc_indexer import index_pdf as _raw\n"
        "def bad(p):\n    return _raw(p)\n"
        "def good(p):\n    pdf_stack.ensure_or_exit('x')\n    return _raw(p)\n"
    )
    tree = ast.parse(src)
    collector = _Sites(frozenset(_WATCHED))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            collector.visit_ImportFrom(node)
    visitor = _Sites(frozenset(collector.watched))
    visitor.visit(tree)
    verdicts = {getattr(outer, "name"): _has_gate(outer) for _l, _n, outer in visitor.sites}
    assert verdicts == {"bad": False, "good": True}


# ── the library backstop in doc_indexer.index_pdf ────────────────────────────


def test_index_pdf_requires_the_stack_before_it_resolves_a_store_or_registers_a_document() -> None:
    tree = ast.parse((_SRC / "doc_indexer.py").read_text(encoding="utf-8"))
    fn = _functions(tree)["index_pdf"]
    require_lines = [
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "require"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "pdf_stack"
    ]
    writes = sorted(
        n.lineno for n in ast.walk(fn)
        if isinstance(n, ast.Call) and _callee(n) in {"_register_or_lookup_doc_id", "_resolve_write_db", "get_or_create_collection"}
    )
    assert require_lines and writes
    assert min(require_lines) < writes[0], "no register-then-rollback: refuse before the first store or catalog touch"


def test_index_pdf_on_a_slim_box_refuses_and_touches_neither_store_nor_catalog(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from nexus import doc_indexer, pdf_stack

    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path / "cfg"))
    pdf_stack.reset_cache()
    monkeypatch.setattr(pdf_stack, "state", lambda: pdf_stack.StackState("absent", "MinerU and torch are not installed"))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 not empty")
    register = MagicMock(side_effect=AssertionError("registered a catalog row before the stack check"))
    resolve = MagicMock(side_effect=AssertionError("resolved a store before the stack check"))
    monkeypatch.setattr(doc_indexer, "_register_or_lookup_doc_id", register)
    monkeypatch.setattr(doc_indexer, "_resolve_write_db", resolve)
    with pytest.raises(pdf_stack.PdfStackMissingError, match="nothing was written"):
        doc_indexer.index_pdf(pdf, "c", t3=MagicMock(), collection_name="docs__c__m__v1", embed_fn=lambda texts: [])
    register.assert_not_called()
    resolve.assert_not_called()


# ── the eight gates take the extractor (RDR-237, nexus-tr1rj.4) ──────────────

#: A gate call that is deliberately NOT extractor-aware, with the reason. ``nx mineru start`` starts
#: the MinerU server: it needs the stack by definition and has no PDF extractor to choose.
_NOT_EXTRACTOR_AWARE: dict[tuple[str, str], str] = {
    ("commands/mineru.py", "nx mineru start"): "starts the MinerU server; there is no extractor to choose",
}

#: The eight stack gates of the extraction path (RDR-237 Technical Design, RF-16), as
#: ``(file, entry label)``: the label is the ``entry`` string the gate passes.
_EIGHT_GATES: frozenset[tuple[str, str]] = frozenset({
    ("pdf_extractor.py", "<f-string>"),   # PDFExtractor.extract's library backstop
    ("doc_indexer.py", "index_pdf"),
    ("commands/index.py", "nx index pdf"),
    ("commands/index.py", "nx index pdf --dir"),
    ("commands/dt.py", "nx dt index"),
    ("commands/dt.py", "nx dt capture"),
    ("indexer.py", "nx index repo"),
    ("commands/collection.py", "nx collection reindex"),
})


def _entry_label(call: ast.Call) -> str:
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return "<f-string>" if call.args and isinstance(call.args[0], ast.JoinedStr) else "<dynamic>"


def _gate_calls() -> list[tuple[str, int, str, bool]]:
    """``(file, line, entry label, passes extractor=)`` for every ``pdf_stack`` gate call outside the
    stack module itself (``ensure_or_exit``, ``ensure``, ``require`` on any name ending ``pdf_stack``)."""
    out: list[tuple[str, int, str, bool]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel == "pdf_stack.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            f = getattr(node, "func", None)
            if not (isinstance(node, ast.Call) and isinstance(f, ast.Attribute) and f.attr in _GATE_ATTRS):
                continue
            if isinstance(f.value, ast.Name) and f.value.id.endswith("pdf_stack"):
                out.append((rel, node.lineno, _entry_label(node), any(k.arg == "extractor" for k in node.keywords)))
    return out


def test_every_stack_gate_takes_the_extractor_except_the_reasoned_ones() -> None:
    calls = _gate_calls()
    blind = [
        f"{rel}:{line} pdf_stack gate {label!r}"
        for rel, line, label, aware in calls
        if not aware and (rel, label) not in _NOT_EXTRACTOR_AWARE
    ]
    assert not blind, (
        "a pdf_stack gate that does not pass extractor= would install or require the PDF stack for "
        "the text-only pymupdf extractor (RDR-237). Pass extractor=..., or add a reasoned entry to "
        "_NOT_EXTRACTOR_AWARE:\n  " + "\n  ".join(blind)
    )


def test_there_are_exactly_the_eight_extractor_aware_gates() -> None:
    """Non-vacuity: the scan finds the eight gates the RDR names, each extractor-aware, and the one
    allowlisted exception; a rename that hid them from the scan fails here, not silently above."""
    calls = _gate_calls()
    aware = {(rel, label) for rel, _line, label, ok in calls if ok}
    assert aware == _EIGHT_GATES, (sorted(aware ^ _EIGHT_GATES), calls)
    assert len([c for c in calls if c[3]]) == 8, "one call each, none duplicated or dropped"
    unaware = {(rel, label) for rel, _line, label, ok in calls if not ok}
    assert unaware == set(_NOT_EXTRACTOR_AWARE), unaware


def test_the_repo_walks_two_hold_calls_pass_the_extractor_and_the_late_recheck_asks_needs_stack() -> None:
    tree = ast.parse((_SRC / "indexer.py").read_text(encoding="utf-8"))
    holds = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_hold_pdfs_without_stack"
    ]
    assert len(holds) == 2, "the early hold and the late recheck"
    assert all(any(k.arg == "extractor" for k in h.keywords) for h in holds)
    run_index = _functions(tree)["_run_index"]
    asks = [
        n for n in ast.walk(run_index)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "needs_stack"
    ]
    assert asks, "the late recheck must skip the stack probe for an extractor that needs none"


# ── the bounds question goes with the gate (nexus-tr1rj.24) ──────────────────

_BOUNDS_ATTRS = frozenset({"refuse_bad_mineru_bounds", "mineru_bounds_problem"})


def _bounds_calls(fn: ast.AST) -> int:
    return sum(
        1 for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _BOUNDS_ATTRS
        and isinstance(n.func.value, ast.Name) and n.func.value.id.endswith("pdf_stack")
    )


def _gates_and_bounds_per_holder() -> dict[tuple[str, str], tuple[list[str], int]]:
    """``(file, outermost holding function)`` -> (the gate labels it holds, its bounds-call count)
    for the seven gates other than ``PDFExtractor.extract``'s own backstop."""
    held: dict[tuple[str, str], list[str]] = {}
    bounds: dict[tuple[str, str], int] = {}
    for rel, line, label, _takes in _gate_calls():
        if (rel, label) not in _EIGHT_GATES or rel == "pdf_extractor.py":
            continue
        tree = ast.parse((_SRC / rel).read_text(encoding="utf-8"))
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        # the outermost function whose span holds the gate call
        holders = [n for n in fns if n.lineno <= line <= (n.end_lineno or n.lineno)]
        outer = min(holders, key=lambda n: n.lineno)
        held.setdefault((rel, outer.name), []).append(label)
        bounds[(rel, outer.name)] = _bounds_calls(outer)
    return {k: (v, bounds[k]) for k, v in held.items()}


def test_every_extraction_entry_asks_the_mineru_bounds_once_per_stack_gate_it_holds() -> None:
    """An unusable ``pdf.mineru_page_timeout_s`` x ``pdf.mineru_page_batch`` is refused inside
    extraction, after the catalog row is written, so each entry that can reach MinerU asks first.
    ``PDFExtractor.extract`` is the backstop and carries its own check; the other seven gates (the
    ``_EIGHT_GATES`` list) must each have a call to ``pdf_stack.refuse_bad_mineru_bounds`` or
    ``mineru_bounds_problem`` in the function that holds them: ``index_pdf_cmd`` holds two gates
    (a file and ``--dir``), so one call there leaves the other unasked. A new entry, or a deleted
    call, fails here."""
    per_holder = _gates_and_bounds_per_holder()
    short = [
        f"{rel}:{fn}() holds {len(labels)} stack gate(s) {labels} and asks the bounds {asked} time(s)"
        for (rel, fn), (labels, asked) in sorted(per_holder.items())
        if asked < len(labels)
    ]
    assert not short, "stack gate with no MinerU bounds question beside it:\n  " + "\n  ".join(short)
    gates = sum(len(labels) for labels, _asked in per_holder.values())
    asked = sum(asked for _labels, asked in per_holder.values())
    assert gates == 7 and asked >= 7, f"non-vacuity: {asked} bounds call(s) for {gates} of the seven gates"
    assert per_holder[("commands/index.py", "index_pdf_cmd")][0] == ["nx index pdf", "nx index pdf --dir"]
