# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tp8yk D3 — re-indexing one PDF must not damage another live document's shared chunk.

RDR-223 (nexus-z0o2p.15) retired the rest of this file. Its scenarios 1 to 3 injected a stale
``existing_ids`` probe and an engine that answered ``update_chunks`` with ``missing=None``
into ``doc_indexer._upsert_skip_reembed`` and asserted that no manifest row was committed for a
batch that never landed (``ChunkLandingUnverifiedError``). Both the function and the
manifest-after-chunks ordering it guarded are gone: every PDF path writes a chunk together with its
owner row in ONE request, so a manifest row for an unlanded chunk cannot exist, and the
fault-injection seam no longer sits on the path. The atomic write's own properties (a killed
client leaves no ownerless chunk, a rerun converges) are pinned by
``tests/integration/test_rdr223_pdf_journey.py``.

What remains drives the PRODUCTION entry point ``nexus.doc_indexer.index_pdf`` against the SHARED
engine substrate every unit test already uses (``tests/conftest.py``'s autouse
``_pin_t2_substrate``). Marked ``@pytest.mark.integration`` — skipped by default addopts; run
explicitly with
``uv run pytest tests/integration/test_tp8yk_manifest_never_outruns_chunks.py -m integration``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.integration]


def _extraction_result(page_count: int = 1):
    from nexus.pdf_extractor import ExtractionResult

    pages = [f"Page {i} tp8yk gate content." for i in range(page_count)]
    pos, bounds = 0, []
    for i, p in enumerate(pages):
        bounds.append({
            "page_number": i + 1, "start_char": pos,
            "page_text_length": len(p) + 1,
        })
        pos += len(p) + 1
    return ExtractionResult(
        text="\n".join(pages),
        metadata={
            "extraction_method": "docling", "page_count": page_count,
            "page_boundaries": bounds, "table_regions": [], "format": "markdown",
        },
    )


def _extract_side_effect(page_count: int, result):
    # **kwargs: absorb newer PDFExtractor.extract keyword args (e.g.
    # nexus-wi1uv's allow_degraded) so this local double does not break on
    # every signature extension (nexus-rzoqx).
    def extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, **kwargs):
        for i in range(page_count):
            if on_page:
                on_page(i, f"Page {i} tp8yk gate content.", {"page_number": i + 1})
        return result
    return extract


def _fake_chunks(n: int, *, prefix: str):
    from nexus.pdf_chunker import TextChunk

    return [
        TextChunk(
            text=f"{prefix} {i} unique_tp8yk_gate_content_{i}",
            chunk_index=i, metadata={"page": 1},
        )
        for i in range(n)
    ]


def _fresh_pdf(tmp_path: Path, *, marker: str) -> Path:
    # Deliberately NOT a real PDF: PDFExtractor/PDFChunker are patched in
    # every scenario below, so pymupdf.open()/real extraction is never on
    # the path (mirrors tests/db/test_5xn3k_runfence_gate.py's identical
    # rationale). *marker* must be unique per scenario — chash is
    # content-addressed off chunk TEXT, and file content feeds the
    # registered doc_id's identity too.
    p = tmp_path / f"doc-{marker}.pdf"
    p.write_bytes(f"%PDF-1.4 tp8yk gate fake content [{marker}]\n".encode())
    return p.resolve()


def _collection() -> str:
    # Hardcoded to the shared engine substrate's ACTUAL configured local
    # embedder (bge-base-en-v15-768 — same as tests/db/test_5xn3k_
    # runfence_gate.py's _COLLECTION), not nexus.db.local_ef.local_model_
    # token(): that resolves the CLIENT's naming preference (fastembed-
    # availability auto-select), which can diverge from what the shared
    # JVM engine process actually loaded — the recovery scenario below
    # calls upsert_chunks_with_embeddings for real and 422s on a mismatch.
    return "docs__tp8yk-gate__bge-base-en-v15-768__v1"


