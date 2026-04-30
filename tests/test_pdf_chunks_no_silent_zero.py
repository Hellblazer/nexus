# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Regression guards for nexus-aold: silent zero-chunk indexing.

A 71MB PDF (DEVONthink 4.2.2 user manual) caused ``nx index pdf
--extractor docling`` to exit silently with 0 chunks indexed. The
multiprocessing-leaked-semaphore warning at process shutdown was
the only signal something went wrong. Acceptance: the indexer must
either succeed or fail loud with an actionable error naming the
failure mode.

Two guards cover the silent-zero case:

1. ``_extract_normalized`` raises when PyMuPDF returns no text (the
   silent fallback that used to mask Docling crashes). Tested in
   ``test_pdf_extractor.py``.

2. ``_pdf_chunks`` raises when extraction succeeded with non-empty
   text but the chunker returned zero chunks. The previous
   behaviour was a silent ``return []`` which the indexer treated
   as "no work" (invisible to the operator).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_pdf_chunks_raises_when_text_present_but_chunker_empty(tmp_path: Path):
    """nexus-aold: text was extracted (non-empty) but the chunker
    returned an empty list. Pre-fix, _pdf_chunks silently returned
    [] and the indexer reported success with 0 records. Post-fix,
    raises an informative RuntimeError so the operator sees the
    silent failure mode named explicitly."""
    from nexus.doc_indexer import _pdf_chunks

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")  # placeholder; extractor is mocked

    with (
        patch("nexus.doc_indexer.PDFExtractor") as ext_cls,
        patch("nexus.doc_indexer.PDFChunker") as chk_cls,
    ):
        ext_cls.return_value.extract.return_value = MagicMock(
            text="some real extracted text content",
            metadata={
                "extraction_method": "docling",
                "page_count": 100,
                "format": "markdown",
                "page_boundaries": [],
            },
        )
        # Chunker returns empty list (the silent-zero failure mode).
        chk_cls.return_value.chunk.return_value = []

        with pytest.raises(RuntimeError, match="zero chunks"):
            _pdf_chunks(
                pdf,
                content_hash="deadbeef" * 8,
                target_model="voyage-context-3",
                now_iso="2026-04-30T00:00:00+00:00",
                corpus="default",
            )


def test_pdf_chunks_returns_empty_when_extraction_empty(tmp_path: Path):
    """When the extractor itself reports empty (the existing guard
    raised at extraction-time), ``_pdf_chunks`` should still surface
    the failure cleanly, but in this branch the chunker isn't even
    reached, so the contract is just 'no silent zero from this layer
    when extraction was empty too'.

    Today _extract_with_docling and _extract_normalized both raise
    on empty extraction, so this case can only occur if a future
    extractor returns empty text without raising. Treat empty text
    + empty chunks as the legitimate (no-op) case so the new guard
    only fires when there's a real text/chunker mismatch."""
    from nexus.doc_indexer import _pdf_chunks

    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    with (
        patch("nexus.doc_indexer.PDFExtractor") as ext_cls,
        patch("nexus.doc_indexer.PDFChunker") as chk_cls,
    ):
        ext_cls.return_value.extract.return_value = MagicMock(
            text="",  # also empty; chunks will also be empty, no inconsistency
            metadata={
                "extraction_method": "docling",
                "page_count": 0,
                "format": "markdown",
                "page_boundaries": [],
            },
        )
        chk_cls.return_value.chunk.return_value = []

        # No raise; both empty is the trivial no-op case.
        result = _pdf_chunks(
            pdf,
            content_hash="deadbeef" * 8,
            target_model="voyage-context-3",
            now_iso="2026-04-30T00:00:00+00:00",
            corpus="default",
        )
        assert result == []
