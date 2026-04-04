# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pipeline stage functions (nexus-qwxz + critique fixes)."""
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
from nexus.pipeline_stages import (
    PipelineCancelled,
    chunker_loop,
    extractor_loop,
    pipeline_index_pdf,
    uploader_loop,
)


@pytest.fixture()
def db(tmp_path: Path) -> PipelineDB:
    return PipelineDB(tmp_path / "pipeline.db")


def _fake_embedding(index: int) -> bytes:
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


def _done_event() -> threading.Event:
    """Return an already-set Event (extraction complete)."""
    e = threading.Event()
    e.set()
    return e


# ── extractor_loop ───────────────────────────────────────────────────────────


class TestExtractorLoop:
    def test_writes_pages_to_buffer(self, db: PipelineDB) -> None:
        result = _make_extraction_result(3)
        db.create_pipeline("h1", "/a.pdf", "docs__test")

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            def fake_extract(pdf_path, *, extractor="auto", on_page=None):
                for i in range(3):
                    if on_page:
                        on_page(i, f"Page {i} text content.", {"page_number": i + 1, "text_length": 21})
                return result
            MockExt.return_value.extract.side_effect = fake_extract

            ret = extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())

        pages = db.read_pages("h1")
        assert len(pages) == 3
        assert pages[0]["page_text"] == "Page 0 text content."
        state = db.get_pipeline_state("h1")
        assert state["total_pages"] == 3
        assert ret is result

    def test_cancel_raises_pipeline_cancelled(self, db: PipelineDB) -> None:
        """Cancel event raises PipelineCancelled, aborting extraction early (S1 fix)."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()
        pages_delivered = []

        def fake_extract(pdf_path, *, extractor="auto", on_page=None):
            for i in range(10):
                if on_page:
                    on_page(i, f"Page {i}", {"page_number": i + 1, "text_length": 6})
                    pages_delivered.append(i)
                if i == 2:
                    cancel.set()  # cancel after page 2
            return _make_extraction_result(10)

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            MockExt.return_value.extract.side_effect = fake_extract
            result = extractor_loop(Path("/a.pdf"), "h1", db, cancel)

        # on_page raised PipelineCancelled after page 2, so extract() aborted.
        # Pages 0-2 were written before cancel; page 3's on_page raised.
        assert len(db.read_pages("h1")) <= 3
        assert result.text == ""  # cancelled result

    def test_resume_skips_existing_pages(self, db: PipelineDB) -> None:
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
            extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())

        assert 0 not in written_indices
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
        db.create_pipeline(hash_, "/a.pdf", "docs__test")
        for i in range(count):
            db.write_page(hash_, i, f"Page {i} content here.", metadata={"page_number": i + 1, "text_length": 22})
        db.update_progress(hash_, total_pages=count, pages_extracted=count)

    def test_produces_chunks_from_pages(self, db: PipelineDB) -> None:
        self._populate_pages(db, "h1", 3)

        fake_chunks = [
            TextChunk(text="chunk 0 text", chunk_index=0, metadata={"page": 1}),
            TextChunk(text="chunk 1 text", chunk_index=1, metadata={"page": 2}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed,
                         extraction_done=_done_event())

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 2
        assert chunks[0]["chunk_text"] == "chunk 0 text"
        assert chunks[0]["embedding"] is not None

        state = db.get_pipeline_state("h1")
        assert state["chunks_created"] == 2
        assert state["chunks_embedded"] == 2

    def test_cancel_exits(self, db: PipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()

        def cancel_soon():
            time.sleep(0.2)
            cancel.set()

        t = threading.Thread(target=cancel_soon)
        t.start()
        with patch("nexus.pipeline_stages.PDFChunker"):
            chunker_loop("h1", db, cancel, embed_fn=lambda t, m: ([], m))
        t.join()

    def test_text_join_contract(self, db: PipelineDB) -> None:
        self._populate_pages(db, "h1", 3)
        joined_text: list[str] = []

        def capture_chunk(text, extraction_metadata):
            joined_text.append(text)
            return []

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.side_effect = capture_chunk
            chunker_loop("h1", db, threading.Event(), embed_fn=lambda t, m: ([], m),
                         extraction_done=_done_event())

        expected = "\n".join(f"Page {i} content here." for i in range(3))
        assert joined_text[0] == expected

    def test_idempotent_resume(self, db: PipelineDB) -> None:
        self._populate_pages(db, "h1", 2)

        fake_chunks = [
            TextChunk(text="chunk 0", chunk_index=0, metadata={}),
            TextChunk(text="chunk 1", chunk_index=1, metadata={}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed,
                         extraction_done=_done_event())
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed,
                         extraction_done=_done_event())

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 2

    def test_embed_fn_none(self, db: PipelineDB) -> None:
        self._populate_pages(db, "h1", 2)

        fake_chunks = [TextChunk(text="chunk 0", chunk_index=0, metadata={})]

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=None,
                         extraction_done=_done_event())

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 1
        assert chunks[0]["embedding"] is None
        assert db.read_uploadable_chunks("h1") == []

    def test_incremental_chunking_before_extraction_done(self, db: PipelineDB) -> None:
        """Chunker produces stable chunks before extraction finishes (C2 fix)."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        extraction_done = threading.Event()
        chunking_done = threading.Event()
        chunks_written: list[int] = []

        # Pre-populate 5 pages (extraction "in progress").
        for i in range(5):
            db.write_page("h1", i, f"Page {i} " + "x" * 2000,
                          metadata={"page_number": i + 1, "text_length": 2006})
        db.update_progress("h1", pages_extracted=5)

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        # Return enough chunks that there's a stable prefix.
        def make_chunks(text, meta):
            count = max(1, len(text) // 1500)
            return [TextChunk(text=f"chunk-{i}", chunk_index=i, metadata={}) for i in range(count)]

        def signal_done_after_delay():
            time.sleep(0.8)
            # Add 2 more pages, then signal done.
            for i in range(5, 7):
                db.write_page("h1", i, f"Page {i} " + "x" * 2000,
                              metadata={"page_number": i + 1, "text_length": 2006})
            db.update_progress("h1", total_pages=7, pages_extracted=7)
            extraction_done.set()

        t = threading.Thread(target=signal_done_after_delay)
        t.start()

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.side_effect = make_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed,
                         extraction_done=extraction_done, chunking_done=chunking_done)

        t.join()
        assert chunking_done.is_set()
        chunks = db.read_ready_chunks("h1")
        assert len(chunks) > 0
        state = db.get_pipeline_state("h1")
        assert state["chunks_created"] is not None


