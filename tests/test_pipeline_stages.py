# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import json
import struct
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, create_autospec, patch

import pytest

from tests._catalog_fixture_ops import active_reader
from tests.conftest import fake_credentials

from nexus.pdf_chunker import TextChunk
from nexus.pdf_extractor import ExtractionResult
from nexus.db.t3 import T3Database
from nexus.db.http_pipeline_client import HttpPipelineDB, PipelineConflictRunning
from tests._owner_write_double import install_streaming_writer
from tests.pipeline_fake_engine import make_fake_engine_db
from nexus.pipeline_stages import (
    _enrich_metadata_from_extraction,
    _update_chunk_metadata,
    chunker_loop,
    extractor_loop,
    pipeline_index_pdf,
    uploader_loop,
)
from tests.conftest import make_vector_test_client


# RDR-109 Phase 2: local-token in test collection names varies by whether
# fastembed is installed (tier 1: bge-base-en-v15-768, else tier 0:
# minilm-l6-v2-384). Resolve at runtime so the suite is deterministic on
# CI (no fastembed) and dev machines (fastembed pulled by experiments).
def _local_token() -> str:
    from nexus.db.local_ef import local_model_token
    return local_model_token()

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
    def extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, allow_degraded=False):
        for i in range(n):
            txt = text_fn(i) if text_fn else f"Page {i} content."
            if on_page:
                on_page(i, txt, {"page_number": i + 1, "text_length": len(txt)})
        return r
    return extract


def _tc(*specs: tuple[str, int, dict]) -> list[TextChunk]:
    return [TextChunk(text=t, chunk_index=ci, metadata=m) for t, ci, m in specs]


