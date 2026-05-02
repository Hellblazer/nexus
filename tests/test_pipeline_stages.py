# SPDX-License-Identifier: AGPL-3.0-or-later
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
    _enrich_metadata_from_extraction,
    _prune_stale_chunks,
    _update_chunk_metadata,
    chunker_loop,
    extractor_loop,
    pipeline_index_pdf,
    uploader_loop,
)

_P_EXT = "nexus.pipeline_stages.PDFExtractor"
_P_CHK = "nexus.pipeline_stages.PDFChunker"


def _fake_embedding(idx: int) -> bytes:
    return struct.pack("4f", float(idx), 0.1, 0.2, 0.3)


def _embed(texts, model):
    return [[0.1] * 4 for _ in texts], model


def _er(page_count: int = 3) -> ExtractionResult:
    pages = [f"Page {i} text content." for i in range(page_count)]
    pos, bounds = 0, []
    for i, p in enumerate(pages):
        bounds.append({"page_number": i + 1, "start_char": pos, "page_text_length": len(p) + 1})
        pos += len(p) + 1
    return ExtractionResult(
        text="\n".join(pages),
        metadata={"extraction_method": "docling", "page_count": page_count,
                  "page_boundaries": bounds, "table_regions": [{"page": 2, "html": "<table/>"}],
                  "format": "markdown"},
    )


def _fx(n: int = 3, result: ExtractionResult | None = None, text_fn=None):
    r = result or _er(n)
    def extract(pdf_path, *, extractor="auto", on_page=None):
        for i in range(n):
            txt = text_fn(i) if text_fn else f"Page {i} content."
            if on_page:
                on_page(i, txt, {"page_number": i + 1, "text_length": len(txt)})
        return r
    return extract


def _tc(*specs: tuple[str, int, dict]) -> list[TextChunk]:
    return [TextChunk(text=t, chunk_index=ci, metadata=m) for t, ci, m in specs]


@pytest.fixture()
def db(tmp_path: Path) -> PipelineDB:
    return PipelineDB(tmp_path / "pipeline.db")


@pytest.fixture()
def done_event() -> threading.Event:
    e = threading.Event()
    e.set()
    return e


@pytest.fixture()
def mock_t3() -> MagicMock:
    m = create_autospec(T3Database, instance=True)
    m.get_or_create_collection.return_value = MagicMock(
        get=MagicMock(return_value={"ids": [], "metadatas": []}))
    return m


def _pop_pages(db: PipelineDB, h: str, n: int) -> None:
    db.create_pipeline(h, "/a.pdf", "docs__test")
    for i in range(n):
        db.write_page(h, i, f"Page {i} content here.",
                      metadata={"page_number": i + 1, "text_length": 22})
    db.update_progress(h, total_pages=n, pages_extracted=n)


def _pop_chunks(db: PipelineDB, h: str, n: int) -> None:
    db.create_pipeline(h, "/a.pdf", "docs__test")
    db.update_progress(h, total_pages=1, pages_extracted=1, chunks_created=n, chunks_embedded=n)
    for i in range(n):
        db.write_chunk(h, i, f"chunk {i} text", f"{h[:16]}_{i}",
                       metadata={"page": 1, "source_path": "/a.pdf", "content_hash": h},
                       embedding=_fake_embedding(i))


def _run_with_col(db, col_get_return, fake_result, fake_chunks,
                  pdf_path="/a.pdf", content_hash="abc123", collection="docs__test"):
    mock_col = MagicMock()
    mock_col.get.return_value = col_get_return
    t3 = create_autospec(T3Database, instance=True)
    t3.get_or_create_collection.return_value = mock_col
    pc = fake_result.metadata["page_count"]
    with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
        ME.return_value.extract.side_effect = _fx(pc, fake_result)
        MC.return_value.chunk.return_value = fake_chunks
        pipeline_index_pdf(Path(pdf_path), content_hash, collection, t3,
                           db=db, embed_fn=_embed)
    return t3, mock_col



