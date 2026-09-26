# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4pj54 end to end: a multi-batch PDF re-index against the real
engine substrate must not delete live chunks before the document completes.

Drives the production entry point ``nexus.doc_indexer.index_pdf`` with the
default hook chain (so ``manifest_write_batch_hook`` and the real catalog
manifest run), a real ``HttpVectorClient`` and server-side embedding, on
the shared per-process engine substrate (``tests/conftest.py``'s
``_pin_t2_substrate``). No API key is needed, so this runs in the default
loop rather than under the ``integration`` marker (tests/AGENTS.md,
scenario journey layer). Extraction and chunking are patched, exactly as
``tests/integration/test_tp8yk_manifest_never_outruns_chunks.py`` does:
the PDF bytes only feed the content hash.

``_INCREMENTAL_THRESHOLD`` and ``_INCREMENTAL_BATCH_SIZE`` are cut to 3 so
a 9-chunk document takes ``_index_pdf_incremental`` in three batches. v2
changes chunks 0 and 1 and keeps 2..8. Batch 1 carries position 0 and
REPLACES the 9-row manifest with 3 rows, dropping c0, c1 and c3..c8.

Before the fix that REPLACE swept all eight at once; c3..c8 were live
chunks batches 2 and 3 were about to re-append, and the uploader then
re-embedded them. The end state looks the same either way (every final
chunk is back in T3), so this test does not rely on the end state alone:
it records every real T3 delete the sweep issues and when it was issued
relative to the document's completion fence.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest

_COLLECTION = "docs__4pj54-gate__bge-base-en-v15-768__v1"
_CORPUS = "4pj54-multibatch-gate"


def _extraction_result():
    from nexus.pdf_extractor import ExtractionResult

    text = "Page 0 4pj54 gate content."
    return ExtractionResult(
        text=text,
        metadata={
            "extraction_method": "docling", "page_count": 1,
            "page_boundaries": [{"page_number": 1, "start_char": 0,
                                 "page_text_length": len(text) + 1}],
            "table_regions": [], "format": "markdown",
        },
    )


def _extract_side_effect(result):
    def extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, **kwargs):
        if on_page:
            on_page(0, "Page 0 4pj54 gate content.", {"page_number": 1})
        return result
    return extract


def _texts(version: int) -> list[str]:
    kept = [f"4pj54 multibatch gate kept chunk {i} unique_marker" for i in range(2, 9)]
    head = [f"4pj54 multibatch gate v{version} chunk {i} unique_marker" for i in range(2)]
    return head + kept


def _chash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _index(pdf_path: Path, texts: list[str]) -> int:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import index_pdf
    from nexus.pdf_chunker import TextChunk

    chunks = [TextChunk(text=t, chunk_index=i, metadata={"page": 1}) for i, t in enumerate(texts)]
    with patch("nexus.doc_indexer.PDFExtractor") as ME, \
         patch("nexus.doc_indexer.PDFChunker") as MC:
        ME.return_value.extract.side_effect = _extract_side_effect(_extraction_result())
        MC.return_value.chunk.return_value = chunks
        return index_pdf(
            pdf_path, _CORPUS, t3=HttpVectorClient(), collection_name=_COLLECTION,
            streaming="never",
        )


def _present(chashes: list[str]) -> set[str]:
    from nexus.db.http_vector_client import HttpVectorClient

    return set(HttpVectorClient().existing_ids(_COLLECTION, chashes))


def test_multibatch_reindex_sweeps_only_superseded_chunks_after_completion(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import nexus.mcp_infra as mcp_infra
    from nexus.catalog.factory import make_catalog_reader
    from nexus.db.http_vector_client import _ServiceCollectionStub
    from nexus.doc_indexer import _register_or_lookup_doc_id

    monkeypatch.setattr("nexus.doc_indexer._INCREMENTAL_THRESHOLD", 3)
    monkeypatch.setattr("nexus.doc_indexer._INCREMENTAL_BATCH_SIZE", 3)

    v1, v2 = _texts(1), _texts(2)
    v1_chashes, v2_chashes = [_chash(t) for t in v1], [_chash(t) for t in v2]
    superseded = set(v1_chashes) - set(v2_chashes)
    assert superseded == {v1_chashes[0], v1_chashes[1]}  # the scenario's own premise

    pdf = tmp_path / "doc-4pj54.pdf"
    pdf.write_bytes(b"%PDF-1.4 4pj54 multibatch gate v1\n")
    assert _index(pdf, v1) == 9
    doc_id = _register_or_lookup_doc_id(
        pdf.resolve(), _CORPUS, content_type="paper", physical_collection=_COLLECTION,
    )
    assert doc_id
    assert [r.chash for r in make_catalog_reader().get_manifest(doc_id)] == v1_chashes
    assert _present(v1_chashes) == set(v1_chashes)

    # Record every real T3 delete (the call still goes to the engine) and
    # the moment the completion fence reaches the deferred sweep.
    events: list[tuple[str, object]] = []
    real_delete = _ServiceCollectionStub.delete

    def _spy_delete(self, ids):
        events.append(("delete", sorted(ids)))
        return real_delete(self, ids)

    real_deferred = mcp_infra.sweep_deferred_superseded_vectors
    at_completion: dict[str, object] = {}

    def _spy_deferred(d_id):
        # The run's last batch has landed and the stamp succeeded; nothing
        # may have been deleted yet, and every v2 chunk must be in T3.
        at_completion["deletes_so_far"] = [e for e in events if e[0] == "delete"]
        at_completion["present"] = _present(v2_chashes)
        events.append(("completion", d_id))
        return real_deferred(d_id)

    monkeypatch.setattr(_ServiceCollectionStub, "delete", _spy_delete)
    monkeypatch.setattr(mcp_infra, "sweep_deferred_superseded_vectors", _spy_deferred)
    mcp_infra.reset_superseded_sweep_stats()

    pdf.write_bytes(b"%PDF-1.4 4pj54 multibatch gate v2 (different bytes)\n")
    assert _index(pdf, v2) == 9

    assert ("completion", doc_id) in events, "the completion fence never reached the deferred sweep"
    assert at_completion["deletes_so_far"] == [], (
        "live chunks were deleted from T3 before the document completed: "
        f"{at_completion['deletes_so_far']}"
    )
    assert at_completion["present"] == set(v2_chashes)

    deleted = {h for kind, ids in events if kind == "delete" for h in ids}
    assert deleted == superseded, (
        f"the sweep must delete exactly the truly superseded chashes; "
        f"extra={sorted(deleted - superseded)} missing={sorted(superseded - deleted)}"
    )
    assert mcp_infra.get_superseded_sweep_stats()["swept"] == 2

    assert [r.chash for r in make_catalog_reader().get_manifest(doc_id)] == v2_chashes
    assert _present(v2_chashes) == set(v2_chashes), "a chash in the final manifest is missing from T3"
    assert _present(sorted(superseded)) == set(), "a superseded chunk survived the completion sweep"
    assert make_catalog_reader().resolve(doc_id).index_state == "complete"
    mcp_infra.reset_superseded_sweep_stats()
