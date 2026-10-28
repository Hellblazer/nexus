# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Pipeline stage functions for streaming PDF indexing (RDR-048).

Three concurrent stages connected by the engine-backed pipeline buffer
(``HttpPipelineDB`` over ``nexus.pdf_pipeline`` — RDR-186 .16; the local
``pipeline.db`` SQLite buffer is retired):

1. **extractor_loop** — extracts pages → ``pdf_pages`` buffer
2. **chunker_loop** — polls pages, chunks stable prefix, embeds → ``pdf_chunks``
3. **uploader_loop** — reads embedded chunks, writes them to T3 with their owner rows (RDR-223)

After all three stages complete, the orchestrator runs post-passes to:
- Enrich chunk metadata from the ExtractionResult (title, author, etc.)
- Tag table-page chunks (table_regions post-pass)
- Correct chunk_count to the final total
- Prune stale chunks from a previous version
"""
from __future__ import annotations

import hashlib

from nexus.chunk_identity import chunk_id as _chunk_id
import json
import struct
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait, ALL_COMPLETED, FIRST_EXCEPTION
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.catalog.types import ManifestRow
    from nexus.hook_registry import HookRegistry

import structlog

from nexus.config import DEFAULT_EXTRACTOR
from nexus.embed_window import window_for_collection
from nexus.errors import UploadStalledError
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import (
    ExtractionResult,
    MineruSystemicFailure,
    PDFExtractor,
    bind_run_cancel,
    stop_mineru_workers,
)
from nexus.db.http_pipeline_client import HttpPipelineDB, PipelineRunFenced
from nexus.retry import _vector_with_retry
from nexus.util.termination import TerminationRequested, sigterm_raises

_log = structlog.get_logger(__name__)

#: The orchestrator waits on its stages in slices this long, so a pending signal handler runs
#: within one slice: CPython runs Python signal handlers on the main thread only, and an untimed
#: wait there need not wake when the OS delivered the signal to a stage thread.
_STAGE_WAIT_SLICE_S = 1.0

_UPLOAD_BATCH_SIZE = 128  # Conservative vs ChromaDB's 300 limit — matches _INCREMENTAL_BATCH_SIZE in doc_indexer
_EMBED_BATCH_SIZE = 32  # Smaller than batch path (128) — favours heartbeat freshness in streaming
# 2.0s (was 0.1 against local SQLite): each poll-driven read now flushes
# buffered writes and issues an HTTP GET, so the cadence matches the
# aspect_worker's 2s idle poll. Governs the uploader's wait-for-chunks
# sleep and the chunker's no-event fallback; the chunker's primary wait is
# extraction_done.wait(timeout=0.5) (always constructed in the real
# pipeline_index_pdf path). Latency cost is bounded by a few poll cycles
# per ingest; extraction dominates wall-clock.
_POLL_INTERVAL = 2.0

#: How many polls the uploader waits, with the chunker finished and no chunk taken, before it warns
#: and before it fails the record (nexus-gjxkb). Counted in polls, not wall-clock, so a test counts
#: calls; 30 and 150 polls are 60 s and 300 s at the 2 s interval. The only legitimate cause of the
#: wait is another thread's chunk flush still in flight (``HttpPipelineDB`` documents it): one HTTP
#: POST, seconds at most. A minute is far past that, so the warning means something is wrong; five
#: minutes leaves room for a slow engine under load before the record is failed instead of the run
#: held open for good.
_STALL_WARN_POLLS = 30
_STALL_FAIL_POLLS = 150

EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]


class PipelineCancelled(Exception):
    """Raised inside on_page to abort extraction when cancel is set."""


# ── Stage 1: Extractor ──────────────────────────────────────────────────────


def _page_rungs(page_metadata: Any, page_index: int) -> dict[int, str]:
    """``{1-based page: rung}`` for one page write, from the ``page_method`` the ladder puts in
    the page metadata (RDR-237). ``{}`` when the metadata carries none (a non-ladder extractor, or
    a buffer row written before the ladder recorded it). *page_metadata* is the dict ``on_page``
    gets or a buffer row's ``metadata_json`` (a JSON string or an already-decoded dict).
    """
    if isinstance(page_metadata, str):
        try:
            page_metadata = json.loads(page_metadata)
        except ValueError:
            return {}
    if not isinstance(page_metadata, dict):
        return {}
    method = page_metadata.get("page_method")
    if not isinstance(method, str) or not method:
        return {}
    numbers = page_metadata.get("page_numbers") or [page_metadata.get("page_number", page_index + 1)]
    return {int(n): method for n in numbers if isinstance(n, int) and not isinstance(n, bool)}


#: The document methods the ladder reports; the only ones a resume recomputes.
_LADDER_METHODS = ("mineru", "mineru+degraded")

#: The rungs the explicit ``pymupdf`` extractor produces; a buffered page on any other rung is MinerU's.
_TEXT_LAYER_RUNGS = ("pymupdf-text", "unextracted")


def _buffer_provenance(rows: list[dict[str, Any]]) -> tuple[dict[int, str], list[int]]:
    """``({1-based page: rung}, [pages with no rung])`` for the buffer rows a resume found.

    Reads only ``page_index`` and ``metadata_json``, so a caller can drop the rows (page text
    included) as soon as this returns. A page is *unknown* when its row carries no ``page_method``:
    a buffer an older build wrote, or a non-ladder extractor's.
    """
    rungs: dict[int, str] = {}
    unknown: list[int] = []
    for row in rows:
        index = int(row.get("page_index", 0))
        found = _page_rungs(row.get("metadata_json"), index)
        if found:
            rungs.update(found)
        else:
            unknown.append(index + 1)
    return rungs, unknown


def _merge_buffered_provenance(
    metadata: dict[str, Any], run_rungs: dict[int, str], buffered_rungs: dict[int, str],
    unknown_pages: list[int] | tuple[int, ...] = (),
) -> None:
    """Make a resumed run's ``extraction_pages`` and ``extraction_method`` describe the document.

    A kept-buffer resume re-extracts every page but writes only those the buffer lacks
    (``extractor_loop``'s ``on_page`` skip), so the chunks come from the BUFFER's text and the
    ladder's own map describes the current run's attempt, not what was indexed. The rung a page
    settled on is stored in its buffer row (``page_method``); that rung wins for a page the buffer
    holds, the current run's for the rest.

    *unknown_pages* are buffered pages whose row names no rung (a buffer an older build wrote).
    Their indexed text is the buffer's and the run's rung for them may not describe it, so the
    record cannot prove them clean: the map lists only the pages whose rung is known (an unknown
    page is left out, never given the run's rung), and a ladder run's method stays
    ``mineru+degraded`` rather than claiming a clean ``mineru``. No-op when the buffer
    recorded no rung and no page is unknown (a first run, a non-ladder extractor): the run's own
    map then stands.

    The explicit ``pymupdf`` extractor (RDR-237) is handled the same way, with one rule of its
    own: ``pymupdf`` names a document whose every indexed page is the PDF's text layer (or
    nothing). :func:`pipeline_index_pdf` therefore never resumes a ``pymupdf`` run over a buffer that
    holds a MinerU page or a page that names no rung: :func:`_discard_buffer_for_text_only` wipes that
    buffer before any stage starts (nexus-tr1rj.14), so such a buffer never reaches this merge. The
    branch for it stays as a backstop for a caller that skips the wipe: the document is then a
    mixture and the method becomes ``mineru+degraded`` (its pages below MinerU's first rung are in
    the map, MinerU's own pages are not). A buffer of text-layer rungs only (an interrupted
    ``pymupdf`` run) stays ``pymupdf``.
    """
    method = metadata.get("extraction_method")
    ladder = method in _LADDER_METHODS
    text_only = method == "pymupdf"
    unknown = bool(unknown_pages) and (ladder or text_only)
    if not buffered_rungs and not unknown:
        return
    from nexus.pdf_extractor import _DEGRADED_METHODS  # noqa: PLC0415 — module-private rung order, shared with the ladder
    unproven = set(unknown_pages) if unknown else set()
    rungs = {**{n: r for n, r in run_rungs.items() if n not in unproven}, **buffered_rungs}
    pages = {
        method: sorted(n for n, r in rungs.items() if r == method)
        for method in _DEGRADED_METHODS
        if any(r == method for r in rungs.values())
    }
    metadata["extraction_pages"] = pages
    if ladder:
        metadata["extraction_method"] = "mineru+degraded" if (pages or unknown) else "mineru"
    elif text_only and (unknown or any(r not in _TEXT_LAYER_RUNGS for r in buffered_rungs.values())):
        metadata["extraction_method"] = "mineru+degraded"


def extractor_loop(
    pdf_path: Path,
    content_hash: str,
    db: HttpPipelineDB,
    cancel: threading.Event,
    extractor: str = DEFAULT_EXTRACTOR,
    extraction_done: threading.Event | None = None,
    allow_degraded_extraction: bool = False,
) -> ExtractionResult:
    """Extract pages to the pipeline buffer via the on_page streaming callback.

    On resume, if all pages are already in the buffer, skips re-extraction
    entirely and returns the stored ExtractionResult metadata.

    *allow_degraded_extraction* (nexus-wi1uv) forwarded to
    :meth:`PDFExtractor.extract`. When the post-extraction quality gate
    raises, pages already streamed to the pipeline buffer stay written but
    the run is marked failed by :func:`pipeline_index_pdf`'s
    ``first_exc``/``clear_orphan_wal`` handling below — no garbage chunks
    ever reach the chunker/uploader stages.
    """
    state = db.get_pipeline_state(content_hash)
    pages_extracted_at_start = state["pages_extracted"] if state else 0
    # RDR-237: the counter the row held when this run began. The resume checks below can lower
    # ``pages_extracted_at_start`` (a wiped buffer), but a heartbeat must never lower the row.
    row_pages_extracted = pages_extracted_at_start
    #: What a resume's buffer says about its pages (RDR-237 provenance: each row carries the rung
    #: its page settled on); empty on a first run. Reduced as soon as the rows are read, so the
    #: buffered page text is not kept alive for the whole extraction.
    buffered_rungs: dict[int, str] = {}
    unknown_pages: list[int] = []

    # Resume fast path: if all pages already in buffer, skip extraction.
    if (state and state["total_pages"] is not None
            and state["pages_extracted"] >= state["total_pages"]
            and state.get("extraction_meta")):
        # nexus-gl99l: the counters above are NOT proof the WAL pages
        # backing them still exist. pipeline_index_pdf's caught-exception
        # handler calls db.mark_failed + db.clear_orphan_wal, which wipes
        # the pdf_pages/pdf_chunks WAL rows. Since nexus-33q80 the engine's
        # clearOrphanWal zeroes pages_extracted and chunks_uploaded in the
        # same transaction as the wipe, so today's wipe leaves no stale
        # counter behind. Rows stranded by an earlier engine or client
        # (which left the counters up) can still exist: trusting the
        # counters alone for such a row means a retry sees a row that
        # CLAIMS full extraction with none of the underlying data, and
        # silently skips re-extraction. Verify the actual WAL page count
        # agrees with the claim before trusting the fast path — this
        # self-heals those rows, not just future failures.
        # A COUNT (not an index-coverage check) is sound here only because
        # of two coupled invariants: clear_orphan_wal deletes ALL of a
        # content_hash's pdf_pages/pdf_chunks rows in one server-side
        # transaction (PipelineRepository.clearOrphanWal, TenantScope
        # single-transaction — all-or-nothing, never a partial wipe), and
        # pages are written in strictly increasing page_index order (the
        # on_page callback below). Together those guarantee "N pages
        # present" means "pages [0, N) present" — a count is equivalent to
        # a contiguous-prefix check. If either invariant changes (a
        # partial/selective clear_orphan_wal, or out-of-order/sparse page
        # writes), this guard must switch to actually verifying index
        # coverage (e.g. the max page_index present), not just a count.
        buffered_rows = db.read_pages(content_hash)
        actual_pages = len(buffered_rows)
        if actual_pages >= state["total_pages"]:
            stored_meta = json.loads(state["extraction_meta"])
            if extraction_done is not None:
                extraction_done.set()
            return ExtractionResult(text="", metadata=stored_meta)
        buffered_rungs, unknown_pages = _buffer_provenance(buffered_rows)
        del buffered_rows
        # The claimed prefix isn't backed by real WAL data — the ENTIRE
        # claimed prefix is suspect (clear_orphan_wal is all-or-nothing
        # per content_hash), so fall through to a full re-extraction
        # rather than trusting pages_extracted_at_start's stale value for
        # the on_page dedup skip below.
        pages_extracted_at_start = actual_pages
    elif pages_extracted_at_start > 0:
        # nexus-6m9zy.1 (#1): total_pages is None whenever the previous
        # attempt did NOT complete cleanly — either a SIGKILL mid-stream
        # (WAL rows for the pages already written stay intact) or a
        # caught exception followed by pipeline_index_pdf's
        # mark_failed + clear_orphan_wal (the WAL is wiped, and today's
        # engine zeroes pages_extracted with it; a row stranded by an
        # earlier engine can still hold the old counter, per the
        # nexus-gl99l comment above).
        # pages_extracted alone cannot distinguish the cases: verify against
        # the actual WAL before trusting it, exactly as the fast path
        # above does for the total_pages-set case. When the WAL still
        # backs the counter this is a no-op (actual_pages ==
        # pages_extracted_at_start); when a wipe left a stale counter, this
        # is what stops on_page from skipping pages the retry needs to
        # re-extract.
        buffered_rows = db.read_pages(content_hash)
        pages_extracted_at_start = len(buffered_rows)
        buffered_rungs, unknown_pages = _buffer_provenance(buffered_rows)
        del buffered_rows

    #: Pages settled so far, as the counter counts them (a list so ``on_page`` can move it).
    settled = [pages_extracted_at_start]
    #: The rung each page settled on in THIS run, by 1-based page, skipped pages included.
    run_rungs: dict[int, str] = {}

    def on_page(page_index: int, page_text: str, page_metadata: dict) -> None:
        if cancel.is_set():
            raise PipelineCancelled("pipeline cancelled")
        run_rungs.update(_page_rungs(page_metadata, page_index))
        if page_index < pages_extracted_at_start:
            return
        db.write_page(content_hash, page_index, page_text, metadata=page_metadata)
        db.update_progress(content_hash, pages_extracted=page_index + 1)
        settled[0] = max(settled[0], page_index + 1)

    def on_heartbeat() -> None:
        # RDR-237: called by the extractor at the start of every MinerU attempt and during its two
        # waits, so a long attempt (up to 240 s) does not let the row go stale (5 min). The
        # ladder holds some pages back from ``on_page``, so ``on_page`` alone is too sparse.
        # It re-sends the counter the row already has, never a lower one. How it reaches the
        # row's ``updated_at``: ``update_progress`` flushes at once only when no page or chunk is
        # pending; while a batch is pending the write rides the next flush, which the chunker
        # triggers about every 0.5 s through ``read_pages_from``. A stop is checked first, like
        # ``on_page`` does, so a cancel ends a long wait; what this raises (PipelineCancelled,
        # PipelineRunFenced from the flush) stops the extraction and never moves a page down a rung.
        if cancel.is_set():
            raise PipelineCancelled("pipeline cancelled")
        db.update_progress(content_hash, pages_extracted=max(row_pages_extracted, settled[0]))

    ext = PDFExtractor()
    try:
        result = ext.extract(
            pdf_path, extractor=extractor, on_page=on_page,
            allow_degraded=allow_degraded_extraction, on_heartbeat=on_heartbeat,
        )
    except PipelineCancelled:
        return ExtractionResult(text="", metadata={"page_count": 0, "table_regions": []})

    # RDR-237 provenance: a resumed run's pages came from the buffer, so the map and method
    # describe those pages' own rungs, not only this run's attempt (nexus-q6nw0 item 6).
    if unknown_pages and result.metadata.get("extraction_method") in (*_LADDER_METHODS, "pymupdf"):
        # A buffered page whose row names no rung cannot be proven clean: say so, and the merge
        # keeps the method at ``mineru+degraded`` rather than claim a clean ``mineru`` (or, for
        # the text-only extractor, claim ``pymupdf`` for text that may be another extractor's).
        _log.warning(
            "resume_provenance_unknown", file_path=str(pdf_path), content_hash=content_hash,
            buffered_rows_without_a_rung=len(unknown_pages), buffered_rows_with_a_rung=len(buffered_rungs),
            first_pages_without_a_rung=sorted(unknown_pages)[:10],
        )
    _merge_buffered_provenance(result.metadata, run_rungs, buffered_rungs, unknown_pages)

    page_count = result.metadata.get("page_count", 0)
    db.update_progress(content_hash, total_pages=page_count)
    # Store extraction metadata for resume (avoids re-extraction on crash recovery).
    db.store_extraction_metadata(content_hash, result.metadata)
    # Signalled ONLY when extraction completed (RDR-223, nexus-z0o2p.11). The chunker takes the
    # event as "no more pages" and writes the final chunks, and the uploader's last request sweeps
    # the previous version's chunks, so a failed extraction must not look like a finished one. The
    # old code signalled from a ``finally`` so a stage waiting on the event never blocked; that is
    # not needed, because the orchestrator sets ``cancel`` on the first stage failure and every
    # stage polls it at least once per poll interval.
    if extraction_done is not None:
        extraction_done.set()
    return result


# ── Stage 2: Chunker ────────────────────────────────────────────────────────


def _rebuild_boundaries(pages: list[dict]) -> list[dict]:
    boundaries: list[dict] = []
    pos = 0
    for row in pages:
        meta = json.loads(row["metadata_json"]) if isinstance(row["metadata_json"], str) else row["metadata_json"]
        boundaries.append({
            "page_number": meta.get("page_number", row["page_index"] + 1),
            "start_char": pos,
            "page_text_length": len(row["page_text"]) + 1,
        })
        pos += len(row["page_text"]) + 1
    return boundaries


def _build_chunk_metadata(
    chunk: Any,
    *,
    content_hash: str,
    pdf_path: str,
    corpus: str,
    embedding_model: str,
    now_iso: str,
    git_meta: dict | None = None,
) -> dict:
    """Build chunk metadata with fields known at chunk time.

    Extraction-dependent fields (source_title, source_author, extraction_method,
    format, page_count, is_image_pdf, has_formulas) are set to defaults here
    and corrected by the metadata post-pass after extraction completes.

    RDR-108 Phase 3 retired ``chunk_index``, ``chunk_count``, ``doc_id``
    from chunk metadata; the catalog ``document_chunks`` manifest is now
    authoritative for document-to-chunk binding.

    *git_meta* — accepted for backwards compatibility; the schema dropped
    git provenance from chunk metadata (RDR-101 Phase 5c). Catalog Document
    carries it at the document level. Parameter retained so existing call
    sites do not need to drop the kwarg simultaneously.

    nexus-w94eo: ``title``/``source_author`` are OMITTED from the returned
    dict rather than stamped as ``""`` placeholders. They are ALWAYS unknown
    at this call site — this function builds the streaming pipeline's STUB,
    written before extraction finishes, and the post-pass (:func:`
    _enrich_metadata_from_extraction`) is the only place either field is ever
    resolved (even when the caller passed ``title_override``, since that is
    threaded to the post-pass, never here). Under the pre-nexus-w94eo
    wholesale-REPLACE write semantics an explicit ``""`` was harmless — the
    post-pass's own write replaced it a moment later. Under the engine's new
    MERGE semantics (``metadata = chunks.metadata || EXCLUDED.metadata``) an
    explicit key, even an empty one, is itself a value the merge can
    re-assert: a late-committing duplicate of THIS stub write (e.g. the
    gateway-504-retry shape the nexus-w94eo diagnosis traced) landing after
    the post-pass would re-merge ``title=""`` back on top of the post-pass's
    real title. Omitting the keys here means a stale stub write has nothing
    to re-assert — the merge leaves whatever the post-pass already set alone.
    """
    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415  — circular-dep avoidance (nexus.metadata_schema)

    # RDR-101 Phase 5c dropped corpus, store_type, git_meta. Title kept.
    # RDR-108 Phase 3 dropped chunk_index, chunk_count, doc_id.
    meta = make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
        content_hash=content_hash,
        chunk_start_char=chunk.metadata.get("chunk_start_char", 0),
        chunk_end_char=chunk.metadata.get("chunk_end_char", 0),
        page_number=chunk.metadata.get("page_number", 0),
        indexed_at=now_iso,
        embedding_model=embedding_model,
        section_title=chunk.metadata.get("section_title", ""),
        section_type=chunk.metadata.get("section_type", ""),
        tags="pdf",
        category="paper",
    )
    # nexus-w94eo: drop the placeholder keys make_chunk_metadata's defaults
    # (title="", source_author="") would otherwise have stamped — see the
    # docstring above. Popped rather than never built, so the shared factory
    # (other content_types legitimately want an explicit empty title) stays
    # untouched.
    meta.pop("title", None)
    meta.pop("source_author", None)
    # nexus-vhyar: same reason for the default frecency_score. The
    # frecency-only reindex owns that key, and a late stub duplicate carrying
    # 0.0 would reset a score it had bumped. Readers default a missing score
    # to 0.0 (scoring.py), so omitting it changes nothing on a fresh chunk.
    meta.pop("frecency_score", None)
    return meta


def _embed_and_write_batch(
    chunks_to_embed: list,
    content_hash: str,
    db: HttpPipelineDB,
    embed_fn: EmbedFn | None,
    cancel: threading.Event,
    total_embedded_so_far: int,
    *,
    pdf_path: str,
    corpus: str,
    target_model: str,
    now_iso: str,
    git_meta: dict | None = None,
) -> tuple[int, str]:
    """Embed and write a batch of chunks. Returns (count_written, actual_model)."""
    if not chunks_to_embed:
        return 0, target_model

    from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)

    chunk_texts = [c.text for c in chunks_to_embed]
    embeddings: list[list[float]] = []
    actual_model = target_model

    if embed_fn is not None:
        for batch_start in range(0, len(chunk_texts), _EMBED_BATCH_SIZE):
            if cancel.is_set():
                break
            batch = chunk_texts[batch_start : batch_start + _EMBED_BATCH_SIZE]
            batch_embs, batch_model = embed_fn(batch, target_model)
            embeddings.extend(batch_embs)
            actual_model = batch_model
            db.update_progress(content_hash, chunks_embedded=total_embedded_so_far + len(embeddings))

    write_count = len(embeddings) if embed_fn is not None else len(chunks_to_embed)
    for i in range(write_count):
        chunk = chunks_to_embed[i]
        emb_bytes: bytes | None
        if i < len(embeddings):
            emb_bytes = struct.pack(f"{len(embeddings[i])}f", *embeddings[i])
        elif embed_fn is None and is_vector_service_mode():
            # nexus-9n1u3: service mode — the JVM embeds server-side at upload.
            # Write a non-NULL empty-blob sentinel (not None) so
            # ``read_uploadable_chunks`` (``embedding IS NOT NULL``) still picks
            # the chunk up; the uploader struct.unpacks ``b""`` to ``[]`` and the
            # writer sends only the chunk text (no client vector), which the
            # engine embeds. Mirrors the batch
            # path (doc_indexer server-side-embed branch). The service-mode
            # check is LOCAL (not inferred from embed_fn=None) so a caller that
            # bypasses the orchestrator and passes embed_fn=None outside service
            # mode does NOT silently write zero-vector chunks — it falls through
            # to emb_bytes=None, which read_uploadable_chunks drops, surfacing
            # the misuse instead of corrupting (review nexus-9n1u3 Sig-1).
            emb_bytes = b""
        else:
            emb_bytes = None
        # RDR-108 D1 / nexus-kmb6: streaming PDF chunk natural ID is
        # chunk_text_hash[:32] (matches code/prose/doc indexer write
        # paths). Identical chunk text in the same collection collapses
        # to one T3 record; the catalog manifest preserves position.
        # nexus-4pvho: single source of truth in nexus.chunk_identity.
        chunk_id = _chunk_id(chunk.text)
        meta = _build_chunk_metadata(
            chunk,
            content_hash=content_hash,
            pdf_path=pdf_path,
            corpus=corpus,
            embedding_model=actual_model,
            now_iso=now_iso,
            git_meta=git_meta,
        )
        db.write_chunk(content_hash, chunk.chunk_index, chunk.text, chunk_id,
                        metadata=meta, embedding=emb_bytes)

    return write_count, actual_model


def chunker_loop(
    content_hash: str,
    db: HttpPipelineDB,
    cancel: threading.Event,
    embed_fn: EmbedFn | None,
    chunk_chars: int = 1500,
    extraction_done: threading.Event | None = None,
    chunking_done: threading.Event | None = None,
    *,
    pdf_path: str = "",
    corpus: str = "",
    target_model: str = "voyage-context-3",
    git_meta: dict | None = None,
    doc_id: str = "",
) -> None:
    """Incrementally chunk pages as they arrive, overlapping with extraction.

    Caches accumulated page text in memory to avoid O(pages²) re-reads from
    SQLite. Only NEW pages are fetched on each iteration via ``read_pages_from``.
    """
    chunker = PDFChunker(chunk_chars=chunk_chars, token_window=window_for_collection(corpus, target_model))
    written_up_to = db.count_embedded_chunks(content_hash)
    total_embedded = written_up_to
    now_iso = datetime.now(UTC).isoformat()
    current_model = target_model

    # In-memory cache: accumulated text and boundaries from all pages seen so far.
    # Only new pages are read from SQLite and appended.
    accumulated_text = ""
    accumulated_boundaries: list[dict] = []
    pages_cached = 0
    char_pos = 0

    def _signal_done() -> None:
        if chunking_done is not None:
            chunking_done.set()

    # chunking_done is signalled only on a SUCCESSFUL finish (the two ``return`` paths below;
    # RDR-223, nexus-z0o2p.11). The uploader reads it as "every chunk is in the buffer": it then
    # sends the writer's last request, whose sweep removes what the previous version owned and this
    # one dropped. A chunker that failed (an embed error, the zero-chunk refusal) must not release
    # that request. The orchestrator sets ``cancel`` on the first stage failure, which the uploader
    # polls, so it does not wait on an event that never comes (this replaces the old finally that
    # signalled from every exit, indexing review C2).
    # Seed cache from existing pages (resume case).
    existing_pages = db.read_pages(content_hash)
    if existing_pages:
        parts = []
        for row in existing_pages:
            meta = json.loads(row["metadata_json"]) if isinstance(row["metadata_json"], str) else row["metadata_json"]
            accumulated_boundaries.append({
                "page_number": meta.get("page_number", row["page_index"] + 1),
                "start_char": char_pos,
                "page_text_length": len(row["page_text"]) + 1,
            })
            parts.append(row["page_text"])
            char_pos += len(row["page_text"]) + 1
        accumulated_text = "\n".join(parts)
        pages_cached = len(existing_pages)

    while not cancel.is_set():
        is_final = False
        if extraction_done is not None:
            is_final = extraction_done.is_set()
        else:
            state = db.get_pipeline_state(content_hash)
            if state and state["total_pages"] is not None and state["pages_extracted"] >= state["total_pages"]:
                is_final = True

        # Read only NEW pages (O(new_pages) not O(all_pages)).
        new_pages = db.read_pages_from(content_hash, pages_cached)

        if not new_pages and not is_final:
            if extraction_done is not None:
                extraction_done.wait(timeout=0.5)
            else:
                time.sleep(_POLL_INTERVAL)
            continue

        # Append new pages to cache.
        if new_pages:
            parts = []
            for row in new_pages:
                meta = json.loads(row["metadata_json"]) if isinstance(row["metadata_json"], str) else row["metadata_json"]
                accumulated_boundaries.append({
                    "page_number": meta.get("page_number", row["page_index"] + 1),
                    "start_char": char_pos,
                    "page_text_length": len(row["page_text"]) + 1,
                })
                parts.append(row["page_text"])
                char_pos += len(row["page_text"]) + 1
            if accumulated_text:
                accumulated_text += "\n" + "\n".join(parts)
            else:
                accumulated_text = "\n".join(parts)
            pages_cached += len(new_pages)

        if not accumulated_text:
            if is_final:
                db.update_progress(content_hash, chunks_created=0, chunks_embedded=0)
                _signal_done()
                return
            continue

        chunk_metadata = {"page_boundaries": accumulated_boundaries, "table_regions": []}
        chunks = chunker.chunk(accumulated_text, chunk_metadata)

        if is_final and not chunks and accumulated_text.strip():
            # nexus-aold: extraction succeeded with non-empty text but the
            # chunker produced zero chunks. Pre-fix this fell through to
            # ``return`` after a no-op ``_embed_and_write_batch`` (the
            # silent 0-chunk failure mode the bead names). Raise so the
            # orchestrator surfaces it instead of completing "successfully".
            raise RuntimeError(
                f"chunker produced zero chunks for {pdf_path} despite "
                f"non-empty extracted text ({len(accumulated_text)} chars "
                f"across {pages_cached} pages). This usually indicates a "
                "chunker bug or a mismatch between extractor output and "
                "chunker expectations; rerun with --extractor mineru or "
                "file a bug with the source PDF."
            )

        batch_kwargs = dict(
            pdf_path=pdf_path, corpus=corpus, target_model=current_model,
            now_iso=now_iso, git_meta=git_meta,
        )

        if is_final:
            new_chunks = chunks[written_up_to:]
            count, actual_model = _embed_and_write_batch(
                new_chunks, content_hash, db, embed_fn, cancel,
                total_embedded, **batch_kwargs,
            )
            total_embedded += count
            written_up_to += count
            current_model = actual_model
            db.update_progress(content_hash, chunks_created=len(chunks), chunks_embedded=total_embedded)
            _signal_done()
            return

        # Hold back the last chunk — its boundary may shift when more pages arrive.
        stable_end = max(written_up_to, len(chunks) - 1)
        new_chunks = chunks[written_up_to:stable_end]
        if new_chunks:
            count, actual_model = _embed_and_write_batch(
                new_chunks, content_hash, db, embed_fn, cancel,
                total_embedded, **batch_kwargs,
            )
            current_model = actual_model
            total_embedded += count
            written_up_to += count
            db.update_progress(content_hash, chunks_created=written_up_to)


# ── Stage 3: Uploader ───────────────────────────────────────────────────────


class PartialUploadResumeError(RuntimeError):
    """An uploader with no writer was handed a buffer whose first chunk to send is not chunk 0.

    An internal invariant, not a state the orchestrator lets a run reach. The multi-batch writer
    keeps its state (the pre-run manifest snapshot, the positions and chashes it wrote) in the
    process that runs it. A process killed part way through the upload took that state with it; a
    writer started on the remaining chunks alone would replace the document's manifest with that
    tail and sweep the head as superseded, and the completion check would pass on the tail's row
    count. :func:`pipeline_index_pdf` therefore puts the buffer's upload flags back before the
    stages start (:func:`_reconcile_resumed_run`), so the uploader always begins at chunk 0 and
    this is never raised from a run it orchestrates. The uploader keeps the check as its own
    invariant: a direct caller handed such a buffer refuses to send.
    """


def _make_upload_catalog() -> Any:
    """The catalog writer the streaming run's writer sends through (a test patch target: the run's
    own registration goes through ``make_catalog_writer`` too, and must not be replaced with it)."""
    from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred import

    return make_catalog_writer()


class UploadRun:
    """The multi-batch writer one streaming run uploads through, and what the run needs of it later.

    The uploader creates the writer when its first batch arrives and the orchestrator stamps it
    complete after the post-passes (``defer_completion``): the passes enrich chunk metadata after
    the last chunk has landed, and a process killed between the stamp and the passes would leave a
    document that looks complete and is missing its title and author. A run made by a direct
    caller of :func:`uploader_loop` (no *run* given) stamps in ``finish()``.
    """

    def __init__(self, on_request: Callable[[], None] | None = None) -> None:
        self.writer: Any = None
        self.cat: Any = None
        self.finished = False          # every chunk landed: the writer's finish() returned
        self._raw_cat: Any = None
        #: Called AFTER a chunk-carrying request that has, or may have, written chunks: when it
        #: answered, or failed with its outcome open (see ``MetadataMergingCatalog._send``). Never
        #: for a request that did not reach the engine or that the engine refused.
        self._on_request = on_request

    def open_writer(
        self, *, doc_id: str, collection: str, content_hash: str, force_re_embed: bool,
        defer_completion: bool,
    ) -> Any:
        from nexus.catalog.multi_batch_write import MultiBatchDocumentWriter  # noqa: PLC0415 — deferred: multi_batch_write imports the vector client
        from nexus.catalog.metadata_merging_catalog import MetadataMergingCatalog  # noqa: PLC0415 — deferred: shared with the other RDR-223 writers

        self._raw_cat = _make_upload_catalog()
        # The streaming stub's metadata is deliberately partial (the post-pass fills title, author
        # and extraction method), so the write MERGES it into what is stored and names no keys to
        # delete: replacing would strip the ``bib_*`` enrichment a forced re-index must keep.
        self.cat = MetadataMergingCatalog(self._raw_cat, collection, [], on_request=self._on_request)
        self.writer = MultiBatchDocumentWriter(
            self.cat, doc_id=doc_id, collection=collection, content_hash=content_hash,
            force_re_embed=force_re_embed, defer_completion=defer_completion)
        return self.writer

    def close(self) -> None:
        close = getattr(self._raw_cat, "close", None)
        self._raw_cat = None
        if close is not None:
            close()


class UploaderInvariantError(RuntimeError):
    """The uploader was about to hand rows to a writer that had already finished (nexus-gjxkb).

    An internal invariant: the uploader finishes its writer only when every chunk is in it, so rows
    arriving after that are a defect, not a state a run reaches. Raised in place of the writer's
    generic ``ValueError`` so the message names the document and the positions."""


#: One batch handed to the writer and not yet flagged uploaded: (rows, ids, documents,
#: embeddings, metadatas).
_Held = tuple[list[dict], list[str], list[str], list[list[float]], list[dict]]


def uploader_loop(
    content_hash: str,
    db: HttpPipelineDB,
    t3: Any,
    collection: str,
    cancel: threading.Event,
    chunking_done: threading.Event | None = None,
    *,
    catalog_doc_id: str = "",
    hooks: "HookRegistry | None" = None,
    dry_run: bool = False,
    force_re_embed: bool = False,
    run: UploadRun | None = None,
) -> None:
    """Poll the chunk buffer for embedded chunks and write them, with their owner rows, to T3.

    RDR-223 (nexus-z0o2p.11): every batch of up to ``_UPLOAD_BATCH_SIZE`` chunks goes to the
    document's multi-batch writer (:class:`~nexus.catalog.multi_batch_write.MultiBatchDocumentWriter`)
    as its rows (positions are the chunker's global ``chunk_index``) plus the chunk payloads its
    rows reference. The writer begins the index-run fence with its first request, replaces the
    manifest with that request (sweep off), appends every later batch with its chunks and owner rows
    in one transaction, and after the last one sweeps what the previous version owned and this one
    dropped. A process killed part way leaves every chunk it wrote owned. There is no separate
    chunk upload and no manifest hook for these chunks.

    The writer holds the newest batch back until it knows whether it is the last, so a chunk is
    flagged uploaded in the buffer only once its request was sent, one batch behind. A chunk's
    post-store hooks fire at the same point. A buffer whose first chunk to send is not chunk 0 (an
    earlier process flagged the head and died) is refused with :class:`PartialUploadResumeError`;
    the orchestrator never hands one over, it resets the flags first (see there).

    The writer is finished only once every chunk is in it (nexus-gjxkb): the chunker was seen done
    BEFORE a read that found nothing new, batches are taken only as a contiguous run from the next
    position (a row not yet visible is waited for, never skipped), and the buffer holds as many
    embedded rows as the chunker made. Finishing early sent the sweep against a manifest missing its
    tail and handed the late chunks to a finished writer.

    *catalog_doc_id* is the document that owns the chunks; without one (and not *dry_run*) the
    run cannot write and raises ``CatalogIdentityMissingError``.

    *run* (the orchestrator passes one) holds the writer so the orchestrator can stamp the run
    complete after the post-passes; a direct caller passes none and the writer stamps itself.

    Pass *dry_run=True* (nexus-uxg4u round 2) for a preview: the chunks go straight into *t3*,
    the caller's throwaway store, with no owner row and no catalog write, and the hook fan-out
    (which would make a real T2 aspect-queue write) is skipped.

    *force_re_embed* (nexus-8143o): forwarded to the writer, the engine's RDR-181 existence
    partition control.
    """
    from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 - deferred to avoid circular import at module load

    # nexus-6m9zy.1 (#3): seed the running total from persisted progress, not 0. Chunks a
    # previous process flagged uploaded are never returned by read_uploadable_chunks again, so a
    # total that restarts at 0 could only count THIS run's uploads, and chunks_uploaded would
    # never reach chunks_created. Sound because the one place that wipes the WAL (the
    # orchestrator's first_exc handler, via _mark_failed_and_reset_wal -> db.clear_orphan_wal)
    # zeroes this counter in the same engine transaction (nexus-33q80).
    _resume_state = db.get_pipeline_state(content_hash)
    persisted_uploaded = (
        _resume_state["chunks_uploaded"]
        if _resume_state and _resume_state["chunks_uploaded"] is not None
        else 0
    )
    total_uploaded = persisted_uploaded

    if hooks is None:
        hooks = HookRegistry()
        install_default_hooks(hooks)
    if not dry_run:
        # The manifest is written by the writer, with the chunks; the batch manifest hook would
        # replace it a second time and stash a deferred sweep for a stamp nobody is waiting for.
        from nexus.mcp_infra import manifest_write_batch_hook  # noqa: PLC0415 - deferred: avoids a module-load-time cross-import

        hooks = hooks.without_batch(manifest_write_batch_hook)

    own_run = run is None
    if run is None:
        run = UploadRun()
    held: _Held | None = None
    last_added = -1
    stalled_polls = 0       # polls since the last batch, with the chunker finished
    stall_warned = False

    def _flag(batch: _Held) -> None:
        """The batch's request was sent: post-store hooks (RDR-095), then flag it uploaded."""
        nonlocal total_uploaded
        batch_rows, ids, documents, embeddings, metadatas = batch
        # Both single-doc and batch chains fire from every storage event; the per-doc loop
        # covers single-shape consumers on CLI ingest.
        if not dry_run:
            hooks.fire_batch(
                ids, collection, documents, embeddings, metadatas,
                catalog_doc_id=catalog_doc_id,
            )
            for _did, _doc in zip(ids, documents):
                hooks.fire_single(_did, collection, _doc)
        db.mark_uploaded(content_hash, [row["chunk_index"] for row in batch_rows])
        total_uploaded += len(batch_rows)
        db.update_progress(content_hash, chunks_uploaded=total_uploaded)

    def _land_held() -> None:
        """Finish the writer (its last request, the deferred sweep, the stamp unless deferred),
        then flag the batch it held back. Nothing to do when the writer never started, already
        finished, or held nothing."""
        nonlocal held
        if held is None or run.writer is None or run.finished:
            return
        result = run.writer.finish()
        run.finished = True
        run.cat.account_unexplained_skips(catalog_doc_id, result.sweep_skipped)
        _flag(held)
        held = None

    def _chunker_finished() -> bool:
        if chunking_done is not None:
            # Orchestrated path: wait for chunking_done event — don't trust provisional
            # chunks_created during incremental chunking.
            return chunking_done.is_set()
        # Resume path (no event): use durable state. Safe because on resume the chunker runs to
        # completion before the uploader starts — there is no incremental chunks_created race.
        state = db.get_pipeline_state(content_hash)
        return bool(state and state["chunks_created"] is not None)

    def _every_chunk_is_in_the_buffer() -> bool:
        """The buffer holds as many embedded rows as the chunker made. Checked before the writer
        is finished, so the decision rests on durable state, not on which thread may still have
        a flush in flight (``HttpPipelineDB`` documents that race)."""
        state = db.get_pipeline_state(content_hash)
        created = state["chunks_created"] if state else None
        return created is not None and db.count_embedded_chunks(content_hash) >= created

    def _wait_for_the_next_chunk() -> None:
        """One poll of waiting on a chunk the chunker has finished making and the buffer does not
        show. Past ``_STALL_WARN_POLLS`` without progress, say so once; past ``_STALL_FAIL_POLLS``,
        fail the record, since nothing here makes the chunk appear (nexus-gjxkb)."""
        nonlocal stalled_polls, stall_warned
        stalled_polls += 1
        if stalled_polls >= _STALL_WARN_POLLS:
            state = db.get_pipeline_state(content_hash)
            created = state["chunks_created"] if state else None
            embedded = db.count_embedded_chunks(content_hash)
            if stalled_polls >= _STALL_FAIL_POLLS:
                raise UploadStalledError(
                    content_hash=content_hash, next_index=last_added + 1, polls=stalled_polls,
                    chunks_created=created, embedded=embedded,
                    seconds=stalled_polls * _POLL_INTERVAL)
            if not stall_warned:
                stall_warned = True
                _log.warning(
                    "uploader_waiting_for_a_chunk",
                    content_hash=content_hash, next_index=last_added + 1, polls=stalled_polls,
                    chunks_created=created, embedded=embedded, uploaded=total_uploaded,
                    fails_after_polls=_STALL_FAIL_POLLS)
        time.sleep(_POLL_INTERVAL)

    try:
        while not cancel.is_set():
            # nexus-gjxkb: look at the chunker BEFORE reading. It writes its last chunks and then
            # signals, so a read made after the signal sees them all; a read made before it can
            # miss chunks written between the read and the check, and the writer would be finished
            # (its last request carries the sweep) with chunks still to come.
            chunker_finished = _chunker_finished()
            # One held batch is still unflagged in the buffer, so read past it.
            rows = db.read_uploadable_chunks(content_hash, limit=2 * _UPLOAD_BATCH_SIZE)
            pending = [r for r in rows if r["chunk_index"] > last_added]
            # Only the run that continues from the next position (positions are dense). A row
            # missing in the middle is one another thread's flush has not landed yet; reading past
            # it would leave it out of the manifest for good, since nothing below last_added is
            # read again.
            batch_rows: list[dict] = []
            for row in pending:
                if row["chunk_index"] != last_added + 1 + len(batch_rows) or len(batch_rows) >= _UPLOAD_BATCH_SIZE:
                    break
                batch_rows.append(row)
            if batch_rows:
                stalled_polls, stall_warned = 0, False

            if not dry_run and run.writer is None and pending and pending[0]["chunk_index"] > 0:
                # Read from the rows and the flags, not the progress counter: the counter is
                # buffered and lags the flags by up to a poll interval. With nothing flagged the
                # head is only not visible yet, and the next poll reads it.
                _embedded, uploaded = db.chunk_counts(content_hash)
                if uploaded > 0:
                    raise PartialUploadResumeError(
                        f"the first chunk left to send is chunk #{pending[0]['chunk_index']}, "
                        "so an earlier process already sent the chunks before it and its write "
                        "state is gone with that process; sending only the rest would replace "
                        "the document's manifest with a fragment and sweep the head as "
                        "superseded. The orchestrator resets the upload flags first "
                        "(_reconcile_resumed_run), so the document is re-sent from chunk 0."
                    )

            if batch_rows:
                if cancel.is_set():
                    return
                ids = [row["chunk_id"] for row in batch_rows]
                documents = [row["chunk_text"] for row in batch_rows]
                embeddings = [
                    list(struct.unpack(f"{len(row['embedding']) // 4}f", row["embedding"]))
                    for row in batch_rows
                ]
                metadatas = [
                    json.loads(row["metadata_json"]) if isinstance(row["metadata_json"], str) else row["metadata_json"]
                    for row in batch_rows
                ]
                batch: _Held = (batch_rows, ids, documents, embeddings, metadatas)

                if dry_run:
                    from nexus.doc_indexer import _preview_upsert  # noqa: PLC0415 - deferred to avoid circular import at module load
                    _preview_upsert(t3, collection, ids, documents, embeddings, metadatas,
                                    force_re_embed=force_re_embed)
                    _flag(batch)
                else:
                    if not catalog_doc_id:
                        from nexus.doc_indexer import _raise_identity_missing  # noqa: PLC0415 - deferred to avoid circular import at module load
                        _raise_identity_missing(f"PDF {content_hash[:12]}", collection, None)
                    if run.writer is not None and run.finished:
                        raise UploaderInvariantError(
                            f"the uploader of document {catalog_doc_id!r} ({collection}) has "
                            f"{len(batch_rows)} chunk(s) to add at positions "
                            f"{[row['chunk_index'] for row in batch_rows]} but its writer is already "
                            "finished: a writer is finished only once every chunk is in it, so this "
                            "is a defect in the uploader, not a state the run can reach.")
                    if run.writer is None:
                        run.open_writer(
                            doc_id=catalog_doc_id, collection=collection, content_hash=content_hash,
                            force_re_embed=force_re_embed, defer_completion=not own_run)
                    # The chunk's natural ID is its chash; the row's position is the chunker's
                    # global ordering. The chunk payload carries the stub the chunker stamped: the
                    # chunk_index is the row's position, not chunk metadata (RDR-108 Phase 3).
                    from nexus.mcp_infra import _manifest_chunk_rows  # noqa: PLC0415 - deferred: avoids a module-load-time cross-import

                    manifest_rows = _manifest_chunk_rows([
                        (row["chunk_index"], {**meta, "chunk_index": row["chunk_index"]})
                        for row, meta in zip(batch_rows, metadatas)
                    ])
                    for m_row, chash in zip(manifest_rows, ids):
                        m_row["chash"] = chash
                    run.writer.add_batch(manifest_rows, [
                        {"chash": chash, "text": text, "metadata": meta}
                        for chash, text, meta in zip(ids, documents, metadatas)
                    ])
                    # Handing this batch over sent every batch before it.
                    if held is not None:
                        _flag(held)
                    held = batch
                last_added = batch_rows[-1]["chunk_index"]

            if not chunker_finished:
                time.sleep(_POLL_INTERVAL)
                continue
            if pending and not batch_rows:
                # The next position is not visible yet.
                _wait_for_the_next_chunk()
                continue

            if cancel.is_set():
                return
            if batch_rows:
                continue

            if held is not None and not run.finished and not _every_chunk_is_in_the_buffer():
                _wait_for_the_next_chunk()
                continue

            # The chunker is done and nothing new is readable: the writer's last request, the
            # sweep, and (for a direct caller) the stamp.
            _land_held()
            if cancel.is_set():
                # nexus-4vx0a review: the run was interrupted while the last request landed and
                # its row was given back as failed; marking it completed now would make the next
                # create wipe the buffer instead of resuming it.
                return
            state = db.get_pipeline_state(content_hash)
            if state and state["chunks_created"] is not None and state["chunks_uploaded"] >= state["chunks_created"]:
                db.mark_completed(content_hash)
                return

            _wait_for_the_next_chunk()
    finally:
        if own_run:
            run.close()


# ── Orchestrator ─────────────────────────────────────────────────────────────


def extraction_record(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """The extraction fields the PDF catalog hook records, from an extraction result's metadata.

    ``extraction_method`` is the document-level value (``mineru``, ``mineru+degraded``,
    ``pymupdf``, or a legacy one), ``""`` when the extractor named none. ``extraction_pages`` is
    the rung-to-sorted-1-based-pages map of the pages that settled below rung 1, ``{}`` when none
    did or the extractor reports no map (a non-ladder extractor): the hook writes it on EVERY
    extraction, because the catalog merge never removes a key and so a clean re-extract can clear
    an old map only by replacing it with ``{}`` (RDR-237).
    """
    meta = metadata or {}
    raw = meta.get("extraction_pages")
    pages: dict[str, list[int]] = {}
    if isinstance(raw, Mapping):
        for rung, nums in raw.items():
            kept: list[int] = []
            for p in nums if isinstance(nums, (list, tuple, set, frozenset)) else ():
                # A page is a real integer. The streaming caller runs this outside any try, after
                # all the work is done, so a malformed entry is skipped and named, never raised.
                if isinstance(p, int) and not isinstance(p, bool):
                    kept.append(p)
                else:
                    _log.warning("extraction_record_skipped_a_malformed_page", rung=str(rung), page=repr(p))
            if kept:
                pages[str(rung)] = sorted(kept)
            elif nums:
                _log.warning("extraction_record_skipped_a_malformed_rung", rung=str(rung), pages=repr(nums))
    return {"extraction_method": str(meta.get("extraction_method") or ""), "extraction_pages": pages}


def _catalog_extraction_meta(extraction: Mapping[str, Any]) -> dict[str, Any]:
    """The ``meta`` keys one extraction record writes: ``extraction_pages`` always (``{}`` when
    clean), ``extraction_method`` only when the extractor named one."""
    meta: dict[str, Any] = {"extraction_pages": dict(extraction.get("extraction_pages") or {})}
    if extraction.get("extraction_method"):
        meta["extraction_method"] = str(extraction["extraction_method"])
    return meta


def record_pdf_extraction_on_document(
    doc_id: str, pdf_path: Path, collection_name: str, extraction: Mapping[str, Any],
) -> None:
    """Record an extraction on an already-registered catalog document (``nx index repo``, RDR-237).

    The repo indexer registers every file, PDFs included, in its catalog pass before it chunks
    anything, so unlike :func:`_catalog_pdf_hook` there is no row to find or register: *doc_id* is
    the document's tumbler. The merge is the same ``meta`` update (``extraction_pages`` written
    whole, ``{}`` included, so a clean re-extract clears an old map). It never raises: an empty
    *doc_id* or a failed write logs ``catalog_pdf_extraction_not_recorded`` (``reason``
    ``repo_indexer_no_document`` or ``repo_indexer_update_failed``) naming the file.
    """
    meta = _catalog_extraction_meta(extraction)

    def _not_recorded(reason: str, **extra: Any) -> None:
        _log.warning(
            "catalog_pdf_extraction_not_recorded", reason=reason, file_path=str(pdf_path),
            doc_id=doc_id, collection=collection_name,
            extraction_method=meta.get("extraction_method", ""),
            extraction_pages=meta["extraction_pages"], **extra,
        )

    if not doc_id:
        _not_recorded("repo_indexer_no_document")
        return
    writer = None
    try:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 - deferred to avoid circular import at module load

        writer = make_catalog_writer()
        if writer is None:
            _not_recorded("no_catalog_writer")
            return
        writer.update(doc_id, meta=meta)
    except Exception as exc:  # noqa: BLE001 - best-effort: the PDF is indexed either way; named, not raised
        _not_recorded("repo_indexer_update_failed", error=str(exc))
    finally:
        if writer is not None:
            writer.close()


def _catalog_pdf_hook(
    pdf_path: Path,
    collection_name: str,
    title: str = "",
    author: str = "",
    year: int = 0,
    corpus: str = "",
    chunk_count: int = 0,
    source_uri: str = "",
    extraction: Mapping[str, Any] | None = None,
) -> None:
    """Register PDF document in catalog after successful indexing. Silently skipped if absent.

    *extraction* (RDR-237, :func:`extraction_record`) is merged into the catalog document's
    ``meta`` on BOTH branches, the update of an existing row and the registration of a new one
    (and the update that follows a registration which resolved onto an existing row, because the
    engine's register returns that row untouched). ``extraction_pages`` is always written, ``{}``
    when clean. ``None`` writes nothing.

    Runs that record NO extraction metadata, each with a ``catalog_pdf_extraction_not_recorded``
    warning that names the document (``reason`` says which):

    - ``no_catalog_reader``: no catalog is reachable (the SQLite opt-out mode, uninitialised).
    - ``ephemeral_path``: a NEW registration of a file under an agent worktree or system temp
      directory is refused (an existing row is updated, so it is recorded). The refusal needs an
      owner with a non-ephemeral ``repo_root``; the curator owners PDFs register under have none,
      so for them this exit is not reached (``should_skip_ephemeral_registration``'s KNOWN
      RESIDUAL), and the warning is the net if that changes.
    - ``hook_failed``: any error in the hook; it is swallowed (the PDF is indexed either way).

    ``nx index repo`` does not call this hook (the repo indexer registers its files in its own
    catalog pass); it records through :func:`record_pdf_extraction_on_document`, whose own
    warnings are ``repo_indexer_no_document`` and ``repo_indexer_update_failed``.

    The callers add their own: the streaming pipeline and ``index_pdf`` do not call the hook on a
    dry run, and ``index_pdf``'s batch-path wrapper logs ``register_in_catalog_raised``.
    """
    reader = None
    writer = None
    # Bound before the try so the except handler always has a name to log (resolve() can raise).
    file_path_str = str(pdf_path)
    extraction_meta = _catalog_extraction_meta(extraction) if extraction is not None else None

    def _not_recorded(reason: str) -> None:
        if extraction_meta is not None:
            _log.warning(
                "catalog_pdf_extraction_not_recorded", reason=reason,
                file_path=file_path_str, source_uri=source_uri, collection=collection_name,
                extraction_method=extraction_meta.get("extraction_method", ""),
                extraction_pages=extraction_meta["extraction_pages"],
            )

    try:
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 - deferred to avoid circular import at module load

        # nexus-e9ru2 (sibling of nexus-f1itv): presence semantics belong to
        # the factory — in service mode the Java service owns the catalog and
        # no local state exists; a local is_initialized pre-check silently
        # skipped registration on every fresh box. make_catalog_reader()
        # returns None only in the SQLite opt-out mode when uninitialised.
        # Resolved unconditionally, first, and unrelated to reader/writer
        # calls below — so the except handler's record_catalog_hook_failure
        # (which reports source_path=file_path_str) always has a bound
        # value, even when a failure occurs mid owner-resolution (a
        # pre-existing UnboundLocalError, incidentally exposed by
        # nexus-5xn3k.4's fence-writer test double).
        file_path_str = str(pdf_path.resolve())
        reader = make_catalog_reader()
        if reader is None:
            _log.debug("catalog_pdf_hook_skipped", reason="catalog not initialized (sqlite opt-out mode)")
            _not_recorded("no_catalog_reader")
            return
        writer = make_catalog_writer()
        effective_title = title or pdf_path.stem
        owner_name = corpus if corpus else "standalone-pdfs"

        # Get or create curator owner. nexus-qnp5s: curator_owner_tumbler_by_name()
        # is implemented on both SQLite Catalog and HttpCatalogClient.
        owner_t = reader.curator_owner_tumbler_by_name(owner_name)
        owner = owner_t if owner_t is not None else writer.register_owner(owner_name, "curator")

        # Dedup by file_path (stable identifier for PDFs). Resolve to an
        # absolute path so downstream consumers (aspect_extractor's disk
        # fallback, link generators, dedup-after-move detection) can open
        # the file without depending on the cwd of the reading process.
        # Portability across machines is now the catalog's source-mtime
        # + content_hash story — both already populated.
        from datetime import UTC, datetime  # noqa: PLC0415 - branch-local; deferred to call time
        # nexus-y8qtj: when source_uri is known, resolve by IT first. The
        # pre-flight _register_or_lookup_doc_id call earlier in this same
        # index run already validated (fail-loud) that source_uri resolves
        # to a live document (or freshly registered one under it), so
        # by_file_path here — which does NOT know about an out-of-band
        # identity like x-devonthink-item://<UUID> — must not be allowed
        # to miss and mint a SECOND Document for the same source. Falling
        # through to the plain by_file_path lookup only when source_uri is
        # absent preserves prior behaviour for every non-DT ingest path.
        existing = reader.by_source_uri(source_uri) if source_uri else None
        if existing is None:
            existing = reader.by_file_path(owner, file_path_str)

        # Known TOCTOU window (Reviewer B/I-3): this stat happens AFTER
        # the PDF was extracted + chunked earlier in the pipeline. A
        # concurrent write between extraction and this stat records an
        # mtime newer than the indexed content, suppressing a later
        # staleness flag. Proper fix requires threading source_mtime
        # from the extraction point. Filed as follow-up; see matching
        # comment in ``doc_indexer._catalog_markdown_hook``.
        try:
            source_mtime = pdf_path.stat().st_mtime
        except OSError:
            source_mtime = 0.0
        if existing:
            # nexus-t952k half 2: this is the tail reconcile step that
            # already stamps physical_collection onto the row unconditionally
            # (below) — the FIX is making a real repoint observable. Without
            # this log, a re-index that lands chunks in a different
            # collection than the row's stale physical_collection silently
            # repoints with no trace, which is exactly how the AgenticScholar
            # incident (2026-09-02, T2 nexus/index-agenticscholar-paper-
            # 2026-09-02) went undetected until a manual `nx catalog verify`
            # dug it up by hand. A ghost/never-indexed row (blank
            # physical_collection) is not a repoint — nothing to compare
            # against — so it is excluded, mirroring nexus-sz89e's identical
            # ghost exemption.
            old_physical_collection = existing.physical_collection
            update_kwargs: dict[str, Any] = dict(
                physical_collection=collection_name,
                chunk_count=chunk_count,
                indexed_at=datetime.now(UTC).isoformat(),
                source_mtime=source_mtime,
            )
            # Stem-guard backfill, mirroring nexus-ivzw8's markdown hook: the
            # RDR-102 D1 pre-flight registers every PDF with title=stem BEFORE
            # extraction, so this branch is the ONLY place the extracted
            # title/author/year can reach the catalog. Fill them only when the
            # row still carries the placeholder (or nothing) — a curated
            # title is never clobbered by a re-index (2026-08-19,
            # papers/2512.11001.pdf landed as "2512.11001").
            # Placeholder shapes: the raw pre-flight stem AND derive_title's
            # normalised stem ("attention-is-all" -> "Attention Is All"), which
            # resolve_pdf_title returns when no extractor/H1 title exists —
            # otherwise that normalised stem would be backfilled once and then
            # locked in as "curated" (nexus-ov5tc critique, S1).
            from nexus.indexer_utils import derive_title  # noqa: PLC0415 - circular-dep avoidance (nexus.indexer_utils)
            _placeholders = {"", pdf_path.stem, derive_title(pdf_path, body=None)}
            existing_title = (existing.title or "").strip()
            if title and title not in _placeholders and existing_title in _placeholders:
                update_kwargs["title"] = title
            if author and not (getattr(existing, "author", "") or "").strip():
                update_kwargs["author"] = author
            if year and not getattr(existing, "year", 0):
                update_kwargs["year"] = year
            if extraction_meta is not None:
                # RDR-237: the service merges meta key by key (jsonb ||), never removing one, so
                # a clean re-extract clears an old map only because extraction_pages is written
                # whole here, {} included.
                update_kwargs["meta"] = extraction_meta
            writer.update(existing.tumbler, **update_kwargs)
            # nexus-t952k (critique [24114] S1): logged AFTER the write, so
            # the claim "repointed" is only made once the row actually moved.
            if old_physical_collection and old_physical_collection != collection_name:
                _log.warning(
                    "catalog_physical_collection_repointed",
                    tumbler=str(existing.tumbler),
                    file_path=file_path_str,
                    old_collection=old_physical_collection,
                    new_collection=collection_name,
                )
        else:
            # nexus-u8n4r: refuse a brand-new registration when
            # ``file_path_str`` (always absolute — see the resolve()
            # above) sits under an agent worktree or system temp dir,
            # unless this owner's own repo_root is itself rooted there.
            # See ``nexus.repo_identity.should_skip_ephemeral_registration``.
            from nexus.repo_identity import (  # noqa: PLC0415 - circular-dep avoidance (nexus.repo_identity)
                canonicalize_worktree_path,
                is_worktree_or_tempdir_path,
                owner_repo_root_best_effort,
                should_skip_ephemeral_registration,
            )
            _owner_repo_root = owner_repo_root_best_effort(reader, owner)
            # nexus-kkumv: rewrite a worktree-marker file_path_str to its
            # primary-repo identity before the refusal check — only when
            # the owner root is not itself a deliberate worktree/tempdir
            # throwaway AND the primary-repo mirror exists on disk.
            # Mirrors doc_indexer.py's identical guard.
            if not is_worktree_or_tempdir_path(_owner_repo_root):
                _canonical_fp = canonicalize_worktree_path(file_path_str)
                if _canonical_fp != file_path_str and Path(_canonical_fp).is_file():
                    file_path_str = _canonical_fp
            if should_skip_ephemeral_registration(file_path_str, _owner_repo_root):
                _log.warning(
                    "ephemeral_path_registration_skipped",
                    path=file_path_str, owner=str(owner), reason="worktree_or_tempdir",
                )
                from nexus.mcp_infra import _record_ephemeral_registration_skip  # noqa: PLC0415 - circular-dep avoidance (nexus.mcp_infra)
                _record_ephemeral_registration_skip(
                    file_path_str, str(owner), reason="worktree_or_tempdir",
                )
                _not_recorded("ephemeral_path")
                return
            # nexus-yzij1: the lookup above is owner-scoped (and the
            # source_uri leg missed, or there was no URI), so this mint can
            # be the second document for a path another owner already holds.
            # That is allowed; going unremarked is not. nexus-r1tnx: the
            # conflict check runs BEFORE register() (querying after would
            # see the just-minted row too), but the announcement itself
            # waits until AFTER, gated on register()'s own created signal —
            # register() can resolve to the pre-existing row instead of
            # minting a new one, and that is not an additional document.
            from nexus.catalog.path_ambiguity import (  # noqa: PLC0415 - circular-dep avoidance (nexus.catalog)
                announce_cross_owner_mint,
                announce_cross_owner_resolve,
                created_from_register_result,
                find_cross_owner_conflict,
                reconcile_stale_physical_collection,
                tumbler_from_register_result,
            )
            _conflict = find_cross_owner_conflict(reader, file_path_str)
            _write_result = writer.register(
                owner=owner, title=effective_title, content_type="paper",
                author=author, year=year, corpus=corpus,
                physical_collection=collection_name,
                chunk_count=chunk_count,
                file_path=file_path_str,
                source_mtime=source_mtime,
                source_uri=source_uri,
                with_created=True,
                **({"meta": extraction_meta} if extraction_meta is not None else {}),
            )
            _created = created_from_register_result(_write_result)
            announce_cross_owner_mint(
                _conflict, file_path=file_path_str, owner=owner,
                context="catalog_pdf_hook", created=_created,
            )
            if not _created:
                # nexus-r1tnx round 2: register() resolved onto an existing
                # row instead of minting one — the same reconciliation the
                # ``if existing:`` branch above already does, closing the
                # nexus-2t63u stale-physical_collection exposure for this
                # (previously unreconciled) resolve path too.
                announce_cross_owner_resolve(
                    _conflict, file_path=file_path_str, owner=owner,
                    context="catalog_pdf_hook", created=_created,
                )
                reconcile_stale_physical_collection(
                    reader, writer,
                    tumbler=tumbler_from_register_result(_write_result),
                    target_collection=collection_name, file_path=file_path_str,
                    owner=owner,
                )
                if extraction_meta is not None:
                    # RDR-237: register() returns an existing row untouched (the engine's
                    # idempotency legs do not merge ``meta``), so the extraction record rides an
                    # update, as on the ``existing`` branch above.
                    writer.update(tumbler_from_register_result(_write_result), meta=extraction_meta)
    except Exception as exc:  # noqa: BLE001 - best-effort catalog PDF hook; logged + audited, cleanup in finally
        # nexus-ou4tb: an indexed PDF that never reached the catalog is
        # invisible to every catalog-routed query. WARNING + audit row.
        _log.warning("catalog_pdf_hook_failed", file_path=file_path_str, source_uri=source_uri,
                     collection=collection_name, exc_info=True)
        _not_recorded("hook_failed")
        from nexus.hook_registry import record_catalog_hook_failure  # noqa: PLC0415 — deferred, avoids an import cycle

        record_catalog_hook_failure(
            source_path=file_path_str or "", collection=collection_name or "",
            hook_name="catalog_pdf_hook", error=str(exc),
        )
    finally:
        if writer is not None:
            writer.close()
        if reader is not None:
            reader.close()  # nexus-qnp5s: HttpCatalogClient.close() is safe


def _force_t3_orphan_cleanup(t3: Any, collection: str, content_hash: str) -> int:
    """Delete orphan T3 chunks for *content_hash* in *collection* — the
    second half of the nexus-9ji ``--force`` deadlock-break.

    The collection stub's ``delete()`` takes an explicit ``ids: list[str]``
    only; it has never accepted a ``where=`` filter. The prior call site
    here (``col.delete(where={"content_hash": content_hash})``) raised
    ``TypeError`` on every single invocation in service mode, silently
    swallowed by a bare ``except Exception`` into a ``force_t3_orphan_
    cleanup_failed`` warning — so ``--force`` has never actually cleaned up
    T3 orphan chunks in service mode. This resolves the matching ids first
    (``get_all_metadata(where=...)``, the collection stub's metadata-filter
    primitive) and deletes them by id.

    Fail-loud by design (no ``except Exception`` swallow): a genuine
    cleanup failure here means ``--force`` did NOT break the deadlock it
    exists to break, and the caller's next re-ingest attempt would hit the
    identical wall with no signal anything went wrong. Both the metadata
    lookup and the delete call propagate their exceptions verbatim,
    including a ``TypeError``-class programming error — it must never again
    be indistinguishable from a transient service failure.

    The *legitimately empty* case (no orphan chunks match) is not an
    error: ``get_all_metadata`` returns an empty id list, ``delete`` is
    never called, and this returns 0 quietly.

    Returns the ACTUAL number of orphan chunks deleted, which may be less
    than ``len(orphan_ids)`` (nexus-o8dil.45, RDR-191 F10c follow-up): this
    function's "orphan" candidates come from a metadata-only lookup
    (``get_all_metadata(where={"content_hash": ...})``), which cannot see
    whether a live catalog manifest row still references one of them.
    ``PgVectorRepository#delete``'s server-side anti-join (nexus-o8dil.5) is
    the authoritative check and can legitimately refuse part of the batch —
    meaning some candidates were never true orphans, and ``--force``'s
    deadlock-break may be incomplete for this *content_hash*. That case is
    NOT the "genuine cleanup failure" the fail-loud contract above talks
    about (no exception; the delete call itself succeeded), so it is
    reported via a WARNING rather than a raise, and the return value
    reflects reality rather than the requested count.
    """
    col = t3.get_or_create_collection(collection)
    # nexus-wbfpw.10 (RDR-192 Step 5 amendment): an "orphan" chunk this
    # cleanup exists to find is, by definition, one with no live
    # own-collection manifest owner -- live(c) would hide exactly the
    # population being cleaned up.
    orphan_meta = col.get_all_metadata(
        where={"content_hash": content_hash}, include_non_live=True,
    )
    orphan_ids = orphan_meta.get("ids", []) or []
    if not orphan_ids:
        _log.info(
            "force_t3_orphan_cleanup_none_found",
            content_hash=content_hash,
            collection=collection,
        )
        return 0
    result = col.delete(orphan_ids)
    actual = result if isinstance(result, int) else len(orphan_ids)
    if actual < len(orphan_ids):
        _log.warning(
            "force_t3_orphan_cleanup_partial_delete",
            content_hash=content_hash,
            collection=collection,
            requested=len(orphan_ids),
            actual=actual,
            note="the server's anti-join refused to delete some candidates "
                 "-- they are still referenced by a live catalog manifest "
                 "row and were never true orphans; the --force deadlock-"
                 "break may be incomplete for this content_hash",
        )
    else:
        _log.info(
            "force_t3_orphan_cleanup_deleted",
            content_hash=content_hash,
            collection=collection,
            orphan_count=actual,
        )
    return actual


def _mark_failed_and_reset_wal(db: HttpPipelineDB, content_hash: str, first_exc: BaseException) -> bool:
    """Terminal bookkeeping for a failed pipeline run: mark the row failed
    and wipe its WAL. Never raises: the caller re-raises *first_exc*, and
    nothing here may mask it. Returns True when the engine FENCED the
    bookkeeping (nexus-8vu8p: a newer resume took the run over between
    the original failure and this cleanup; the row and its WAL belong to
    the new owner, and the caller must not stamp the catalog document
    failed either), False otherwise.

    nexus-33q80: ``clear_orphan_wal`` now zeroes ``chunks_uploaded`` (and
    ``pages_extracted``, nexus-gl99l's own counter) on the pipeline row in
    the SAME transaction as the wipe. This used to be a SEPARATE
    ``db.update_progress(chunks_uploaded=0)`` call right after
    ``clear_orphan_wal`` (review of df5c4f035); if the wipe landed and
    that second call independently failed, the counter survived a wiped
    WAL and a retry seeded the uploader from it, eventually refusing
    completion with ``IndexRunVerifyRefused`` (reproduced by
    substantive-critic as a persisted ``chunks_uploaded`` of 20 for a
    true 10-chunk document, T2 nexus/critique-59c07fe5b-uploader-chunks-
    uploaded-inflation-nexus-6m9zy [26147]). Two client calls could never
    be atomic; one engine call now is, so the second call and its
    ``pipeline_chunks_uploaded_reset_failed_after_wal_wipe`` failure log
    are gone."""
    try:
        db.mark_failed(content_hash, error=str(first_exc))
        db.clear_orphan_wal(content_hash)
    except PipelineRunFenced as fenced:
        _log.warning(
            "pipeline_run_fenced_at_cleanup",
            content_hash=content_hash,
            pipeline_id=fenced.pipeline_id,
            run_epoch=fenced.run_epoch,
            current_epoch=fenced.current_epoch,
            original_error=str(first_exc),
        )
        return True
    except Exception:  # noqa: BLE001 — boundary catch: terminal-state bookkeeping must never mask first_exc
        _log.warning(
            "pipeline_terminal_mark_failed",
            content_hash=content_hash,
            original_error=str(first_exc),
            exc_info=True,
        )
    return False


def _discard_buffer_for_text_only(db: HttpPipelineDB, pdf_path: Path, content_hash: str) -> bool:
    """Wipe the resumed buffer when an explicit ``pymupdf`` run must not resume over it (nexus-tr1rj.14).

    ``pymupdf`` names a document whose every page is the PDF's text layer. A buffer a MinerU run kept
    (a breaker trip keeps it) holds that run's pages, a buffer page that names no rung cannot be
    proven text-only, and a finished extraction another extractor made is not a text-only one. In
    each case the rerun starts clean instead: the whole document is the text layer and is recorded as
    ``pymupdf``. A buffer of text-layer rungs only (an interrupted ``pymupdf`` run) is resumed, and so
    is a finished ``pymupdf`` extraction. Returns ``True`` when it wiped.

    It runs in :func:`pipeline_index_pdf` BEFORE the stages are submitted, never inside a stage: the
    chunker snapshots the buffer's pages and embedded-chunk count when it starts, so a wipe that ran
    beside it would leave it holding the discarded MinerU text and skipping chunks the wipe deleted.
    One engine call wipes the pages and chunks and zeroes both counters; it is fenced, so a newer run's
    takeover raises PipelineRunFenced like any other call of this run.
    """
    state = db.get_pipeline_state(content_hash)
    if not state or not state["pages_extracted"] > 0:
        return False
    rows = db.read_pages(content_hash)
    if not rows:
        return False
    rungs, unknown = _buffer_provenance(rows)
    total_pages = state["total_pages"]
    finished_by_another = False
    if (
        total_pages is not None and state["pages_extracted"] >= total_pages
        and state.get("extraction_meta") and len(rows) >= total_pages
    ):
        # A finished extraction the buffer really backs (its page count agrees with the claim).
        finished_by_another = json.loads(state["extraction_meta"]).get("extraction_method") != "pymupdf"
    del rows
    mineru_pages = sorted(n for n, r in rungs.items() if r not in _TEXT_LAYER_RUNGS)
    if not (finished_by_another or unknown or mineru_pages):
        return False
    _log.warning(
        "resume_buffer_discarded_for_text_only", file_path=str(pdf_path), content_hash=content_hash,
        buffered_pages=len(rungs) + len(unknown), complete_extraction=finished_by_another,
        pages_on_a_mineru_rung=mineru_pages[:10], buffered_rows_without_a_rung=len(unknown),
    )
    db.clear_orphan_wal(content_hash)
    return True


def _reconcile_resumed_run(db: HttpPipelineDB, doc_id: str, content_hash: str) -> str:
    """Decide what a RESUMED run does with the buffer an earlier process left (RDR-223).

    Returns ``"fresh"`` when the buffer holds no upload evidence, ``"tail"`` when the earlier
    process sent every chunk and only the completion stamp is left, ``"reset"`` when it was sent
    in part (or the stamp would not be safe) and the buffer's upload flags were put back so the
    run re-sends the document from position 0, and ``"restarted"`` when the buffer itself was
    unusable and was discarded.

    The multi-batch writer's state lives in the process that ran it, so a resume cannot continue
    an upload that was under way: sending the remaining chunks alone would replace the document's
    manifest with that tail and sweep the head. What the killed process left that is worth keeping
    is the extracted pages and the chunks with their embeddings, and those are untouched by the
    upload flags. The resume therefore asks the engine to reset the flags
    (:meth:`~nexus.db.http_pipeline_client.HttpPipelineDB.reset_uploaded`, which carries the run's
    epoch) and runs the stages again: the extractor finds its pages, the chunker its chunks, and
    the uploader re-sends from chunk 0 through a fresh writer. Nothing is extracted again, and the
    engine's existence partition (RDR-181) means nothing is embedded again. Evidence of an upload
    is a chunk flagged uploaded or a progress counter above zero, both read, because the counter
    is buffered and lags the flags by up to a poll interval.

    The state is read with COUNTS (:meth:`~nexus.db.http_pipeline_client.HttpPipelineDB.chunk_counts`),
    never by reading the unflagged rows.

    The one resume that keeps its flags is the finished upload, and it must prove the upload
    FINISHED, not merely that every chunk written so far is flagged:

    * extraction is complete (``pages_extracted == total_pages``) and the chunker is done
      (``chunks_embedded`` is set, and only the chunker's final branch sets it; its progressive
      updates set ``chunks_created`` alone), so ``chunks_created`` is the final count. While the
      chunker runs ``chunks_created`` is provisional and the uploader holds its newest batch
      back, so "every chunk flagged" is never true of an upload in flight, but a test pins that
      rather than trusting it: a run whose buffer says ``chunks_created == flagged`` without
      those two signals is reset, not stamped;
    * the embedded rows, the flagged rows and ``chunks_created`` all agree;
    * the document's index-run fence is still ``indexing`` for THIS content hash, which is what a
      run leaves that sent its last request and never stamped (a failed post-pass, a kill before
      the stamp). If the document was indexed at other bytes since, the fence carries their hash
      and the manifest is not this buffer's.

    What the tail's stamp proves, and what it does not (M-3): the proof is the file-content hash
    (the buffer and the fence name the same bytes) plus the engine's own check at stamp time, which
    compares the manifest's ROW COUNT and the chunks' existence against the ``chunk_count`` the
    stamp carries and refuses otherwise. A buffer whose manifest was written by a DIFFERENT run of
    the same bytes that left the same fence hash (a ``--streaming never`` run of this PDF chunks it
    differently and stops before its stamp) is caught by that row-count and existence check, not by
    anything this function reads; a manifest with the right count and existing chunks that differs
    in content would pass, and the engine has nothing to compare it against either.

    A buffer the chunker finished whose embedded rows disagree with ``chunks_created`` cannot be
    re-sent (a chunk is missing), so it is the one case that is cleared with
    :meth:`~nexus.db.http_pipeline_client.HttpPipelineDB.clear_orphan_wal` and run from scratch.
    """
    state = db.get_pipeline_state(content_hash) or {}
    counter = int(state.get("chunks_uploaded") or 0)
    embedded, uploaded = db.chunk_counts(content_hash)
    if not uploaded and not counter:
        return "fresh"
    created = state.get("chunks_created")
    total_pages = state.get("total_pages")
    extraction_complete = total_pages is not None and int(state.get("pages_extracted") or 0) >= total_pages
    chunking_complete = state.get("chunks_embedded") is not None and created is not None
    if chunking_complete and embedded != created:
        db.clear_orphan_wal(content_hash)
        _log.warning(
            "pipeline_resume_unusable_buffer_discarded",
            content_hash=content_hash, embedded_rows=embedded, chunks_created=created,
            reason="the chunker finished but the buffer holds a different number of embedded chunks "
                   "than it created; the buffer is discarded and the document runs again from scratch",
        )
        return "restarted"
    if extraction_complete and chunking_complete and uploaded == embedded == created:
        from nexus.doc_indexer import _index_fence_state  # noqa: PLC0415 - deferred to avoid circular import at module load

        fence_state, fence_hash = _index_fence_state(doc_id)
        if fence_state == "indexing" and fence_hash == content_hash:
            if counter != created:
                db.update_progress(content_hash, chunks_uploaded=created)
            _log.info(
                "pipeline_resume_finished_upload_kept",
                content_hash=content_hash, chunks=created,
                reason="every chunk was sent and the fence is still indexing for this content; "
                       "only the completion stamp is left",
            )
            return "tail"
        why = (f"every chunk was sent but the document's fence reads state={fence_state!r} "
               f"hash={'other' if fence_hash != content_hash else 'this'}: the manifest may not be "
               "this buffer's")
    else:
        why = "an earlier process sent part of the document and its write state is gone"
    flagged_reset = db.reset_uploaded(content_hash)
    _log.warning(
        "pipeline_resume_uploads_reset",
        content_hash=content_hash, flagged_rows=flagged_reset, embedded_rows=embedded,
        counter=counter,
        reason=why + "; the upload flags are reset and the document is sent again from chunk 0 "
                     "(nothing is extracted again, and chunks the engine already holds are not "
                     "re-embedded)",
    )
    return "reset"


def _release_interrupted_run(
    db: HttpPipelineDB, content_hash: str, interrupt: BaseException, stage_error: BaseException | None = None,
) -> bool:
    """Give the pipeline row back after a SIGTERM or Ctrl-C (nexus-4vx0a).

    The row moves to ``failed``, so the next run of the document is answered
    ``resuming`` at once instead of ``PipelineConflictRunning`` until the
    heartbeat is five minutes old. The buffer is KEPT, as a crash keeps it: the
    interrupt says nothing about the document, so the next run resumes from the
    pages already extracted, and a stage still running when this is called can
    only add pages to the buffer, never leave a wiped WAL under a counter that
    would skip pages on resume. *stage_error*, when a stage had failed before the
    interrupt, is recorded too (the release then stands in for that failure's
    WAL wipe, which is unsafe while a stage still runs). Never raises. Returns
    True when the engine fenced the write (a newer run owns the row and nothing
    of it is this run's to touch), False otherwise."""
    reason = "interrupted by SIGTERM" if isinstance(interrupt, TerminationRequested) else "interrupted"
    if stage_error is not None:
        reason = f"{reason} after a stage failed ({stage_error})"
    try:
        db.mark_failed(content_hash, error=f"{reason}; the buffer is kept and the next run resumes it")
    except PipelineRunFenced as fenced:
        _log.warning(
            "pipeline_run_fenced_at_interrupt",
            content_hash=content_hash, pipeline_id=fenced.pipeline_id,
            run_epoch=fenced.run_epoch, current_epoch=fenced.current_epoch,
        )
        return True
    except Exception:  # noqa: BLE001 — boundary catch: the interrupt must propagate, not a /fail transport error
        _log.warning("pipeline_interrupt_release_failed", content_hash=content_hash, exc_info=True)
        return False
    _log.info("pipeline_run_released_on_interrupt", content_hash=content_hash, reason=reason)
    return False


def _release_systemic_failure(db: HttpPipelineDB, content_hash: str, failure: MineruSystemicFailure) -> bool:
    """Give the pipeline row back after the MinerU breaker tripped (RDR-237), buffer KEPT.

    The failure says the install or the machine is failing, which says nothing about the
    document, so the exit is :func:`_release_interrupted_run`'s and not
    :func:`_mark_failed_and_reset_wal`'s: the row moves to ``failed``, the pipeline buffer is
    not wiped, and the next run (after the install is repaired) does not write the buffered pages
    again; MinerU still re-extracts every earlier page, so the buffer spares only the writes.
    Chunks of earlier pages that already reached T3 stay there, as an interrupted run's do; no
    catalog manifest is written for the document because the run never reaches the
    tail. The recorded error names the failure class and the worker's last stderr line (the
    exception's message). Never raises. Returns True when the engine fenced the write."""
    try:
        db.mark_failed(content_hash, error=str(failure))
    except PipelineRunFenced as fenced:
        _log.warning(
            "pipeline_run_fenced_at_systemic_failure",
            content_hash=content_hash, pipeline_id=fenced.pipeline_id,
            run_epoch=fenced.run_epoch, current_epoch=fenced.current_epoch,
        )
        return True
    except Exception:  # noqa: BLE001 — boundary catch: the failure must propagate, not a /fail transport error
        _log.warning("pipeline_systemic_failure_release_failed", content_hash=content_hash, exc_info=True)
        return False
    _log.warning(
        "pipeline_run_released_on_systemic_failure",
        content_hash=content_hash, failure_class=failure.failure_class,
        stderr_line=failure.stderr_line, buffer="kept",
    )
    return False


def _extractor_loop_bound_to_cancel(
    pdf_path: Path, content_hash: str, db: HttpPipelineDB, cancel: threading.Event, **kwargs: Any,
) -> ExtractionResult:
    """:func:`extractor_loop` with this run's *cancel* bound to the thread's MinerU workers
    (nexus-4vx0a): a worker started after the run was interrupted is killed as it registers."""
    bind_run_cancel(cancel)
    try:
        return extractor_loop(pdf_path, content_hash, db, cancel, **kwargs)
    finally:
        bind_run_cancel(None)


def _interrupt_after_create(db: HttpPipelineDB, content_hash: str, step: Callable[[], Any]) -> Any:
    """Run *step* between ``create_pipeline`` and the stage block, releasing the row on an interrupt.

    The row is ``running`` from the create on; a Ctrl-C or SIGTERM in this stretch would otherwise
    leave it so for the stale window, the bug nexus-4vx0a fixes for the stages."""
    try:
        return step()
    except (KeyboardInterrupt, TerminationRequested) as exc:
        _release_interrupted_run(db, content_hash, exc)
        raise


def _rearm_failed(db: HttpPipelineDB, content_hash: str, reason: str) -> None:
    """Move the pipeline row to ``failed`` so the next run RESUMES it (``create`` answers
    ``resuming``) instead of finding a leftover and wiping it. Best effort: it keeps the buffer, so
    a failure to write the state costs a re-extraction on the next run and must never mask the
    failure that is propagating."""
    try:
        db.mark_failed(content_hash, error=reason)
    except Exception:  # noqa: BLE001 — boundary catch: best-effort bookkeeping, mirrors the other terminal-state writes in this module
        _log.warning("pipeline_terminal_mark_failed", content_hash=content_hash, reason=reason, exc_info=True)


@sigterm_raises()
def pipeline_index_pdf(
    pdf_path: Path,
    content_hash: str,
    collection: str,
    t3: Any,
    *,
    db: HttpPipelineDB | None = None,
    embed_fn: EmbedFn | None = None,
    extractor: str = DEFAULT_EXTRACTOR,
    corpus: str = "",
    target_model: str = "voyage-context-3",
    git_meta: dict | None = None,
    force: bool = False,
    force_re_embed: bool = False,
    doc_id: str = "",
    hooks: "HookRegistry | None" = None,
    source_uri: str = "",
    allow_degraded_extraction: bool = False,
    dry_run: bool = False,
    on_doc_registered: Callable[[str, bool], None] | None = None,
    extraction_stats: dict | None = None,
    title_override: str = "",
    on_write_started: Callable[[], None] | None = None,
) -> int:
    """Three-stage streaming pipeline for PDFs.

    *extraction_stats* (nexus-i0cwh), when given, receives ``page_count``
    and ``pages_with_text`` from the extraction result once the extract
    stage completes, so ``index_pdf`` can report page coverage without a
    second extraction.

    *title_override* (nexus-1uov1), when non-empty, wins over
    :func:`~nexus.indexer_utils.resolve_pdf_title`'s guess everywhere
    this pipeline resolves a title — the catalog-registration hook below
    and the metadata-enrichment post-pass that stamps every chunk's own
    ``title``. See :func:`nexus.doc_indexer._pdf_chunks`'s docstring for
    the motivating case (``nx dt index``).

    After the three stages complete, runs post-passes to:
    - Enrich chunk metadata from the ExtractionResult
    - Tag table-page chunks
    - Correct chunk_count to the final total
    - Prune stale chunks from a previous version

    *allow_degraded_extraction* (nexus-wi1uv): forwarded to
    ``extractor_loop``/``PDFExtractor.extract``. Default ``False`` — a
    document whose extraction fails the post-extraction quality gate
    raises inside the extractor stage, which this function's
    ``first_exc`` handling turns into a failed pipeline run (fence marked
    failed, orphan WAL cleared) rather than a completed one.

    Args:
        force: Break the partial-ingest deadlock (nexus-9ji). When True,
            pre-flight deletes both (a) the engine pipeline-buffer rows
            for this ``content_hash`` across ``nexus.pdf_pipeline``/
            ``pdf_pages``/``pdf_chunks`` and (b) any orphan T3 chunks in *collection*
            whose ``content_hash`` matches — so neither the pipeline
            state nor half-written prior chunks can silently skip the
            re-ingest or race the upsert. No-op when False.

            The T3 orphan delete in (b) is NOT a general-purpose clear:
            ``_force_t3_orphan_cleanup``'s server-side delete goes through
            ``PgVectorRepository``'s anti-join (nexus-o8dil.5), which
            REFUSES to delete a chunk a live catalog manifest row still
            references. For an already-indexed PDF being re-run with
            ``--force`` (the common case — a fresh PDF has no prior
            manifest row to protect anything), that means the CURRENT
            chunks survive this cleanup untouched: re-upload hits the
            SAME chashes, and without *force_re_embed* the server's
            existence-partition (RDR-181) skips the billed re-embed for
            them, refreshing only metadata. ``force`` alone does NOT
            imply a fresh embed on a re-index.
        force_re_embed: DECOUPLED from *force* (nexus-8143o, mirroring
            nexus-4jj40 round 5's ``repo`` split). Forwarded to ``uploader_loop``,
            which hands it to the multi-batch writer for every request of the
            document — the actual RDR-181 server-side re-embed control. This is the streaming pipeline's
            OWN skip point; it has nothing to do with ``embed_fn`` (the
            chunker stage's optional client-side embedder, used only in
            local/dry-run mode to populate the pipeline staging buffer —
            it always computes whatever it is asked to compute and never
            consults *force_re_embed*, which only governs the SERVER's
            final upsert decision).

    Pass *dry_run=True* (nexus-uxg4u) to skip every catalog/T2 write this
    function would otherwise make — the fallback pre-flight registration
    below, the completion fence (``_fence_begin``/``_fence_complete``/
    ``_fence_fail``), and the ``_catalog_pdf_hook`` registration/linking
    at the tail. Not gated by inference from *embed_fn* or *t3*'s shape:
    a caller that swaps in a no-op embedder for a preview must say so
    explicitly, or a future change reintroducing a real query against
    that throwaway handle would silently start touching the catalog
    again. The pipeline buffer is an IN-MEMORY one on a dry run (RDR-223,
    :func:`~nexus.db.inmemory_pipeline.make_in_memory_pipeline_db`): it is
    the same staging the stages always use, which is what makes the
    preview's chunk counts real, but it sends the engine nothing and never
    shares a row with a real run of the same bytes. A *db* passed with
    *dry_run* must be one of those (``DryRunStoreError`` otherwise).

    Pass *on_doc_registered* (nexus-uxg4u round 2, code-review-expert
    Finding B) to be notified as ``(doc_id, created)`` whenever THIS
    call's own fallback registration (below) fires and resolves an
    identity. *doc_id* can arrive here empty for a reason OTHER than
    "no catalog": the caller's own pre-flight registration can return
    "" via the worktree/tempdir ephemeral-skip path or a swallowed
    registration exception — in which case this function's fallback
    mint is the only registration event for the whole run, and a
    caller tracking created-vs-matched for rollback purposes (e.g.
    ``index_pdf``'s own closure) has no way to see it without this
    callback.

    *on_write_started* is called just before each request that carries chunks (RDR-223): from
    the first one the document may have chunks in the store, all with owner rows, and a caller
    that would undo a fresh registration on failure must not (``index_pdf``'s rollback). A failure
    of the completion stamp after the last chunk landed is one such failure: it propagates, and the
    document's fence stays ``indexing``.

    An interrupt after the row is created (nexus-4vx0a: Ctrl-C, or SIGTERM,
    which :func:`~nexus.util.termination.sigterm_raises` turns into an exception
    on the main thread for the length of this call) hands the pipeline row back
    as ``failed`` with its buffer kept, at once, so an immediate rerun resumes
    instead of meeting ``PipelineConflictRunning``. It also kills this process's
    MinerU workers and stops the stages without waiting for them. If a stage
    had already failed, that failure's own bookkeeping runs instead. The
    interrupt is what propagates, and a SIGTERM is delivered again on the way
    out, so the process still ends as it would have.

    Returns total chunks indexed.
    """
    if dry_run:
        from nexus.doc_indexer import _require_throwaway_pipeline, _require_throwaway_store  # noqa: PLC0415 - deferred to avoid circular import at module load

        _require_throwaway_store(t3, "pipeline_index_pdf")
        if db is None:
            # A preview sends the engine nothing, so its buffer is in memory: the same stages run
            # against it, it never shares (or flags, or resets) a real run's row for these bytes,
            # and a preview that dies leaves nothing behind.
            from nexus.db.inmemory_pipeline import make_in_memory_pipeline_db  # noqa: PLC0415 - deferred: only a dry run needs the twin

            db, _ = make_in_memory_pipeline_db()
        else:
            _require_throwaway_pipeline(db, "pipeline_index_pdf")
    if db is None:
        # Unconditional — no local/service mode dispatch (resolves the
        # bead's backend-selection question): post-RDR-155-P4a the
        # nexus-service IS the serving path in BOTH modes (local mode's
        # endpoint is the bundled local PG engine), so the engine-backed
        # buffer is the only backend.
        db = HttpPipelineDB()

    # Normalize to absolute so staleness checks are path-form-independent.
    pdf_path = pdf_path.resolve()

    # Resolve git provenance once at the entrypoint so chunker_loop and
    # _build_chunk_metadata can stamp every chunk without re-detecting
    # (nexus-2my fix #3).
    if git_meta is None:
        from nexus.indexer_utils import detect_git_metadata  # noqa: PLC0415 - deferred to avoid circular import at module load
        git_meta = detect_git_metadata(pdf_path)

    # RDR-102 Phase A: pre-flight catalog registration for the streaming
    # path. When called via index_pdf (the routing case) the caller already
    # resolved doc_id and passed it through; otherwise (direct invocation,
    # e.g. tests / future callers) resolve it here so chunker_loop can
    # thread doc_id through every chunk metadata. Idempotent on re-index
    # via Catalog.register's by_file_path early-return; returns "" when
    # the catalog is absent (no-catalog ingest contract preserved).
    if not doc_id and not dry_run:
        from nexus.doc_indexer import _register_or_lookup_doc_id  # noqa: PLC0415 - deferred to avoid circular import at module load
        # nexus-uxg4u round 2: with_created=True + on_doc_registered so a
        # mint made HERE (the caller's own doc_id arrived empty) is not
        # invisible to the caller's rollback tracking — see this
        # function's own docstring for why doc_id can be empty here for a
        # reason other than "no catalog".
        _reg_result = _register_or_lookup_doc_id(
            pdf_path, corpus,
            content_type="paper",
            physical_collection=collection,
            with_created=True,
        )
        if isinstance(_reg_result, tuple):
            doc_id, _fallback_created = _reg_result
        else:
            doc_id, _fallback_created = _reg_result, False
        if on_doc_registered is not None:
            on_doc_registered(doc_id, _fallback_created)

    # RDR-223: a chunk is written together with its owner row, and there is no owner without a
    # catalog document. Refuse before extracting, not after minutes of work.
    if not doc_id and not dry_run:
        from nexus.doc_indexer import _raise_identity_missing  # noqa: PLC0415 - deferred to avoid circular import at module load
        _raise_identity_missing(pdf_path, collection, None)

    # nexus-9ji: --force must break the partial-ingest deadlock. Both
    # pipeline-buffer state and T3 orphan chunks can independently block
    # re-ingest; wipe both before the pre-flight.
    if force:
        # nexus-edjmu: this runs BEFORE create_pipeline, so the client holds
        # no pipeline_id yet; name THIS document's row by all three fields
        # or a sibling document sharing the bytes loses its run (the
        # cross-row WAL destruction of the parked key-widen attempt).
        db.delete_pipeline_data(content_hash, collection=collection, pdf_path=str(pdf_path))
        _force_t3_orphan_cleanup(t3, collection, content_hash)

    # Pre-flight: check if pipeline should run before resolving credentials.
    result = db.create_pipeline(content_hash, str(pdf_path), collection)
    if result not in ("created", "resuming"):
        # nexus-edjmu: a document-identity create never answers "skip" (a
        # leftover completed row is reset engine-side and answered
        # "created"), and a fresh-heartbeat 'running' row raises
        # PipelineConflictRunning from create_pipeline() above. Anything
        # else is an engine this client does not know; a silent 0 here was
        # the bead's own symptom (a catalog Document with no manifest).
        raise RuntimeError(
            f"POST /v1/pipeline/create answered status={result!r} for "
            f"content_hash={content_hash}; expected 'created' or 'resuming'"
        )

    # Resolve embed_fn from credentials when not provided (matches batch path).
    # nexus-4vx0a: the row is 'running' from here; an interrupt before the stage block takes over
    # gives it back (_interrupt_after_create), as the stage block does for one during the stages.
    if embed_fn is None:
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)
        if _interrupt_after_create(db, content_hash, is_vector_service_mode):
            # nexus-9n1u3 / RDR-152 Seam B: leave embed_fn=None — the service
            # embeds server-side at upload time. The embed stage writes a
            # non-NULL empty-blob sentinel and the uploader's write ignores
            # client vectors (the JVM embeds).
            # Mirrors the non-streaming path (doc_indexer.index_pdf).
            pass
        else:
            # nexus-sghyo: non-service streaming embedding was retired —
            # the client no longer embeds via Voyage (Hal determination
            # 2026-07-28).
            try:
                db.mark_failed(content_hash, error="non-service embedding retired (nexus-sghyo)")
            except Exception:  # noqa: BLE001 — boundary catch: the RuntimeError below must propagate, not a /fail transport error
                _log.warning(
                    "pipeline_terminal_mark_failed",
                    content_hash=content_hash,
                    exc_info=True,
                )
            raise RuntimeError(
                "non-service embedding was retired: the client no longer "
                "embeds via Voyage. Set NX_STORAGE_BACKEND_VECTORS=service "
                "(the default) or unset it."
            )

    # A resumed row may carry an upload an earlier process died in the middle of; see
    # _reconcile_resumed_run. A dry run sends nothing to an engine, so it leaves the buffer alone.
    resumed_tail = False
    if result == "resuming" and not dry_run:
        try:
            resumed_tail = _reconcile_resumed_run(db, doc_id, content_hash) == "tail"
        except BaseException as exc:
            # create_pipeline answered 'resuming' and the row is 'running' now. A reconcile that
            # dies here (a failed read, the reset call) would leave it so, and the next run would
            # be refused with a 409 until the heartbeat aged out. Hand the row back as failed, the
            # buffer untouched, so the next run resumes it.
            if not isinstance(exc, PipelineRunFenced):
                _rearm_failed(db, content_hash, f"resume reconcile failed: {exc}")
            raise

    # nexus-tr1rj.14: an explicit ``pymupdf`` run does not resume over a MinerU buffer. This runs here,
    # before the stages exist, because the chunker snapshots the buffer when it starts (see
    # _discard_buffer_for_text_only). After a wipe the document uploads through a new writer, whose
    # completion is the stamp's first branch, so ``resumed_tail`` (a finished upload kept) is never read.
    if extractor == "pymupdf":
        try:
            _discard_buffer_for_text_only(db, pdf_path, content_hash)
        except BaseException as exc:
            # The row is 'running' (create_pipeline answered); hand it back like the reconcile above.
            if not isinstance(exc, PipelineRunFenced):
                _rearm_failed(db, content_hash, f"text-only buffer discard failed: {exc}")
            raise

    # RDR-223: the index-run fence begins inside the writer, as its first request, so no chunk
    # lands before it (the old explicit begin sat here). The writer stays open until the tail
    # stamps it complete, after the post-passes.
    run = _interrupt_after_create(db, content_hash, lambda: UploadRun(on_request=on_write_started))
    try:
        cancel = threading.Event()
        extraction_done = threading.Event()
        chunking_done = threading.Event()
        first_exc: BaseException | None = None
        interrupt: BaseException | None = None
        released_fenced = False

        # Not a `with` block: its exit is shutdown(wait=True), which an interrupted run must not
        # take, since a stage inside an uninterruptible call would hold the exit for minutes.
        # The handler below covers the submissions too: a fast first stage can make the run
        # worth interrupting before the last stage is even submitted (nexus-4vx0a).
        pool = ThreadPoolExecutor(max_workers=3)
        all_futures: set[Future] = set()
        try:
            try:
                extract_future = pool.submit(
                    _extractor_loop_bound_to_cancel, pdf_path, content_hash, db, cancel,
                    extractor=extractor,
                    extraction_done=extraction_done,
                    allow_degraded_extraction=allow_degraded_extraction,
                )
                all_futures.add(extract_future)
                chunk_future = pool.submit(
                    chunker_loop, content_hash, db, cancel, embed_fn,
                    extraction_done=extraction_done, chunking_done=chunking_done,
                    pdf_path=str(pdf_path), corpus=corpus, target_model=target_model,
                    git_meta=git_meta, doc_id=doc_id,
                )
                all_futures.add(chunk_future)
                if hooks is None:
                    from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 - deferred to avoid circular import at module load
                    hooks = HookRegistry()
                    install_default_hooks(hooks)
                upload_future = pool.submit(
                    uploader_loop, content_hash, db, t3, collection, cancel,
                    chunking_done,
                    catalog_doc_id=doc_id,
                    hooks=hooks,
                    dry_run=dry_run,
                    force_re_embed=force_re_embed,
                    run=run,
                )
                all_futures.add(upload_future)

                while True:
                    done, not_done = wait(all_futures, timeout=_STAGE_WAIT_SLICE_S, return_when=FIRST_EXCEPTION)
                    if not not_done or any(f.exception() is not None for f in done):
                        break
                for f in done:
                    exc = f.exception()
                    if exc is not None:
                        first_exc = exc
                        cancel.set()
                        break
                while not_done:
                    _, not_done = wait(not_done, timeout=_STAGE_WAIT_SLICE_S, return_when=ALL_COMPLETED)
            except BaseException as exc:  # noqa: BLE001 — nexus-6m9zy.3 (#4), nexus-4vx0a: see below
                # A KeyboardInterrupt (Ctrl-C), or a SIGTERM that sigterm_raises turned into
                # TerminationRequested, lands here on the main thread, inside wait() or a submit,
                # never inside a stage. Setting cancel makes the stages stop at their next check
                # (nexus-6m9zy.3: left running, the uploader once marked the interrupted row
                # 'completed'), and the pool is shut down without waiting below.
                #
                # nexus-4vx0a: when the interrupt is the FIRST failure, the row is given back at
                # once, buffer kept (_release_interrupted_run), before anything that could take
                # time, so a SIGKILL that follows the SIGTERM finds it released. When a stage had
                # already failed, the stage failure owns the bookkeeping (mark failed, WAL wiped,
                # its error recorded) exactly as without the interrupt; cancel has been set since
                # that failure. Either way the interrupt is what propagates, so a SIGTERM still
                # ends the process and a Ctrl-C still reads as one. Any other exception here (a
                # submit that failed) is a failure like a stage's.
                cancel.set()
                if isinstance(exc, (KeyboardInterrupt, TerminationRequested)):
                    interrupt = exc
                    stop_mineru_workers()
                    if first_exc is None:
                        released_fenced = _release_interrupted_run(db, content_hash, exc)
                elif first_exc is None:
                    first_exc = exc
        finally:
            pool.shutdown(wait=interrupt is None, cancel_futures=interrupt is not None)

        if interrupt is not None and first_exc is None:
            if doc_id and not dry_run and not released_fenced:
                from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 - deferred to avoid circular import at module load
                _fence_fail(doc_id, str(interrupt) or type(interrupt).__name__)
            elif doc_id and released_fenced:
                from nexus.mcp_infra import discard_deferred_superseded_vectors  # noqa: PLC0415 - deferred to avoid circular import at module load
                discard_deferred_superseded_vectors(doc_id)
            raise interrupt

        if first_exc is None:
            for f in all_futures:
                exc = f.exception()
                if exc is not None:
                    first_exc = exc
                    break

        if first_exc is not None:
            # nexus-2fyb code-review C-int-2: keep the pipeline row marked
            # 'failed' with the error message (audit trail) BUT clear the
            # orphan pdf_pages / pdf_chunks rows. Otherwise the next
            # create_pipeline() transitions failed → resuming and the
            # chunker_loop seed cache replays the orphaned pages, causing
            # deterministic failures (math PDF + MinerU unavailable) to cycle
            # forever: failed → resuming → re-fail with replayed orphans.
            # The cleared WAL means retry runs extract from scratch — same
            # RuntimeError fires immediately, which is correct.
            # nexus-rewgw (review of the ptctu fix): mark_failed's /fail POST can
            # ITSELF fail (persistent app-level 500 — the gtltb doc-3 class). If
            # that raised through here, clear_orphan_wal and `raise first_exc`
            # would never run: the caller would see the /fail transport error
            # instead of the ORIGINAL failure, and the row would strand
            # 'running' with no audit trail — ptctu's both consequences, one
            # call downstream. The terminal-state writes are best-effort;
            # first_exc propagating is the load-bearing part. The residual (row
            # stays 'running' when the engine's /fail endpoint is down) is
            # covered systemically by lcmbp's young-running-row conflict
            # semantics: the NEXT retry is loud, never a silent skip.
            # nexus-8vu8p: a run the engine fenced (a newer resume of the same
            # document took the row over) owns nothing any more. Its terminal
            # bookkeeping is refused, and it must not stamp the catalog document
            # failed either: that is the SAME doc_id the new owner is indexing
            # (a takeover is per document), and a late stamp would flip a
            # document the new owner has already completed. The fence is
            # discovered either as the stage's own exception or inside the
            # cleanup's /fail call, hence both checks.
            fenced = isinstance(first_exc, PipelineRunFenced)
            if fenced:
                _log.warning(
                    "pipeline_run_fenced",
                    content_hash=content_hash,
                    pipeline_id=first_exc.pipeline_id,
                    run_epoch=first_exc.run_epoch,
                    current_epoch=first_exc.current_epoch,
                )
            elif interrupt is not None and not all(f.done() for f in all_futures):
                # nexus-4vx0a review: the interrupt cut the drain short and a stage is still
                # running. Its next write could land after a WAL wipe and leave a page counter that
                # skips pages on resume, so the row is released with its buffer kept instead; a
                # deterministic failure is wiped by the next run, which fails the same way with
                # every stage stopped.
                fenced = _release_interrupted_run(db, content_hash, interrupt, stage_error=first_exc)
            elif isinstance(first_exc, MineruSystemicFailure):
                # RDR-237: the MinerU install or machine is failing, not this document. Keep the
                # buffer (a rerun skips the writes of the pages already buffered, though MinerU
                # re-extracts them); wiping it would also redo those writes for a cause the PDF
                # did not have.
                fenced = _release_systemic_failure(db, content_hash, first_exc)
            else:
                fenced = _mark_failed_and_reset_wal(db, content_hash, first_exc)
            # nexus-5xn3k.4: _fence_fail never raises, so first_exc propagation
            # below cannot be masked by a fence-write failure. nexus-uxg4u:
            # never touch the catalog on a dry run (doc_id is already "" in
            # that case per the pre-flight gate above, but check explicitly
            # for the same defense-in-depth reason as the fence-begin gate).
            if doc_id and not dry_run and not fenced:
                from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 - deferred to avoid circular import at module load
                _fence_fail(doc_id, str(first_exc))
            elif doc_id and fenced:
                # nexus-4pj54: the fenced path skips _fence_fail, which is where
                # a failed run's deferred superseded-vector sweep is discarded.
                # Discard it here instead: this run no longer owns the manifest,
                # so its held candidates must never be swept.
                from nexus.mcp_infra import discard_deferred_superseded_vectors  # noqa: PLC0415 - deferred to avoid circular import at module load
                discard_deferred_superseded_vectors(doc_id)
            if interrupt is not None:
                raise interrupt from first_exc
            raise first_exc

        # ── Post-passes (after all three stages complete) ────────────────────────

        extraction_result = extract_future.result()
        if extraction_stats is not None:
            _em = getattr(extraction_result, "metadata", None) or {}
            extraction_stats["page_count"] = int(_em.get("page_count", 0) or 0)
            # None when the extraction predates the field (a resumed pipeline
            # buffer written by an older version): "unverified", never "no pages".
            _pwt = _em.get("pages_with_text")
            extraction_stats["pages_with_text"] = list(_pwt) if _pwt is not None else None
            extraction_stats.update(extraction_record(_em))

        # Resolve collection once for all post-passes (avoids repeated API calls).
        col = t3.get_or_create_collection(collection)

        # From here until the stamp has landed, the buffer is what a retry needs: the uploader has
        # marked the row 'completed', and a 'completed' leftover is WIPED by the next create (so a
        # kill in the post-passes or before the stamp would cost a full re-extraction). Hand the
        # row back as 'failed' now, so the next run resumes it and _reconcile_resumed_run sends
        # only the stamp. The buffer is deleted after the stamp, never before.
        _rearm_failed(db, content_hash, "upload finished — kept until the completion stamp lands")

        # Track post-pass success — pipeline data preserved on failure (nexus-pfmr).
        post_pass_ok = True

        # nexus-ahm18: how many chunks the run uploaded, so a post-pass that selects none of them
        # (a manifest that names none of this collection's chunks) fails loud instead of passing.
        _uploaded_state = db.get_pipeline_state(content_hash)
        _uploaded = int(_uploaded_state["chunks_uploaded"] or 0) if _uploaded_state else 0

        # 1. Metadata enrichment from ExtractionResult.
        if not _enrich_metadata_from_extraction(
            content_hash, extraction_result, pdf_path, t3, col, collection,
            title_override=title_override, doc_id=doc_id, dry_run=dry_run, chunks_uploaded=_uploaded,
        ):
            post_pass_ok = False

        # 2. table_regions post-pass.
        table_regions = extraction_result.metadata.get("table_regions", [])
        if table_regions:
            table_pages: set[int] = {r["page"] for r in table_regions}

            def _tag_table_page(meta: dict) -> bool:
                if meta.get("page_number", 0) in table_pages and meta.get("chunk_type") != "table_page":
                    meta["chunk_type"] = "table_page"
                    return True
                return False

            if not _update_chunk_metadata(
                t3, col, collection, content_hash, _tag_table_page, doc_id=doc_id,
                dry_run=dry_run, chunks_uploaded=_uploaded,
            ):
                post_pass_ok = False

        # 3. Stale chunk pruning: DELETED dead code (nexus-tbkk1). This used
        # to query T3 via nexus.doc_indexer._identity_where's source_path
        # fallback, which RDR-102 D2 (2026-05-02) made permanently unable to
        # match any real chunk row (make_chunk_metadata dropped source_path
        # entirely). The real cross-document dedup/prune protection is
        # mcp_infra._sweep_superseded_vectors (manifest-diff based), proven
        # end-to-end at tests/integration/test_tp8yk_manifest_never_outruns_
        # chunks.py::test_union_guard_keeps_shared_chunk_at_the_production_
        # wiring.
        #
        # SIGNAL NARROWING (both reviewers, nexus-tbkk1 fix round): the
        # deleted step's own try/except used to set post_pass_ok=False (and
        # so preserve this run's checkpoint for retry, see below) on a T3
        # QUERY failure during the prune window — that specific signal is
        # gone. Narrow: passes 1/2 above already probe the same T3
        # collection, so a genuinely degraded service is still very likely
        # caught there; only a failure window unique to the (now-removed)
        # prune's own query timing is silently lost.

        state = db.get_pipeline_state(content_hash)
        total_chunks = state["chunks_uploaded"] if state else 0

        if not post_pass_ok:
            # nexus-6m9zy.5 (#10): uploader_loop already called db.mark_completed() -- BEFORE any
            # post-pass ran -- the moment chunks_uploaded caught up to chunks_created. A
            # 'completed' leftover is wiped by the next create_pipeline() (nexus-edjmu), which
            # would discard the very checkpoint this branch preserves. The row was handed back as
            # 'failed' above (never clear_orphan_wal: that would delete the chunk/page data too),
            # so the next create sees 'failed' -> 'resuming', every stage short-circuits (all of it
            # is uploaded) and the post-passes run again for a genuine retry.
            _log.warning(
                "pipeline_data_preserved",
                content_hash=content_hash,
                reason="one or more post-passes failed — data kept for retry",
            )

        # Catalog hook: register PDF in catalog (opt-in, graceful absence)
        # 2026-08-19: this used to read ``metadata["title"]`` / ``["author"]`` —
        # keys no extractor writes (``pdf_title``/``pdf_author``
        # are the real ones) — so every streamed PDF registered as its filename
        # stem with no author. One shared chain for all PDF title resolution.
        #
        # nexus-uxg4u: never touch the catalog on a dry run — this hook
        # (unlike the fence calls above) writes unconditionally regardless of
        # *doc_id*, so it needs its own explicit gate, not just an empty
        # *doc_id* check.
        if not dry_run:
            from nexus.indexer_utils import resolve_pdf_title  # noqa: PLC0415 - circular-dep avoidance (nexus.indexer_utils)
            _meta = getattr(extraction_result, "metadata", None) or {}
            title = title_override or resolve_pdf_title(_meta, pdf_path, getattr(extraction_result, "text", None))
            author = str(_meta.get("pdf_author") or _meta.get("author") or "")
            # Extract year from pdf_creation_date or explicit year field
            year_raw = 0
            if _meta:
                year_raw = _meta.get("year", 0)
                if not year_raw:
                    creation_date = _meta.get("pdf_creation_date", "")
                    if creation_date:
                        import re as _re  # noqa: PLC0415 - branch-local; deferred to call time
                        m = _re.search(r"(\d{4})", str(creation_date))
                        if m:
                            year_raw = int(m.group(1))
            _catalog_pdf_hook(
                pdf_path=pdf_path,
                collection_name=collection,
                title=title,
                author=author,
                year=int(year_raw) if year_raw else 0,
                corpus=corpus,
                chunk_count=total_chunks,
                source_uri=source_uri,
                extraction=extraction_record(_meta),
            )

            # RDR-089 document-grain chain — once per PDF boundary at the
            # streaming pipeline tail. content="" (PDF text streamed not
            # retained); the hook reads source_path itself per the P0.1
            # content-sourcing contract.
            # nexus-tdgc: _catalog_pdf_hook ran above so the catalog entry
            # now exists; resolve the doc_id and forward it to the document
            # chain.
            from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - deferred to avoid circular import at module load
            from nexus.doc_indexer import _lookup_existing_doc_id  # noqa: PLC0415 - deferred to avoid circular import at module load
            _cat = make_catalog_reader()
            hooks.fire_document(
                str(pdf_path), collection, "",
                doc_id=_lookup_existing_doc_id(_cat, str(pdf_path), corpus),
            )

        # nexus-5xn3k.4 RUNFENCE C2: the fence tail — after every possible
        # manifest touch (including the fire_document hook above), before
        # returning. Gated on doc_id per the no-catalog ingest contract
        # (nexus-uxg4u: and explicitly on dry_run).
        if doc_id and not dry_run:
            from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 - deferred to avoid circular import at module load
            if total_chunks == 0:
                # MUST: zero extraction is a FAILURE, never /complete(0) — a
                # zero-chunk run would trivially satisfy the fail-closed gate
                # (referenced=0 == chunk_count=0) and stamp 'complete' on a
                # silently-failed extraction. No content-free exception exists
                # for PDFs.
                _fence_fail(doc_id, "zero chunks extracted")
            elif post_pass_ok:
                from nexus.errors import IndexRunVerifyRefused  # noqa: PLC0415 - deferred: keeps the module import light

                try:
                    if run.writer is not None and run.finished:
                        # The writer sent every request and left the run 'indexing'
                        # (defer_completion): the post-passes above have run, so the document is
                        # whole. A refusal propagates, as the explicit stamp's did; the writer
                        # recorded it.
                        run.writer.complete()
                    elif resumed_tail:
                        # Every chunk was written by an earlier process (a retry of a run whose
                        # post-pass failed, or that died before its stamp: nothing was left to
                        # upload, so no writer ran) and the document's fence proved the manifest
                        # is that run's (_reconcile_resumed_run). Stamp it, fail-closed: an
                        # unstamped answer or a transport failure raises and the fence stays
                        # 'indexing'.
                        from nexus.doc_indexer import _stamp_finished_upload  # noqa: PLC0415 - deferred to avoid circular import at module load
                        _stamp_finished_upload(doc_id, content_hash, total_chunks)
                    else:
                        raise RuntimeError(
                            f"pipeline_index_pdf: {total_chunks} chunk(s) are uploaded for "
                            f"{content_hash[:12]} but no writer finished and the buffer is not a "
                            "finished upload; refusing to stamp the document complete"
                        )
                except IndexRunVerifyRefused:
                    # The engine says this buffer is not the document (row count or chunk
                    # existence). Re-stamping it would be refused again for ever, so the buffer
                    # goes and the next run extracts afresh. The fence stays 'indexing'.
                    db.delete_pipeline_data(content_hash)
                    raise
                # Any other stamp failure leaves the row 'failed' and the buffer in place (above),
                # so the next run resumes it as a tail.
            else:
                _log.warning(
                    "index_run_complete_skipped_post_pass_failed",
                    doc_id=doc_id,
                    content_hash=content_hash,
                    reason="post-pass failed — fence stays 'indexing' for retry",
                )

        # The buffer goes LAST, after the stamp (or where none is needed): a stamp that failed, or
        # a kill before it, must find the buffer so the next run sends only the stamp.
        if post_pass_ok:
            db.delete_pipeline_data(content_hash)

        return total_chunks
    finally:
        run.close()


def _enrich_metadata_from_extraction(
    content_hash: str,
    result: ExtractionResult,
    pdf_path: Path,
    t3: Any,
    col: Any,
    collection: str,
    *,
    title_override: str = "",
    doc_id: str,
    dry_run: bool = False,
    chunks_uploaded: int = 0,
) -> bool:
    """Post-pass: update chunk metadata with fields from ExtractionResult.

    nexus-ahm18: the chunks to update are the ones the PDF's own manifest names
    (*doc_id* -> ``get_manifest`` -> chash ids), never ``where content_hash=H``. A
    chunk row's ``content_hash`` is the last writer's, so that where missed this
    PDF's chunks another document had restamped and hit rows the PDF does not own.
    Selection by manifest updates every chunk the PDF holds (a shared one included)
    and no chunk it does not.

    It does NOT make the values per-document, and it writes rows develop's selection
    never reached. A row another document restamped between this PDF's upload and
    this post-pass (its ``content_hash`` is not *content_hash*) is now written: its
    ``title``, ``source_author`` and ``extraction_method`` are replaced by this PDF's
    (the last-writer-wins limitation until those keys live in the catalog,
    nexus-6pfgb.6, and the chunk-row keys are retired, nexus-6pfgb.27). Nothing else
    is touched on such a row: no ``delete_keys`` (the co-owner's
    ``quality_gate_overridden`` audit flag is never removed, and this PDF's own flag
    is not set on it), and a key whose value here is empty is skipped, so a
    co-owner's non-empty value is never blanked. A row that still carries this
    PDF's hash is written in full (the empty strings, the flag's removal on a healthy
    run), as develop wrote it. Each selected row's ``content_hash`` is read first
    (one ``col.get(ids=...)`` per 300), then only keys are written, so the
    nexus-w94eo merge race stays closed.

    *doc_id* is keyword-required, so a caller states whether it has a document: an empty one
    (``""``, a direct caller with no catalog identity) keeps the ``content_hash`` selection, as
    does *dry_run* (a throwaway store with no catalog and nothing shared). On a real run with
    *doc_id* a selection of no chunk while *chunks_uploaded* is positive fails the
    post-pass (``post_pass_selected_no_chunks``): zero rows examined is not a pass.

    Resolves source_title (pdf_title → first heading → filename) and
    source_author — matching the batch path in doc_indexer._pdf_chunks.
    *title_override* (nexus-1uov1), when non-empty, wins over that
    resolution — see :func:`nexus.doc_indexer._pdf_chunks`'s docstring
    for the DEVONthink motivating case; this is the streaming path's
    equivalent hook, since streaming discards per-chunk metadata as it
    flushes and corrects title here, after upload, instead.

    RDR-108 Phase 3: ``chunk_count`` was retired from the chunk schema
    (catalog ``document_chunks`` manifest carries it at document scope),
    so the post-pass no longer has to correct chunk_count after the fact.

    Returns True on success, False on failure (nexus-pfmr).
    """
    meta = result.metadata
    page_count = meta.get("page_count", 0) or 1
    text_len = len(result.text) if result.text else 0

    from nexus.indexer_utils import resolve_pdf_title  # noqa: PLC0415 — circular-dep avoidance (nexus.indexer_utils)
    source_title = title_override or resolve_pdf_title(meta, pdf_path, result.text)

    # `title`, `source_author`, (nexus-1oguj) `extraction_method`, and
    # (nexus-wi1uv round-2) `quality_gate_overridden` are the only
    # extraction-dependent fields in ALLOWED_TOP_LEVEL — the other fields
    # below (source_date, format, page_count, pdf_subject, pdf_keywords,
    # is_image_pdf, has_formulas) are dropped by metadata_schema.normalize()
    # so writing them costs cycles for no payload. Keep this dict minimal.
    #
    # nexus-w94eo: the engine now MERGES this dict into each row's stored
    # metadata (metadata = chunks.metadata || EXCLUDED.metadata) rather than
    # replacing it, so omitting quality_gate_overridden no longer clears a
    # stale True from an earlier degraded run — a merge can only add/
    # overwrite a key, never retract one by omission. When THIS run's
    # extraction did not trip the quality gate, request the key's removal
    # explicitly via delete_keys instead of counting on the write to wipe it.
    # Deleting a key that was never present is a Postgres jsonb no-op, so
    # this is safe to send unconditionally on the healthy branch.
    enrichment = {
        "title": source_title,
        "source_author": meta.get("pdf_author", ""),
        "extraction_method": meta.get("extraction_method", ""),
    }
    delete_keys: list[str] = []
    if meta.get("quality_gate_overridden", False):
        enrichment["quality_gate_overridden"] = True
    else:
        delete_keys.append("quality_gate_overridden")

    try:
        # nexus-w94eo: ids only — no read-modify-write. The pre-fix shape
        # ({**m, **enrichment} over every fetched row's CURRENT metadata) was
        # itself a second race on top of the one this bead fixes: any write
        # landing between this read and this method's own write (another
        # post-pass, a concurrent frecency reindex) was silently discarded
        # the moment this method's copy-of-the-old-state committed over it.
        # The engine's merge makes the read of the metadata unnecessary —
        # sending only `enrichment`'s keys leaves every other key (and any
        # write that lands in between) untouched. (nexus-ahm18: the only read
        # left is each selected row's content_hash, to tell a row this PDF last
        # wrote from one another document restamped; no metadata is written
        # back from it.)
        if doc_id and not dry_run:
            chashes = list(dict.fromkeys(r.chash for r in _own_manifest_rows(doc_id, collection)))
            stored = _stored_rows_by_id(col, chashes)
            if not stored and _selected_nothing(
                "enrich", doc_id, collection, content_hash, len(chashes), chunks_uploaded,
            ):
                return False
            _warn_manifest_chunks_not_live("enrich", doc_id, collection, content_hash, chashes, stored)
            own_ids = [c for c in chashes if c in stored and stored[c].get("content_hash") == content_hash]
            other_ids = [c for c in chashes if c in stored and stored[c].get("content_hash") != content_hash]
        else:
            own_ids, other_ids = _throwaway_ids_by_content_hash(col, content_hash), []

        if own_ids:
            t3.update_chunks(
                collection, own_ids, [enrichment] * len(own_ids), delete_keys=delete_keys,
            )
        # Rows another document restamped: values only. No delete_keys, no empty value, and not
        # quality_gate_overridden, which is this document's fact about its own rows.
        shared = {k: v for k, v in enrichment.items() if k != "quality_gate_overridden" and v not in ("", None)}
        if other_ids and shared:
            t3.update_chunks(collection, other_ids, [shared] * len(other_ids))
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort metadata enrichment; logged via log.warning, returns False
        _log.warning("metadata_enrichment_failed", content_hash=content_hash, error=str(exc))
        return False


_MANIFEST_ID_BATCH = 300


def _own_manifest_rows(doc_id: str, collection: str) -> list[ManifestRow]:
    """nexus-ahm18: the rows of *doc_id*'s own manifest that belong to *collection*, in position order.

    One ``get_manifest`` call. A row's own ``collection`` (nexus-kzso5) is honoured when the engine
    sends it; ``None`` (a pre-field engine) is taken as this collection. A catalog-read failure
    propagates: the post-passes turn it into a failed post-pass, which keeps the pipeline buffer for a
    retry, never into a silent empty selection.
    """
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - circular-dep avoidance (nexus.catalog.factory)

    reader = make_catalog_reader()
    try:
        rows = reader.get_manifest(doc_id)
    finally:
        reader.close()
    return [
        r for r in rows
        if getattr(r, "chash", "") and getattr(r, "collection", None) in (None, "", collection)
    ]


def _stored_rows_by_id(col: Any, chashes: list[str]) -> dict[str, dict]:
    """The stored metadata of the chunk rows *chashes*, by id, read in batches of 300.

    A chash no row holds is absent from the result. A failing batch raises, before any write.
    """
    stored: dict[str, dict] = {}
    for start in range(0, len(chashes), _MANIFEST_ID_BATCH):
        batch = _vector_with_retry(
            col.get,
            ids=chashes[start:start + _MANIFEST_ID_BATCH],
            include=["metadatas"],
        )
        stored.update(zip(batch.get("ids", []), batch.get("metadatas", [])))
    return stored


def _selected_nothing(
    post_pass: str, doc_id: str, collection: str, content_hash: str, manifest_chunks: int, chunks_uploaded: int,
) -> bool:
    """True, after a loud warning, when a real run's post-pass selected no chunk although some were uploaded.

    A manifest that names none of this collection's chunks (unreadable as empty, every row of another
    collection, no row live) would otherwise pass having examined nothing, and the PDF would finish with
    no title, author or extraction method and no signal (the nexus-moht0 vacuous-gate doctrine). The
    caller turns True into a failed post-pass, which keeps the pipeline buffer for a retry. No chunk
    uploaded (a zero-chunk PDF) is not this case.
    """
    if chunks_uploaded <= 0:
        return False
    _log.warning(
        "post_pass_selected_no_chunks",
        post_pass=post_pass, doc_id=doc_id, collection=collection, content_hash=content_hash,
        manifest_chunks_in_collection=manifest_chunks, chunks_uploaded=chunks_uploaded,
        remedy=_SELECTED_NO_CHUNKS_REMEDY,
    )
    return True


_SELECTED_NO_CHUNKS_REMEDY = (
    "a rerun resumes the kept pipeline buffer and fails this post-pass again; "
    "`nx index pdf --force <path>` drops the buffer and re-extracts"
)


def _warn_manifest_chunks_not_live(
    post_pass: str, doc_id: str, collection: str, content_hash: str, chashes: list[str], stored: dict[str, dict],
) -> None:
    """Warn, with the count, when some of the manifest's chunks have no live T3 row although others do.

    The post-pass updates the rows that exist and passes; a manifest chash with no row (purged, or
    never written) is otherwise dropped without a trace. (None live at all is the louder
    ``post_pass_selected_no_chunks`` failure.)
    """
    missing = len(chashes) - len(stored)
    if stored and missing > 0:
        _log.warning(
            "post_pass_manifest_chunks_not_live",
            post_pass=post_pass, doc_id=doc_id, collection=collection, content_hash=content_hash,
            manifest_chunks_in_collection=len(chashes), not_live=missing, live=len(stored),
        )


def _throwaway_ids_by_content_hash(col: Any, content_hash: str) -> list[str]:
    """The ids of *content_hash*'s rows in a dry run's throwaway store.

    The only place a post-pass still selects by ``content_hash``: a dry run has no catalog, so no
    manifest to read, and its store holds nothing another document wrote, so the value cannot have
    been restamped by a co-owner (nexus-ahm18).
    """
    ids: list[str] = []
    offset = 0
    while True:
        batch = _vector_with_retry(
            col.get,
            where={"content_hash": content_hash},
            limit=300,
            offset=offset,
        )
        ids.extend(batch.get("ids", []))
        if len(batch.get("ids", [])) < 300:
            return ids
        offset += 300


def _own_labels_view(meta: dict, row: Any) -> dict:
    """A copy of a chunk row's metadata whose labels are the document's own.

    A chunk several documents hold has one T3 row, and its ``page_number`` / ``section_*`` are the
    last writer's. The manifest row carries the label of this document's own position, and
    ``labels_written`` says it is authoritative (an engine before the label columns, and a row
    written before them, send ``labels_written`` false: the chunk row is then the only record, and
    is used as it stands). Page 0 travels as null on the wire and means "not paged".
    """
    view = dict(meta)
    if getattr(row, "labels_written", False):
        view["page_number"] = row.page_number or 0
        view["section_title"] = row.section_title or ""
        view["section_type"] = row.section_type or ""
    return view


def _update_chunk_metadata(
    t3: Any,
    col: Any,
    collection: str,
    content_hash: str,
    update_fn: Callable[[dict], bool],
    *,
    doc_id: str,
    dry_run: bool = False,
    chunks_uploaded: int = 0,
) -> bool:
    """Generic post-pass: apply update_fn to each chunk of the document's own manifest.

    nexus-ahm18: the chunks are the ones *doc_id*'s manifest names, read with one ``get_manifest``,
    never ``where content_hash=H`` (the last writer's value on a shared row). Their rows are read by
    id in batches of 300, and *update_fn* is handed each row's metadata with the document's own
    labels laid over it (:func:`_own_labels_view`), so a position judged by page is judged by the
    manifest's page for this document, not by the page a co-owner wrote on the shared row. A chunk
    the manifest names at several positions is updated if any of them says so. The overlay is never
    written: only the keys *update_fn* added or changed against that view are.

    A row another document restamped since this PDF's upload (its ``content_hash`` is not
    *content_hash*) is written where develop's selection never reached it, so the write there is
    non-destructive: a position whose manifest row has no label (``labels_written`` false, every row
    on engine 0.1.164) is skipped, since the only page it has is the co-owner's and a tag judged by
    it would mark the co-owner's chunk with this document's table page; a key whose new value is
    empty is skipped, so a co-owner's non-empty value is never blanked; nothing is deleted (no
    caller deletes a key). A row that still carries *content_hash* behaves as it always did, the
    chunk row's page being this document's own. Document-level keys written onto a shared row
    still overwrite the co-owner's until they move to the catalog (nexus-6pfgb.6) and the chunk-row
    keys are retired (nexus-6pfgb.27).

    *dry_run* (a throwaway store with no catalog and nothing shared) selects by *content_hash*, as
    does an empty *doc_id*, and has no manifest labels to overlay. On a real run with *doc_id* a
    selection of no chunk while *chunks_uploaded* is positive fails the post-pass.

    Returns True on success, False on failure (nexus-f8it).

    nexus-vhyar: only the keys *update_fn* added or changed are written. The engine merges, so the
    rest of the row is untouched; writing the whole row back re-asserted every key as it stood at
    read time over any write that committed in between (the read-modify-write race nexus-w94eo
    removed from the enrichment post-pass). A key *update_fn* deletes is not written and so not
    removed; no caller deletes one today.
    """
    try:
        # (chunk id, the manifest row whose labels judge it or None, the row's metadata, whether this
        # document was the row's last writer)
        selected: list[tuple[str, Any, dict, bool]] = []
        if doc_id and not dry_run:
            rows = _own_manifest_rows(doc_id, collection)
            chashes = list(dict.fromkeys(r.chash for r in rows))
            stored = _stored_rows_by_id(col, chashes)
            selected = [
                (r.chash, r, stored[r.chash], stored[r.chash].get("content_hash") == content_hash)
                for r in rows if r.chash in stored
            ]
            if not selected and _selected_nothing(
                "table_page", doc_id, collection, content_hash, len(chashes), chunks_uploaded,
            ):
                return False
            _warn_manifest_chunks_not_live("table_page", doc_id, collection, content_hash, chashes, stored)
        else:
            all_ids: list[str] = []
            all_metas: list[dict] = []
            offset = 0
            while True:
                batch = _vector_with_retry(
                    col.get,
                    where={"content_hash": content_hash},
                    include=["metadatas"],
                    limit=300,
                    offset=offset,
                )
                all_ids.extend(batch.get("ids", []))
                all_metas.extend(batch.get("metadatas", []))
                if len(batch.get("ids", [])) < 300:
                    break
                offset += 300
            selected = [(cid, None, meta, True) for cid, meta in zip(all_ids, all_metas)]
    except Exception as exc:  # noqa: BLE001 - best-effort chunk-metadata query; logged via log.warning, returns False
        _log.warning("chunk_metadata_query_failed", content_hash=content_hash, doc_id=doc_id, error=str(exc))
        return False

    deltas: dict[str, dict] = {}
    for cid, row, meta, own in selected:
        if not own and not getattr(row, "labels_written", False):
            continue  # the page on this row is the co-owner's: it cannot judge this document's chunk
        view = _own_labels_view(meta, row) if row is not None else meta
        before = dict(view)
        if update_fn(view):
            delta = {k: v for k, v in view.items() if k not in before or before[k] != v}
            if not own:
                delta = {k: v for k, v in delta.items() if v not in ("", None)}
            if delta:
                deltas.setdefault(cid, {}).update(delta)

    if deltas:
        try:
            t3.update_chunks(collection, list(deltas), list(deltas.values()))
        except Exception as exc:  # noqa: BLE001 - best-effort chunk-metadata update; logged via log.warning, returns False
            _log.warning("chunk_metadata_update_failed", count=len(deltas), error=str(exc))
            return False
    return True


# nexus-tbkk1: _prune_stale_chunks DELETED as dead code. It queried T3
# via nexus.doc_indexer._identity_where's source_path fallback, which
# RDR-102 D2 (2026-05-02, commit 83ac62c7) made permanently unable to
# match any real chunk row — make_chunk_metadata dropped source_path
# from chunk metadata entirely, so every chunk this pipeline writes
# carries no source_path key (empirically confirmed zero-source_path
# across a representative T3 sample — code__1-1, docs__1-1,
# knowledge__knowledge/dt-papers/augur-oracle-papers/interpretability —
# see nexus.doc_indexer._identity_where's docstring). The where-clause
# always matched zero rows in production; the union-guard LOGIC it
# exercised (nexus.indexer_utils.orphaned_chashes) remains live — called
# directly by mcp_infra._sweep_superseded_vectors — and is covered by
# tests/db/test_http_catalog_integration.py::TestPruneUnionGuard. Its
# thin wrapper prune_orphan_candidates (built specifically for this
# call site and its three siblings, all now deleted) was ALSO deleted
# in this same fix round (zero production callers survived) — see
# indexer_utils.py's deletion comment. This closes only the
# doc_indexer.py/pipeline_stages.py HALF of RDR-102 D2's "Phase 5b"
# 4-site dead-code class — the indexer.py/indexer_utils.py sibling sites
# (nx index repo's code/prose paths) were audited and deleted by
# nexus-afudo (2026-08-05); Phase 5b is now fully closed. Automatic (fires-on-every-reindex)
# replacement protection for THIS pipeline is mcp_infra._sweep_
# superseded_vectors (manifest-diff based, fires from the same
# fire_batch/fire_document hook chain this pipeline already calls),
# proven end-to-end at tests/integration/test_tp8yk_manifest_never_
# outruns_chunks.py::test_union_guard_keeps_shared_chunk_at_the_
# production_wiring — but it cannot see a legacy row whose owning
# document's manifest never referenced it. nx t3 gc (RDR-108 Phase 4
# chash-vs-manifest sweep, src/nexus/commands/t3.py:219) is the
# comprehensive but manual/operator-triggered backstop for that
# population; no one-time sweep was run as part of closing this bead
# (production mutation, Hal-gated).