class TestExtractorLoop:
    def test_writes_pages_to_buffer(self, db: PipelineDB) -> None:
        result = _er(3)
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with patch(_P_EXT) as ME:
            def f(pdf_path, *, extractor="auto", on_page=None):
                for i in range(3):
                    if on_page:
                        on_page(i, f"Page {i} text content.",
                                {"page_number": i + 1, "text_length": 21})
                return result
            ME.return_value.extract.side_effect = f
            ret = extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())
        assert len(db.read_pages("h1")) == 3
        assert db.get_pipeline_state("h1")["total_pages"] == 3
        assert ret is result

    def test_cancel_raises_pipeline_cancelled(self, db: PipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()
        def f(pdf_path, *, extractor="auto", on_page=None):
            for i in range(10):
                if on_page:
                    on_page(i, f"Page {i}", {"page_number": i + 1, "text_length": 6})
                if i == 2:
                    cancel.set()
            return _er(10)
        with patch(_P_EXT) as ME:
            ME.return_value.extract.side_effect = f
            result = extractor_loop(Path("/a.pdf"), "h1", db, cancel)
        assert len(db.read_pages("h1")) <= 3
        assert result.text == ""

    def test_resume_skips_existing_pages(self, db: PipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "Original page 0")
        db.update_progress("h1", pages_extracted=1)
        written: list[int] = []
        orig = db.write_page
        def track(ch, pi, text, metadata=None):
            written.append(pi)
            orig(ch, pi, text, metadata)
        db.write_page = track  # type: ignore[assignment]
        with patch(_P_EXT) as ME:
            ME.return_value.extract.side_effect = _fx(3, text_fn=lambda i: f"Page {i} new")
            extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())
        assert 0 not in written and 1 in written

    def test_returns_extraction_result(self, db: PipelineDB) -> None:
        result = _er()
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with patch(_P_EXT) as ME:
            ME.return_value.extract.side_effect = lambda *a, **kw: result
            ret = extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())
        assert ret.metadata["table_regions"] == [{"page": 2, "html": "<table/>"}]



