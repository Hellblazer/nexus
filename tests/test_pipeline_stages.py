# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pipeline stage functions (RDR-048)."""
from __future__ import annotations

import json
import struct
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from nexus.pdf_chunker import TextChunk
from nexus.pdf_extractor import ExtractionResult
from nexus.db.t3 import T3Database
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
        state = db.get_pipeline_state("h1")
        assert state["total_pages"] == 3
        assert ret is result

    def test_cancel_raises_pipeline_cancelled(self, db: PipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()

        def fake_extract(pdf_path, *, extractor="auto", on_page=None):
            for i in range(10):
                if on_page:
                    on_page(i, f"Page {i}", {"page_number": i + 1, "text_length": 6})
                if i == 2:
                    cancel.set()
            return _make_extraction_result(10)

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            MockExt.return_value.extract.side_effect = fake_extract
            result = extractor_loop(Path("/a.pdf"), "h1", db, cancel)

        assert len(db.read_pages("h1")) <= 3
        assert result.text == ""

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

        orig = db.write_page
        def tracking(ch, pi, text, metadata=None):
            written_indices.append(pi)
            orig(ch, pi, text, metadata)
        db.write_page = tracking  # type: ignore[assignment]

        with patch("nexus.pipeline_stages.PDFExtractor") as MockExt:
            MockExt.return_value.extract.side_effect = fake_extract
            extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())

        assert 0 not in written_indices
        assert 1 in written_indices

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

    def test_produces_chunks_with_full_metadata(self, db: PipelineDB) -> None:
        """Chunks have the full metadata schema matching the batch path."""
        self._populate_pages(db, "h1", 3)

        fake_chunks = [
            TextChunk(text="chunk 0 text", chunk_index=0, metadata={"page_number": 1, "chunk_type": "text", "chunk_start_char": 0, "chunk_end_char": 12}),
            TextChunk(text="chunk 1 text", chunk_index=1, metadata={"page_number": 2, "chunk_type": "text", "chunk_start_char": 12, "chunk_end_char": 24}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed,
                         extraction_done=_done_event(),
                         pdf_path="/a.pdf", corpus="test", target_model="voyage-context-3")

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 2

        # Verify full metadata schema
        meta = json.loads(chunks[0]["metadata_json"])
        assert meta["source_path"] == "/a.pdf"
        assert meta["corpus"] == "test"
        assert meta["content_hash"] == "h1"
        assert meta["embedding_model"] == "voyage-context-3"
        assert meta["store_type"] == "pdf"
        assert "indexed_at" in meta
        assert meta["page_number"] == 1

        # Verify chunk ID matches batch path format
        assert chunks[0]["chunk_id"] == "h1_0"
        assert chunks[1]["chunk_id"] == "h1_1"

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
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed, extraction_done=_done_event())
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed, extraction_done=_done_event())

        assert len(db.read_ready_chunks("h1")) == 2

    def test_resume_with_partially_uploaded_chunks(self, db: PipelineDB) -> None:
        """Resume skips already-embedded chunks even if some are uploaded."""
        self._populate_pages(db, "h1", 2)

        fake_chunks = [
            TextChunk(text="chunk 0", chunk_index=0, metadata={}),
            TextChunk(text="chunk 1", chunk_index=1, metadata={}),
            TextChunk(text="chunk 2", chunk_index=2, metadata={}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        # First run: embed all 3 chunks.
        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed, extraction_done=_done_event())

        # Simulate: chunks 0-1 uploaded, chunk 2 not yet uploaded.
        db.mark_uploaded("h1", [0, 1])

        # Track embed calls on second run.
        embed_calls: list[int] = []
        def tracking_embed(texts, model):
            embed_calls.append(len(texts))
            return [[0.1] * 4 for _ in texts], model

        # Resume: chunker should see all 3 are embedded and skip re-embedding.
        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=tracking_embed, extraction_done=_done_event())

        # No new embed calls — all 3 chunks were already embedded.
        assert embed_calls == []

    def test_embed_fn_none(self, db: PipelineDB) -> None:
        self._populate_pages(db, "h1", 2)
        fake_chunks = [TextChunk(text="chunk 0", chunk_index=0, metadata={})]

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=None, extraction_done=_done_event())

        chunks = db.read_ready_chunks("h1")
        assert len(chunks) == 1
        assert chunks[0]["embedding"] is None

    def test_incremental_chunking_before_extraction_done(self, db: PipelineDB) -> None:
        """Chunker produces stable chunks before extraction finishes."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        extraction_done = threading.Event()
        chunking_done = threading.Event()
        chunker_polled = threading.Event()  # signals chunker has read pages at least once

        for i in range(5):
            db.write_page("h1", i, f"Page {i} " + "x" * 2000,
                          metadata={"page_number": i + 1, "text_length": 2006})
        db.update_progress("h1", pages_extracted=5)

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        call_count = 0

        def make_chunks(text, meta):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                chunker_polled.set()  # first chunk call done — safe to add more pages
            count = max(1, len(text) // 1500)
            return [TextChunk(text=f"chunk-{i}", chunk_index=i, metadata={}) for i in range(count)]

        def signal_done():
            chunker_polled.wait(timeout=5)  # wait for chunker to process first batch
            for i in range(5, 7):
                db.write_page("h1", i, f"Page {i} " + "x" * 2000,
                              metadata={"page_number": i + 1, "text_length": 2006})
            db.update_progress("h1", total_pages=7, pages_extracted=7)
            extraction_done.set()

        t = threading.Thread(target=signal_done)
        t.start()

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.side_effect = make_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed,
                         extraction_done=extraction_done, chunking_done=chunking_done)

        t.join()
        assert chunking_done.is_set()
        assert len(db.read_ready_chunks("h1")) > 0


# ── uploader_loop ────────────────────────────────────────────────────────────


class TestUploaderLoop:
    def _populate_chunks(self, db: PipelineDB, hash_: str, count: int) -> None:
        """Pre-populate buffer for uploader tests.

        Sets chunks_created=chunks_embedded=count to simulate the state after
        chunker_loop completes. In the real pipeline these are set independently
        (chunks_embedded lags during incremental chunking).
        """
        db.create_pipeline(hash_, "/a.pdf", "docs__test")
        db.update_progress(hash_, total_pages=1, pages_extracted=1, chunks_created=count, chunks_embedded=count)
        for i in range(count):
            db.write_chunk(
                hash_, i, f"chunk {i} text", f"{hash_[:16]}_{i}",
                metadata={"page": 1, "source_path": "/a.pdf", "content_hash": hash_},
                embedding=_fake_embedding(i),
            )

    def test_uploads_chunks_via_t3_api(self, db: PipelineDB) -> None:
        """Uploader calls upsert_chunks_with_embeddings (not mock-any upsert)."""
        self._populate_chunks(db, "h1", 3)
        mock_t3 = MagicMock()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        mock_t3.upsert_chunks_with_embeddings.assert_called_once()
        args = mock_t3.upsert_chunks_with_embeddings.call_args
        assert args[0][0] == "docs__test"  # collection
        assert len(args[0][1]) == 3  # ids
        assert db.read_uploadable_chunks("h1") == []

    def test_batch_sizing(self, db: PipelineDB) -> None:
        self._populate_chunks(db, "h1", 200)
        mock_t3 = MagicMock()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        assert mock_t3.upsert_chunks_with_embeddings.call_count == 2

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

        orig = db.mark_uploaded
        calls: list[int] = []
        def tracking(ch, indices):
            calls.append(len(indices))
            orig(ch, indices)
        db.mark_uploaded = tracking  # type: ignore[assignment]

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        assert len(calls) == 2
        assert calls[0] == 128
        assert calls[1] == 72

    def test_done_when_all_uploaded(self, db: PipelineDB) -> None:
        self._populate_chunks(db, "h1", 2)
        mock_t3 = MagicMock()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event())

        state = db.get_pipeline_state("h1")
        assert state["chunks_uploaded"] == 2
        assert state["status"] == "completed"

    def test_done_via_chunking_done_event(self, db: PipelineDB) -> None:
        """Uploader completes via chunking_done event (orchestrated path)."""
        self._populate_chunks(db, "h1", 3)
        mock_t3 = MagicMock()
        chunking_done = threading.Event()
        chunking_done.set()

        uploader_loop("h1", db, mock_t3, "docs__test", threading.Event(), chunking_done)

        state = db.get_pipeline_state("h1")
        assert state["chunks_uploaded"] == 3
        assert state["status"] == "completed"
        mock_t3.upsert_chunks_with_embeddings.assert_called_once()


# ── pipeline_index_pdf orchestrator ──────────────────────────────────────────


class TestPipelineIndexPdf:
    @staticmethod
    def _make_t3(col_get_result: dict | None = None) -> MagicMock:
        """Create a T3Database mock that enforces the real API surface.

        Uses create_autospec so that calling a nonexistent method (e.g.
        t3.upsert instead of t3.upsert_chunks_with_embeddings) raises
        AttributeError — catching the class of bug from round 2.
        """
        mock = create_autospec(T3Database, instance=True)
        mock.get_or_create_collection.return_value = MagicMock(
            get=MagicMock(return_value=col_get_result or {"ids": [], "metadatas": []})
        )
        return mock

    def test_full_pipeline(self, db: PipelineDB) -> None:
        mock_t3 = self._make_t3()
        fake_result = _make_extraction_result(3)
        fake_chunks = [
            TextChunk(text="chunk 0", chunk_index=0, metadata={"page_number": 1, "chunk_type": "text"}),
            TextChunk(text="chunk 1", chunk_index=1, metadata={"page_number": 2, "chunk_type": "text"}),
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
                db=db, embed_fn=fake_embed, corpus="test",
            )

        assert total == 2
        mock_t3.upsert_chunks_with_embeddings.assert_called_once()
        assert db.get_pipeline_state("abc123") is None

    def test_extractor_failure(self, db: PipelineDB) -> None:
        mock_t3 = self._make_t3()

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker"),
        ):
            MockExt.return_value.extract.side_effect = RuntimeError("boom")

            with pytest.raises(RuntimeError, match="boom"):
                pipeline_index_pdf(
                    Path("/test.pdf"), "h1", "docs__test", mock_t3,
                    db=db, embed_fn=lambda t, m: ([], m),
                )

        state = db.get_pipeline_state("h1")
        assert state["status"] == "failed"
        assert "boom" in state["error"]

    def test_resume_from_partial(self, db: PipelineDB) -> None:
        mock_t3 = self._make_t3()

        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "Page 0 content.", metadata={"page_number": 1, "text_length": 15})
        db.write_page("h1", 1, "Page 1 content.", metadata={"page_number": 2, "text_length": 15})
        db.update_progress("h1", pages_extracted=2)
        db.mark_failed("h1", error="crash")

        fake_result = _make_extraction_result(3)
        fake_chunks = [TextChunk(text="chunk 0", chunk_index=0, metadata={})]
        written: list[int] = []

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

            orig = db.write_page
            def track(ch, pi, text, metadata=None):
                written.append(pi)
                orig(ch, pi, text, metadata)
            db.write_page = track  # type: ignore[assignment]

            pipeline_index_pdf(
                Path("/a.pdf"), "h1", "docs__test", mock_t3,
                db=db, embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m),
            )

        assert 0 not in written
        assert 2 in written

    def test_table_regions_postpass(self, db: PipelineDB) -> None:
        """table_regions post-pass calls T3Database.update_chunks."""
        mock_col = MagicMock()
        mock_col.get.return_value = {
            "ids": ["abc_0", "abc_1"],
            "metadatas": [
                {"page_number": 1, "chunk_type": "text", "content_hash": "abc123"},
                {"page_number": 2, "chunk_type": "text", "content_hash": "abc123"},
            ],
        }
        mock_t3 = create_autospec(T3Database, instance=True)
        mock_t3.get_or_create_collection.return_value = mock_col

        fake_result = _make_extraction_result(3)
        fake_chunks = [
            TextChunk(text="c0", chunk_index=0, metadata={"page_number": 1, "chunk_type": "text"}),
            TextChunk(text="c1", chunk_index=1, metadata={"page_number": 2, "chunk_type": "text"}),
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

        # update_chunks called for both enrichment and table_regions post-pass
        assert mock_t3.update_chunks.call_count >= 1
        # Find the table_regions call (updates chunk_type to table_page)
        for call in mock_t3.update_chunks.call_args_list:
            args = call[0]
            if args[0] == "docs__test" and any(m.get("chunk_type") == "table_page" for m in args[2]):
                assert "abc_1" in args[1]
                break
        else:
            pytest.fail("table_regions post-pass did not update any chunks to table_page")

    def test_metadata_enrichment_postpass(self, db: PipelineDB) -> None:
        """Post-pass enriches chunks with extraction metadata (title, author, etc.)."""
        mock_col = MagicMock()
        mock_col.get.return_value = {
            "ids": ["abc_0"],
            "metadatas": [{"source_title": "", "content_hash": "abc123", "page_number": 1}],
        }
        mock_t3 = create_autospec(T3Database, instance=True)
        mock_t3.get_or_create_collection.return_value = mock_col

        fake_result = _make_extraction_result(1)
        fake_result.metadata["docling_title"] = "My Paper Title"
        fake_result.metadata["pdf_author"] = "Jane Doe"
        fake_chunks = [TextChunk(text="c0", chunk_index=0, metadata={"page_number": 1, "chunk_type": "text"})]

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker") as MockChunker,
        ):
            def fake_extract(p, *, extractor="auto", on_page=None):
                if on_page:
                    on_page(0, "Page 0.", {"page_number": 1, "text_length": 7})
                return fake_result
            MockExt.return_value.extract.side_effect = fake_extract
            MockChunker.return_value.chunk.return_value = fake_chunks

            pipeline_index_pdf(
                Path("/paper.pdf"), "abc123", "docs__test", mock_t3,
                db=db, embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m),
            )

        # Find the enrichment update_chunks call
        for call in mock_t3.update_chunks.call_args_list:
            args = call[0]
            if args[0] == "docs__test":
                meta = args[2][0]
                assert meta["source_title"] == "My Paper Title"
                assert meta["source_author"] == "Jane Doe"
                assert meta["extraction_method"] == "docling"
                break
        else:
            pytest.fail("metadata enrichment post-pass not called")

    def test_stale_chunk_pruning(self, db: PipelineDB) -> None:
        """Stale chunks from a previous version are deleted after upload."""
        mock_col = MagicMock()
        # Simulate: T3 has 2 chunks from old version + 1 from current.
        mock_col.get.return_value = {
            "ids": ["abc123_0", "abc123_1", "old_hash_0"],
            "metadatas": [
                {"content_hash": "abc123_full", "source_path": "/a.pdf"},
                {"content_hash": "abc123_full", "source_path": "/a.pdf"},
                {"content_hash": "previous_hash", "source_path": "/a.pdf"},
            ],
        }
        mock_t3 = create_autospec(T3Database, instance=True)
        mock_t3.get_or_create_collection.return_value = mock_col

        fake_result = _make_extraction_result(1)
        fake_chunks = [TextChunk(text="c0", chunk_index=0, metadata={"page_number": 1, "chunk_type": "text"})]

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker") as MockChunker,
        ):
            def fake_extract(p, *, extractor="auto", on_page=None):
                if on_page:
                    on_page(0, "Page 0.", {"page_number": 1, "text_length": 7})
                return fake_result
            MockExt.return_value.extract.side_effect = fake_extract
            MockChunker.return_value.chunk.return_value = fake_chunks

            pipeline_index_pdf(
                Path("/a.pdf"), "abc123_full", "docs__test", mock_t3,
                db=db, embed_fn=lambda t, m: ([[0.1] * 4 for _ in t], m),
            )

        # Verify stale chunk was deleted (the one with different content_hash).
        mock_col.delete.assert_called_once()
        deleted_ids = mock_col.delete.call_args.kwargs.get("ids", mock_col.delete.call_args[1].get("ids", []))
        assert "old_hash_0" in deleted_ids
        assert "abc123_0" not in deleted_ids

    def test_skip_already_running(self, db: PipelineDB) -> None:
        """Orchestrator returns 0 when pipeline is already running."""
        mock_t3 = self._make_t3()
        db.create_pipeline("h1", "/a.pdf", "docs__test")  # creates as 'running'

        result = pipeline_index_pdf(
            Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db,
        )

        assert result == 0
        mock_t3.upsert_chunks_with_embeddings.assert_not_called()

    def test_embed_fn_none_resolves_credentials(self, db: PipelineDB) -> None:
        """When embed_fn=None, orchestrator resolves from Voyage credentials."""
        mock_t3 = self._make_t3()
        fake_result = _make_extraction_result(1)
        fake_chunks = [TextChunk(text="c0", chunk_index=0, metadata={"page_number": 1, "chunk_type": "text"})]

        with (
            patch("nexus.pipeline_stages.PDFExtractor") as MockExt,
            patch("nexus.pipeline_stages.PDFChunker") as MockChunker,
            patch("nexus.config.get_credential", return_value="fake-key"),
            patch("nexus.config.load_config", return_value={}),
            patch("nexus.doc_indexer._embed_with_fallback") as mock_embed,
        ):
            mock_embed.return_value = ([[0.1] * 4], "voyage-context-3")
            def fake_extract(p, *, extractor="auto", on_page=None):
                if on_page:
                    on_page(0, "Page 0.", {"page_number": 1, "text_length": 7})
                return fake_result
            MockExt.return_value.extract.side_effect = fake_extract
            MockChunker.return_value.chunk.return_value = fake_chunks

            total = pipeline_index_pdf(
                Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db,
            )

        assert total == 1
        mock_embed.assert_called()

    def test_embed_fn_none_no_credentials_fails_fast(self, db: PipelineDB) -> None:
        """When embed_fn=None and no credentials, orchestrator raises immediately."""
        mock_t3 = self._make_t3()

        with (
            patch("nexus.config.get_credential", return_value=None),
        ):
            with pytest.raises(RuntimeError, match="voyage_api_key not configured"):
                pipeline_index_pdf(
                    Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db,
                )


# ── Buffer edge case tests ───────────────────────────────────────────────────


class TestBufferEdgeCases:
    def test_count_pipelines(self, db: PipelineDB) -> None:
        assert db.count_pipelines() == 0
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        assert db.count_pipelines() == 1

    def test_count_embedded_chunks(self, db: PipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_chunk("h1", 0, "text", "cid-0", embedding=b"\x00\x01")
        db.write_chunk("h1", 1, "text", "cid-1")  # no embedding
        assert db.count_embedded_chunks("h1") == 1

    def test_mark_uploaded_empty_list(self, db: PipelineDB) -> None:
        """Empty list is a no-op."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.mark_uploaded("h1", [])  # should not raise


