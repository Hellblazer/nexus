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
and completes, refusing honestly if the stale stamp makes the manifest
disagree) rather than silently falling out of RUNFENCE altogether.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from nexus.errors import IndexRunVerifyRefused, SourceUriCollectionMismatchError

pytestmark = [pytest.mark.integration]


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

        verify = reader.manifest_verify(doc_id)
        assert verify["referenced"] == verify["present"] == 4, verify
        assert verify["missing"] == 0, (
            f"manifest_verify must be clean once physical_collection is "
            f"reconciled BEFORE the manifest write — got {verify}"
        )

    def test_kill_control_without_reconcile_reproduces_the_wedge(
        self, tmp_path,
    ) -> None:
        """KILL CONTROL (mandatory). Reverts ``_register_or_lookup_doc_id``'s
        by_file_path hit to its EXACT pre-nexus-2t63u shape (no
        reconciliation) and proves the damage REAPPEARS under the IDENTICAL
        re-index-into-a-different-collection scenario as the base test
        above: chunks land correctly in the NEW collection, but the
        manifest stays stamped to the STALE collection, so the completion
        verify refuses and manifest_verify reports missing chunks that are
        actually present — just under the wrong collection. Without this
        control, the base test's green could be incidental.
        """
        from nexus.catalog.factory import make_catalog_reader
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
            with pytest.raises(IndexRunVerifyRefused) as excinfo:
                index_pdf(
                    pdf_path, corpus, t3=HttpVectorClient(),
                    collection_name=collection_b, force=True, streaming="never",
                )

        assert excinfo.value.missing > 0, (
            "kill control failed to reproduce the wedge — expected the "
            f"stale collection stamp to make the completion verify refuse "
            f"with missing > 0, got {excinfo.value.missing}"
        )

        reader = make_catalog_reader()
        entry = reader.resolve(doc_id)
        assert entry is not None
        assert entry.physical_collection == collection_a, (
            "the kill control must reproduce the STALE stamp — "
            f"physical_collection must remain {collection_a!r}, got "
            f"{entry.physical_collection!r}"
        )
        assert entry.index_state == "indexing", (
            "the completion stamp must be REFUSED (never 'complete') — "
            f"got {entry.index_state!r}"
        )

        verify = reader.manifest_verify(doc_id)
        assert verify["missing"] > 0, (
            f"kill control failed to reproduce the wedge — expected "
            f"manifest_verify to report missing chunks against the stale "
            f"collection stamp, got {verify}"
        )
        assert verify["present"] < verify["referenced"], verify


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
        inside RUNFENCE (fences begin/complete and refuses HONESTLY on the
        now-stale manifest) rather than silently succeeding with an
        untouched, stale index_state — which is what doc_id="" would look
        like (no exception, no fence activity at all).
        """
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

        with patch("nexus.doc_indexer.PDFExtractor") as ME2, \
             patch("nexus.doc_indexer.PDFChunker") as MC2, \
             patch.object(HttpCatalogClient, "update", side_effect=RuntimeError("simulated transient reconcile-write failure")):
            ME2.return_value.extract.side_effect = _extract_side_effect(1, result)
            MC2.return_value.chunk.return_value = _fake_chunks(4, prefix="run2")
            with pytest.raises(IndexRunVerifyRefused) as excinfo:
                index_pdf(
                    pdf_path, corpus, t3=HttpVectorClient(),
                    collection_name=collection_b, force=True, streaming="never",
                )

        # The run stayed FENCED: it reached the completion verify (which is
        # what raises IndexRunVerifyRefused) rather than silently returning
        # a chunk count with doc_id="" and no fence activity at all — the
        # damage this bead fixes. The refusal itself is EXPECTED and
        # correct here (the reconcile write failed, so the manifest is
        # still stamped to the stale collection_a) — both outcomes (a
        # completion that succeeds against the old collection, or an
        # honest refusal) are acceptable; SILENTLY SKIPPING the fence is
        # not.
        assert excinfo.value.doc_id == doc_id
        assert excinfo.value.missing > 0

        entry = make_catalog_reader().resolve(doc_id)
        assert entry is not None
        assert entry.physical_collection == collection_a, (
            "the reconcile write failed — physical_collection must remain "
            f"stale at {collection_a!r}, got {entry.physical_collection!r}"
        )
        assert entry.index_state == "indexing", (
            "the fence must have BEGUN (index_state transitions away from "
            "run 1's 'complete' the moment _fence_begin fires) and then "
            "been correctly refused at completion — 'indexing' proves the "
            f"fence ran; got {entry.index_state!r}"
        )


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