class TestChunkerLoop:
    def test_produces_chunks_with_full_metadata(self, db, done_event) -> None:
        _pop_pages(db, "h1", 3)
        ci = _tc(("chunk 0 text", 0, {"page_number": 1, "chunk_type": "text",
                                       "chunk_start_char": 0, "chunk_end_char": 12}),
                 ("chunk 1 text", 1, {"page_number": 2, "chunk_type": "text",
                                       "chunk_start_char": 12, "chunk_end_char": 24}))
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.return_value = ci
            chunker_loop("h1", db, threading.Event(), embed_fn=_embed,
                         extraction_done=done_event, pdf_path="/a.pdf",
                         corpus="test", target_model="voyage-context-3")
        out = db.read_ready_chunks("h1")
        assert len(out) == 2
        meta = json.loads(out[0]["metadata_json"])
        for k, v in [("source_path", "/a.pdf"), ("corpus", "test"),
                     ("content_hash", "h1"), ("embedding_model", "voyage-context-3"),
                     ("store_type", "pdf"), ("page_number", 1)]:
            assert meta[k] == v
        assert "indexed_at" in meta
        assert out[0]["chunk_id"] == "h1_0" and out[1]["chunk_id"] == "h1_1"

    def test_cancel_exits(self, db) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()
        t = threading.Thread(target=lambda: (time.sleep(0.2), cancel.set()))
        t.start()
        with patch(_P_CHK):
            chunker_loop("h1", db, cancel, embed_fn=lambda t, m: ([], m))
        t.join()

    def test_text_join_contract(self, db, done_event) -> None:
        _pop_pages(db, "h1", 3)
        joined: list[str] = []
        # Return one chunk so the nexus-aold guard (raises on zero chunks
        # from non-empty text) doesn't fire. The test's purpose is the
        # join contract, not zero-chunk handling.
        sentinel = _tc(("captured", 0, {}))
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.side_effect = lambda text, meta: (joined.append(text), sentinel)[1]
            chunker_loop("h1", db, threading.Event(), embed_fn=lambda t, m: ([], m),
                         extraction_done=done_event)
        assert joined[0] == "\n".join(f"Page {i} content here." for i in range(3))

    def test_raises_when_text_present_but_chunker_empty(self, db, done_event) -> None:
        """nexus-aold: streaming path silent-zero guard.

        Pages were extracted (non-empty accumulated text) but the chunker
        returned zero chunks. Pre-fix, ``chunker_loop`` quietly recorded
        ``chunks_created=0`` and returned, the indexer reported success
        with 0 records (the failure mode the bead names). Post-fix,
        raises an informative RuntimeError so the orchestrator surfaces
        the failure instead of swallowing it.
        """
        _pop_pages(db, "h1", 3)
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.return_value = []
            with pytest.raises(RuntimeError, match="zero chunks"):
                chunker_loop(
                    "h1", db, threading.Event(),
                    embed_fn=lambda t, m: ([], m),
                    extraction_done=done_event,
                    pdf_path="/a.pdf",
                )

    def test_idempotent_resume(self, db, done_event) -> None:
        _pop_pages(db, "h1", 2)
        ci = _tc(("chunk 0", 0, {}), ("chunk 1", 1, {}))
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.return_value = ci
            chunker_loop("h1", db, threading.Event(), embed_fn=_embed, extraction_done=done_event)
            chunker_loop("h1", db, threading.Event(), embed_fn=_embed, extraction_done=done_event)
        assert len(db.read_ready_chunks("h1")) == 2

    def test_resume_with_partially_uploaded_chunks(self, db, done_event) -> None:
        _pop_pages(db, "h1", 2)
        ci = _tc(("c0", 0, {}), ("c1", 1, {}), ("c2", 2, {}))
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.return_value = ci
            chunker_loop("h1", db, threading.Event(), embed_fn=_embed, extraction_done=done_event)
        db.mark_uploaded("h1", [0, 1])
        calls: list[int] = []
        def tracking(texts, model):
            calls.append(len(texts))
            return [[0.1] * 4 for _ in texts], model
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.return_value = ci
            chunker_loop("h1", db, threading.Event(), embed_fn=tracking, extraction_done=done_event)
        assert calls == []

    def test_embed_fn_none(self, db, done_event) -> None:
        _pop_pages(db, "h1", 2)
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.return_value = _tc(("chunk 0", 0, {}))
            chunker_loop("h1", db, threading.Event(), embed_fn=None, extraction_done=done_event)
        out = db.read_ready_chunks("h1")
        assert len(out) == 1 and out[0]["embedding"] is None

    def test_incremental_chunking_before_extraction_done(self, db) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        ext_done, chk_done, polled = threading.Event(), threading.Event(), threading.Event()
        for i in range(5):
            db.write_page("h1", i, f"Page {i} " + "x" * 2000,
                          metadata={"page_number": i + 1, "text_length": 2006})
        db.update_progress("h1", pages_extracted=5)
        n_calls = 0
        def make_chunks(text, meta):
            nonlocal n_calls
            n_calls += 1
            if n_calls == 1:
                polled.set()
            n = max(1, len(text) // 1500)
            return [TextChunk(text=f"chunk-{i}", chunk_index=i, metadata={}) for i in range(n)]
        def signal_done():
            polled.wait(timeout=5)
            for i in range(5, 7):
                db.write_page("h1", i, f"Page {i} " + "x" * 2000,
                              metadata={"page_number": i + 1, "text_length": 2006})
            db.update_progress("h1", total_pages=7, pages_extracted=7)
            ext_done.set()
        t = threading.Thread(target=signal_done)
        t.start()
        with patch(_P_CHK) as MC:
            MC.return_value.chunk.side_effect = make_chunks
            chunker_loop("h1", db, threading.Event(), embed_fn=_embed,
                         extraction_done=ext_done, chunking_done=chk_done)
        t.join()
        assert chk_done.is_set() and len(db.read_ready_chunks("h1")) > 0



class TestUploaderLoop:
    def test_uploads_chunks_via_t3_api(self, db) -> None:
        _pop_chunks(db, "h1", 3)
        t3 = MagicMock()
        uploader_loop("h1", db, t3, "docs__test", threading.Event())
        t3.upsert_chunks_with_embeddings.assert_called_once()
        assert len(t3.upsert_chunks_with_embeddings.call_args[0][1]) == 3
        assert db.read_uploadable_chunks("h1") == []

    def test_batch_sizing(self, db) -> None:
        _pop_chunks(db, "h1", 200)
        t3 = MagicMock()
        uploader_loop("h1", db, t3, "docs__test", threading.Event())
        assert t3.upsert_chunks_with_embeddings.call_count == 2

    def test_cancel_exits(self, db) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()
        t = threading.Thread(target=lambda: (time.sleep(0.2), cancel.set()))
        t.start()
        uploader_loop("h1", db, MagicMock(), "docs__test", cancel)
        t.join()

    def test_marks_uploaded_per_batch(self, db) -> None:
        _pop_chunks(db, "h1", 200)
        t3 = MagicMock()
        orig, calls = db.mark_uploaded, []
        db.mark_uploaded = lambda ch, idx: (calls.append(len(idx)), orig(ch, idx))  # type: ignore[assignment]
        uploader_loop("h1", db, t3, "docs__test", threading.Event())
        assert calls == [128, 72]

    def test_done_when_all_uploaded(self, db) -> None:
        _pop_chunks(db, "h1", 2)
        uploader_loop("h1", db, MagicMock(), "docs__test", threading.Event())
        s = db.get_pipeline_state("h1")
        assert s["chunks_uploaded"] == 2 and s["status"] == "completed"

    def test_done_via_chunking_done_event(self, db) -> None:
        _pop_chunks(db, "h1", 3)
        t3 = MagicMock()
        cd = threading.Event()
        cd.set()
        uploader_loop("h1", db, t3, "docs__test", threading.Event(), cd)
        s = db.get_pipeline_state("h1")
        assert s["chunks_uploaded"] == 3 and s["status"] == "completed"
        t3.upsert_chunks_with_embeddings.assert_called_once()



class TestPipelineIndexPdf:
    def test_full_pipeline(self, db, mock_t3) -> None:
        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}),
                 ("chunk 1", 1, {"page_number": 2, "chunk_type": "text"}))
        fr = _er(3)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(3, fr)
            MC.return_value.chunk.return_value = fc
            total = pipeline_index_pdf(Path("/test/doc.pdf"), "abc123", "docs__test",
                                       mock_t3, db=db, embed_fn=_embed, corpus="test")
        assert total == 2
        mock_t3.upsert_chunks_with_embeddings.assert_called_once()
        assert db.get_pipeline_state("abc123") is None

    def test_extractor_failure(self, db, mock_t3) -> None:
        with patch(_P_EXT) as ME, patch(_P_CHK):
            ME.return_value.extract.side_effect = RuntimeError("boom")
            with pytest.raises(RuntimeError, match="boom"):
                pipeline_index_pdf(Path("/test.pdf"), "h1", "docs__test", mock_t3,
                                   db=db, embed_fn=lambda t, m: ([], m))
        s = db.get_pipeline_state("h1")
        assert s["status"] == "failed" and "boom" in s["error"]

    def test_resume_from_partial(self, db, mock_t3) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_page("h1", 0, "Page 0 content.", metadata={"page_number": 1, "text_length": 15})
        db.write_page("h1", 1, "Page 1 content.", metadata={"page_number": 2, "text_length": 15})
        db.update_progress("h1", pages_extracted=2)
        db.mark_failed("h1", error="crash")
        written: list[int] = []
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(3)
            MC.return_value.chunk.return_value = _tc(("chunk 0", 0, {}))
            orig = db.write_page
            db.write_page = lambda ch, pi, text, metadata=None: (written.append(pi), orig(ch, pi, text, metadata))  # type: ignore[assignment]
            pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3,
                               db=db, embed_fn=_embed)
        assert 0 not in written and 2 in written

    def test_table_regions_postpass(self, db) -> None:
        t3, col = _run_with_col(
            db,
            col_get_return={"ids": ["abc_0", "abc_1"], "metadatas": [
                {"page_number": 1, "chunk_type": "text", "content_hash": "abc123"},
                {"page_number": 2, "chunk_type": "text", "content_hash": "abc123"}]},
            fake_result=_er(3),
            fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"}),
                            ("c1", 1, {"page_number": 2, "chunk_type": "text"})))
        assert t3.update_chunks.call_count >= 1
        for call in t3.update_chunks.call_args_list:
            a = call[0]
            if a[0] == "docs__test" and any(m.get("chunk_type") == "table_page" for m in a[2]):
                assert "abc_1" in a[1]
                break
        else:
            pytest.fail("table_regions post-pass did not update chunks to table_page")

    def test_metadata_enrichment_postpass(self, db) -> None:
        """Post-pass writes the resolved title and author into the chunk
        metadata. ``source_title`` was collapsed into ``title``; the
        post-pass now writes ``title`` only."""
        fr = _er(1)
        fr.metadata["docling_title"] = "My Paper Title"
        fr.metadata["pdf_author"] = "Jane Doe"
        t3, _ = _run_with_col(
            db,
            col_get_return={"ids": ["abc_0"], "metadatas": [
                {"title": "", "content_hash": "abc123", "page_number": 1}]},
            fake_result=fr,
            fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"})),
            pdf_path="/paper.pdf")
        for call in t3.update_chunks.call_args_list:
            a = call[0]
            if a[0] == "docs__test":
                m = a[2][0]
                assert m["title"] == "My Paper Title"
                assert m["source_author"] == "Jane Doe"
                # extraction_method is dropped by normalize() — not in
                # ALLOWED_TOP_LEVEL since no read site uses it.
                assert "extraction_method" not in m
                break
        else:
            pytest.fail("metadata enrichment post-pass not called")

    def test_stale_chunk_pruning(self, db) -> None:
        _, col = _run_with_col(
            db,
            col_get_return={"ids": ["abc123_0", "abc123_1", "old_hash_0"], "metadatas": [
                {"content_hash": "abc123_full", "source_path": "/a.pdf"},
                {"content_hash": "abc123_full", "source_path": "/a.pdf"},
                {"content_hash": "previous_hash", "source_path": "/a.pdf"}]},
            fake_result=_er(1),
            fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"})),
            content_hash="abc123_full")
        col.delete.assert_called_once()
        ids = col.delete.call_args.kwargs.get("ids", col.delete.call_args[1].get("ids", []))
        assert "old_hash_0" in ids and "abc123_0" not in ids

    def test_skip_already_running(self, db, mock_t3) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        assert pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db) == 0
        mock_t3.upsert_chunks_with_embeddings.assert_not_called()

    def test_embed_fn_none_resolves_credentials(self, db, mock_t3) -> None:
        fr, fc = _er(1), _tc(("c0", 0, {"page_number": 1, "chunk_type": "text"}))
        with (patch(_P_EXT) as ME, patch(_P_CHK) as MC,
              patch("nexus.config.get_credential", return_value="fake-key"),
              patch("nexus.config.load_config", return_value={}),
              patch("nexus.doc_indexer._embed_with_fallback") as me):
            me.return_value = ([[0.1] * 4], "voyage-context-3")
            ME.return_value.extract.side_effect = _fx(1, fr)
            MC.return_value.chunk.return_value = fc
            total = pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db)
        assert total == 1
        me.assert_called()

    def test_embed_fn_none_no_credentials_fails_fast(self, db, mock_t3) -> None:
        with patch("nexus.config.get_credential", return_value=None):
            with pytest.raises(RuntimeError, match="voyage_api_key not configured"):
                pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db)

    def test_writes_doc_id_when_catalog_initialized(
        self, db, tmp_path, monkeypatch,
    ) -> None:
        """RDR-102 D4 #2 (streaming pipeline mirror): pipeline_index_pdf
        must populate ``doc_id`` on every chunk it uploads when the
        catalog is initialized.

        Pre-Phase-A this fails because the streaming pipeline registers
        the catalog Document via ``_catalog_pdf_hook`` AFTER the upload
        completes (line 729 of pipeline_stages.py); ``chunker_loop``
        builds chunk metadata via ``_build_chunk_metadata`` →
        ``make_chunk_metadata()`` with no ``doc_id`` argument. Phase A
        registers the Document at the streaming-entry boundary and
        threads doc_id through chunker_loop's metadata builder.
        """
        import chromadb
        from nexus.catalog import reset_cache
        from nexus.catalog.catalog import Catalog
        from nexus.db.t3 import T3Database

        cat_dir = tmp_path / "test-catalog"
        Catalog.init(cat_dir)
        reset_cache()
        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)
        monkeypatch.setattr(
            "nexus.config._global_config_path",
            lambda: Path("/nonexistent"),
        )

        pdf_path = tmp_path / "stream.pdf"
        pdf_path.write_bytes(b"fake pdf bytes for streaming pipeline test")
        client = chromadb.EphemeralClient()
        t3 = T3Database(_client=client, local_mode=True)

        fc = _tc(
            ("chunk 0 text", 0, {"page_number": 1, "chunk_type": "text",
                                  "chunk_start_char": 0, "chunk_end_char": 12}),
            ("chunk 1 text", 1, {"page_number": 2, "chunk_type": "text",
                                  "chunk_start_char": 12, "chunk_end_char": 24}),
        )
        fr = _er(2)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(2, fr)
            MC.return_value.chunk.return_value = fc
            total = pipeline_index_pdf(
                pdf_path, "rdr102streamhash", "docs__rdr102_stream",
                t3, db=db, embed_fn=_embed,
                corpus="rdr102_stream",
            )
        assert total == 2, f"expected 2 chunks uploaded; got {total}"

        col = t3.get_or_create_collection("docs__rdr102_stream")
        rows = col.get(include=["metadatas"])
        assert rows["metadatas"], (
            "expected at least one chunk in docs__rdr102_stream"
        )
        missing_doc_id = [m for m in rows["metadatas"] if not m.get("doc_id")]
        assert not missing_doc_id, (
            f"{len(missing_doc_id)}/{len(rows['metadatas'])} streaming "
            f"pipeline chunks missing doc_id. Phase A must register the "
            f"catalog Document at the pipeline_index_pdf entry boundary "
            f"and thread doc_id through chunker_loop's metadata builder, "
            f"not via the post-upload _catalog_pdf_hook."
        )



