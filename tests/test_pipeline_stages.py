# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for pipeline stage functions (RDR-048)."""
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
        assert embed_calls == [] or (len(embed_calls) == 1 and embed_calls[0] == 0)

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

        for i in range(5):
            db.write_page("h1", i, f"Page {i} " + "x" * 2000,
                          metadata={"page_number": i + 1, "text_length": 2006})
        db.update_progress("h1", pages_extracted=5)

        def fake_embed(texts, model):
            return [[0.1] * 4 for _ in texts], model

        def make_chunks(text, meta):
            count = max(1, len(text) // 1500)
            return [TextChunk(text=f"chunk-{i}", chunk_index=i, metadata={}) for i in range(count)]

        def signal_done():
            time.sleep(0.8)
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


# ── pipeline_index_pdf orchestrator ──────────────────────────────────────────


class TestPipelineIndexPdf:
    def test_full_pipeline(self, db: PipelineDB) -> None:
        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = MagicMock(
            get=MagicMock(return_value={"ids": [], "metadatas": []})
        )
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
        mock_t3 = MagicMock()

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
        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = MagicMock(
            get=MagicMock(return_value={"ids": [], "metadatas": []})
        )

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
        mock_t3 = MagicMock()
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
        mock_t3 = MagicMock()
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