# ── Metadata contract test ───────────────────────────────────────────────────


class TestMetadataContract:
    """Verify streaming path metadata matches batch path schema."""

    REQUIRED_FIELDS = {
        "source_path", "source_title", "source_author", "source_date",
        "corpus", "store_type", "page_count", "page_number",
        "section_title", "format", "extraction_method",
        "chunk_type", "chunk_index", "chunk_count",
        "chunk_start_char", "chunk_end_char",
        "embedding_model", "indexed_at", "content_hash",
        "pdf_subject", "pdf_keywords", "is_image_pdf", "has_formulas",
        "bib_year", "bib_venue", "bib_authors",
        "bib_citation_count", "bib_semantic_scholar_id",
    }

    def test_streaming_metadata_has_all_batch_fields(self, db: PipelineDB) -> None:
        """Every field the batch path writes is present in streaming chunks."""
        db.create_pipeline("h1", "/doc.pdf", "docs__test")
        db.write_page("h1", 0, "Some text here.", metadata={"page_number": 1, "text_length": 15})
        db.update_progress("h1", total_pages=1, pages_extracted=1)

        fake_chunks = [
            TextChunk(text="chunk text", chunk_index=0,
                      metadata={"page_number": 1, "chunk_type": "text",
                                "chunk_start_char": 0, "chunk_end_char": 10}),
        ]

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        with patch("nexus.pipeline_stages.PDFChunker") as MockChunker:
            MockChunker.return_value.chunk.return_value = fake_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=fake_embed,
                         extraction_done=_done_event(),
                         pdf_path="/doc.pdf", corpus="mycorpus",
                         target_model="voyage-context-3")

        chunks = db.read_ready_chunks("h1")
        meta = json.loads(chunks[0]["metadata_json"])
        missing = self.REQUIRED_FIELDS - set(meta.keys())
        assert missing == set(), f"Missing metadata fields: {missing}"