@pytest.fixture()
def db() -> HttpPipelineDB:
    store, _engine = make_fake_engine_db()
    return store


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production poll widened to 2.0s for the HTTP backend (RDR-186
    .16); the fake engine is in-memory, so poll fast to keep the suite
    quick."""
    monkeypatch.setattr("nexus.pipeline_stages._POLL_INTERVAL", 0.01)


@pytest.fixture(autouse=True)
def _stub_fence_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    """nexus-5xn3k.4: this file's ``t3``/``mock_t3`` fixtures are
    ``create_autospec(T3Database)`` doubles — no chunk ever actually lands
    in the REAL local-engine T3 substrate that ``_register_or_lookup_doc_id``
    / manifest writes talk to (this suite predates RUNFENCE and was never
    mocking the catalog). The fence's engine-side verify-then-stamp
    (memo §3.3) correctly sees "0 chunks present" for a doc whose manifest
    it just wrote and refuses completion — genuinely correct behavior, but
    orthogonal to what this file's tests exercise (stage orchestration, not
    fence/catalog integration; that is ``tests/test_5xn3k_fence_ordering.py``'s
    job, with its own properly-scoped recording double). Stub only the
    completion call so ``IndexRunVerifyRefused`` doesn't leak into every
    pipeline-stage test that happens to complete; ``_fence_begin``/
    ``_fence_fail`` still hit the real substrate unchanged.
    """
    monkeypatch.setattr("nexus.doc_indexer._fence_complete", lambda *a, **k: None)


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


def _bound_the_polling(monkeypatch: pytest.MonkeyPatch, limit: int = 400) -> None:
    """Turn a stage that polls forever into a failure. A run whose uploader waits for a count that
    can never be reached has no exit; the pool's non-daemon threads would hang the whole pytest
    process, so the poll sleep itself raises once it has been called *limit* times."""
    import nexus.pipeline_stages as ps

    calls = {"n": 0}
    real = ps.time

    class _BoundedTime:
        def sleep(self, seconds: float) -> None:
            calls["n"] += 1
            if calls["n"] > limit:
                raise AssertionError(f"a stage polled {limit} times without finishing: the run never ends")
            real.sleep(seconds)

        def __getattr__(self, name: str):
            return getattr(real, name)

    monkeypatch.setattr(ps, "time", _BoundedTime())


def _pop_pages(db: HttpPipelineDB, h: str, n: int) -> None:
    db.create_pipeline(h, "/a.pdf", "docs__test")
    for i in range(n):
        db.write_page(h, i, f"Page {i} content here.",
                      metadata={"page_number": i + 1, "text_length": 22})
    db.update_progress(h, total_pages=n, pages_extracted=n)


def _pop_chunks(db: HttpPipelineDB, h: str, n: int) -> None:
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


def _run_service_mode(db, fake_result, fake_chunks, *, service=True, voyage=None,
                      pdf_path="/a.pdf", content_hash="svc123", collection="docs__test"):
    """nexus-9n1u3: drive pipeline_index_pdf with embed_fn=None under a patched
    service-mode flag and Voyage-credential, mirroring _run_with_col."""
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "metadatas": []}
    t3 = create_autospec(T3Database, instance=True)
    t3.get_or_create_collection.return_value = mock_col
    pc = fake_result.metadata["page_count"]
    with patch(_P_EXT) as ME, patch(_P_CHK) as MC, \
            patch("nexus.db.http_vector_client.is_vector_service_mode",
                  return_value=service), \
            patch("nexus.config.get_credential", side_effect=fake_credentials(voyage)):
        ME.return_value.extract.side_effect = _fx(pc, fake_result)
        MC.return_value.chunk.return_value = fake_chunks
        pipeline_index_pdf(Path(pdf_path), content_hash, collection, t3,
                           db=db, embed_fn=None)
    return t3, mock_col


class TestServiceModeStreaming:
    """nexus-9n1u3: PDF streaming pipeline must server-side-embed in service
    mode instead of demanding a Voyage key (which broke ALL PDF ingestion)."""

    @pytest.fixture(autouse=True)
    def writer(self, monkeypatch: pytest.MonkeyPatch):
        return install_streaming_writer(monkeypatch)

    def test_service_mode_no_voyage_completes_and_server_embeds(self, db, writer) -> None:
        t3, _ = _run_service_mode(
            db, _er(2), _tc(("chunk a", 0, {}), ("chunk b", 1, {})))
        # Did NOT raise; the uploader wrote the chunks with their owner rows and no chunk upload
        # of its own.
        t3.upsert_chunks_with_embeddings.assert_not_called()
        (w,) = writer.instances
        (rows, chunks), = w.batches
        assert len(chunks) == 2
        # The service embeds: no client vector rides the write (RDR-152 Seam B).
        assert all("embedding" not in c for c in chunks)

    def test_non_service_no_voyage_still_raises(self, db) -> None:
        # nexus-sghyo (2026-08-06): the legacy (non-service) embed path is
        # now retired outright rather than credential-gated — the client
        # no longer embeds via Voyage at all (Hal determination
        # 2026-07-28), so the message names the retirement, not a missing
        # key.
        with pytest.raises(RuntimeError, match="non-service embedding was retired"):
            _run_service_mode(
                db, _er(1), _tc(("chunk x", 0, {})),
                service=False, voyage=None, content_hash="raw123")



class TestExtractorLoop:
    def test_writes_pages_to_buffer(self, db: HttpPipelineDB) -> None:
        result = _er(3)
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with patch(_P_EXT) as ME:
            def f(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, allow_degraded=False):
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

    def test_cancel_raises_pipeline_cancelled(self, db: HttpPipelineDB) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()
        def f(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, allow_degraded=False):
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

    def test_resume_skips_existing_pages(self, db: HttpPipelineDB) -> None:
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

    def test_returns_extraction_result(self, db: HttpPipelineDB) -> None:
        result = _er()
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with patch(_P_EXT) as ME:
            ME.return_value.extract.side_effect = lambda *a, **kw: result
            ret = extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())
        assert ret.metadata["table_regions"] == [{"page": 2, "html": "<table/>"}]

    def test_retry_after_caught_failure_reextracts_all_pages(self, db: HttpPipelineDB) -> None:
        """nexus-6m9zy.1 (#1), superseded by nexus-33q80: pipeline_index_pdf's
        caught-exception handler is mark_failed + clear_orphan_wal (see
        :func:`extractor_loop`'s nexus-gl99l comment). Before nexus-33q80,
        clear_orphan_wal wiped the pdf_pages WAL rows but never reset the
        pages_extracted progress counter, so a naive retry could trust a
        stale counter to skip pages the WAL no longer held. nexus-33q80
        makes the engine zero pages_extracted (and chunks_uploaded) in the
        SAME transaction as the wipe, so the counter is now accurate --
        this test's own regression protection (the retry must still
        re-extract every page, never skip any) stays exactly as strong
        with an accurate counter as it did with the old defensive
        WAL-count re-verification in extractor_loop's fast path.
        """
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with patch(_P_EXT) as ME:
            def fail_mid(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, allow_degraded=False):
                for i in range(10):
                    if i == 6:
                        raise RuntimeError("boom mid-extraction")
                    on_page(i, f"Page {i} text.", {"page_number": i + 1, "text_length": 12})
                return _er(10)
            ME.return_value.extract.side_effect = fail_mid
            with pytest.raises(RuntimeError):
                extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())
        # pipeline_index_pdf's own caught-exception handling, replayed directly.
        db.mark_failed("h1", error="boom")
        db.clear_orphan_wal("h1")
        state = db.get_pipeline_state("h1")
        assert state["pages_extracted"] == 0  # nexus-33q80: reset in the same transaction as the wipe
        assert db.read_pages("h1") == []  # and the WAL is gone too

        with patch(_P_EXT) as ME:
            ME.return_value.extract.side_effect = _fx(10)
            extractor_loop(Path("/a.pdf"), "h1", db, threading.Event())
        indices = sorted(r["page_index"] for r in db.read_pages("h1"))
        assert indices == list(range(10)), f"retry skipped pages the cleared WAL no longer had: {indices}"


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
                         corpus="test", target_model="model-ctx")
        out = db.read_ready_chunks("h1")
        assert len(out) == 2
        meta = json.loads(out[0]["metadata_json"])
        # RDR-102 D2 dropped source_path; RDR-101 Phase 5c (nexus-o6aa.13)
        # dropped store_type, corpus, git_meta. content_type / content_hash
        # / embedding_model / page_number remain as the canonical chunk-
        # time identity / routing fields.
        for k, v in [("content_type", "pdf"),
                     ("content_hash", "h1"),
                     ("embedding_model", "model-ctx"),
                     ("page_number", 1)]:
            assert meta[k] == v
        for dropped in ("source_path", "store_type", "corpus", "git_meta"):
            assert dropped not in meta
        assert "indexed_at" in meta
        # RDR-180 (nexus-jxizy.3): chunk_id is the FULL chunk_text_hash.
        import hashlib as _hl
        expected_0 = _hl.sha256(b"chunk 0 text").hexdigest()
        expected_1 = _hl.sha256(b"chunk 1 text").hexdigest()
        assert out[0]["chunk_id"] == expected_0
        assert out[1]["chunk_id"] == expected_1

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

    def test_embed_fn_none_writes_service_mode_sentinel(self, db, done_event) -> None:
        # nexus-9n1u3: in SERVICE mode, embed_fn=None makes the embed stage
        # write a non-NULL empty-blob sentinel so the chunk stays uploadable;
        # the JVM embeds at upload time.
        _pop_pages(db, "h1", 2)
        with patch(_P_CHK) as MC, patch(
            "nexus.db.http_vector_client.is_vector_service_mode", return_value=True
        ):
            MC.return_value.chunk.return_value = _tc(("chunk 0", 0, {}))
            chunker_loop("h1", db, threading.Event(), embed_fn=None, extraction_done=done_event)
        out = db.read_ready_chunks("h1")
        assert len(out) == 1 and out[0]["embedding"] == b""
        # non-NULL sentinel -> the uploader will pick it up (was dropped when None)
        assert len(db.read_uploadable_chunks("h1")) == 1

    def test_embed_fn_none_non_service_does_not_write_sentinel(self, db, done_event) -> None:
        # review Sig-1: the b"" sentinel is gated LOCALLY on is_vector_service_mode().
        # A caller that bypasses the orchestrator and passes embed_fn=None in
        # NON-service mode must NOT silently write an uploadable zero-vector
        # chunk — it falls through to NULL (dropped by read_uploadable_chunks),
        # surfacing the misuse instead of corrupting.
        _pop_pages(db, "h1", 2)
        with patch(_P_CHK) as MC, patch(
            "nexus.db.http_vector_client.is_vector_service_mode", return_value=False
        ):
            MC.return_value.chunk.return_value = _tc(("chunk 0", 0, {}))
            chunker_loop("h1", db, threading.Event(), embed_fn=None, extraction_done=done_event)
        out = db.read_ready_chunks("h1")
        assert len(out) == 1 and out[0]["embedding"] is None
        assert db.read_uploadable_chunks("h1") == []

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
    """RDR-223 (nexus-z0o2p.11): the uploader hands every batch to the document's multi-batch writer
    (rows + the chunks they reference); it makes no chunk upload of its own. ``writer`` records the
    calls the real writer would send (the real engine refuses these tests' fake chunk ids); the
    writer's behaviour against the real engine is in ``tests/integration/test_rdr223_pdf_journey.py``."""

    @pytest.fixture(autouse=True)
    def writer(self, monkeypatch: pytest.MonkeyPatch):
        return install_streaming_writer(monkeypatch)

    def _up(self, db, *, t3=None, cancel=None, chunking_done=None, **kw) -> None:
        kw.setdefault("catalog_doc_id", "1.1.1")
        uploader_loop("h1", db, t3 if t3 is not None else MagicMock(), "docs__test",
                      cancel or threading.Event(), chunking_done, **kw)

    def test_hands_the_chunks_to_the_writer_and_makes_no_upsert(self, db, writer) -> None:
        _pop_chunks(db, "h1", 3)
        t3 = MagicMock()
        self._up(db, t3=t3)
        t3.upsert_chunks_with_embeddings.assert_not_called()
        (w,) = writer.instances
        (rows, chunks), = w.batches
        assert [r["position"] for r in rows] == [0, 1, 2], "position is the chunker's global index"
        assert [r["chash"] for r in rows] == [c["chash"] for c in chunks] == [f"h1_{i}" for i in range(3)]
        assert [c["text"] for c in chunks] == [f"chunk {i} text" for i in range(3)]
        assert all("chunk_index" not in c["metadata"] for c in chunks), \
            "the chunk's position is its manifest row, not chunk metadata (RDR-108 Phase 3)"
        assert w.kwargs["doc_id"] == "1.1.1" and w.kwargs["collection"] == "docs__test"
        assert w.kwargs["content_hash"] == "h1"
        assert w.finished and db.read_uploadable_chunks("h1") == []

    def test_a_direct_caller_gets_a_writer_that_stamps_itself(self, db, writer) -> None:
        _pop_chunks(db, "h1", 2)
        self._up(db)
        assert writer.instances[0].kwargs["defer_completion"] is False
        assert ("complete",) not in writer.events

    def test_an_orchestrated_run_defers_the_stamp_to_the_orchestrator(self, db, writer) -> None:
        from nexus.pipeline_stages import UploadRun

        _pop_chunks(db, "h1", 2)
        run = UploadRun()
        self._up(db, run=run)
        assert writer.instances[0].kwargs["defer_completion"] is True
        assert run.finished and run.writer is writer.instances[0]
        assert ("complete",) not in writer.events, "only the orchestrator stamps"

    def test_force_re_embed_true_reaches_the_writer(self, db, writer) -> None:
        """nexus-8143o: uploader_loop's own force_re_embed kwarg (default False) is the writer's
        force_re_embed -- the streaming pipeline's one RDR-181 server-side re-embed control."""
        _pop_chunks(db, "h1", 3)
        self._up(db, force_re_embed=True)
        assert writer.instances[0].kwargs["force_re_embed"] is True

    def test_force_re_embed_default_is_false(self, db, writer) -> None:
        _pop_chunks(db, "h1", 3)
        self._up(db)
        assert writer.instances[0].kwargs["force_re_embed"] is False

    def test_batch_sizing(self, db, writer) -> None:
        _pop_chunks(db, "h1", 200)
        self._up(db)
        assert [len(r) for r, _ in writer.instances[0].batches] == [128, 72]
        assert len(writer.instances) == 1, "one writer for the whole document"

    def test_cancel_exits(self, db) -> None:
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        cancel = threading.Event()
        t = threading.Thread(target=lambda: (time.sleep(0.2), cancel.set()))
        t.start()
        uploader_loop("h1", db, MagicMock(), "docs__test", cancel)
        t.join()

    def test_marks_uploaded_per_batch_once_its_request_was_sent(self, db, writer) -> None:
        _pop_chunks(db, "h1", 200)
        orig, calls = db.mark_uploaded, []
        db.mark_uploaded = lambda ch, idx: (calls.append((len(idx), list(writer.events))), orig(ch, idx))  # type: ignore[assignment]
        self._up(db)
        assert [n for n, _ in calls] == [128, 72]
        # The writer holds a batch back until it knows whether it is the last: batch 0 is flagged
        # after batch 1 arrived (which sent it), batch 1 only after finish() sent it.
        assert ("sent", 0) in calls[0][1] and ("sent", 1) not in calls[0][1]
        assert ("finish",) in calls[1][1] and ("sent", 1) in calls[1][1]

    def test_done_when_all_uploaded(self, db) -> None:
        _pop_chunks(db, "h1", 2)
        self._up(db)
        s = db.get_pipeline_state("h1")
        assert s["chunks_uploaded"] == 2 and s["status"] == "completed"

    def test_done_via_chunking_done_event(self, db, writer) -> None:
        _pop_chunks(db, "h1", 3)
        cd = threading.Event()
        cd.set()
        self._up(db, chunking_done=cd)
        s = db.get_pipeline_state("h1")
        assert s["chunks_uploaded"] == 3 and s["status"] == "completed"
        assert len(writer.instances[0].batches) == 1

    def test_the_last_batch_waits_for_the_chunker_to_finish(self, db, writer) -> None:
        """The writer's last request carries the sweep, so it must not be sent while the chunker can
        still produce chunks: the uploader hands over what it has and waits."""
        _pop_chunks(db, "h1", 3)
        cd = threading.Event()
        cancel = threading.Event()
        th = threading.Thread(target=self._up, args=(db,), kwargs={"chunking_done": cd, "cancel": cancel},
                              daemon=True)
        th.start()
        time.sleep(0.3)
        assert th.is_alive() and not writer.instances[0].finished, "held until the chunker is done"
        cd.set()
        th.join(timeout=3.0)
        assert not th.is_alive() and writer.instances[0].finished

    def test_a_resume_with_everything_already_uploaded_needs_no_writer(self, db, writer) -> None:
        """A retry of a run whose post-pass failed: every chunk was flagged by the earlier process,
        so there is nothing to send and no writer runs (the orchestrator stamps the run itself)."""
        _pop_chunks(db, "h1", 6)
        db.mark_uploaded("h1", list(range(6)))
        db.update_progress("h1", chunks_uploaded=6)
        cd = threading.Event()
        cd.set()
        self._up(db, chunking_done=cd)
        assert writer.instances == []
        s = db.get_pipeline_state("h1")
        assert s["chunks_uploaded"] == 6 and s["status"] == "completed"

    @pytest.mark.parametrize("counter", [4, 0], ids=["counter-current", "counter-lagging"])
    def test_a_writer_less_uploader_whose_first_chunk_is_not_the_first_refuses(
        self, db, writer, counter, monkeypatch,
    ) -> None:
        """Internal invariant. nexus-6m9zy.1 (#3) was a crash-resume that added its uploads to the
        persisted count. With one writer per document that resume is unsafe: the writer's state (the
        pre-run manifest snapshot, the positions written) died with the process, and sending only
        the remaining chunks would replace the manifest with that tail and sweep the head as
        superseded. The orchestrator discards such a buffer before it gets here; an uploader that is
        handed one anyway refuses to send. It reads the first chunk it would send, not the counter
        (the counter is buffered and lags)."""
        from nexus.pipeline_stages import PartialUploadResumeError

        _bound_the_polling(monkeypatch)
        _pop_chunks(db, "h1", 6)
        db.mark_uploaded("h1", [0, 1, 2, 3])  # run 1 uploaded 4 of 6, then died
        if counter:
            db.update_progress("h1", chunks_uploaded=counter)
        cd = threading.Event()
        cd.set()
        with pytest.raises(PartialUploadResumeError, match=r"chunk #4"):
            self._up(db, chunking_done=cd)
        assert writer.instances == [], "nothing was sent"

    def test_second_stage_failure_after_upload_progress_does_not_inflate_chunks_uploaded(self, db) -> None:
        """nexus-6m9zy.1 (#3) ship-blocker fix (substantive-critic T2
        nexus/critique-59c07fe5b-uploader-chunks-uploaded-inflation-
        nexus-6m9zy [26147]): the first cut of this fix seeded
        total_uploaded from the persisted chunks_uploaded unconditionally.
        clear_orphan_wal wipes every pdf_chunks row -- uploaded=true rows
        included -- but never resets that counter (same class as
        pages_extracted, nexus-gl99l). When upload had already made real
        progress before a LATER pipeline stage's failure triggered
        mark_failed + clear_orphan_wal, the stale counter got added ON
        TOP of the resumed run's genuine re-upload count: reproduced as a
        persisted chunks_uploaded of 20 for a true 10-chunk document.

        This calls pipeline_index_pdf's own first_exc handler
        (_mark_failed_and_reset_wal: mark_failed + clear_orphan_wal + the
        chunks_uploaded=0 reset) directly, since driving the real
        three-stage concurrent orchestrator to fail deterministically
        AFTER genuine upload progress is not reproducible without a race.
        """
        h = "hFail"
        db.create_pipeline(h, "/fail.pdf", "docs__test")
        for i in range(4):
            db.write_chunk(h, i, f"chunk {i} text", f"{h}_{i}",
                           metadata={"page": 1}, embedding=_fake_embedding(i))
        db.update_progress(h, chunks_created=4, chunks_embedded=4)
        # chunking_done is a real, already-set Event: the orchestrated branch
        # every production caller takes (pipeline_index_pdf always passes one),
        # not the chunking_done=None resume branch (critique of df5c4f035).
        _done = threading.Event()
        _done.set()
        uploader_loop(h, db, MagicMock(), "docs__test", threading.Event(), _done,
                      catalog_doc_id="1.1.1")
        assert db.get_pipeline_state(h)["chunks_uploaded"] == 4

        # A later stage now fails: pipeline_index_pdf's own first_exc
        # handler, called, not replayed, so deleting the production reset
        # turns this test red (review of df5c4f035).
        from nexus.pipeline_stages import _mark_failed_and_reset_wal  # noqa: PLC0415 — deferred, matches the file's other in-test imports
        _mark_failed_and_reset_wal(db, h, RuntimeError("boom"))

        assert db.get_pipeline_state(h)["chunks_uploaded"] == 0, "the fix must reset the stale counter"
        assert db.read_uploadable_chunks(h) == []

        # Retry: the document re-chunks and re-embeds from scratch (WAL
        # was wiped), this time producing 10 chunks in full.
        assert db.create_pipeline(h, "/fail.pdf", "docs__test") == "resuming"
        for i in range(10):
            db.write_chunk(h, i, f"chunk {i} text v2", f"{h}_{i}",
                           metadata={"page": 1}, embedding=_fake_embedding(i))
        db.update_progress(h, chunks_created=10, chunks_embedded=10)
        _done = threading.Event()
        _done.set()
        uploader_loop(h, db, MagicMock(), "docs__test", threading.Event(), _done,
                      catalog_doc_id="1.1.1")

        final = db.get_pipeline_state(h)
        assert final["chunks_uploaded"] == 10, (
            f"expected the true chunk count (10), got {final['chunks_uploaded']} "
            f"-- the stale pre-clear counter must not be double-counted"
        )
        assert final["status"] == "completed"

    def test_batch_hooks_fire_per_landed_batch_with_the_chunks_the_writer_sent(self, db, writer) -> None:
        """The batch hook chain (taxonomy assign) reads stored chunks, so it fires for a batch only
        once the writer sent it: batch 0 after batch 1 arrived, batch 1 after finish. The manifest
        hook is left out (the writer writes the manifest with the chunks)."""
        _pop_chunks(db, "h1", 200)
        seen: list[tuple[int, list]] = []

        def _capture_batch(doc_ids, collection, contents, embeddings, metadatas, **kwargs):
            seen.append((len(doc_ids), list(writer.events)))
            assert kwargs.get("catalog_doc_id") == "1.1.1"

        from nexus.hook_registry import HookRegistry
        from nexus.mcp_infra import manifest_write_batch_hook
        hooks = HookRegistry()
        hooks.register_batch(_capture_batch)
        hooks.register_batch(manifest_write_batch_hook)

        self._up(db, hooks=hooks)

        assert [n for n, _ in seen] == [128, 72]
        assert ("sent", 0) in seen[0][1] and ("sent", 1) not in seen[0][1]
        assert ("sent", 1) in seen[1][1]

    def test_the_manifest_hook_is_not_fired(self, db, monkeypatch) -> None:
        _pop_chunks(db, "h1", 3)
        fired: list[int] = []

        def fake_manifest_hook(doc_ids, collection, contents, embeddings, metadatas, **kwargs):
            fired.append(len(doc_ids))

        from nexus.hook_registry import HookRegistry
        monkeypatch.setattr("nexus.mcp_infra.manifest_write_batch_hook", fake_manifest_hook)
        hooks = HookRegistry()
        hooks.register_batch(fake_manifest_hook)
        self._up(db, hooks=hooks)
        assert fired == [], "the writer writes the manifest with the chunks; the hook would write it twice"

    def test_no_catalog_document_means_no_write(self, db, writer) -> None:
        from nexus.errors import CatalogIdentityMissingError

        _pop_chunks(db, "h1", 3)
        with pytest.raises(CatalogIdentityMissingError):
            self._up(db, catalog_doc_id="")
        assert writer.instances == []

    def test_a_dry_run_puts_the_chunks_in_the_throwaway_store_and_writes_no_catalog(self, db, writer) -> None:
        _pop_chunks(db, "h1", 3)
        t3 = MagicMock()
        self._up(db, t3=t3, dry_run=True, catalog_doc_id="")
        t3.upsert_chunks_with_embeddings.assert_called_once()
        assert len(t3.upsert_chunks_with_embeddings.call_args[0][1]) == 3
        assert writer.instances == []
        assert db.get_pipeline_state("h1")["chunks_uploaded"] == 3


class TestMarkFailedAndResetWal:
    """nexus-33q80: the engine now zeroes chunks_uploaded/pages_extracted
    inside clear_orphan_wal's own transaction, so the client's terminal
    handler makes exactly ONE reset-relevant call sequence -- mark_failed
    then clear_orphan_wal -- and never a separate update_progress call to
    undo the wipe's staleness. Two client calls could never be atomic;
    removing the second one removes the failure window entirely, not just
    the class of failure that leaves a log line behind."""

    def test_makes_exactly_mark_failed_then_clear_orphan_wal_no_separate_reset(self) -> None:
        from nexus.pipeline_stages import _mark_failed_and_reset_wal

        mock_db = MagicMock()
        mock_db.mock_calls.clear()

        _mark_failed_and_reset_wal(mock_db, "hX", RuntimeError("boom"))

        assert [c[0] for c in mock_db.mock_calls] == ["mark_failed", "clear_orphan_wal"], (
            f"expected exactly mark_failed then clear_orphan_wal; got {mock_db.mock_calls} "
            f"-- a separate update_progress(chunks_uploaded=0) call is the nexus-6m9zy.1 "
            f"non-atomicity nexus-33q80 removes"
        )
        mock_db.update_progress.assert_not_called()
        mock_db.mark_failed.assert_called_once_with("hX", error="boom")
        mock_db.clear_orphan_wal.assert_called_once_with("hX")


class TestPipelineIndexPdf:
    @pytest.fixture(autouse=True)
    def writer(self, monkeypatch: pytest.MonkeyPatch):
        """The multi-batch writer is a recorder here: this class drives the orchestration with
        fake chunk ids the real engine refuses (its behaviour against the engine is in
        ``tests/integration/test_rdr223_pdf_journey.py``)."""
        return install_streaming_writer(monkeypatch)

    def test_full_pipeline(self, db, mock_t3, writer) -> None:
        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}),
                 ("chunk 1", 1, {"page_number": 2, "chunk_type": "text"}))
        fr = _er(3)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(3, fr)
            MC.return_value.chunk.return_value = fc
            total = pipeline_index_pdf(Path("/test/doc.pdf"), "abc123", "docs__test",
                                       mock_t3, db=db, embed_fn=_embed, corpus="test")
        assert total == 2
        mock_t3.upsert_chunks_with_embeddings.assert_not_called()
        (w,) = writer.instances
        assert [len(r) for r, _ in w.batches] == [2]
        # The stamp is the orchestrator's, after the post-passes: the writer finished, then it was
        # stamped complete.
        assert writer.events[-2:] == [("finish",), ("complete",)]
        assert db.get_pipeline_state("abc123") is None

    def test_post_pass_failure_can_be_retried_not_skipped(self, db) -> None:
        """nexus-6m9zy.5 (#10, no probe -- READ finding, test written from
        the bead description). uploader_loop already calls
        db.mark_completed() -- BEFORE any post-pass runs -- the moment
        chunks_uploaded catches up with chunks_created. When a post-pass
        (metadata enrichment here) then fails, delete_pipeline_data is
        skipped so the checkpoint data is 'kept for retry', but the row's
        status stayed 'completed' -- the ONLY status create_pipeline()
        treats as skip -- so the very next `nx index pdf` of that file
        returned 0 chunks immediately, on every subsequent run, until
        --force. Drives a real pipeline row to that exact state via a
        failing t3.update_chunks call, then asserts a second real
        pipeline_index_pdf call re-enters (and this time succeeds)
        instead of skipping.
        """
        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": ["abc123_0"], "metadatas": [
            {"page_number": 1, "chunk_type": "text", "content_hash": "abc123"}]}
        t3 = create_autospec(T3Database, instance=True)
        t3.get_or_create_collection.return_value = mock_col
        t3.update_chunks.side_effect = [Exception("quota exceeded"), None]

        fr = _er(1)
        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}))

        # Run 1: extraction/chunking/upload all succeed (the row is
        # marked 'completed' by uploader_loop mid-run), then the
        # enrichment post-pass fails.
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(1, fr)
            MC.return_value.chunk.return_value = fc
            first_total = pipeline_index_pdf(Path("/postpass.pdf"), "abc123", "docs__test",
                                             t3, db=db, embed_fn=_embed, corpus="test")
        assert first_total > 0  # chunks WERE uploaded -- only the post-pass failed

        state = db.get_pipeline_state("abc123")
        assert state is not None, "pipeline data must be kept for retry, not deleted"
        assert state["status"] != "completed", (
            f"row wrongly left 'completed' after a failed post-pass: {state['status']!r} "
            f"-- the next create_pipeline() call would skip instead of retrying"
        )

        # Run 2: update_chunks now succeeds. Everything else is already
        # uploaded, so all three stages short-circuit near-instantly on
        # resume and execution reaches the post-pass again for a genuine
        # retry -- this must NOT be a silent 0-chunk skip.
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(1, fr)
            MC.return_value.chunk.return_value = fc
            second_total = pipeline_index_pdf(Path("/postpass.pdf"), "abc123", "docs__test",
                                              t3, db=db, embed_fn=_embed, corpus="test")
        assert second_total > 0, "the retry silently skipped instead of re-entering the pipeline"
        assert t3.update_chunks.call_count == 2, (
            "the retry must actually re-attempt the failed post-pass, not just re-report the old total"
        )

    def test_force_re_embed_true_forwards_true(self, db, mock_t3, writer) -> None:
        """nexus-8143o: pipeline_index_pdf's own force_re_embed kwarg
        reaches the multi-batch writer as force_re_embed=True -- the fix for the ship-blocker where
        --force --re-embed was a no-op on the streaming path (_STREAMING_THRESHOLD=0,
        nearly every real PDF)."""
        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}))
        fr = _er(3)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(3, fr)
            MC.return_value.chunk.return_value = fc
            pipeline_index_pdf(Path("/test/doc2.pdf"), "abc124", "docs__test",
                               mock_t3, db=db, embed_fn=_embed, corpus="test",
                               force_re_embed=True)
        assert writer.instances[0].kwargs["force_re_embed"] is True

    def test_force_re_embed_default_forwards_false(self, db, mock_t3, writer) -> None:
        """force_re_embed defaulting to False must NOT set True on the
        write -- the whole point of this bead's decoupling (the
        deadlock-break *force* flag, tested separately, is orthogonal --
        see pipeline_index_pdf's docstring)."""
        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}))
        fr = _er(3)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(3, fr)
            MC.return_value.chunk.return_value = fc
            pipeline_index_pdf(Path("/test/doc3.pdf"), "abc125", "docs__test",
                               mock_t3, db=db, embed_fn=_embed, corpus="test")
        assert writer.instances[0].kwargs["force_re_embed"] is False

    def test_streaming_register_failure_feeds_identity_drop_collector(self, db, mock_t3, writer) -> None:
        """nexus-2xu6t follow-up (critic round, 2026-08-05): a preflight
        catalog-register exception must feed the nexus-94fxl identity-drop
        collector on the STREAMING path too, not just the non-streaming
        fallback ``tests/test_doc_indexer.py::test_preflight_register_
        failure_feeds_identity_drop_collector`` pins.

        ``_STREAMING_THRESHOLD = 0`` (doc_indexer.py) means every REAL PDF
        that ``index_pdf`` can open with pymupdf routes through THIS
        function (``pipeline_index_pdf``) unconditionally, so the streaming
        path is the forced production route.

        RDR-223 (nexus-z0o2p.11): a chunk is written together with its owner
        row and there is no owner without a catalog document, so a run whose
        registration failed no longer uploads its chunks and reports an
        unindexed document: it raises ``CatalogIdentityMissingError`` BEFORE
        it extracts anything, writes nothing, and the identity-drop collector
        records the document as one that was NOT written (``written=False``).
        This drives ``pipeline_index_pdf`` directly (the routing decision is
        pinned by ``TestStreamingRouting`` in test_doc_indexer.py) with a
        broken catalog writer, proving the ``_register_or_lookup_doc_id``
        swallow (returns ``""``) reaches the refusal.
        """
        from nexus.errors import CatalogIdentityMissingError
        from nexus.mcp_infra import (
            get_manifest_identity_drops,
            reset_manifest_identity_drops,
        )

        reset_manifest_identity_drops()

        reader = MagicMock()
        reader.by_file_path.return_value = None
        reader.by_source_uri.return_value = None
        reader.curator_owner_tumbler_by_name.return_value = "1.99"
        cat_writer = MagicMock()
        cat_writer.register.side_effect = RuntimeError("integrity constraint violation")

        fake_result = _er(2)
        fake_chunks = _tc(
            ("chunk one", 0, {"page_number": 1, "chunk_type": "text"}),
        )

        with patch(_P_EXT) as ME, patch(_P_CHK) as MC, \
             patch("nexus.catalog.factory.make_catalog_reader", return_value=reader), \
             patch("nexus.catalog.factory.make_catalog_writer", return_value=cat_writer):
            ME.return_value.extract.side_effect = _fx(fake_result.metadata["page_count"], fake_result)
            MC.return_value.chunk.return_value = fake_chunks
            with pytest.raises(CatalogIdentityMissingError, match="no catalog document to own"):
                pipeline_index_pdf(
                    Path("/streamreg.pdf"), "streamregfail1", "docs__test",
                    mock_t3, db=db, embed_fn=_embed, corpus="test",
                )
            ME.return_value.extract.assert_not_called()

        # Nothing was written, and no pipeline row was created for the refused run.
        mock_t3.upsert_chunks_with_embeddings.assert_not_called()
        assert writer.instances == []
        assert db.get_pipeline_state("streamregfail1") is None
        # cat_writer.register was reached (the pre-flight call in pipeline_index_pdf) --
        # confirms the broken double was actually on the path, not bypassed.
        cat_writer.register.assert_called()

        drops = get_manifest_identity_drops()
        assert drops and all(d.get("written") is False for d in drops), (
            "a preflight catalog-register exception on the STREAMING path "
            "did not feed the identity-drop collector as a document that was not written "
            "-- nx dt index / nx index pdf would report plain success on this failure when "
            "routed through pipeline_index_pdf"
        )

    def test_first_exc_propagates_even_when_mark_failed_raises(self, db, mock_t3) -> None:
        """nexus-rewgw: the /fail POST's own failure must never mask the
        ORIGINAL pipeline exception nor skip `raise first_exc`. The
        terminal-state bookkeeping (mark_failed + clear_orphan_wal) is
        best-effort; first_exc propagating is load-bearing. Falsified by
        removing the try/except around the bookkeeping in
        pipeline_stages' first_exc block."""
        def _fail_post(*a, **k):
            raise RuntimeError("fail endpoint down: 500")
        db.mark_failed = _fail_post  # type: ignore[method-assign]
        with patch(_P_EXT) as ME, patch(_P_CHK):
            ME.return_value.extract.side_effect = RuntimeError("original boom")
            with pytest.raises(RuntimeError, match="original boom"):
                pipeline_index_pdf(Path("/test.pdf"), "h1", "docs__test", mock_t3,
                                   db=db, embed_fn=lambda t, m: ([], m))

    def test_extractor_failure(self, db, mock_t3) -> None:
        with patch(_P_EXT) as ME, patch(_P_CHK):
            ME.return_value.extract.side_effect = RuntimeError("boom")
            with pytest.raises(RuntimeError, match="boom"):
                pipeline_index_pdf(Path("/test.pdf"), "h1", "docs__test", mock_t3,
                                   db=db, embed_fn=lambda t, m: ([], m))
        s = db.get_pipeline_state("h1")
        assert s["status"] == "failed" and "boom" in s["error"]

    def test_the_stamp_waits_for_the_post_pass_and_a_failed_post_pass_never_stamps(
        self, db, writer, monkeypatch,
    ) -> None:
        """The writer sends every request and leaves the run 'indexing'; the orchestrator stamps it
        after the post-passes. A post-pass that fails leaves it unstamped, so a process killed
        between the last chunk and the enrichment never leaves a complete-looking document that is
        missing its title and author (RDR-223, nexus-z0o2p.11)."""
        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": ["abc123_0"], "metadatas": [
            {"page_number": 1, "chunk_type": "text", "content_hash": "abc123"}]}
        t3 = create_autospec(T3Database, instance=True)
        t3.get_or_create_collection.return_value = mock_col
        t3.update_chunks.side_effect = Exception("quota exceeded")
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(1, _er(1))
            MC.return_value.chunk.return_value = _tc(("chunk 0", 0, {"page_number": 1}))
            pipeline_index_pdf(Path("/postpass.pdf"), "abc123", "docs__test", t3,
                               db=db, embed_fn=_embed, corpus="test")
        (w,) = writer.instances
        assert w.finished, "every request was sent"
        assert not w.completed and ("complete",) not in writer.events

    def test_a_post_pass_retry_with_nothing_left_to_upload_stamps_without_a_writer(
        self, db, writer, monkeypatch,
    ) -> None:
        """The retry of a run whose post-pass failed finds every chunk already flagged uploaded: no
        writer runs, and the orchestrator stamps the run through the fail-closed helper (see
        ``tests/test_rdr223_pdf_resume_tail.py`` for what that helper does on a missing route or a
        transport failure)."""
        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": ["abc123_0"], "metadatas": [
            {"page_number": 1, "chunk_type": "text", "content_hash": "abc123"}]}
        t3 = create_autospec(T3Database, instance=True)
        t3.get_or_create_collection.return_value = mock_col
        t3.update_chunks.side_effect = [Exception("quota exceeded"), None]
        calls: list[tuple] = []
        monkeypatch.setattr("nexus.doc_indexer._stamp_finished_upload", lambda *a: calls.append(a))
        # The document's fence as the earlier run left it: begun for this content, never stamped.
        # (The writer here is a recorder, so the real engine's fence was never begun.)
        monkeypatch.setattr("nexus.doc_indexer._index_fence_state", lambda doc_id: ("indexing", "abc123"))

        def _run() -> int:
            with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
                ME.return_value.extract.side_effect = _fx(1, _er(1))
                MC.return_value.chunk.return_value = _tc(("chunk 0", 0, {"page_number": 1}))
                return pipeline_index_pdf(Path("/postpass.pdf"), "abc123", "docs__test", t3,
                                          db=db, embed_fn=_embed, corpus="test")

        _run()
        assert calls == [], "the failed post-pass stamped nothing"
        assert _run() == 1
        assert len(writer.instances) == 1, "the retry had nothing to send and made no writer"
        assert len(calls) == 1 and calls[0][1:] == ("abc123", 1)

    @pytest.mark.parametrize("counter", [4, 0], ids=["counter-current", "counter-lagging"])
    def test_a_resume_after_a_killed_upload_restarts_fresh_in_the_same_invocation(
        self, db, mock_t3, writer, counter, monkeypatch,
    ) -> None:
        """An earlier process flagged 4 of 6 chunks uploaded and died with its writer's state. The
        resumed run must not send the other 2 alone (that would replace the manifest with a
        fragment and sweep the head as superseded): it discards the buffer and re-runs the document
        from scratch in this same invocation, with no failed run in between.

        ``counter=0`` is the kill that lands after the flag and before the buffered progress counter
        flushed (the counter lags by up to a poll interval): the rows say 4 chunks went out, the
        counter says none. The decision reads the rows."""
        _bound_the_polling(monkeypatch)
        _pop_chunks(db, "h1", 6)
        db.mark_uploaded("h1", [0, 1, 2, 3])
        if counter:
            db.update_progress("h1", chunks_uploaded=counter)
        db.mark_failed("h1", error="killed")
        six = _tc(*[(f"chunk {i} text", i, {"page_number": 1}) for i in range(6)])

        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(3)
            MC.return_value.chunk.return_value = six
            n = pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3,
                                   db=db, embed_fn=_embed, corpus="test")

        assert n == 6
        (w,) = writer.instances
        assert sum(len(r) for r, _ in w.batches) == 6, "the whole document went through one writer"
        assert [r["position"] for rows, _ in w.batches for r in rows] == list(range(6)), \
            "never a tail alone: positions start at 0"
        assert db.get_pipeline_state("h1") is None, "the run finished and cleaned up its buffer"

    def test_a_resume_of_a_dry_run_leaves_the_buffer_alone(self, db, mock_t3) -> None:
        """A dry run writes no catalog and sends no request, so a partial buffer is no hazard to it
        and it must not wipe a real run's extraction work."""
        _pop_chunks(db, "h1", 6)
        db.mark_uploaded("h1", [0, 1, 2, 3])
        db.update_progress("h1", chunks_uploaded=4)
        db.mark_failed("h1", error="killed")
        cleared: list[str] = []
        real_clear = db.clear_orphan_wal
        db.clear_orphan_wal = lambda ch: (cleared.append(ch), real_clear(ch))  # type: ignore[method-assign]
        six = _tc(*[(f"chunk {i} text", i, {"page_number": 1}) for i in range(6)])

        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(3)
            MC.return_value.chunk.return_value = six
            pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3,
                               db=db, embed_fn=_embed, corpus="test", dry_run=True)

        assert cleared == []

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
                # nexus-1oguj: the post-pass also backfills the extractor
                # identity from ExtractionResult.metadata once extraction
                # completes — ``_er()`` defaults to "docling".
                assert m["extraction_method"] == "docling"
                break
        else:
            pytest.fail("metadata enrichment post-pass not called")

    def test_metadata_enrichment_postpass_clears_stale_quality_gate_override(self, db) -> None:
        """nexus-w94eo: the engine now MERGES metadata rather than replacing
        it, so a healthy re-index (this run's extraction did NOT trip the
        quality gate) must actively request removal of a stale
        ``quality_gate_overridden`` from an earlier degraded run — omitting
        the key from this write's dict is no longer enough to clear it."""
        fr = _er(1)
        fr.metadata["docling_title"] = "My Paper Title"
        t3, _ = _run_with_col(
            db,
            col_get_return={"ids": ["abc_0"], "metadatas": [{"content_hash": "abc123"}]},
            fake_result=fr,
            fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"})),
            pdf_path="/paper.pdf")
        for call in t3.update_chunks.call_args_list:
            args, kwargs = call
            if args and args[0] == "docs__test" and any(
                m.get("title") == "My Paper Title" for m in args[2]
            ):
                assert kwargs.get("delete_keys") == ["quality_gate_overridden"], (
                    f"a healthy re-index must clear a stale override; got kwargs={kwargs!r}"
                )
                assert "quality_gate_overridden" not in args[2][0], (
                    "a healthy run must not itself stamp quality_gate_overridden"
                )
                break
        else:
            pytest.fail("metadata enrichment post-pass not called")

    def test_metadata_enrichment_postpass_sets_quality_gate_override_no_delete(self, db) -> None:
        """The inverse of the test above: when THIS run's extraction DID trip
        the quality gate, the post-pass sets the key (not delete_keys)."""
        fr = _er(1)
        fr.metadata["docling_title"] = "My Paper Title"
        fr.metadata["quality_gate_overridden"] = True
        t3, _ = _run_with_col(
            db,
            col_get_return={"ids": ["abc_0"], "metadatas": [{"content_hash": "abc123"}]},
            fake_result=fr,
            fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"})),
            pdf_path="/paper.pdf")
        for call in t3.update_chunks.call_args_list:
            args, kwargs = call
            if args and args[0] == "docs__test" and any(
                m.get("title") == "My Paper Title" for m in args[2]
            ):
                assert args[2][0]["quality_gate_overridden"] is True
                assert kwargs.get("delete_keys") == [], (
                    f"a degraded run must not request its own key's deletion; got kwargs={kwargs!r}"
                )
                break
        else:
            pytest.fail("metadata enrichment post-pass not called")

    def test_stale_chunk_pruning_post_pass_removed_as_dead_code(self, db) -> None:
        """nexus-tbkk1: the stale-chunk-pruning post-pass (formerly
        ``_prune_stale_chunks``, called from ``pipeline_index_pdf`` after
        the metadata-enrichment and table_regions post-passes) is DELETED
        dead code, not merely runtime-unreachable.

        This test replaces ``test_stale_chunk_pruning`` and
        ``test_stale_chunk_pruning_kept_when_shared_with_another_
        document`` (nexus-tp8yk D3), which asserted the prune's union
        guard correctly distinguished a genuinely-orphaned stale chash
        from one still referenced by another live document. Both tests
        only ever passed because their ``MagicMock col.get`` ignored the
        ``where=`` argument entirely and served the seeded stale row
        regardless — a green suite that was never evidence the where
        clause the production code actually issues
        (``{"source_path": pdf_path}``, via ``nexus.doc_indexer.
        _identity_where``) matches anything real. RDR-102 D2
        (2026-05-02) removed ``source_path`` from ``make_chunk_metadata``
        entirely, so it never does; the prune's real chunk-fetch query
        was permanently a zero-row no-op in production, which is why
        nexus-tbkk1 deletes it outright rather than "fixing" the where
        clause.

        The union-guard LOGIC these superseded tests exercised
        (``indexer_utils.orphaned_chashes``) is untouched by this
        deletion and remains covered by tests/db/test_http_catalog_
        integration.py::TestPruneUnionGuard. Its thin wrapper
        ``prune_orphan_candidates`` — built specifically for this call
        site and its three siblings, all now deleted — was ALSO deleted
        in this same fix round (zero production callers survived; see
        ``nexus.indexer_utils``'s deletion comment), along with its
        now-pointless dedicated test file. The real cross-document prune
        protection (``mcp_infra._sweep_superseded_vectors``, which calls
        ``orphaned_chashes`` directly) is proven at tests/integration/
        test_tp8yk_manifest_never_outruns_chunks.py::test_union_guard_
        keeps_shared_chunk_at_the_production_wiring.

        Kill control: seed a legacy-shaped stale row (mismatched
        content_hash, source_path metadata) that the OLD post-pass would
        have deleted. If the post-pass call were reintroduced, ``col.
        delete`` would fire and this assertion would fail.
        """
        with patch(
            "nexus.catalog.factory.make_catalog_reader",
            return_value=MagicMock(docs_for_chashes=MagicMock(return_value={})),
        ):
            _, col = _run_with_col(
                db,
                col_get_return={"ids": ["abc123_0", "abc123_1", "old_hash_0"], "metadatas": [
                    {"content_hash": "abc123_full", "source_path": "/a.pdf"},
                    {"content_hash": "abc123_full", "source_path": "/a.pdf"},
                    {"content_hash": "previous_hash", "source_path": "/a.pdf"}]},
                fake_result=_er(1),
                fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"})),
                content_hash="abc123_full")
        col.delete.assert_not_called()

    def test_conflict_already_running(self, db, mock_t3) -> None:
        """nexus-lcmbp: a retry against a fresh-heartbeat 'running' row is
        a LOUD failure — never a silent 0-chunk success."""
        db.create_pipeline("h1", "/a.pdf", "docs__test")
        with pytest.raises(PipelineConflictRunning):
            pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db)
        mock_t3.upsert_chunks_with_embeddings.assert_not_called()

    # nexus-sghyo (2026-08-06): test_embed_fn_none_resolves_credentials
    # DELETED — it proved that, given a valid voyage credential, the
    # legacy non-service embed path succeeded via
    # doc_indexer._embed_with_fallback. That whole path is retired
    # outright regardless of credential (Hal determination 2026-07-28:
    # "we do no embedding on the client") — no surviving subject. See
    # test_embed_fn_none_no_credentials_fails_fast below, which now
    # covers the (only reachable) unconditional-raise behavior.

    def test_embed_fn_none_no_credentials_fails_fast(self, db, mock_t3) -> None:
        # nexus-sghyo: non-service embedding is retired unconditionally
        # now, not merely credential-gated — the client no longer embeds
        # via Voyage at all.
        with (patch("nexus.db.http_vector_client.is_vector_service_mode",
                    return_value=False),
              patch("nexus.config.get_credential", side_effect=fake_credentials(None))):
            with pytest.raises(RuntimeError, match="non-service embedding was retired"):
                pipeline_index_pdf(Path("/a.pdf"), "h1", "docs__test", mock_t3, db=db)

    def test_streaming_pdf_does_not_emit_source_path(
        self, db, tmp_path, monkeypatch, writer,
    ) -> None:
        """RDR-102 Phase B / D2: pipeline_stages._build_chunk_metadata
        (the make_chunk_metadata call inside the streaming chunker_loop)
        must drop source_path from its kwargs. The streaming PDF write path
        was missed in the original RDR-102 draft and added at the
        substantive-critic gate (RF-4 row 4). Without this drop, every PDF
        indexed via the streaming pipeline would continue regressing
        source_path post-Phase-B. The chunks' metadata is what the multi-batch
        writer sends (RDR-223), so it is read from the writer's payload.
        """
        from nexus.db.t3 import T3Database

        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)
        monkeypatch.setattr(
            "nexus.config._global_config_path",
            lambda: Path("/nonexistent"),
        )

        pdf_path = tmp_path / "stream_b.pdf"
        pdf_path.write_bytes(b"fake pdf bytes for Phase B streaming test")
        client = make_vector_test_client()
        t3 = T3Database(_client=client, local_mode=True)

        fc = _tc(
            ("chunk B0", 0, {"page_number": 1, "chunk_type": "text",
                              "chunk_start_char": 0, "chunk_end_char": 8}),
            ("chunk B1", 1, {"page_number": 2, "chunk_type": "text",
                              "chunk_start_char": 8, "chunk_end_char": 16}),
        )
        fr = _er(2)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(2, fr)
            MC.return_value.chunk.return_value = fc
            pipeline_index_pdf(
                pdf_path, "rdr102streamhash_b", f"docs__rdr102-stream-b__{_local_token()}__v1",
                t3, db=db, embed_fn=_embed,
                corpus="rdr102_stream_b",
            )

        metas = [c["metadata"] for w in writer.instances for _, chunks in w.batches for c in chunks]
        assert metas, "expected chunks to land"
        leaked = [m for m in metas if "source_path" in m]
        assert not leaked, (
            f"{len(leaked)}/{len(metas)} streaming-pipeline "
            f"chunks still carry source_path. Phase B must drop "
            f"source_path=pdf_path from _build_chunk_metadata "
            f"— the streaming PDF write path."
        )

    def test_writes_doc_id_when_catalog_initialized(
        self, db, tmp_path, monkeypatch, writer,
    ) -> None:
        """RDR-108 Phase 3: pipeline_index_pdf no longer stamps ``doc_id``
        on chunks. The streaming pipeline registers the catalog Document at
        the entry boundary and (RDR-223) writes the chunks together with
        that document's manifest rows. Verify chunks lack doc_id and the
        write names the registered Document, one row per chunk at its
        global position.
        """
        from nexus.db.t3 import T3Database

        monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
        monkeypatch.delenv("CHROMA_API_KEY", raising=False)
        monkeypatch.setattr(
            "nexus.config._global_config_path",
            lambda: Path("/nonexistent"),
        )

        pdf_path = tmp_path / "stream.pdf"
        pdf_path.write_bytes(b"fake pdf bytes for streaming pipeline test")
        client = make_vector_test_client()
        t3 = T3Database(_client=client, local_mode=True)

        fc = _tc(
            ("chunk 0 text", 0, {"page_number": 1, "chunk_type": "text",
                                  "chunk_start_char": 0, "chunk_end_char": 12}),
            ("chunk 1 text", 1, {"page_number": 2, "chunk_type": "text",
                                  "chunk_start_char": 12, "chunk_end_char": 24}),
        )
        fr = _er(2)
        collection = f"docs__rdr102-stream__{_local_token()}__v1"
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC:
            ME.return_value.extract.side_effect = _fx(2, fr)
            MC.return_value.chunk.return_value = fc
            total = pipeline_index_pdf(
                pdf_path, "rdr102streamhash", collection,
                t3, db=db, embed_fn=_embed,
                corpus="rdr102_stream",
            )
        assert total == 2, f"expected 2 chunks uploaded; got {total}"

        (w,) = writer.instances
        (rows, chunks), = w.batches
        assert len(chunks) == 2
        # Phase 3: no doc_id on chunk metadata.
        for c in chunks:
            assert "doc_id" not in c["metadata"]

        # nexus-aqbrk: pipeline_index_pdf registers via the factory.
        documents = active_reader().list_by_collection(collection)
        assert documents, "catalog must register a Document for the streaming PDF"
        assert w.kwargs["doc_id"] in {str(e.tumbler) for e in documents}
        assert [r["position"] for r in rows] == [0, 1]
        assert [r["chash"] for r in rows] == [c["chash"] for c in chunks]

    def test_keyboard_interrupt_stops_stages_and_does_not_complete(self, db, mock_t3) -> None:
        """nexus-6m9zy.3 (#4): a KeyboardInterrupt landing in the
        orchestrator's wait() call never set cancel -- the three stage
        loops ran to completion via ThreadPoolExecutor.__exit__'s
        shutdown(wait=True), and the uploader's own resume-completion
        check marked the pipeline row 'completed' out from under the
        interrupted caller. Every later run of that file then hit
        create_pipeline's 'completed' -> skip path and reported 0 chunks
        until --force.

        Injects the interrupt by making the orchestrator's own `wait()`
        call raise KeyboardInterrupt directly, rather than firing a real
        signal/`_thread.interrupt_main()` on a timer: this environment's
        `_thread.interrupt_main()` does not preempt a thread blocked in
        an indefinite `concurrent.futures.wait()` -- the pending
        interrupt is only observed once that call returns on its own
        (confirmed empirically: an `Event.wait()` with nothing ever
        setting it is never interrupted by `_thread.interrupt_main()` on
        a timer). Mocking `wait()` itself is deterministic and, since the
        three stage futures are real and still running when it fires,
        exercises exactly the code path a genuinely-preempted wait()
        would take.
        """
        pages_done: list[int] = []

        def slow_extract(pdf_path, *, extractor="auto", on_formula_oom="fail", on_page=None, allow_degraded=False):
            for i in range(8):
                time.sleep(0.05)
                on_page(i, f"page {i} text.", {"page_number": i + 1, "text_length": 12})
                pages_done.append(i)
            return _er(8)

        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}))

        with pytest.raises(KeyboardInterrupt):
            with patch(_P_EXT) as ME, patch(_P_CHK) as MC, \
                 patch("nexus.pipeline_stages.wait", side_effect=KeyboardInterrupt):
                ME.return_value.extract.side_effect = slow_extract
                MC.return_value.chunk.return_value = fc
                pipeline_index_pdf(Path("/sigint.pdf"), "hK", "docs__test",
                                   mock_t3, db=db, embed_fn=_embed, corpus="test")

        assert len(pages_done) < 8, "extraction ran to completion despite the interrupt -- cancel was never set"
        state = db.get_pipeline_state("hK")
        assert state is not None
        assert state["status"] != "completed", f"pipeline row wrongly marked completed: {state['status']!r}"
        assert db.create_pipeline("hK", "/sigint.pdf", "docs__test") != "skip", (
            "a later run must not silently skip the file"
        )


