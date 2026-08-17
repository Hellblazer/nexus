# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-2t63u — a path-based re-index into a DIFFERENT ``--collection``
must reconcile the document's stale ``catalog_documents.physical_collection``
BEFORE the manifest write, not leave it stamped to the OLD collection.

Root defect (T2 nexus/nexus-2t63u-debug-2026-08-06, the debugger's dispositive
evidence chain): ``_register_or_lookup_doc_id``'s ``by_file_path`` hit
returned the existing tumbler WITHOUT reconciling ``physical_collection`` to
the run's target ``--collection`` (contrast the ``source_uri`` branch of the
SAME function, which RAISES ``SourceUriCollectionMismatchError`` on exactly
this divergence). The engine's ``writeManifestRows``/``appendManifestChunks``
stamp EVERY manifest row's ``collection`` from ``catalog_documents.
physical_collection`` unconditionally at write time — so a re-index into a
new collection lands chunks correctly in the NEW collection while the
manifest keeps pointing at the OLD one. ``manifest_verify`` then joins
against the WRONG collection and reports live, present chunks as
"missing", tripping the RUNFENCE completion refusal
(``IndexRunVerifyRefused``) — BEFORE ``_catalog_pdf_hook`` (the only other
writer of this field) ever runs, since it sits at the tail of ``index_pdf``,
reached only on a non-refused completion. Every subsequent run reproduces
the identical counts: a self-perpetuating wedge. Measured in production:
doc 1.14.3 (bft-to-smr.pdf), referenced=107 present=18 missing=89, two
independent runs, byte-identical counts.

Fix (Option A, orchestrator DESIGN CHOICE on the bead, per the debugger's
A/B handoff): reconcile ``physical_collection`` EARLY in the ``by_file_path``
hit — ``writer.update(existing.tumbler, physical_collection=...)`` plus a
WARNING naming the move — mirroring what ``_catalog_pdf_hook`` already does
at the tail, just moved ahead of the manifest write.

SUPERSEDED MECHANISM (RDR-191 GATE-2, 498c92953 — read this before editing
anything below). The paragraph above describes the engine as it WAS: manifest
rows took their ``collection`` from ``catalog_documents.physical_collection``
at write time, which is what made a stale document stamp poison the manifest.
GATE-2 removed that inference entirely — ``physicalCollectionOf`` is deleted,
the manifest writers take a REQUIRED caller-supplied ``collection`` and stamp
it verbatim on every row, and the indexer supplies the run's own collection
(``manifest_write_batch_hook``'s ``collection`` argument is the T3 batch's).
The nexus-2t63u reconcile still matters — ``physical_collection`` is the
document's own home-collection truth, read by catalog-scoped routing and by
``manifest_backfill``'s derivation — but it is no longer what keeps the
manifest honest. Consequence for the tests here: the two scenarios that used
to end in ``IndexRunVerifyRefused`` (the kill control, and the reconcile-
write-failure fence proof) now run CLEAN, and both were re-derived to pin the
new contract rather than deleted or weakened. A real-refusal control is
preserved inside the kill control via an explicit constructed-missing-chunk
write; if you touch that control, keep a real refusal exercised. RE-RE-DERIVED
(2026-08-17, catalog-029-manifest-chunk-fk.xml): that control used to prove
``IndexRunVerifyRefused`` at ``complete_index_run``; RDR-191's manifest FK now
refuses the same construction one step earlier, at ``append_manifest_chunks``
itself (``httpx.HTTPStatusError`` 409) — a stronger fence, not a weaker one.

Drives the PRODUCTION entry point ``nexus.doc_indexer.index_pdf`` (never
``_register_or_lookup_doc_id`` directly for the reconciliation proof) against
the SHARED engine substrate every unit test already uses (``tests/conftest.py``'s
autouse ``_pin_t2_substrate``), same pattern and rationale as
``tests/integration/test_tp8yk_manifest_never_outruns_chunks.py``.

Marked ``@pytest.mark.integration`` for the same reason as that file: real
PDF-indexing round trips through the catalog + T3, heavier than a pure-unit
test, though self-provisioned against the local hermetic substrate (no
external services/credentials needed).

