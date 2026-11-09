# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-243 Phase 2 review r1 F3 (nexus-rfvyy.7.11): every literal ``sensitivity="include"`` in ``src/nexus`` is
listed here by file and enclosing function, with the reason it may read sensitive chunks.

A vector read takes its ``sensitivity`` as a required argument. The ones an LLM-bound surface reaches take the posture
(``read_sensitivity()``); the rest are index maintenance, doctor probes, backfills, deletes, migration rungs and
operator CLIs that read ids, hashes or metadata, or run only under an operator's own hand, and never hand chunk text
to a model. A NEW ``"include"`` literal fails this test until someone adds it here with a reason, so the choice is
made on purpose and shows up in review. A listed entry whose literal is gone fails too (a stale row hides a hole).

The scan finds the literal as a keyword argument (``f(sensitivity="include")``), as a parameter default and as a
plain assignment (``sensitivity = "include"``). A value computed or passed through (``sensitivity=sensitivity``) is
not a literal and is checked by the entry-point tests in ``tests/test_read_sensitivity_entry_points.py``.

The reasons were assigned by what each function does (read from its name, docstring and the call's ``include=``
list), not by tracing every caller: the tracing is the reviewer's job when a row's reason looks thin.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

SRC = Path(__file__).resolve().parents[1] / "src" / "nexus"

_META = "reads ids/metadata only, no chunk text leaves the process"
_HASH = "reads chunk text to hash or compare it for index maintenance; the text goes nowhere else"
_OPS = "operator maintenance verb (an explicit CLI command); never an LLM-bound surface"
_DOCTOR = "doctor/health probe; counts or samples, output is not model input"
_EXISTS = "existence/presence check; nothing is returned to a caller"
_DELETE = "delete path: must find a sensitive chunk to remove it; nothing is returned to a model"
_T1 = "T1 session scratch (the scratch collection), not T3 knowledge: the catalog sensitivity flag does not apply to it"
_TAXONOMY = "taxonomy compute (clustering and c-TF-IDF term scoring) reads no text for a model; the label and review surfaces that do carry their own gate (taxonomy_cmd.py)"
_REEMBED = "re-embed/reindex copies chunk rows between collections for the operator; not LLM-bound"
_MIGRATION = "upgrade-ladder rung / reconcile / recovery tooling; a one-shot operator migration"
_T3FACADE = "T3Database facade method (injected test/dry-run client); callers pass their own posture elsewhere"