class TestPipelineIndexPdfDryRun:
    """nexus-uxg4u: dry_run must gate every catalog/T2 write this
    function makes on its own account (the fallback pre-flight
    registration for a direct caller, the completion fence, and the
    catalog hook) -- this is the streaming route the observed bug's
    fence refusal (IndexRunVerifyRefused, claimed_chunk_count=84) came
    through when called via doc_indexer.index_pdf."""

    def test_dry_run_never_registers_or_touches_catalog(self, db, mock_t3) -> None:
        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}))
        fr = _er(1)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC, \
                patch("nexus.doc_indexer._register_or_lookup_doc_id") as mock_register, \
                patch("nexus.doc_indexer._fence_begin") as mock_begin, \
                patch("nexus.doc_indexer._fence_complete") as mock_complete, \
                patch("nexus.doc_indexer._fence_fail") as mock_fail, \
                patch("nexus.pipeline_stages._catalog_pdf_hook") as mock_hook:
            ME.return_value.extract.side_effect = _fx(1, fr)
            MC.return_value.chunk.return_value = fc
            total = pipeline_index_pdf(
                Path("/dry-run.pdf"), "dryrun123", "docs__test",
                mock_t3, db=db, embed_fn=_embed, corpus="test", dry_run=True,
            )
        assert total == 1
        mock_register.assert_not_called()
        mock_begin.assert_not_called()
        mock_complete.assert_not_called()
        mock_fail.assert_not_called()
        mock_hook.assert_not_called()

    def test_dry_run_with_default_hooks_uploader_loop_fires_zero_hooks(
        self, db, mock_t3,
    ) -> None:
        """nexus-uxg4u round 2 (Critical, both reviewers): uploader_loop
        (Stage 3, spawned via pool.submit) fires hooks.fire_batch/
        fire_single unconditionally on every uploaded batch -- dry_run
        was not even in its signature. install_default_hooks wires a
        REAL T2 aspect-extraction-queue write (aspect_extraction_
        enqueue_hook is a document hook, not reached from uploader_loop
        directly, but manifest_write_batch_hook / taxonomy_assign_
        batch_hook ARE batch hooks fired straight from here) -- proven
        with hooks=None (a real default registry), not the caller's own
        empty one."""
        fc = _tc(("chunk 0", 0, {"page_number": 1, "chunk_type": "text"}))
        fr = _er(1)
        with patch(_P_EXT) as ME, patch(_P_CHK) as MC, \
                patch("nexus.mcp_infra.manifest_write_batch_hook") as mock_manifest, \
                patch("nexus.mcp_infra.taxonomy_assign_batch_hook") as mock_taxonomy:
            ME.return_value.extract.side_effect = _fx(1, fr)
            MC.return_value.chunk.return_value = fc
            total = pipeline_index_pdf(
                Path("/dry-run-hooks.pdf"), "dryrunhooks123", "docs__test",
                mock_t3, db=db, embed_fn=_embed, corpus="test", dry_run=True,
                hooks=None,
            )
        assert total == 1
        mock_manifest.assert_not_called()
        mock_taxonomy.assert_not_called()


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
    # Identity / spans / position — RDR-102 D2 dropped source_path.
    # RDR-108 Phase 3 (nexus-bdag) dropped chunk_index, chunk_count, doc_id
    # in favour of the catalog ``document_chunks`` manifest.
    "content_hash", "chunk_text_hash",
    "chunk_start_char", "chunk_end_char", "line_start", "line_end", "page_number",
    # Display / routing — RDR-101 Phase 5c (nexus-o6aa.13) dropped
    # ``store_type``, ``corpus``, ``git_meta``. ``title``/``source_author``
    # are NOT required here (nexus-w94eo): both are unknown at streaming
    # chunk-time and are omitted rather than stamped as "" placeholders —
    # see test_streaming_metadata_omits_title_and_source_author below.
    "section_title", "section_type", "tags", "category",
    "content_type", "embedding_model",
    # Lifecycle. ``frecency_score`` is NOT required (nexus-vhyar): the stub
    # omits its 0.0 default so a late duplicate cannot reset a score the
    # frecency-only reindex bumped; readers default a missing score to 0.0.
    "indexed_at", "ttl_days", "source_agent", "session_id",
    # bib_* intentionally omitted (drop-when-empty by normalize)
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
                     corpus="mycorpus", target_model="model-ctx")
    meta = json.loads(db.read_ready_chunks("h1")[0]["metadata_json"])
    assert _REQUIRED_META - set(meta.keys()) == set()
    # nexus-w94eo: title/source_author must be ABSENT, not present as "".
    # See test_streaming_metadata_omits_title_and_source_author for the
    # dedicated regression test and the full rationale.
    assert "title" not in meta
    assert "source_author" not in meta