ROUND 2 (nexus-ir68m, code-review Important #1 / substantive-critic
CRITICAL, empirically proven by the critic's probe): the reconcile
``writer.update(...)`` call in ``_register_or_lookup_doc_id``'s
``by_file_path`` hit originally sat inside the function's OWN broad
``except Exception: return ""`` — a transient failure on THAT write
(nothing to do with resolving the document, which already succeeded)
discarded an already-known tumbler, which downstream in ``index_pdf`` skips
BOTH ``_fence_begin`` and ``_fence_complete`` (gated on ``if
_catalog_doc_id_for_batch:``) and drops the run out of RUNFENCE entirely —
reintroducing the RDR-102/nexus-5xn3k/nexus-tp8yk un-fenced-completion gap
in service of fixing nexus-2t63u. ``TestReconcileWriteFailureIsolated``
below proves the fix: the reconcile write is now isolated in its OWN narrow
try/except, so a failure there is advisory (logged, swallowed) and the
already-resolved tumbler is never discarded — the run stays fenced (begins
and completes) rather than silently falling out of RUNFENCE altogether.
(As originally written that clause ended "...refusing honestly if the stale
stamp makes the manifest disagree" — see SUPERSEDED MECHANISM above: post-
GATE-2 a stale document stamp can no longer make the manifest disagree, so
the test now pins the fence-ran claim directly instead of inferring it from
a refusal.)
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from nexus.errors import SourceUriCollectionMismatchError

pytestmark = [pytest.mark.integration]


def _manifest_verify_via_client_reads(reader, doc_id: str) -> dict:
    """RDR-191 Phase 6 (nexus-o8dil.33), 2026-08-15: replaces the retired
    ``HttpCatalogClient.manifest_verify`` for this test file's verification
    use — that client method (and the GET /manifest/verify route it wrapped)
    is retired; the manifest-chunk FK makes the dangling state it diagnosed
    unreachable. ``nexus.manifest_verify(text)``, the underlying SQL
    function, is NOT dropped — ``CatalogRepository.completeIndexRun``
    depends on it internally — there is simply no client route onto it any
    more.

    Reconstructs the same ``{referenced, present, missing}`` shape
    client-side via ``get_manifest`` (still live) + T3 ``existing_ids``, the
    same idiom ``integrity.py``'s ``_verify_scoped`` (the only
    damage-detection surface left in the ``nx`` CLI) uses. Rows are grouped
    by their OWN stamped ``collection`` (nexus-kzso5 field), not the owning
    document's ``physical_collection`` — matching engine semantics exactly.
    """
    from nexus.db import make_t3

    rows = reader.get_manifest(doc_id)
    referenced = len(rows)
    if not rows:
        return {"referenced": 0, "present": 0, "missing": 0}
    by_collection: dict[str, list[str]] = {}
    for r in rows:
        by_collection.setdefault(r.collection or "", []).append(r.chash)
    t3 = make_t3()
    present = 0
    for coll, chashes in by_collection.items():
        if not coll:
            continue
        present += len(t3.existing_ids(coll, chashes))
    return {"referenced": referenced, "present": present, "missing": referenced - present}


def _extraction_result(page_count: int = 1):
    from nexus.pdf_extractor import ExtractionResult

    pages = [f"Page {i} 2t63u gate content." for i in range(page_count)]
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
    def extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, allow_degraded=False):
        for i in range(page_count):
            if on_page:
                on_page(i, f"Page {i} 2t63u gate content.", {"page_number": i + 1})
        return result
    return extract


def _fake_chunks(n: int, *, prefix: str):
    from nexus.pdf_chunker import TextChunk

    return [
        TextChunk(
            text=f"{prefix} {i} unique_2t63u_gate_content_{i}",
            chunk_index=i, metadata={"page": 1},
        )
        for i in range(n)
    ]


def _fresh_pdf(tmp_path: Path, *, marker: str) -> Path:
    # Deliberately NOT a real PDF: PDFExtractor/PDFChunker are patched in
    # every scenario below (mirrors test_tp8yk's identical rationale).
    p = tmp_path / f"doc-{marker}.pdf"
    p.write_bytes(f"%PDF-1.4 2t63u gate fake content [{marker}]\n".encode())
    return p.resolve()


