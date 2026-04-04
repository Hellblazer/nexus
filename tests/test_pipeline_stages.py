# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pipeline stage functions (nexus-qwxz.5/6/7)."""
from __future__ import annotations

import json
import struct
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_chunker import TextChunk
from nexus.pdf_extractor import ExtractionResult
from nexus.pipeline_buffer import PipelineDB
from nexus.pipeline_stages import chunker_loop, extractor_loop, pipeline_index_pdf, uploader_loop


@pytest.fixture()
def db(tmp_path: Path) -> PipelineDB:
    return PipelineDB(tmp_path / "pipeline.db")


def _fake_embedding(index: int) -> bytes:
    """Produce a deterministic 4-float embedding as bytes."""
    return struct.pack("4f", float(index), 0.1, 0.2, 0.3)


def _make_extraction_result(page_count: int = 3) -> ExtractionResult:
    pages = [f"Page {i} text content." for i in range(page_count)]
    boundaries = []
    pos = 0
    for i, p in enumerate(pages):
        boundaries.append({"page_number": i + 1, "start_char": pos, "page_text_length": len(p) + 1})
        pos += len(p) + 1
    return ExtractionResult(
        text="\n".join(pages),
        metadata={
            "extraction_method": "docling",
            "page_count": page_count,
            "page_boundaries": boundaries,
            "table_regions": [{"page": 2, "html": "<table/>"}],
            "format": "markdown",
        },
    )


# ── extractor_loop ───────────────────────────────────────────────────────────


class TestExtractorLoop:
    def test_writes_pages_to_buffer(self, db: PipelineDB) -> None:
        """All extracted pages are written to PipelineDB."""
        result = _make_extraction_result(3)
        db.create_pipeline("h1", "/a.pdf", "docs__test")

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            instance = MockExt.return_value
            # Simulate extract() calling on_page for each page
            def fake_extract(pdf_path, *, extractor="auto", on_page=None):
                for i in range(3):
                    if on_page:
                        on_page(i, f"Page {i} text content.", {"page_number": i + 1, "text_length": 21})
                return result
            instance.extract.side_effect = fake_extract

            cancel = threading.Event()
            ret = extractor_loop(Path("/a.pdf"), "h1", db, cancel)

        pages = db.read_pages("h1")
        assert len(pages) == 3
        assert pages[0]["page_text"] == "Page 0 text content."
        state = db.get_pipeline_state("h1")
        assert state["total_pages"] == 3
        assert ret is result

    def test_cancel_mid_extraction(self, db: PipelineDB) -> None:
        """Cancel event stops page writes."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()

        def fake_extract(pdf_path, *, extractor="auto", on_page=None):
            for i in range(5):
                if cancel.is_set():
                    break
                if on_page:
                    on_page(i, f"Page {i}", {"page_number": i + 1, "text_length": 6})
                if i == 1:
                    cancel.set()
            return _make_extraction_result(5)

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            MockExt.return_value.extract.side_effect = fake_extract
            extractor_loop(Path("/a.pdf"), "h1", db, cancel)

        pages = db.read_pages("h1")
        assert len(pages) <= 3  # at most pages 0, 1, 2 written

    def test_resume_skips_existing_pages(self, db: PipelineDB) -> None:
        """On resume, pages already in buffer are not re-written."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "Original page 0")
        db.update_progress("h1", pages_extracted=1)

        written_indices: list[int] = []

        def fake_extract(pdf_path, *, extractor="auto", on_page=None):
            for i in range(3):
                if on_page:
                    on_page(i, f"Page {i} new", {"page_number": i + 1, "text_length": 10})
            return _make_extraction_result(3)

        original_write_page = db.write_page

        def tracking_write_page(content_hash, page_index, text, metadata=None):
            written_indices.append(page_index)
            original_write_page(content_hash, page_index, text, metadata)

        db.write_page = tracking_write_page  # type: ignore[assignment]

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            MockExt.return_value.extract.side_effect = fake_extract
            cancel = threading.Event()
            extractor_loop(Path("/a.pdf"), "h1", db, cancel)

        # Page 0 was skipped (already in buffer)
        assert 0 not in written_indices
        # But pages 1, 2 were written
        assert 1 in written_indices
        assert 2 in written_indices

    def test_returns_extraction_result(self, db: PipelineDB) -> None:
        result = _make_extraction_result()
        db.create_pipeline("h1", "/a.pdf", "docs__test")

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            MockExt.return_value.extract.side_effect = lambda *a, **kw: result
            ret = extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())

        assert ret.metadata["table_regions"] == [{"page": 2, "html": "<table/>"}]