def test_streaming_metadata_omits_title_and_source_author(db, done_event) -> None:
    """nexus-w94eo: the streaming uploader's chunk-time stub must not stamp
    ``title``/``source_author`` as empty-string placeholders.

    Diagnosis (T2 nexus/nexus-w94eo-diagnosis): the engine used to REPLACE a
    chunk's metadata wholesale on every write. A late-committing duplicate of
    THIS chunk-time write (the gateway-504-retry shape the diagnosis traced)
    landing after the post-extraction enrichment post-pass would revert
    title/extraction_method back to this stub's payload. The engine now
    MERGES metadata instead (this bead's other half, in
    ``PgVectorRepository``), so a stale duplicate can no longer overwrite a
    key it doesn't carry — but that only helps for keys this stub never
    sends. An explicit ``title=""`` here would still be a real value the
    merge could re-assert. Omitting the key entirely closes the gap.
    """
    db.create_pipeline("h1", "/doc.pdf", "docs__test")
    db.write_page("h1", 0, "Some text here.", metadata={"page_number": 1, "text_length": 15})
    db.update_progress("h1", total_pages=1, pages_extracted=1)
    with patch(_P_CHK) as MC:
        MC.return_value.chunk.return_value = _tc(
            ("chunk text", 0, {"page_number": 1, "chunk_type": "text",
                               "chunk_start_char": 0, "chunk_end_char": 10}))
        chunker_loop("h1", db, threading.Event(), embed_fn=_embed,
                     extraction_done=done_event, pdf_path="/doc.pdf",
                     corpus="mycorpus", target_model="model-ctx")
    meta = json.loads(db.read_ready_chunks("h1")[0]["metadata_json"])
    assert "title" not in meta, (
        f"streaming chunk-time metadata must omit title, not stamp it \"\"; got {meta!r}"
    )
    assert "source_author" not in meta, (
        f"streaming chunk-time metadata must omit source_author, not stamp it \"\"; got {meta!r}"
    )


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

    def _tag(m: dict) -> bool:
        m["chunk_type"] = "table_page"
        return True

    assert _update_chunk_metadata(t3, col, "docs__test", "abc123", _tag) is expected