def _collection(suffix: str) -> str:
    # Hardcoded to the shared engine substrate's ACTUAL configured local
    # embedder (bge-base-en-v15-768), same rationale + value as
    # test_tp8yk_manifest_never_outruns_chunks.py::_collection: the
    # CLIENT's naming preference (nexus.db.local_ef.local_model_token())
    # can diverge from what the shared JVM engine process actually loaded,
    # and this test upserts real chunks for real.
    return f"docs__2t63u-gate-{suffix}__bge-base-en-v15-768__v1"


def _pre_2t63u_register_or_lookup_doc_id(
    file_path, corpus, *, content_type, physical_collection,
    title="", author="", year=0, base_path=None, source_uri="",
):
    """KILL CONTROL fixture (mandatory —
    feedback_falsify_by_deleting_the_code / T2 nexus/nexus-2t63u-debug-
    2026-08-06's regression-test sketch item 2): the EXACT pre-nexus-2t63u
    shape of the ``by_file_path`` hit inside ``_register_or_lookup_doc_id``
    — return the existing tumbler with NO ``physical_collection``
    reconciliation. Used ONLY by the kill-control test below, patched in
    for the SECOND (re-index) call only, so the document already exists by
    the time this runs — the registration branch is intentionally not
    reproduced here (not exercised by that test).
    """
    from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
    from nexus.catalog.types import make_relative

    reader = make_catalog_reader()
    writer = make_catalog_writer()
    try:
        owner_name = corpus if corpus else ("standalone-pdfs" if content_type == "paper" else "standalone-docs")
        owner_t = reader.curator_owner_tumbler_by_name(owner_name)
        owner = owner_t if owner_t is not None else writer.register_owner(owner_name, "curator")
        fp = make_relative(file_path, base_path) if base_path else str(file_path)
        existing = reader.by_file_path(owner, fp)
        assert existing is not None, (
            "kill control fixture only covers the by_file_path-hit branch"
        )
        return str(existing.tumbler)
    finally:
        writer.close()
        reader.close()