ALLOWED: dict[tuple[str, str], str] = {
    ("catalog/catalog_spans.py", "resolve_span_in_t3"): "span resolution for `nx catalog` CLI verbs: returns text to the operator",
    ("catalog/catalog_spans.py", "fallback_chash_scan"): "span resolution fallback for `nx catalog` CLI verbs: returns text to the operator",
    ("catalog/catalog_spans.py", "resolve_span_text_for_entry"): "span resolution for `nx catalog` CLI verbs: returns text to the operator",
    ("catalog/manifest_backfill.py", "_fetch_chunks_by_key"): "manifest backfill: " + _META,
    ("catalog/manifest_backfill.py", "_fetch_chunk_by_id"): "manifest backfill: " + _META,
    ("catalog/manifest_backfill.py", "_forward_hints_for_chashes"): "manifest backfill: " + _META,
    ("catalog/meta_backfill.py", "_chunk_rows"): "meta backfill: " + _META,
    ("catalog/orphan_backfill.py", "gather_titled_chunks"): "orphan backfill: reads titles/hashes to link orphans; " + _OPS,
    ("catalog/orphan_backfill.py", "link_by_content_hash"): "orphan backfill: " + _HASH,
    ("catalog/recovery_bundle.py", "_collection_rows"): "recovery-bundle export to a file the operator owns; " + _MIGRATION,
    ("collection_audit.py", "sample_live_distances"): _DOCTOR,
    ("commands/catalog.py", "_backfill_rdrs"): "catalog backfill: " + _OPS,
    ("commands/catalog.py", "_backfill_papers"): "catalog backfill: " + _OPS,
    ("commands/catalog.py", "_backfill_per_file_from_t3"): "catalog backfill: " + _OPS,
    ("commands/catalog_cmds/doctor.py", "_run_chunk_size_distribution"): _DOCTOR,
    ("commands/catalog_cmds/doctor.py", "_run_chunk_text_dedup"): _DOCTOR + "; hashes text to find duplicates",
    ("commands/catalog_cmds/doctor.py", "_run_name_vs_embed_dim._probe"): _DOCTOR,
    ("commands/catalog_cmds/doctor.py", "_run_visible_outside_manifest"): _DOCTOR,
    ("commands/catalog_cmds/integrity.py", "note_chunks_present"): _EXISTS,
    ("commands/collection.py", "reindex_cmd"): _REEMBED,
    ("commands/collection.py", "_upsert_reembed_batch"): _REEMBED,
    ("commands/collection.py", "_reembed_collection"): _REEMBED,
    ("commands/enrich.py", "_backfill_catalog_from_chunks"): "bib enrichment backfill reads chunk metadata only (include=['metadatas']); " + _OPS,
    ("commands/enrich.py", "run_bib_enrichment"): "bib enrichment reads chunk metadata only (include=['metadatas']); the per-document gate (Phase 1 Step 4c) covers it",
    ("commands/enrich.py", "_extract_identifiers_for_title"): "reads chunk text to find DOIs/ISBNs by pattern, not by a model; the per-document gate covers it",
    ("commands/index.py", "index_pdf_cmd"): "indexer: reads back the document's own chunk ids; " + _META,
    ("commands/store.py", "delete_cmd"): _DELETE,
    ("commands/store.py", "_delete_by_title.present"): _EXISTS,
    ("db/http_vector_client.py", "HttpVectorClient._note_rows"): "note-row lookup by id inside the client for a note's own pieces; the callers hold their own posture",
    ("db/reconcile.py", "iter_collection_chunks"): _MIGRATION,
    ("db/t1.py", "T1Database._resolve_id"): _T1,
    ("db/t1.py", "T1Database.get"): _T1,
    ("db/t1.py", "T1Database.search._query"): _T1,
    ("db/t1.py", "T1Database.list_entries._page"): _T1,
    ("db/t1.py", "T1Database.flag._do"): _T1,
    ("db/t1.py", "T1Database.unflag._do"): _T1,
    ("db/t1.py", "T1Database.promote"): _T1,
    ("db/t1.py", "T1Database.delete._do"): _T1,
    ("db/t1.py", "T1Database.clear._do"): _T1,
    ("db/t2/http_taxonomy_store.py", "HttpTaxonomyStore._svc_fetch_all_embeddings"): _TAXONOMY,
    ("db/t2/http_taxonomy_store.py", "HttpTaxonomyStore.project_against"): _TAXONOMY,
    ("db/t3.py", "_rewrite_collection_metadata"): _T3FACADE,
    ("db/t3.py", "T3Database.get_embeddings"): _T3FACADE,
    ("db/t3.py", "T3Database.expire"): _T3FACADE,
    ("db/t3.py", "T3Database.list_store"): "store_list returns titles and metadata unfiltered by design (RDR-243 Technical Design, /store-list)",
    ("db/t3.py", "T3Database.list_unique_source_paths"): _T3FACADE,
    ("db/t3.py", "T3Database.ids_for_source"): _T3FACADE,
    ("db/t3.py", "T3Database.ids_for_doc_id"): _T3FACADE,
    ("db/t3.py", "T3Database.list_chunks_with_metadata"): _T3FACADE,
    ("db/t3.py", "T3Database.update_source_path"): _T3FACADE,
    ("db/t3.py", "T3Database.delete_by_id"): _T3FACADE,
    ("db/t3.py", "T3Database.find_ids_by_title"): _T3FACADE,
    ("db/t3.py", "T3Database.existing_ids"): _T3FACADE,
    ("db/t3.py", "verify_collection_deep"): _DOCTOR,
    ("doc_indexer.py", "_own_chunk_metadata"): "indexer: " + _META,
    ("doctor_assignments.py", "probe_collection"): _DOCTOR,
    ("doctor_embeddings.py", "probe_collection"): _DOCTOR,
    ("doctor_search.py", "_default_probe_for"): _DOCTOR + "; a retrieval-quality probe that prints distances",
    ("doctor_search.py", "_neighbour_probe"): _DOCTOR,
    ("doctor_search.py", "run_retrieval_quality_probe"): _DOCTOR,
    ("exporter.py", "export_collection"): "`nx store export` to a file the operator owns; " + _OPS,
    ("health.py", "_pdf_stubs_in_collection._read"): _DOCTOR,
    ("indexer.py", "_paginated_get"): "indexer: paged read of ids/metadata for staleness; " + _META,
    ("indexer.py", "_present_ids"): "indexer: " + _EXISTS,
    ("indexer_utils.py", "seed_staleness_cache_from_catalog"): "indexer: " + _META,
    ("mcp/core.py", "store_delete"): _DELETE,
    ("plans/session_cache.py", "PlanSessionCache.populate"): "plan-match session cache over the plan library (T2 plans in an in-memory collection), not T3 knowledge",
    ("upgrade_ladder/rungs/manifest_section_labels.py", "_chunk_metadatas"): _MIGRATION + "; metadata only",
}