def test_update_chunk_metadata_writes_only_the_changed_keys() -> None:
    """nexus-vhyar: the post-pass sends the keys update_fn changed, not the row
    it read. Echoing the read-back row re-asserted stale values over any write
    that committed between the read and this write (the engine merges)."""
    t3, col = MagicMock(), MagicMock()
    col.get.return_value = {
        "ids": ["id1", "id2"],
        "metadatas": [
            {"chunk_type": "text", "title": "stale", "page_number": 3},
            {"chunk_type": "table_page", "title": "stale", "page_number": 3},
        ],
    }

    def _tag(m: dict) -> bool:
        if m["chunk_type"] == "table_page":
            return False
        m["chunk_type"] = "table_page"
        return True

    assert _update_chunk_metadata(t3, col, "docs__test", "abc123", _tag) is True
    t3.update_chunks.assert_called_once_with("docs__test", ["id1"], [{"chunk_type": "table_page"}])


# nexus-tbkk1: test_prune_stale_chunks (a parametrized unit test of the
# query/delete pagination mechanics of nexus.pipeline_stages.
# _prune_stale_chunks) DELETED along with the function it tested — see
# test_stale_chunk_pruning_post_pass_removed_as_dead_code above for the
# full rationale and the tests that still cover the surviving, unrelated
# machinery (indexer_utils.orphaned_chashes union guard, its now-deleted
# prune_orphan_candidates wrapper's fate; mcp_infra._sweep_superseded_
# vectors real protection).