class TestReconcileOnCollectionRetarget:
    def test_reindex_into_different_collection_reconciles_and_verifies_clean(
        self, tmp_path,
    ) -> None:
        """The bead's core assertion: re-indexing the SAME document into a
        DIFFERENT --collection (the bft-to-smr.pdf shape — a force re-index
        that retargets --collection) must reconcile physical_collection
        BEFORE the manifest write, land the completion stamp cleanly, and
        leave manifest_verify clean.
        """
        from nexus.catalog.factory import make_catalog_reader
        from nexus.db.http_vector_client import HttpVectorClient
        from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf

        corpus = "2t63u-gate-reconcile"
        collection_a = _collection("reconcile-a")
        collection_b = _collection("reconcile-b")
        pdf_path = _fresh_pdf(tmp_path, marker="reconcile")

        result = _extraction_result(1)

        # Run 1: index into collection A.
        with patch("nexus.doc_indexer.PDFExtractor") as ME, \
             patch("nexus.doc_indexer.PDFChunker") as MC:
            ME.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC.return_value.chunk.return_value = _fake_chunks(4, prefix="run1")
            n1 = index_pdf(
                pdf_path, corpus, t3=HttpVectorClient(),
                collection_name=collection_a, streaming="never",
            )
        assert n1 == 4

        doc_id = _register_or_lookup_doc_id(
            pdf_path, corpus, content_type="paper", physical_collection=collection_a,
        )
        assert doc_id, "catalog registration must succeed against the real service"

        reader = make_catalog_reader()
        entry_a = reader.resolve(doc_id)
        assert entry_a is not None
        assert entry_a.physical_collection == collection_a
        assert entry_a.index_state == "complete"

        # Run 2: FORCE re-index the SAME path into a DIFFERENT collection,
        # with content that DOES NOT overlap run 1's chashes (mirrors the
        # real bft-to-smr.pdf incident's partial-overlap shape without the
        # confound of identical content masking the defect — see module
        # docstring: the manifest joins the WRONG collection, and if run 2's
        # chunks happened to be byte-identical to run 1's, they'd already be
        # present under the stale collection too, hiding the bug).
        with capture_logs() as cap, \
             patch("nexus.doc_indexer.PDFExtractor") as ME2, \
             patch("nexus.doc_indexer.PDFChunker") as MC2:
            ME2.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC2.return_value.chunk.return_value = _fake_chunks(4, prefix="run2")
            n2 = index_pdf(
                pdf_path, corpus, t3=HttpVectorClient(),
                collection_name=collection_b, force=True, streaming="never",
            )
        assert n2 == 4, "chunks land in the NEW collection regardless — this was never about chunk loss"

        warnings = [
            e for e in cap
            if e.get("log_level") == "warning" and e.get("event") == "doc_physical_collection_reconciled"
        ]
        assert len(warnings) == 1, f"expected exactly one reconcile warning, got {cap}"
        assert warnings[0]["tumbler"] == doc_id
        assert warnings[0]["old_collection"] == collection_a
        assert warnings[0]["new_collection"] == collection_b
        assert warnings[0]["file_path"]

        entry_b = reader.resolve(doc_id)
        assert entry_b is not None
        assert entry_b.physical_collection == collection_b, (
            "physical_collection must be reconciled to the run's target "
            f"collection BEFORE the manifest write — got "
            f"{entry_b.physical_collection!r}"
        )
        assert entry_b.index_state == "complete", (
            "the completion stamp must succeed once the manifest is "
            f"stamped under the correct (new) collection — got "
            f"{entry_b.index_state!r}"
        )

        verify = _manifest_verify_via_client_reads(reader, doc_id)
        assert verify["referenced"] == verify["present"] == 4, verify
        assert verify["missing"] == 0, (
            f"manifest_verify must be clean once physical_collection is "
            f"reconciled BEFORE the manifest write — got {verify}"
        )

    def test_kill_control_without_reconcile_no_longer_wedges_after_gate2(
        self, tmp_path,
    ) -> None:
        """KILL CONTROL, RE-DERIVED (RDR-191 GATE-2, nexus-sh9v2).

        This test used to assert the OPPOSITE: revert
        ``_register_or_lookup_doc_id``'s by_file_path hit to its EXACT
        pre-nexus-2t63u shape and the wedge REAPPEARS — completion refused,
        ``manifest_verify`` reporting live chunks as missing. That is no
        longer reachable, and NOT because the fence weakened. RDR-191 GATE-2
        (498c92953) eradicated the disease at its source: the engine's
        manifest writers take a **caller-supplied** ``collection`` and stamp
        it verbatim on every row (``CatalogRepository`` "writers SEND the
        collection; the engine STAMPS it verbatim, NEVER [infers it]";
        ``physicalCollectionOf`` was deleted outright). The wedge's entire
        premise was that the engine DERIVED each manifest row's collection
        from ``catalog_documents.physical_collection`` at write time, so a
        stale document stamp poisoned the manifest and
        ``nexus.manifest_verify``'s ``k.collection = m.collection`` join
        looked in the wrong collection. Post-GATE-2 the rows carry the
        RUN's own collection (``manifest_write_batch_hook``'s ``collection``
        argument is the T3 batch's collection), so a stale
        ``physical_collection`` cannot reach the manifest at all.

        A kill control whose disease has been eradicated is REWRITTEN to pin
        the eradication, never deleted and never weakened to pass vacuously.
        So this now asserts the NEW contract — the pre-2t63u shape runs
        CLEAN because the manifest is stamped with the write's own
        collection — and then, because a green built on "no refusal" is
        worthless if the refusal path is dead, ends with an explicit
        non-vacuity control proving the fence STILL refuses on a genuinely
        missing chunk.

        Probe evidence (2026-08-14, engine v0.1.75 substrate): with the
        pre-2t63u shape patched in, run 2's manifest rows came back stamped
        ``collection_b`` (not the stale ``collection_a``),
        ``manifest_verify`` returned referenced=present=4/missing=0, and
        appending one manifest row for a chash absent from T3 made
        ``complete_index_run`` raise ``IndexRunVerifyRefused(missing=1)``.
        """
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer
        from nexus.db.http_vector_client import HttpVectorClient
        from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf

        corpus = "2t63u-gate-killctl"
        collection_a = _collection("killctl-a")
        collection_b = _collection("killctl-b")
        pdf_path = _fresh_pdf(tmp_path, marker="killctl")

        result = _extraction_result(1)

        with patch("nexus.doc_indexer.PDFExtractor") as ME, \
             patch("nexus.doc_indexer.PDFChunker") as MC:
            ME.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC.return_value.chunk.return_value = _fake_chunks(4, prefix="run1")
            n1 = index_pdf(
                pdf_path, corpus, t3=HttpVectorClient(),
                collection_name=collection_a, streaming="never",
            )
        assert n1 == 4

        doc_id = _register_or_lookup_doc_id(
            pdf_path, corpus, content_type="paper", physical_collection=collection_a,
        )
        assert doc_id

        with patch("nexus.doc_indexer.PDFExtractor") as ME2, \
             patch("nexus.doc_indexer.PDFChunker") as MC2, \
             patch(
                 "nexus.doc_indexer._register_or_lookup_doc_id",
                 side_effect=_pre_2t63u_register_or_lookup_doc_id,
             ):
            ME2.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC2.return_value.chunk.return_value = _fake_chunks(4, prefix="run2")
            # NO pytest.raises: post-GATE-2 the un-reconciled shape is no
            # longer damaging. A refusal HERE would mean the manifest is
            # being stamped from something other than the write's own
            # collection — i.e. GATE-2 regressed — so an unexpected
            # IndexRunVerifyRefused propagates and fails the test loudly.
            n2 = index_pdf(
                pdf_path, corpus, t3=HttpVectorClient(),
                collection_name=collection_b, force=True, streaming="never",
            )
        assert n2 == 4

        reader = make_catalog_reader()

        # ── The eradication itself: every manifest row carries the RUN's
        # collection, NOT the document's (still-stale) physical_collection.
        # This is the assertion the old wedge could not have satisfied.
        rows = reader.get_manifest(doc_id)
        assert rows, "run 2 wrote no manifest rows at all"
        stamped = {r.collection for r in rows}
        assert stamped == {collection_b}, (
            "RDR-191 GATE-2 contract broken: manifest rows must carry the "
            f"collection the WRITE supplied ({collection_b!r}) even when the "
            "document's physical_collection was never reconciled. Got "
            f"{sorted(stamped)!r} — a row stamped {collection_a!r} means the "
            "engine is inferring the collection from the owning document "
            "again (physicalCollectionOf resurrection), which is exactly the "
            "nexus-2t63u wedge's root cause."
        )

        verify = _manifest_verify_via_client_reads(reader, doc_id)
        assert verify["referenced"] == verify["present"] == 4, verify
        assert verify["missing"] == 0, (
            "manifest_verify must be clean without any reconciliation — the "
            "rows and the chunks are both in the run's own collection, so "
            f"the k.collection = m.collection join matches. Got {verify}"
        )

        entry = reader.resolve(doc_id)
        assert entry is not None
        assert entry.index_state == "complete", (
            "the completion stamp must land: nothing is missing, so the "
            f"fence has nothing to refuse. Got {entry.index_state!r}"
        )
        # The document's own stamp still converges — but at the TAIL, via
        # _catalog_pdf_hook, which only runs because the completion was NOT
        # refused. (The sibling test above gets the SAME end state from the
        # EARLY nexus-2t63u reconcile instead. Pre-GATE-2 the refusal fired
        # first and the tail hook was never reached, which is what made the
        # wedge self-perpetuating.) Asserted so the old assertion's slot is
        # not left as a silent hole.
        assert entry.physical_collection == collection_b, (
            "without the early reconcile, physical_collection must still "
            f"converge at the tail hook — expected {collection_b!r}, got "
            f"{entry.physical_collection!r}"
        )

        # ── Non-vacuity control (mandatory; feedback_falsify_by_deleting_
        # the_code). Everything above is a green built on the ABSENCE of a
        # refusal, which is worthless if the refusal path is dead. Construct
        # a genuinely missing chunk — a manifest row naming a chash that was
        # never upserted into T3 — and prove a fence still bites.
        #
        # RE-DERIVED (2026-08-17, catalog-029-manifest-chunk-fk.xml): this
        # used to prove IndexRunVerifyRefused at complete_index_run, one
        # step AFTER a successful append_manifest_chunks. RDR-191's manifest
        # FK (nexus.catalog_document_chunks -> nexus.chunks) now refuses the
        # SAME construction earlier and harder — the append call itself
        # 409s, since `absent_chash` names no nexus.chunks row at all. That
        # is a STRONGER fence than the one this control originally pinned,
        # not a weaker one: the dangling reference can no longer be written
        # at all, so IndexRunVerifyRefused never gets a chance to fire on
        # this path. The control's intent is unchanged — prove a real fence
        # still bites for a genuinely missing chunk — it now bites at
        # append_manifest_chunks instead of complete_index_run.
        import httpx

        writer = make_catalog_writer()
        try:
            absent_chash = "de" * 32  # 64 hex, never upserted to T3
            with pytest.raises(httpx.HTTPStatusError) as excinfo:
                writer.append_manifest_chunks(
                    doc_id, [{"position": 99, "chash": absent_chash}],
                    collection=collection_b,
                )
            assert excinfo.value.response.status_code == 409, excinfo.value
        finally:
            writer.close()