# ── uploader_loop ────────────────────────────────────────────────────────────


class TestUploaderLoop:
    def _populate_chunks(self, db: PipelineDB, hash_: str, count: int) -> None:
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

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        mock_t3.upsert.assert_called_once()
        kw = mock_t3.upsert.call_args.kwargs
        assert kw["collection_name"] == "docs__test"
        assert len(kw["ids"]) == 3

        assert db.read_uploadable_chunks("h1") == []
        assert db.get_pipeline_state("h1")["chunks_uploaded"] == 3

    def test_batch_sizing(self, db: PipelineDB) -> None:
        self._populate_chunks(db, "h1", 200)
        mock_t3 = MagicMock()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        assert mock_t3.upsert.call_count == 2

    def test_cancel_exits(self, db: PipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
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
        self._populate_chunks(db, "h1", 200)
        mock_t3 = MagicMock()

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
        self._populate_chunks(db, "h1", 2)
        mock_t3 = MagicMock()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        state = db.get_pipeline_state("h1")
        assert state["chunks_uploaded"] == 2
        assert state["status"] == "completed"


# ── pipeline_index_pdf orchestrator ──────────────────────────────────────────


class TestPipelineIndexPdf:
    def test_full_pipeline_mock(self, db: PipelineDB) -> None:
        mock_t3 = MagicMock()
        mock_t3.get.return_value = {"ids": [], "metadatas": []}
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
                Path("/test/doc.pdf"), "abc123", "docs__test", mock_t3,
                db=db, embed_fn=fake_embed,
            )

        assert total == 2
        mock_t3.upsert.assert_called_once()
        assert db.get_pipeline_state("abc123") is None

    def test_extractor_failure_cancels_others(self, db: PipelineDB) -> None:
        mock_t3 = MagicMock()

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker"),
        ):
            MockExt.return_value.extract.side_effect = RuntimeError("extraction boom")

            with pytest.raises(RuntimeError, match="extraction boom"):
                pipeline_index_pdf(
                    Path("/test.pdf"), "h1", "docs__test", mock_t3,
                    db=db, embed_fn=lambda t, m: ([], m),
                )

        state = db.get_pipeline_state("h1")
        assert state is not None
        assert state["status"] == "failed"
        assert "extraction boom" in state["error"]

    def test_resume_from_partial_buffer(self, db: PipelineDB) -> None:
        mock_t3 = MagicMock()
        mock_t3.get.return_value = {"ids": [], "metadatas": []}

        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "Page 0 content.", metadata={"page_number": 1, "text_length": 15})
        db.write_page("h1", 1, "Page 1 content.", metadata={"page_number": 2, "text_length": 15})
        db.update_progress("h1", pages_extracted=2)
        db.mark_failed("h1", error="simulated crash")

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

            orig_write = db.write_page
            def tracking_write(ch, pi, text, metadata=None):
                written_pages.append(pi)
                orig_write(ch, pi, text, metadata)
            db.write_page = tracking_write  # type: ignore[assignment]

            pipeline_index_pdf(
                Path("/a.pdf"), "h1", "docs__test", mock_t3,
                db=db, embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m),
            )

        assert 0 not in written_pages
        assert 1 not in written_pages
        assert 2 in written_pages

    def test_table_regions_postpass(self, db: PipelineDB) -> None:
        """table_regions from extraction result triggers metadata update (S3 fix)."""
        mock_t3 = MagicMock()
        mock_t3.get.return_value = {
            "ids": ["abc123_0", "abc123_1"],
            "metadatas": [
                {"page_number": 1, "chunk_type": "text"},
                {"page_number": 2, "chunk_type": "text"},
            ],
        }

        fake_result = _make_extraction_result(3)  # has table_regions: [{"page": 2}]
        fake_chunks = [
            TextChunk(text="chunk 0", chunk_index=0, metadata={"page": 1}),
            TextChunk(text="chunk 1", chunk_index=1, metadata={"page": 2}),
        ]

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker") as MockChunker,
        ):
            def fake_extract(p, *, extractor="auto", on_page=None):
                for i in range(3):
                    if on_page:
                        on_page(i, f"Page {i}.", {"page_number": i + 1, "text_length": 7})
                return fake_result

            MockExt.return_value.extract.side_effect = fake_extract
            MockChunker.return_value.chunk.return_value = fake_chunks

            pipeline_index_pdf(
                Path("/a.pdf"), "abc123", "docs__test", mock_t3,
                db=db, embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m),
            )

        # Verify t3.update was called for the table page chunk.
        mock_t3.update.assert_called_once()
        update_args = mock_t3.update.call_args.kwargs
        assert update_args["ids"] == ["abc123_1"]
        assert update_args["metadatas"][0]["chunk_type"] == "table_page"