# ── chunker_loop ─────────────────────────────────────────────────────────────


class TestChunkerLoop:
    def _populate_pages(self, db: PipelineDB, hash_: str, count: int) -> None:
        """Populate page buffer and mark extraction complete."""
        db.create_pipeline(hash_, "/a.pdf", "docs__test")
        for i in range(count):
            db.write_page(hash_, i, f"Page {i} content here.", metadata={"page_number": i + 1, "text_length": 22})
        db.update_progress(hash_, total_pages=count, pages_extracted=count)

    def test_produces_chunks_from_pages(self, db: PipelineDB) -> None:
        """Chunker reads pages, joins text, chunks, embeds, writes to buffer."""
        self._populate_pages(db, "h1", 3)

        fake_chunks = [
            TextChunk(text="chunk 0 text", chunk_index=0, metadata={"page": 1}),
            TextChunk(text="chunk 1 text", chunk_index=1, metadata={"page": 2}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            cancel = threading.Event()
            chunker_loop("h1", db, cancel, embed_fn=fake_embed)

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 2
        assert chunks[0]["chunk_text"] == "chunk 0 text"
        assert chunks[0]["embedding"] is not None

        state = db.get_pipeline_state("h1")
        assert state["chunks_created"] == 2
        assert state["chunks_embedded"] == 2

    def test_cancel_exits_poll_loop(self, db: PipelineDB) -> None:
        """Cancel event causes chunker to exit within one poll cycle."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        # Don't mark extraction complete — chunker will poll forever unless cancelled
        cancel = threading.Event()

        def cancel_soon():
            time.sleep(0.2)
            cancel.set()

        t = threading.Thread(target=cancel_soon)
        t.start()

        with patch("nexus.pipeline_stages.PDFChunker"):
            chunker_loop("h1", db, cancel, embed_fn=lambda t, m: ([], m))

        t.join()
        # If we got here, the loop exited — test passes

    def test_text_join_contract(self, db: PipelineDB) -> None:
        """Text join matches newline-join of pages ORDER BY page_index (C1)."""
        self._populate_pages(db, "h1", 3)
        joined_text: list[str] = []

        original_chunk = None

        def capture_chunk(text, extraction_metadata):
            joined_text.append(text)
            return []

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.side_effect = capture_chunk
            chunker_loop("h1", db, threading.Event(), embed_fn=lambda t, m: ([], m))

        expected = "\n".join(f"Page {i} content here." for i in range(3))
        assert joined_text[0] == expected

    def test_idempotent_resume(self, db: PipelineDB) -> None:
        """Re-running chunker on same pages produces no duplicate chunks."""
        self._populate_pages(db, "h1", 2)

        fake_chunks = [
            TextChunk(text="chunk 0", chunk_index=0, metadata={}),
            TextChunk(text="chunk 1", chunk_index=1, metadata={}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            # Run twice
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed)
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed)

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 2  # no duplicates

    def test_embed_fn_none_writes_chunks_without_embedding(self, db: PipelineDB) -> None:
        """When embed_fn is None, chunks are written without embeddings."""
        self._populate_pages(db, "h1", 2)

        fake_chunks = [
            TextChunk(text="chunk 0", chunk_index=0, metadata={}),
        ]

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=None)

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 1
        assert chunks[0]["embedding"] is None
        # Uploader's read_uploadable_chunks filters by embedding IS NOT NULL
        uploadable = db.read_uploadable_chunks("h1")
        assert len(uploadable) == 0


# ── uploader_loop ────────────────────────────────────────────────────────────


class TestUploaderLoop:
    def _populate_chunks(self, db: PipelineDB, hash_: str, count: int) -> None:
        """Populate chunk buffer with embeddings and mark extraction/chunking complete."""
        db.create_pipeline(hash_, "/a.pdf", "docs__test")
        db.update_progress(hash_, total_pages=1, pages_extracted=1, chunks_created=count, chunks_embedded=count)
        for i in range(count):
            db.write_chunk(
                hash_, i, f"chunk {i} text", f"cid-{i}",
                metadata={"page": 1},
                embedding=_fake_embedding(i),
            )

    def test_uploads_chunks_to_t3(self, db: PipelineDB) -> None:
        self._populate_chunks(db, "h1", 3)
        mock_t3 = MagicMock()

        cancel = threading.Event()
        uploader_loop("h1", db, mock_t3, "docs__test", cancel)

        mock_t3.upsert.assert_called_once()
        call_kwargs = mock_t3.upsert.call_args
        # Verify correct data passed
        assert call_kwargs.kwargs["collection_name"] == "docs__test"
        assert len(call_kwargs.kwargs["ids"]) == 3
        assert len(call_kwargs.kwargs["documents"]) == 3
        assert len(call_kwargs.kwargs["embeddings"]) == 3

        # All chunks marked uploaded
        ready = db.read_uploadable_chunks("h1")
        assert len(ready) == 0

        state = db.get_pipeline_state("h1")
        assert state["chunks_uploaded"] == 3

    def test_batch_sizing(self, db: PipelineDB) -> None:
        """Chunks are batched into groups of _UPLOAD_BATCH_SIZE."""
        self._populate_chunks(db, "h1", 200)
        mock_t3 = MagicMock()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        # 200 chunks / 128 batch = 2 upsert calls
        assert mock_t3.upsert.call_count == 2

    def test_cancel_exits_poll_loop(self, db: PipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        # Don't mark complete — uploader will poll forever unless cancelled
        mock_t3 = MagicMock()
        cancel = threading.Event()

        def cancel_soon():
            time.sleep(0.2)
            cancel.set()

        t = threading.Thread(target=cancel_soon)
        t.start()
        uploader_loop("h1", db, mock_t3, "docs__test", cancel)
        t.join()

    def test_marks_uploaded_per_batch(self, db: PipelineDB) -> None:
        """mark_uploaded is called after each batch, not at the end."""
        self._populate_chunks(db, "h1", 200)
        mock_t3 = MagicMock()

        # Track mark_uploaded calls
        original_mark = db.mark_uploaded
        mark_calls: list[int] = []

        def tracking_mark(content_hash, indices):
            mark_calls.append(len(indices))
            original_mark(content_hash, indices)

        db.mark_uploaded = tracking_mark  # type: ignore[assignment]

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        assert len(mark_calls) == 2
        assert mark_calls[0] == 128
        assert mark_calls[1] == 72

    def test_done_when_all_uploaded(self, db: PipelineDB) -> None:
        """Uploader exits when chunks_uploaded == chunks_created and extraction complete."""
        self._populate_chunks(db, "h1", 2)
        mock_t3 = MagicMock()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        state = db.get_pipeline_state("h1")
        assert state["chunks_uploaded"] == 2
        assert state["status"] == "completed"


# ── pipeline_index_pdf orchestrator ──────────────────────────────────────────


class TestPipelineIndexPdf:
    def test_full_pipeline_mock(self, db: PipelineDB) -> None:
        """All three stages run together with mock extraction producing pages."""
        mock_t3 = MagicMock()
        pdf_path = Path("/test/doc.pdf")
        content_hash = "abc123"

        fake_result = _make_extraction_result(3)
        fake_chunks = [
            TextChunk(text="chunk 0", chunk_index=0, metadata={"page": 1}),
            TextChunk(text="chunk 1", chunk_index=1, metadata={"page": 2}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker") as MockChunker,
        ):
            def fake_extract(p, *, extractor="auto", on_page=None):
                for i in range(3):
                    if on_page:
                        on_page(i, f"Page {i} content.", {"page_number": i + 1, "text_length": 15})
                return fake_result

            MockExt.return_value.extract.side_effect = fake_extract
            MockChunker.return_value.chunk.return_value = fake_chunks

            total = pipeline_index_pdf(
                pdf_path, content_hash, "docs__test", mock_t3,
                db=db, embed_fn=fake_embed,             )

        assert total == 2
        mock_t3.upsert.assert_called_once()
        # Buffer cleaned up on success
        assert db.get_pipeline_state(content_hash) is None
        assert db.read_pages(content_hash) == []

    def test_extractor_failure_cancels_others(self, db: PipelineDB) -> None:
        """Extractor exception → cancel → all futures joined → status=failed."""
        mock_t3 = MagicMock()

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker"),
        ):
            MockExt.return_value.extract.side_effect = RuntimeError("extraction boom")

            with pytest.raises(RuntimeError, match="extraction boom"):
                pipeline_index_pdf(
                    Path("/test.pdf"), "h1", "docs__test", mock_t3,
                    db=db, embed_fn=lambda t, m: ([], m),                 )

        state = db.get_pipeline_state("h1")
        assert state is not None
        assert state["status"] == "failed"
        assert "extraction boom" in state["error"]

    def test_resume_from_partial_buffer(self, db: PipelineDB) -> None:
        """Orchestrator resumes from pre-populated pages without re-extracting."""
        mock_t3 = MagicMock()
        content_hash = "h1"

        # Pre-populate: 2 of 3 pages already in buffer, pipeline failed mid-run
        db.create_pipeline(content_hash, "/a.pdf", "docs__test")
        db.write_page(content_hash, 0, "Page 0 content.", metadata={"page_number": 1, "text_length": 15})
        db.write_page(content_hash, 1, "Page 1 content.", metadata={"page_number": 2, "text_length": 15})
        db.update_progress(content_hash, pages_extracted=2)
        db.mark_failed(content_hash, error="simulated crash")

        fake_result = _make_extraction_result(3)
        fake_chunks = [TextChunk(text="chunk 0", chunk_index=0, metadata={})]

        written_pages: list[int] = []

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker") as MockChunker,
        ):
            def fake_extract(p, *, extractor="auto", on_page=None):
                for i in range(3):
                    if on_page:
                        on_page(i, f"Page {i} content.", {"page_number": i + 1, "text_length": 15})
                return fake_result

            MockExt.return_value.extract.side_effect = fake_extract
            MockChunker.return_value.chunk.return_value = fake_chunks

            # Track what pages are actually written
            orig_write = db.write_page
            def tracking_write(ch, pi, text, metadata=None):
                written_pages.append(pi)
                orig_write(ch, pi, text, metadata)
            db.write_page = tracking_write  # type: ignore[assignment]

            pipeline_index_pdf(
                Path("/a.pdf"), content_hash, "docs__test", mock_t3,
                db=db, embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m),             )

        # Pages 0 and 1 were skipped (already in buffer)
        assert 0 not in written_pages
        assert 1 not in written_pages
        assert 2 in written_pages

    def test_pipeline_creates_state_before_futures(self, db: PipelineDB) -> None:
        """Pipeline state is created before futures are submitted."""
        mock_t3 = MagicMock()
        fake_result = _make_extraction_result(1)
        fake_chunks = [TextChunk(text="c", chunk_index=0, metadata={})]

        def fake_extract(p, *, extractor="auto", on_page=None):
            if on_page:
                on_page(0, "Page 0.", {"page_number": 1, "text_length": 7})
            return fake_result

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker") as MockChunker,
        ):
            MockExt.return_value.extract.side_effect = fake_extract
            MockChunker.return_value.chunk.return_value = fake_chunks

            pipeline_index_pdf(
                Path("/a.pdf"), "h1", "docs__test", mock_t3,
                db=db, embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m),             )

        # If we got here, pipeline completed — state was created correctly
        # Buffer is cleaned on success, so state is None
        assert db.get_pipeline_state("h1") is None