class TestReconcileWriteFailureIsolated:
    """nexus-ir68m (round 2). A transient failure writing the reconcile
    itself must NEVER discard an already-resolved tumbler or drop the run
    out of RUNFENCE. Patches ``HttpCatalogClient.update`` to raise — the
    critic's own probe — rather than anything at the ``_register_or_lookup_
    doc_id`` call boundary, so these tests exercise the EXACT failure mode
    the finding described (a transport/engine failure on the reconcile
    write, not a bad argument or a caller error).
    """

    def test_write_failure_does_not_discard_the_tumbler(self, tmp_path) -> None:
        from nexus.catalog.factory import make_catalog_reader
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.db.http_vector_client import HttpVectorClient
        from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf

        corpus = "2t63u-gate-writefail"
        collection_a = _collection("writefail-a")
        collection_b = _collection("writefail-b")
        pdf_path = _fresh_pdf(tmp_path, marker="writefail")

        result = _extraction_result(1)
        with patch("nexus.doc_indexer.PDFExtractor") as ME, \
             patch("nexus.doc_indexer.PDFChunker") as MC:
            ME.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC.return_value.chunk.return_value = _fake_chunks(4, prefix="run1")
            n1 = index_pdf(
                pdf_path, corpus, t3=HttpVectorClient(),
                collection_name=collection_a, streaming="never",
            )
        assert n1 == 4

        doc_id = _register_or_lookup_doc_id(
            pdf_path, corpus, content_type="paper", physical_collection=collection_a,
        )
        assert doc_id

        with capture_logs() as cap, \
             patch.object(HttpCatalogClient, "update", side_effect=RuntimeError("simulated transient reconcile-write failure")):
            returned = _register_or_lookup_doc_id(
                pdf_path, corpus, content_type="paper", physical_collection=collection_b,
            )

        assert returned == doc_id, (
            "a transient failure writing the RECONCILE must never discard "
            f"an already-resolved tumbler — expected {doc_id!r}, got "
            f"{returned!r} (empty-string return is the nexus-ir68m damage: "
            f"it skips both _fence_begin and _fence_complete downstream)"
        )

        warnings = [
            e for e in cap
            if e.get("log_level") == "warning" and e.get("event") == "doc_physical_collection_reconcile_write_failed"
        ]
        assert len(warnings) == 1, f"expected exactly one advisory warning, got {cap}"
        assert warnings[0]["tumbler"] == doc_id
        assert warnings[0]["old_collection"] == collection_a
        assert warnings[0]["new_collection"] == collection_b

        # The write genuinely failed — physical_collection must NOT have
        # silently changed despite the advisory swallow.
        entry = make_catalog_reader().resolve(doc_id)
        assert entry is not None
        assert entry.physical_collection == collection_a, (
            "the reconcile write failed — physical_collection must remain "
            f"at its OLD value, got {entry.physical_collection!r}"
        )

    def test_write_failure_leaves_the_run_fenced_not_skipped(self, tmp_path) -> None:
        """The stronger, end-to-end proof: drives the PRODUCTION entry
        point with the reconcile write failing, and shows the run stays
        inside RUNFENCE (both fence legs fire) rather than silently falling
        out of it — which is what ``doc_id=""`` would look like (no fence
        activity at all, run 1's completion record left untouched).

        RE-DERIVED for RDR-191 GATE-2 (nexus-sh9v2). The original form
        asserted the fence stayed fenced by watching it REFUSE
        (``IndexRunVerifyRefused``), since the failed reconcile left
        ``physical_collection`` stale and — under the pre-GATE-2 engine —
        that staleness propagated into the manifest rows. It no longer
        does: manifest rows are stamped with the caller-supplied collection
        (the run's own, ``collection_b``), so the verify is clean and the
        completion LANDS. This test's own prior comment already ruled that
        outcome acceptable — "both outcomes (a completion that succeeds
        against the old collection, or an honest refusal) are acceptable;
        SILENTLY SKIPPING the fence is not" — so the assertion moves to
        what the test was always about: did the fence RUN.

        Keeping the old ``pytest.raises`` would have been the weakening
        move (it would only pass again if GATE-2 regressed). Instead the
        fence-ran claim is pinned two independent ways, neither of which a
        discarded tumbler could satisfy: (1) ``_fence_begin`` /
        ``_fence_complete`` are wrapped spies — production behaviour intact
        — and must BOTH be called with the resolved tumbler; (2) the
        engine-side ``index_run_id`` must ROTATE away from run 1's value
        (``_fence_begin`` mints it), with ``index_state`` back at
        ``complete``. The live refusal path stays covered by
        ``test_kill_control_without_reconcile_no_longer_wedges_after_gate2``'s
        non-vacuity control.
        """
        import nexus.doc_indexer as doc_indexer_module

        from nexus.catalog.factory import make_catalog_reader
        from nexus.catalog.http_catalog_client import HttpCatalogClient
        from nexus.db.http_vector_client import HttpVectorClient
        from nexus.doc_indexer import _register_or_lookup_doc_id, index_pdf

        corpus = "2t63u-gate-writefail-fence"
        collection_a = _collection("writefail-fence-a")
        collection_b = _collection("writefail-fence-b")
        pdf_path = _fresh_pdf(tmp_path, marker="writefail-fence")

        result = _extraction_result(1)
        with patch("nexus.doc_indexer.PDFExtractor") as ME, \
             patch("nexus.doc_indexer.PDFChunker") as MC:
            ME.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC.return_value.chunk.return_value = _fake_chunks(4, prefix="run1")
            n1 = index_pdf(
                pdf_path, corpus, t3=HttpVectorClient(),
                collection_name=collection_a, streaming="never",
            )
        assert n1 == 4

        doc_id = _register_or_lookup_doc_id(
            pdf_path, corpus, content_type="paper", physical_collection=collection_a,
        )
        assert doc_id

        reader = make_catalog_reader()
        run1_entry = reader.resolve(doc_id)
        assert run1_entry is not None
        run1_run_id = run1_entry.index_run_id
        assert run1_run_id, "run 1 must have left a fenced completion to contrast against"

        with patch("nexus.doc_indexer.PDFExtractor") as ME2, \
             patch("nexus.doc_indexer.PDFChunker") as MC2, \
             patch.object(HttpCatalogClient, "update", side_effect=RuntimeError("simulated transient reconcile-write failure")), \
             patch("nexus.doc_indexer._fence_begin",
                   wraps=doc_indexer_module._fence_begin) as spy_begin, \
             patch("nexus.doc_indexer._fence_complete",
                   wraps=doc_indexer_module._fence_complete) as spy_complete:
            ME2.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC2.return_value.chunk.return_value = _fake_chunks(4, prefix="run2")
            # No pytest.raises: post-GATE-2 the manifest carries the run's
            # own collection, so there is nothing for the fence to refuse.
            # An IndexRunVerifyRefused here would propagate and fail loudly.
            n2 = index_pdf(
                pdf_path, corpus, t3=HttpVectorClient(),
                collection_name=collection_b, force=True, streaming="never",
            )
        assert n2 == 4

        # ── Proof 1: both fence legs ran, each with the RESOLVED tumbler.
        # The nexus-ir68m damage (reconcile failure discarding the tumbler,
        # returning "") leaves both of these at zero calls, because
        # index_pdf gates them on `if _catalog_doc_id_for_batch:`.
        assert spy_begin.call_count == 1, (
            "_fence_begin never fired — the run fell OUT of RUNFENCE, which "
            "is the nexus-ir68m damage: a failed reconcile write discarded "
            f"an already-resolved tumbler. Calls: {spy_begin.call_args_list!r}"
        )
        assert spy_begin.call_args_list[0].args[0] == doc_id, spy_begin.call_args_list
        assert spy_complete.call_count == 1, (
            "_fence_complete never fired — the run began but never closed "
            f"the fence. Calls: {spy_complete.call_args_list!r}"
        )
        assert spy_complete.call_args_list[0].args[0] == doc_id, spy_complete.call_args_list

        entry = reader.resolve(doc_id)
        assert entry is not None

        # ── Proof 2 (independent of the spies, engine-observable): the fence
        # minted a NEW index_run_id. A skipped fence leaves run 1's value in
        # place, so this discriminates fenced-completion from no-fence-at-all
        # even though both end at index_state == "complete".
        assert entry.index_run_id and entry.index_run_id != run1_run_id, (
            "index_run_id did not rotate — _fence_begin never minted a run "
            f"for this pass (run 1: {run1_run_id!r}, now: "
            f"{entry.index_run_id!r}), i.e. the run was never fenced"
        )
        assert entry.index_state == "complete", (
            "the fence must have BEGUN and then COMPLETED: post-RDR-191 "
            "GATE-2 the manifest rows carry the run's own collection, so the "
            "completion verify has nothing to refuse even though the "
            f"reconcile write failed. Got {entry.index_state!r}"
        )

        # The reconcile write genuinely failed — physical_collection must NOT
        # have silently changed. (The tail _catalog_pdf_hook would normally
        # converge it; here its writer.update is the same patched failure,
        # logged as catalog_pdf_hook_failed and swallowed.)
        assert entry.physical_collection == collection_a, (
            "the reconcile write failed — physical_collection must remain "
            f"stale at {collection_a!r}, got {entry.physical_collection!r}"
        )

        # ── Proof 3: the stale document stamp did NOT reach the manifest.
        # This is why there was nothing to refuse, stated as a contract
        # rather than left as an inference from the absent exception.
        stamped = {r.collection for r in reader.get_manifest(doc_id)}
        assert stamped == {collection_b}, (
            "manifest rows must carry the WRITE's collection "
            f"({collection_b!r}) even with the document still stamped "
            f"{collection_a!r}. Got {sorted(stamped)!r}"
        )
        verify = _manifest_verify_via_client_reads(reader, doc_id)
        assert verify["missing"] == 0, verify