class TestBufferEdgeCases:
    def test_count_pipelines(self, db) -> None:
        assert db.count_pipelines() == 0
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        assert db.count_pipelines() == 1

    def test_count_embedded_chunks(self, db) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.write_chunk("h1", 0, "text", "cid-0", embedding=b"\x00\x01")
        db.write_chunk("h1", 1, "text", "cid-1")
        assert db.count_embedded_chunks("h1") == 1

    def test_mark_uploaded_empty_list(self, db) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        db.mark_uploaded("h1", [])


_REQUIRED_META = {
    # Identity / spans / position
    "source_path", "content_hash", "chunk_text_hash", "chunk_index", "chunk_count",
    "chunk_start_char", "chunk_end_char", "line_start", "line_end", "page_number",
    # Display / routing (post source_title→title collapse, expires_at→indexed_at swap)
    "title", "source_author", "section_title", "section_type", "tags", "category",
    "content_type", "store_type", "corpus", "embedding_model",
    # Lifecycle
    "indexed_at", "ttl_days", "frecency_score", "source_agent", "session_id",
    # bib_* and git_meta intentionally omitted (drop-when-empty by normalize)
}


def test_streaming_metadata_has_all_batch_fields(db, done_event) -> None:
    db.create_pipeline("h1", "/doc.pdf", "docs__test")
    db.write_page("h1", 0, "Some text here.", metadata={"page_number": 1, "text_length": 15})
    db.update_progress("h1", total_pages=1, pages_extracted=1)
    with patch(_P_CHK) as MC:
        MC.return_value.chunk.return_value = _tc(
            ("chunk text", 0, {"page_number": 1, "chunk_type": "text",
                               "chunk_start_char": 0, "chunk_end_char": 10}))
        chunker_loop("h1", db, threading.Event(), embed_fn=_embed,
                     extraction_done=done_event, pdf_path="/doc.pdf",
                     corpus="mycorpus", target_model="voyage-context-3")
    meta = json.loads(db.read_ready_chunks("h1")[0]["metadata_json"])
    assert _REQUIRED_META - set(meta.keys()) == set()