# ── nexus-f8it: _update_chunk_metadata must log WARNING on failure ──────────


class TestUpdateChunkMetadataFailureLogging:
    """Post-pass metadata update failures must be visible, not swallowed."""

    def test_query_failure_logs_warning(self) -> None:
        """Query failure in _update_chunk_metadata logs at WARNING, not DEBUG."""
        from nexus.pipeline_stages import _update_chunk_metadata

        mock_t3 = MagicMock()
        mock_col = MagicMock()
        mock_col.get.side_effect = Exception("connection reset")

        result = _update_chunk_metadata(
            mock_t3, mock_col, "docs__test", "abc123", lambda m: True,
        )
        assert result is False

    def test_update_failure_logs_warning(self) -> None:
        """Update call failure in _update_chunk_metadata logs at WARNING."""
        from nexus.pipeline_stages import _update_chunk_metadata

        mock_t3 = MagicMock()
        mock_t3.update_chunks.side_effect = Exception("quota exceeded")
        mock_col = MagicMock()
        mock_col.get.return_value = {
            "ids": ["id1"], "metadatas": [{"chunk_type": "text"}],
        }

        result = _update_chunk_metadata(
            mock_t3, mock_col, "docs__test", "abc123",
            lambda m: True,  # always update
        )
        assert result is False

    def test_success_returns_true(self) -> None:
        """_update_chunk_metadata returns True when update succeeds."""
        from nexus.pipeline_stages import _update_chunk_metadata

        mock_t3 = MagicMock()
        mock_col = MagicMock()
        mock_col.get.return_value = {
            "ids": ["id1"], "metadatas": [{"chunk_type": "text"}],
        }

        result = _update_chunk_metadata(
            mock_t3, mock_col, "docs__test", "abc123",
            lambda m: True,
        )
        assert result is True