class TestSourceUriBranchUnchanged:
    def test_source_uri_collection_mismatch_still_raises(self, tmp_path) -> None:
        """Regression pin (T2 nexus/nexus-2t63u-debug-2026-08-06 sketch item
        4): nexus-2t63u's edit lands in the SIBLING ``by_file_path`` branch
        of ``_register_or_lookup_doc_id`` only. The ``source_uri`` branch's
        existing fail-loud contract — a ``--source-uri``/``--collection``
        pair that disagrees with the document's home RAISES rather than
        reconciling — must be completely unchanged. Colocated with the
        nexus-2t63u fix rather than relying solely on
        tests/test_y8qtj_source_uri_client_wave.py continuing to exist.
        """
        from nexus.doc_indexer import _register_or_lookup_doc_id

        from tests._catalog_fixture_ops import ActiveCatalog

        cat = ActiveCatalog()
        owner = cat.register_owner("2t63u-source-uri-mismatch", "curator")
        dt_uri = "x-devonthink-item://2t63u-MISMATCH-REGRESSION-PIN"
        home_collection = _collection("srcuri-home")
        cat.register(
            owner, "2t63u Source URI Mismatch Paper",
            content_type="paper",
            file_path="2t63u-mismatch.pdf",
            physical_collection=home_collection,
            source_uri=dt_uri,
        )

        path = tmp_path / "2t63u-mismatch.pdf"
        path.write_bytes(b"%PDF-1.4 stub")

        with pytest.raises(SourceUriCollectionMismatchError):
            _register_or_lookup_doc_id(
                path, "2t63u-source-uri-mismatch",
                content_type="paper",
                physical_collection=_collection("srcuri-target"),
                source_uri=dt_uri,
            )