@pytest.mark.parametrize("get_exc,upd_exc,expected", [
    pytest.param(Exception("connection reset"), None, False, id="query_failure"),
    pytest.param(None, Exception("quota exceeded"), False, id="update_failure"),
    pytest.param(None, None, True, id="success"),
])
def test_update_chunk_metadata(get_exc, upd_exc, expected) -> None:
    t3, col = MagicMock(), MagicMock()
    if get_exc:
        col.get.side_effect = get_exc
    else:
        col.get.return_value = {"ids": ["id1"], "metadatas": [{"chunk_type": "text"}]}
    if upd_exc:
        t3.update_chunks.side_effect = upd_exc
    assert _update_chunk_metadata(t3, col, "docs__test", "abc123", lambda m: True) is expected


@pytest.mark.parametrize("get_exc,get_ret,del_exc,expected,del_called", [
    pytest.param(Exception("connection reset"), None, None, False, False, id="query_failure"),
    pytest.param(None, {"ids": ["old1", "old2"], "metadatas": [
        {"content_hash": "stale", "source_path": "/doc.pdf"},
        {"content_hash": "stale", "source_path": "/doc.pdf"}]},
        Exception("quota exceeded"), False, True, id="delete_failure"),
    pytest.param(None, {"ids": ["old1"], "metadatas": [{"content_hash": "stale"}]},
        None, True, True, id="success"),
])
def test_prune_stale_chunks(get_exc, get_ret, del_exc, expected, del_called) -> None:
    col = MagicMock()
    col.get = MagicMock(side_effect=get_exc) if get_exc else MagicMock(return_value=get_ret)
    if del_exc:
        col.delete = MagicMock(side_effect=del_exc)
    assert _prune_stale_chunks(col, "/doc.pdf", "new_hash") is expected
    (col.delete.assert_called if del_called else col.delete.assert_not_called)()


def test_pipeline_data_kept_on_enrichment_failure(db) -> None:
    t3, col = MagicMock(), MagicMock()
    col.get.return_value = {"ids": ["id1"], "metadatas": [{"content_hash": "h1"}]}
    t3.update_chunks.side_effect = Exception("quota exceeded")
    ro = MagicMock(spec=ExtractionResult)
    ro.text, ro.metadata, ro.title = "some text", {"page_count": 1, "docling_title": "Test"}, "Test"
    assert _enrich_metadata_from_extraction("h1", ro, Path("/test.pdf"), t3, col, "docs__test") is False
