# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tp8yk — PDF ingest must not commit manifest rows for chunk
batches that never landed in T3.

Design memo (T2 nexus "tp8yk-design-2026-08-04") §5 TDD PLAN, scenarios 1
and 4. Drives the PRODUCTION entry point ``nexus.doc_indexer.index_pdf``
(never ``_upsert_skip_reembed`` directly) against the SHARED engine
substrate every unit test already uses (``tests/conftest.py``'s autouse
``_pin_t2_substrate`` -> ``t2_service_env`` ->
``tests/_engine_substrate.ensure_engine``/``mint_test_tenant``) — no
dedicated module-scoped PG+service boot needed here, unlike
``tests/db/test_5xn3k_runfence_gate.py``'s sibling gate, because this
file's fault injection never needs the CLOUD version probe (it patches
the vector-CLIENT methods directly, never touching ``/v1/vectors/*`` for
real) while that gate constructs a from-scratch service instance
specifically to drive T3 writes for real.

Fault injection is at the vector-client seam (the memo's stated
mechanism): ``HttpVectorClient.existing_ids`` is patched to report a
STALE POSITIVE (probe says every id is already present; nothing was ever
written), and ``HttpVectorClient.update_chunks`` to return
``missing=None`` — a pre-nexus-5xn3k engine, or a mixed-version fleet
mid-rolling-deploy, reporting "cannot tell". Pre-nexus-tp8yk,
``_upsert_skip_reembed`` treated ``missing is None`` as "no reroute,
proceed" — the caller's manifest hook then wrote rows for a batch never
confirmed landed (design memo §1 P1, the ``_upsert_skip_reembed``
mechanism). Post-fix it raises ``ChunkLandingUnverifiedError`` BEFORE
``hooks.fire_batch`` (and therefore the manifest hook) ever runs, so the
manifest rows become structurally unreachable for an unlanded batch.

Marked ``@pytest.mark.integration`` — skipped by default addopts
(``-m 'not integration and not slow'``); run explicitly with
``uv run pytest tests/integration/test_tp8yk_manifest_never_outruns_chunks.py -m integration``.
Not because it needs external services/credentials (it self-provisions
against the same local hermetic substrate the default unit suite already
boots) but per this directory's existing convention (every file under
``tests/integration/`` carries the marker) and because it drives a real
PDF-indexing round trip through the catalog, which is heavier than a
pure-unit test.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.errors import ChunkLandingUnverifiedError

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


def _pre_tp8yk_upsert_skip_reembed(
    db, collection_name, ids, documents, embeddings, metadatas, *, force=False,
):
    """Exact pre-nexus-tp8yk shape of ``_upsert_skip_reembed`` (the D1 kill
    control): ``missing=None`` degrades to "no reroute" and the function
    returns normally instead of raising. Used ONLY by the kill-control
    test below to prove the base test's green is driven by the D1 raise.
    """
    present = set(db.existing_ids(collection_name, ids))
    if not present:
        db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)
        return len(ids)
    old_idx = [i for i, cid in enumerate(ids) if cid in present]
    if old_idx:
        db.update_chunks(
            collection_name, [ids[i] for i in old_idx], [metadatas[i] for i in old_idx],
        )
    return 0


def test_unlanded_batch_raises_and_commits_no_manifest_rows(tmp_path) -> None:
    """THE bead's core assertion (memo §5 scenario 1): a stale-positive
    ``existing_ids`` probe combined with an engine that omits "missing"
    (cannot tell) must raise ``ChunkLandingUnverifiedError`` BEFORE any
    manifest row is committed — never silently proceed and let the caller
    write a manifest for chunks that were never confirmed landed.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf

    corpus = "tp8yk-gate-unlanded"
    collection = _collection()
    pdf_path = _fresh_pdf(tmp_path, marker="unlanded")
    doc_id = _register_or_lookup_doc_id(
        pdf_path, corpus, content_type="paper", physical_collection=collection,
    )
    assert doc_id, "catalog registration must succeed against the real service"

    result = _extraction_result(1)
    fake_chunks = _fake_chunks(3, prefix="unlanded")
    t3 = HttpVectorClient()

    def _stale_positive_existing_ids(collection_arg, ids):
        # Probe reports EVERY id present — none were ever written. This is
        # the stale-positive shape (a concurrent delete, a prior partial
        # run) the memo's mechanism describes.
        return list(ids)

    def _cannot_tell_update_chunks(collection_arg, ids, metadatas):
        # Engine response omitted "missing" entirely.
        return None

    with patch("nexus.doc_indexer.PDFExtractor") as ME, \
         patch("nexus.doc_indexer.PDFChunker") as MC, \
         patch.object(t3, "existing_ids", side_effect=_stale_positive_existing_ids), \
         patch.object(t3, "update_chunks", side_effect=_cannot_tell_update_chunks):
        ME.return_value.extract.side_effect = _extract_side_effect(1, result)
        MC.return_value.chunk.return_value = fake_chunks

        with pytest.raises(ChunkLandingUnverifiedError) as excinfo:
            index_pdf(
                pdf_path, corpus, t3=t3, collection_name=collection,
                streaming="never",
            )

    assert excinfo.value.collection == collection
    assert excinfo.value.count == 3

    manifest = make_catalog_reader().get_manifest(doc_id)
    assert manifest == [], (
        "manifest rows were committed for a batch never confirmed landed "
        f"in T3 — got {len(manifest)} rows: {manifest}"
    )
    entry = make_catalog_reader().resolve(doc_id)
    assert entry is not None
    assert entry.index_state == "failed", (
        f"expected the fence to record the abort as 'failed' (_fence_fail "
        f"fires from index_pdf's except-block), got {entry.index_state!r}"
    )


def test_kill_control_reverting_the_raise_reproduces_the_damage(tmp_path) -> None:
    """KILL CONTROL (mandatory — feedback_falsify_by_deleting_the_code /
    memo §5 item 4). Reverts ``_upsert_skip_reembed`` to its EXACT
    pre-nexus-tp8yk shape (``missing=None`` -> no reroute, return
    normally) and drives the IDENTICAL fault injection as the base test
    above with D1's raise gone.

    SUBTLE, and worth recording: under the FULL nexus-tp8yk codebase (D1
    reverted, D2's independent tripwire still live), the overall call
    still raises — but as ``IndexRunVerifyRefused`` from the explicit
    ``_fence_complete`` (D2a), not ``ChunkLandingUnverifiedError`` (D1).
    D2's engine-side fail-closed verify catches the SAME underlying
    anomaly one step later, at the COMPLETION STAMP.

    RE-DERIVED (2026-08-17, catalog-029-manifest-chunk-fk.xml). This used
    to assert the bead's original P1 damage reproduces WITHOUT D1: 3
    dangling manifest rows committed for chunks that never landed,
    ``hooks.fire_batch`` -> ``manifest_write_batch_hook`` having nothing
    left to stop it once D1's raise is gone. That is no longer reachable,
    and NOT because this control weakened. RDR-191's manifest FK
    (``nexus.catalog_document_chunks`` -> ``nexus.chunks``,
    ``fk_catalog_chunks_chunk``) landed AFTER nexus-tp8yk and closes the
    SAME gap from underneath, independent of D1: ``manifest_write_batch_
    hook`` is best-effort (any failure logged and swallowed, never
    propagated — its own docstring), so the FK 409 does not surface as an
    exception here, but it DOES mean the per-doc write is refused at the
    database and zero rows land. Probe evidence (this run, engine
    substrate): ``manifest_write_many_doc_failed ... reason='foreign key
    violation (doc_id not registered?) [fk_catalog_chunks_chunk]'``, then
    ``get_manifest(doc_id) == []``. D1 is STILL what makes the failure
    LOUD and immediate (``ChunkLandingUnverifiedError`` before
    ``hooks.fire_batch`` even runs) rather than a swallowed warning plus a
    later, unrelated-looking ``IndexRunVerifyRefused`` — that half of the
    contract this kill control exists to pin is unchanged and still
    proven below. What changed is the SECOND assertion: the manifest is
    no longer merely "correctly refused completion despite being dirty"
    — it is never dirty in the first place, because the FK is a second,
    independent guard the bead's original design did not have.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf
    from nexus.errors import IndexRunVerifyRefused

    corpus = "tp8yk-gate-killctl"
    collection = _collection()
    pdf_path = _fresh_pdf(tmp_path, marker="killctl")
    doc_id = _register_or_lookup_doc_id(
        pdf_path, corpus, content_type="paper", physical_collection=collection,
    )
    assert doc_id

    result = _extraction_result(1)
    fake_chunks = _fake_chunks(3, prefix="killctl")
    t3 = HttpVectorClient()

    def _stale_positive_existing_ids(collection_arg, ids):
        return list(ids)

    def _cannot_tell_update_chunks(collection_arg, ids, metadatas):
        return None

    with patch("nexus.doc_indexer.PDFExtractor") as ME, \
         patch("nexus.doc_indexer.PDFChunker") as MC, \
         patch.object(t3, "existing_ids", side_effect=_stale_positive_existing_ids), \
         patch.object(t3, "update_chunks", side_effect=_cannot_tell_update_chunks), \
         patch("nexus.doc_indexer._upsert_skip_reembed", side_effect=_pre_tp8yk_upsert_skip_reembed):
        ME.return_value.extract.side_effect = _extract_side_effect(1, result)
        MC.return_value.chunk.return_value = fake_chunks

        # D2's independent, engine-side fail-closed verify (untouched by
        # this kill control — it patches ONLY _upsert_skip_reembed) still
        # refuses the COMPLETION STAMP once it sees the manifest reference
        # chashes with present=0. That refusal is real and correct; it is
        # simply too late to stop the manifest write itself.
        with pytest.raises(IndexRunVerifyRefused):
            index_pdf(
                pdf_path, corpus, t3=t3, collection_name=collection,
                streaming="never",
            )

    # THE FK CLOSES THE GAP INDEPENDENTLY OF D1: existing_ids lied,
    # update_chunks never confirmed anything, and hooks.fire_batch (via
    # manifest_write_batch_hook) DID attempt to write 3 rows for chunks
    # that were never actually upserted — but catalog-029-manifest-chunk-
    # fk.xml's FK refuses that write at the database, so the manifest
    # stays clean (0 rows) even with D1 gone. This is the new, STRONGER
    # contract: a second, independent guard now backstops D1's raise.
    manifest = make_catalog_reader().get_manifest(doc_id)
    assert manifest == [], (
        "the manifest-chunk FK must refuse the dangling write even with "
        f"D1's raise reverted — expected 0 rows, got {len(manifest)}: {manifest}"
    )
    entry = make_catalog_reader().resolve(doc_id)
    assert entry is not None
    assert entry.index_state == "indexing", (
        "the completion stamp was correctly refused by D2's independent "
        f"verify (over-work, not data loss) — the fence must stay at "
        f"'indexing', never 'complete'; got {entry.index_state!r}"
    )


def test_reindex_converges_after_abort(tmp_path) -> None:
    """memo §5 scenario 3: re-running WITHOUT --force after an aborted
    (fenced 'failed') run must fully recover — real content lands, the
    manifest matches, and the fence reads 'complete'. Uses the REAL
    (unpatched) vector-client methods for the recovery run.
    """
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf

    corpus = "tp8yk-gate-recover"
    collection = _collection()
    pdf_path = _fresh_pdf(tmp_path, marker="recover")
    doc_id = _register_or_lookup_doc_id(
        pdf_path, corpus, content_type="paper", physical_collection=collection,
    )
    assert doc_id

    result = _extraction_result(1)
    fake_chunks = _fake_chunks(2, prefix="recover")
    t3_aborted = HttpVectorClient()

    with patch("nexus.doc_indexer.PDFExtractor") as ME, \
         patch("nexus.doc_indexer.PDFChunker") as MC, \
         patch.object(t3_aborted, "existing_ids", side_effect=lambda c, ids: list(ids)), \
         patch.object(t3_aborted, "update_chunks", side_effect=lambda c, ids, m: None):
        ME.return_value.extract.side_effect = _extract_side_effect(1, result)
        MC.return_value.chunk.return_value = fake_chunks

        with pytest.raises(ChunkLandingUnverifiedError):
            index_pdf(
                pdf_path, corpus, t3=t3_aborted, collection_name=collection,
                streaming="never",
            )

    manifest_after_abort = make_catalog_reader().get_manifest(doc_id)
    assert manifest_after_abort == []
    entry_after_abort = make_catalog_reader().resolve(doc_id)
    assert entry_after_abort is not None
    assert entry_after_abort.index_state == "failed"

    # Recovery run — no fault injection, real vector-client methods.
    with patch("nexus.doc_indexer.PDFExtractor") as ME2, \
         patch("nexus.doc_indexer.PDFChunker") as MC2:
        ME2.return_value.extract.side_effect = _extract_side_effect(1, result)
        MC2.return_value.chunk.return_value = fake_chunks

        n = index_pdf(
            pdf_path, corpus, t3=HttpVectorClient(), collection_name=collection,
            streaming="never",
        )

    assert n == 2, "recovery run must report indexed chunks, not a no-op"
    manifest_final = make_catalog_reader().get_manifest(doc_id)
    assert len(manifest_final) == 2, manifest_final
    entry_final = make_catalog_reader().resolve(doc_id)
    assert entry_final is not None
    assert entry_final.index_state == "complete"


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