def test_union_guard_keeps_shared_chunk_at_the_production_wiring(tmp_path) -> None:
    """memo §5 TDD plan item 6 (D3), driven at the PRODUCTION entry point
    (``index_pdf``) against the REAL engine — substantive-critic
    SIGNIFICANT (2026-08-04): the memo's own TDD plan specified this exact
    scenario (two docs sharing a chash, re-index one, assert the other's
    manifest survives). It was originally substituted with tests of the
    extracted ``orphaned_chashes`` HELPER (tests/db/test_http_catalog_
    integration.py::TestPruneUnionGuard) — that proves the helper is
    correct in isolation, not that production wiring reaches it. This
    test closes that gap end-to-end, against the real engine.

    CORRECTED SCOPE NOTE (found while building this test, substantive-
    critic SIGNIFICANT #3 follow-up): this test's PASS is driven by
    ``mcp_infra._sweep_superseded_vectors`` (the manifest-diff-based
    sweep — compares doc A's own before/after manifest chash sets),
    which fires whenever a re-indexed document's batch includes position
    0. It is NOT driven by the ``prune_orphan_candidates`` call nexus-
    tp8yk added inside ``index_pdf``'s own ``_identity_where``-based
    prune block (confirmed by an instrumented run: that call's candidate
    list is empty here). That block's ``source_path``-keyed candidate
    query was a PRE-EXISTING dead path for PDF/markdown content — RDR-102
    D2 hard-removed ``source_path`` from ``metadata_schema.make_chunk_
    metadata``, the sole factory every doc_indexer.py/pipeline_stages.py
    chunk write routes through — tracked separately as nexus-tbkk1, out
    of nexus-tp8yk's scope to fix at the time this test was written.

    nexus-tbkk1 UPDATE (2026-08-05): that dead prune block (and nexus-
    tp8yk's ``prune_orphan_candidates`` union-guard wrapper around it,
    which had zero production callers left once all four of its call
    sites were deleted) are now GONE. ``tests/test_doc_indexer.py::
    test_index_pdf_prune_union_guard_wired_at_call_site`` (referenced
    above by an earlier version of this docstring) is superseded by
    ``test_index_pdf_small_doc_prune_deleted_as_dead_code`` in the same
    file, which proves the deletion rather than the wiring.
    ``prune_orphan_candidates``'s own dedicated test file (tests/
    test_indexer_utils_prune_orphan_candidates.py) is deleted; the
    surviving ``orphaned_chashes`` helper remains covered by
    tests/db/test_http_catalog_integration.py::TestPruneUnionGuard.

    This test remains valuable on its own terms regardless: it is a
    real, production-entry-point, real-engine proof that nexus-tp8yk's
    overall D3 goal — re-indexing one document must never damage another
    live document's shared chunk — holds for the actual system an
    operator runs, via whichever mechanism (``_sweep_superseded_vectors``
    today) actually does the work.

    Two documents share one chunk (identical text -> identical chash,
    content addressing working as designed). Doc A is then re-indexed
    with DIFFERENT content that drops the shared chunk from A's OWN
    manifest — forcing A's re-index to consider the now-stale chash for
    removal. The union guard must keep it (doc B still references it)
    while still deleting the chunk that was exclusively A's.
    """
    import hashlib

    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf
    from nexus.pdf_chunker import TextChunk

    collection = _collection()
    corpus = "tp8yk-d3-wiring"
    result1 = _extraction_result(1)

    shared_text = "tp8yk_d3_shared_chunk_text_unique_marker"
    exclusive_a_text = "tp8yk_d3_exclusive_to_doc_a_unique_marker"
    exclusive_b_text = "tp8yk_d3_exclusive_to_doc_b_unique_marker"
    replacement_a_text = "tp8yk_d3_doc_a_after_reindex_unique_marker"

    shared_chash = hashlib.sha256(shared_text.encode()).hexdigest()
    exclusive_a_chash = hashlib.sha256(exclusive_a_text.encode()).hexdigest()
    exclusive_b_chash = hashlib.sha256(exclusive_b_text.encode()).hexdigest()

    # -- Index doc A: [shared, exclusive_a] --
    pdf_a = tmp_path / "doc-a.pdf"
    pdf_a.write_bytes(b"%PDF-1.4 tp8yk d3 doc A v1\n")
    t3_a = HttpVectorClient()
    with patch("nexus.doc_indexer.PDFExtractor") as ME, \
         patch("nexus.doc_indexer.PDFChunker") as MC:
        ME.return_value.extract.side_effect = _extract_side_effect(1, result1)
        MC.return_value.chunk.return_value = [
            TextChunk(text=shared_text, chunk_index=0, metadata={"page": 1}),
            TextChunk(text=exclusive_a_text, chunk_index=1, metadata={"page": 1}),
        ]
        n_a = index_pdf(
            pdf_a, corpus, t3=t3_a, collection_name=collection, streaming="never",
        )
    assert n_a == 2
    doc_a_id = _register_or_lookup_doc_id(
        pdf_a, corpus, content_type="paper", physical_collection=collection,
    )
    assert doc_a_id

    # -- Index doc B: [shared, exclusive_b] --
    pdf_b = tmp_path / "doc-b.pdf"
    pdf_b.write_bytes(b"%PDF-1.4 tp8yk d3 doc B v1\n")
    with patch("nexus.doc_indexer.PDFExtractor") as ME2, \
         patch("nexus.doc_indexer.PDFChunker") as MC2:
        ME2.return_value.extract.side_effect = _extract_side_effect(1, result1)
        MC2.return_value.chunk.return_value = [
            TextChunk(text=shared_text, chunk_index=0, metadata={"page": 1}),
            TextChunk(text=exclusive_b_text, chunk_index=1, metadata={"page": 1}),
        ]
        n_b = index_pdf(
            pdf_b, corpus, t3=HttpVectorClient(), collection_name=collection,
            streaming="never",
        )
    assert n_b == 2
    doc_b_id = _register_or_lookup_doc_id(
        pdf_b, corpus, content_type="paper", physical_collection=collection,
    )
    assert doc_b_id
    assert doc_b_id != doc_a_id

    manifest_b_before = make_catalog_reader().get_manifest(doc_b_id)
    assert {r.chash for r in manifest_b_before} == {shared_chash, exclusive_b_chash}

    # -- Re-index doc A with DIFFERENT content: drops BOTH shared and
    #    exclusive_a from A's own chunk set, triggering the prune.
    pdf_a.write_bytes(b"%PDF-1.4 tp8yk d3 doc A v2 (different bytes)\n")
    with patch("nexus.doc_indexer.PDFExtractor") as ME3, \
         patch("nexus.doc_indexer.PDFChunker") as MC3:
        ME3.return_value.extract.side_effect = _extract_side_effect(1, result1)
        MC3.return_value.chunk.return_value = [
            TextChunk(text=replacement_a_text, chunk_index=0, metadata={"page": 1}),
        ]
        n_a2 = index_pdf(
            pdf_a, corpus, t3=HttpVectorClient(), collection_name=collection,
            streaming="never",
        )
    assert n_a2 == 1

    # THE ASSERTION: doc B's manifest — and the T3 row it depends on —
    # must survive doc A's re-index prune.
    manifest_b_after = make_catalog_reader().get_manifest(doc_b_id)
    assert {r.chash for r in manifest_b_after} == {shared_chash, exclusive_b_chash}, (
        "doc B's manifest was damaged by doc A's re-index prune — the "
        "union guard failed at the WIRED index_pdf call site"
    )

    col = t3_a.get_or_create_collection(collection)
    shared_row = col.get(ids=[shared_chash], include=[])
    assert shared_row.get("ids") == [shared_chash], (
        "the shared chunk's T3 row was deleted despite doc B still "
        f"referencing it — got {shared_row}"
    )
    # The genuinely-exclusive-to-A chunk must still be pruned — the guard
    # must not degrade into "never delete anything".
    exclusive_row = col.get(ids=[exclusive_a_chash], include=[])
    assert exclusive_row.get("ids") == [], (
        "the chunk exclusively owned by doc A's PRIOR version was not "
        f"pruned — got {exclusive_row}"
    )