def _include_literals() -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        stack: list[str] = []

        def is_include(node: ast.AST | None) -> bool:
            return isinstance(node, ast.Constant) and node.value == "include"

        class Visitor(ast.NodeVisitor):
            def _scope(self, node: ast.AST) -> None:
                stack.append(node.name)  # type: ignore[attr-defined]
                self.generic_visit(node)
                stack.pop()

            visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope  # noqa: N815

            def visit_keyword(self, node: ast.keyword) -> None:
                if node.arg == "sensitivity" and is_include(node.value):
                    found.append((rel, ".".join(stack) or "<module>", node.value.lineno))
                self.generic_visit(node)

            def visit_Assign(self, node: ast.Assign) -> None:
                if is_include(node.value) and any(isinstance(t, ast.Name) and t.id == "sensitivity" for t in node.targets):
                    found.append((rel, ".".join(stack) or "<module>", node.value.lineno))
                self.generic_visit(node)

            def visit_arguments(self, node: ast.arguments) -> None:
                positional = [*node.posonlyargs, *node.args]
                pairs = list(zip(reversed(positional), reversed(node.defaults))) + list(zip(node.kwonlyargs, node.kw_defaults))
                for arg, default in pairs:
                    if arg.arg == "sensitivity" and is_include(default):
                        found.append((rel, ".".join(stack) or "<module>", default.lineno))
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def test_every_include_literal_is_listed_with_a_reason():
    found = _include_literals()
    assert len(found) >= 60, f"the scan found only {len(found)} literals: it is not looking at src/nexus"
    unlisted = sorted({(f, fn) for f, fn, _ in found} - ALLOWED.keys())
    assert not unlisted, (
        'sensitivity="include" appears in a function the allowlist does not list. If it does not hand chunk text to a '
        "model, add (file, function) to ALLOWED with a one-line reason; otherwise pass the posture "
        "(nexus.sensitivity.read_sensitivity() at the entry point). New literal(s):\n  "
        + "\n  ".join(f"{f} :: {fn}" for f, fn in unlisted)
    )


def test_no_allowlist_row_is_stale():
    live = {(f, fn) for f, fn, _ in _include_literals()}
    stale = sorted(ALLOWED.keys() - live)
    assert not stale, f"these rows no longer match a literal; delete them: {stale}"


def test_every_reason_is_a_real_sentence():
    short = [k for k, v in ALLOWED.items() if len(v.split()) < 3]
    assert not short, f"rows without a reason: {short}"


def test_the_scan_sees_a_literal_it_is_given(tmp_path, monkeypatch):
    """Control: the scanner finds each of the three literal shapes in a throwaway module."""
    import tests.test_sensitivity_include_literals_allowlist as me

    mod = tmp_path / "probe.py"
    mod.write_text(
        "def a(x):\n    return x.get(sensitivity='include')\n"
        "def b(sensitivity='include'):\n    return sensitivity\n"
        "def c():\n    sensitivity = 'include'\n    return sensitivity\n"
    )
    monkeypatch.setattr(me, "SRC", tmp_path)
    assert sorted(fn for _f, fn, _l in me._include_literals()) == ["a", "b", "c"]