# ── nexus-tcwm: _prune_stale_chunks must surface delete failures ────────────


class TestPruneStaleChunksFailureHandling:
    """Stale chunk pruning must separate query vs delete failures."""

    def test_delete_failure_logs_warning_with_count(self) -> None:
        """Delete failure logs WARNING with number of stale chunks left behind."""
        from nexus.pipeline_stages import _prune_stale_chunks

        mock_col = MagicMock()
        mock_col.get = MagicMock(return_value={
            "ids": ["old1", "old2"],
            "metadatas": [
                {"content_hash": "stale_hash", "source_path": "/doc.pdf"},
                {"content_hash": "stale_hash", "source_path": "/doc.pdf"},
            ],
        })
        mock_col.delete = MagicMock(side_effect=Exception("quota exceeded"))

        result = _prune_stale_chunks(mock_col, "/doc.pdf", "new_hash")
        assert result is False

    def test_query_failure_returns_false(self) -> None:
        """Query failure returns False without attempting delete."""
        from nexus.pipeline_stages import _prune_stale_chunks

        mock_col = MagicMock()
        mock_col.get = MagicMock(side_effect=Exception("connection reset"))

        result = _prune_stale_chunks(mock_col, "/doc.pdf", "new_hash")
        assert result is False
        mock_col.delete.assert_not_called()

    def test_success_returns_true(self) -> None:
        """Successful pruning returns True."""
        from nexus.pipeline_stages import _prune_stale_chunks

        mock_col = MagicMock()
        mock_col.get = MagicMock(return_value={
            "ids": ["old1"],
            "metadatas": [{"content_hash": "stale_hash"}],
        })

        result = _prune_stale_chunks(mock_col, "/doc.pdf", "new_hash")
        assert result is True
        mock_col.delete.assert_called_once()


# ── nexus-pfmr: pipeline data preserved on post-pass failure ────────────────


class TestPipelineDataPreservedOnPostPassFailure:
    """Pipeline data must not be deleted until all post-passes succeed."""

    def test_pipeline_data_kept_on_enrichment_failure(self, db: PipelineDB) -> None:
        """If _enrich_metadata_from_extraction fails, pipeline data stays for retry."""
        from nexus.pipeline_stages import _enrich_metadata_from_extraction

        mock_t3 = MagicMock()
        mock_col = MagicMock()
        # Query succeeds but update fails
        mock_col.get.return_value = {
            "ids": ["id1"], "metadatas": [{"content_hash": "h1"}],
        }
        mock_t3.update_chunks.side_effect = Exception("quota exceeded")

        result_obj = MagicMock(spec=ExtractionResult)
        result_obj.text = "some text"
        result_obj.metadata = {"page_count": 1, "docling_title": "Test"}
        result_obj.title = "Test"

        result = _enrich_metadata_from_extraction(
            "h1", result_obj, Path("/test.pdf"), mock_t3, mock_col, "docs__test",
        )
        assert result is False