def test_pipeline_data_kept_on_enrichment_failure(db) -> None:
    t3, col = MagicMock(), MagicMock()
    col.get.return_value = {"ids": ["id1"], "metadatas": [{"content_hash": "h1"}]}
    t3.update_chunks.side_effect = Exception("quota exceeded")
    ro = MagicMock(spec=ExtractionResult)
    ro.text, ro.metadata, ro.title = "some text", {"page_count": 1, "docling_title": "Test"}, "Test"
    assert _enrich_metadata_from_extraction("h1", ro, Path("/test.pdf"), t3, col, "docs__test") is False


def test_enrich_metadata_title_override_wins_over_resolve_pdf_title() -> None:
    """nexus-1uov1: the streaming path's post-pass is the OTHER site that
    stamps every chunk's title (per-chunk metadata is discarded during
    flush, so title is corrected here after upload) — title_override
    must win here exactly as it does in doc_indexer._pdf_chunks's
    small-document path."""
    t3, col = MagicMock(), MagicMock()
    col.get.return_value = {"ids": ["id1"], "metadatas": [{"content_hash": "h1"}]}
    ro = MagicMock(spec=ExtractionResult)
    ro.text, ro.metadata = "some text", {"page_count": 1, "docling_title": "Fragment Only"}
    override = "Self-Aware Vector Embeddings for Retrieval-Augmented Generation"

    assert _enrich_metadata_from_extraction(
        "h1", ro, Path("/test.pdf"), t3, col, "docs__test", title_override=override,
    ) is True

    written_metas = t3.update_chunks.call_args[0][2]
    assert all(m["title"] == override for m in written_metas)


class TestStreamingCatalogHookTitle:
    """The streaming tail used to read ``metadata["title"]`` / ``["author"]``
    — keys no extractor writes (they are ``docling_title`` / ``pdf_title`` /
    ``pdf_author``) — so every streamed PDF reached ``_catalog_pdf_hook``
    with ``title=stem, author=""``. Resolve through the one shared chain
    (``resolve_pdf_title``) and the real author key (2026-08-19,
    papers/2512.11001.pdf registered as "2512.11001")."""

    def test_hook_receives_resolved_title_and_author(self, db) -> None:
        fr = _er(1)
        fr.metadata["docling_title"] = "My Paper Title"
        fr.metadata["pdf_author"] = "Jane Doe"
        with patch("nexus.pipeline_stages._catalog_pdf_hook") as hook:
            _run_with_col(
                db,
                col_get_return={"ids": ["abc_0"], "metadatas": [
                    {"title": "", "content_hash": "abc123", "page_number": 1}]},
                fake_result=fr,
                fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"})),
                pdf_path="/paper.pdf")
        assert hook.call_count == 1
        kw = hook.call_args.kwargs
        assert kw["title"] == "My Paper Title"
        assert kw["author"] == "Jane Doe"

    def test_hook_falls_back_to_h1_when_extractor_titles_empty(self, db) -> None:
        """MinerU path: both metadata titles empty, markdown opens with # H1."""
        fr = _er(1)
        fr.text = "# Rethinking Query Optimization\n\n" + fr.text
        fr.metadata["docling_title"] = ""
        fr.metadata["pdf_title"] = ""
        with patch("nexus.pipeline_stages._catalog_pdf_hook") as hook:
            _run_with_col(
                db,
                col_get_return={"ids": ["abc_0"], "metadatas": [
                    {"title": "", "content_hash": "abc123", "page_number": 1}]},
                fake_result=fr,
                fake_chunks=_tc(("c0", 0, {"page_number": 1, "chunk_type": "text"})),
                pdf_path="/2512.11001.pdf")
        assert hook.call_args.kwargs["title"] == "Rethinking Query Optimization"
