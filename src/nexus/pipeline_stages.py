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
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait, ALL_COMPLETED, FIRST_EXCEPTION
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.hook_registry import HookRegistry

import structlog

from nexus.embed_window import window_for_model
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import ExtractionResult, PDFExtractor
from nexus.db.http_pipeline_client import HttpPipelineDB, PipelineRunFenced
from nexus.retry import _vector_with_retry

_log = structlog.get_logger(__name__)

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

EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]


class PipelineCancelled(Exception):
    """Raised inside on_page to abort extraction when cancel is set."""


# ── Stage 1: Extractor ──────────────────────────────────────────────────────


def extractor_loop(
    pdf_path: Path,
    content_hash: str,
    db: HttpPipelineDB,
    cancel: threading.Event,
    extractor: str = "auto",
    on_formula_oom: str = "fail",
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

    # Resume fast path: if all pages already in buffer, skip extraction.
    if (state and state["total_pages"] is not None
            and state["pages_extracted"] >= state["total_pages"]
            and state.get("extraction_meta")):
        # nexus-gl99l: the counters above are NOT proof the WAL pages
        # backing them still exist. pipeline_index_pdf's caught-exception
        # handler calls db.mark_failed + db.clear_orphan_wal, which wipes
        # the pdf_pages/pdf_chunks WAL rows but never resets these
        # progress counters (deliberately — mark_failed's audit trail is
        # load-bearing, nexus-2fyb/nexus-rewgw). Trusting the counters
        # alone after ANY caught pipeline exception means a retry sees a
        # row that CLAIMS full extraction with none of the underlying
        # data, and silently skips re-extraction. Verify the actual WAL
        # page count agrees with the claim before trusting the fast path
        # — this also self-heals rows stranded by any earlier code
        # version that never checked, not just future failures.
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
        actual_pages = len(db.read_pages(content_hash))
        if actual_pages >= state["total_pages"]:
            stored_meta = json.loads(state["extraction_meta"])
            if extraction_done is not None:
                extraction_done.set()
            return ExtractionResult(text="", metadata=stored_meta)
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
        # mark_failed + clear_orphan_wal (the WAL is wiped but
        # pages_extracted survives it, per the nexus-gl99l comment above).
        # pages_extracted alone cannot distinguish the two: verify against
        # the actual WAL before trusting it, exactly as the fast path
        # above does for the total_pages-set case. When the WAL still
        # backs the counter this is a no-op (actual_pages ==
        # pages_extracted_at_start); when clear_orphan_wal wiped it, this
        # is what stops on_page from skipping pages the retry needs to
        # re-extract.
        pages_extracted_at_start = len(db.read_pages(content_hash))

    def on_page(page_index: int, page_text: str, page_metadata: dict) -> None:
        if cancel.is_set():
            raise PipelineCancelled("pipeline cancelled")
        if page_index < pages_extracted_at_start:
            return
        db.write_page(content_hash, page_index, page_text, metadata=page_metadata)
        db.update_progress(content_hash, pages_extracted=page_index + 1)

    ext = PDFExtractor()
    try:
        try:
            result = ext.extract(
                pdf_path, extractor=extractor, on_formula_oom=on_formula_oom, on_page=on_page,
                allow_degraded=allow_degraded_extraction,
            )
        except PipelineCancelled:
            return ExtractionResult(text="", metadata={"page_count": 0, "table_regions": []})

        page_count = result.metadata.get("page_count", 0)
        db.update_progress(content_hash, total_pages=page_count)
        # Store extraction metadata for resume (avoids re-extraction on crash recovery).
        db.store_extraction_metadata(content_hash, result.metadata)
        return result
    finally:
        # nexus-2fyb code-review C-int-1: must signal extraction_done even on
        # raise. The chunker_loop spins on extraction_done.wait(timeout=0.5)
        # and would otherwise block for a full timeout cycle on every
        # extraction failure (math PDF without MinerU, etc.). The
        # cancel-set + wait-not_done shutdown path observes this eventually,
        # so today this is liveness-degradation not deadlock — but raise must
        # NOT be allowed to leave downstream stages waiting.
        if extraction_done is not None:
            extraction_done.set()


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
    chunker = PDFChunker(chunk_chars=chunk_chars, token_window=window_for_model(target_model))
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

    # Indexing review C2: every exit path must signal chunking_done so the
    # uploader doesn't block forever. Previously ``_signal_done()`` sat after
    # the while loop and after the early-return in the final branch — an
    # exception in the embed/write step skipped both, relying on the
    # orchestrator's cancel.set() to rescue. Wrap in try/finally instead
    # so the event fires regardless of how we leave the loop.
    try:
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
    finally:
        _signal_done()


# ── Stage 3: Uploader ───────────────────────────────────────────────────────


class PartialUploadResumeError(RuntimeError):
    """A resumed streaming run found chunks an earlier process had already sent, and more to send.

    The multi-batch writer keeps its state (the pre-run manifest snapshot, the positions and
    chashes it wrote) in the process that runs it. A process killed part way through the upload
    took that state with it; a resumed run that sent only the remaining chunks would replace the
    document's manifest with that tail and sweep the head as superseded, and the completion
    check would pass on the tail's row count. So the run fails instead, and the failure handler
    clears the buffer: the next run starts the document afresh (the engine skips re-embedding
    chunks it already holds).
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

    def __init__(self) -> None:
        self.writer: Any = None
        self.cat: Any = None
        self.finished = False          # every chunk landed: the writer's finish() returned
        self._raw_cat: Any = None

    def open_writer(
        self, *, doc_id: str, collection: str, content_hash: str, force_re_embed: bool,
        defer_completion: bool,
    ) -> Any:
        from nexus.catalog.multi_batch_write import MultiBatchDocumentWriter  # noqa: PLC0415 — deferred: multi_batch_write imports the vector client
        from nexus.doc_indexer import _MetadataMergingCatalog  # noqa: PLC0415 — deferred: doc_indexer imports this module lazily

        self._raw_cat = _make_upload_catalog()
        # The streaming stub's metadata is deliberately partial (the post-pass fills title, author
        # and extraction method), so the write MERGES it into what is stored and names no keys to
        # delete: replacing would strip the ``bib_*`` enrichment a forced re-index must keep.
        self.cat = _MetadataMergingCatalog(self._raw_cat, [])
        self.writer = MultiBatchDocumentWriter(
            self.cat, doc_id=doc_id, collection=collection, content_hash=content_hash,
            force_re_embed=force_re_embed, defer_completion=defer_completion)
        return self.writer

    def close(self) -> None:
        close = getattr(self._raw_cat, "close", None)
        self._raw_cat = None
        if close is not None:
            close()


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
    post-store hooks fire at the same point. A resumed run that finds chunks an earlier process
    flagged and more to send fails with :class:`PartialUploadResumeError` (see there).

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
        from nexus.doc_indexer import _account_write_result  # noqa: PLC0415 - deferred to avoid circular import at module load

        result = run.writer.finish()
        run.finished = True
        _account_write_result(result, run.cat.sweep_errors, catalog_doc_id, collection)
        _flag(held)
        held = None

    try:
        while not cancel.is_set():
            # One held batch is still unflagged in the buffer, so read past it.
            rows = db.read_uploadable_chunks(content_hash, limit=2 * _UPLOAD_BATCH_SIZE)
            batch_rows = [r for r in rows if r["chunk_index"] > last_added][:_UPLOAD_BATCH_SIZE]

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
                    if persisted_uploaded and run.writer is None:
                        raise PartialUploadResumeError(
                            f"{persisted_uploaded} chunk(s) of this PDF were uploaded by an earlier "
                            "run that did not finish, and its write state is gone with that "
                            "process; sending only the rest would replace the document's manifest "
                            "with a fragment. The run's buffer is cleared: run the index again to "
                            "start the document afresh (chunks the engine already holds are not "
                            "re-embedded)."
                        )
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

            if chunking_done is not None:
                # Orchestrated path: wait for chunking_done event — don't trust provisional
                # chunks_created during incremental chunking.
                chunker_finished = chunking_done.is_set()
            else:
                # Resume path (no event): use durable state. Safe because on resume the chunker
                # runs to completion before the uploader starts — there is no incremental
                # chunks_created race.
                state = db.get_pipeline_state(content_hash)
                chunker_finished = bool(state and state["chunks_created"] is not None)
            if not chunker_finished:
                time.sleep(_POLL_INTERVAL)
                continue

            if cancel.is_set():
                return
            if batch_rows:
                continue

            # The chunker is done and nothing new is readable: the writer's last request, the
            # sweep, and (for a direct caller) the stamp.
            _land_held()
            state = db.get_pipeline_state(content_hash)
            if state and state["chunks_created"] is not None and state["chunks_uploaded"] >= state["chunks_created"]:
                db.mark_completed(content_hash)
                return

            time.sleep(_POLL_INTERVAL)
    finally:
        if own_run:
            run.close()


# ── Orchestrator ─────────────────────────────────────────────────────────────


def _catalog_pdf_hook(
    pdf_path: Path,
    collection_name: str,
    title: str = "",
    author: str = "",
    year: int = 0,
    corpus: str = "",
    chunk_count: int = 0,
    source_uri: str = "",
) -> None:
    """Register PDF document in catalog after successful indexing. Silently skipped if absent."""
    reader = None
    writer = None
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
    except Exception as exc:  # noqa: BLE001 - best-effort catalog PDF hook; logged + audited, cleanup in finally
        # nexus-ou4tb: an indexed PDF that never reached the catalog is
        # invisible to every catalog-routed query. WARNING + audit row.
        _log.warning("catalog_pdf_hook_failed", exc_info=True)
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


def pipeline_index_pdf(
    pdf_path: Path,
    content_hash: str,
    collection: str,
    t3: Any,
    *,
    db: HttpPipelineDB | None = None,
    embed_fn: EmbedFn | None = None,
    extractor: str = "auto",
    on_formula_oom: str = "fail",
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
    again. The pipeline buffer (``db``/``HttpPipelineDB``) itself is
    UNCHANGED by this flag — it is transient extraction/chunking
    staging, not the catalog Document graph, and is what makes the
    preview's chunk counts real; it is already cleaned up via
    ``db.delete_pipeline_data`` on a successful run.

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

    Returns total chunks indexed.
    """
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
    if embed_fn is None:
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)
        if is_vector_service_mode():
            # nexus-9n1u3 / RDR-152 Seam B: leave embed_fn=None — the service
            # embeds server-side at upload time. The embed stage writes a
            # non-NULL empty-blob sentinel and the uploader's write ignores
            # client vectors (the JVM embeds).
            # Mirrors the batch path (doc_indexer._index_pdf_document).
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

    # RDR-223: the index-run fence begins inside the writer, as its first request, so no chunk
    # lands before it (the old explicit begin sat here). The writer stays open until the tail
    # stamps it complete, after the post-passes.
    run = UploadRun()
    try:
        cancel = threading.Event()
        extraction_done = threading.Event()
        chunking_done = threading.Event()
        first_exc: BaseException | None = None

        with ThreadPoolExecutor(max_workers=3) as pool:
            extract_future = pool.submit(
                extractor_loop, pdf_path, content_hash, db, cancel,
                extractor=extractor, on_formula_oom=on_formula_oom,
                extraction_done=extraction_done,
                allow_degraded_extraction=allow_degraded_extraction,
            )
            chunk_future = pool.submit(
                chunker_loop, content_hash, db, cancel, embed_fn,
                extraction_done=extraction_done, chunking_done=chunking_done,
                pdf_path=str(pdf_path), corpus=corpus, target_model=target_model,
                git_meta=git_meta, doc_id=doc_id,
            )
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

            all_futures: set[Future] = {extract_future, chunk_future, upload_future}

            try:
                done, not_done = wait(all_futures, return_when=FIRST_EXCEPTION)
                for f in done:
                    exc = f.exception()
                    if exc is not None:
                        first_exc = exc
                        cancel.set()
                        break
                if not_done:
                    wait(not_done, return_when=ALL_COMPLETED)
            except BaseException as exc:  # noqa: BLE001 — nexus-6m9zy.3 (#4): see below
                # A KeyboardInterrupt (Ctrl-C) delivered to THIS thread lands
                # here, inside the blocking wait() call, NOT inside any stage
                # future -- the `for f in done` loop above never runs, so
                # cancel was never set. Left uncaught, this exception would
                # propagate straight out of the `with` block; ThreadPoolExecutor
                # .__exit__ still calls shutdown(wait=True) first, which blocks
                # until all three stages run to NATURAL completion (nothing
                # ever told them to stop), and the uploader's own
                # resume-completion check marks the row 'completed' out from
                # under the interrupted caller -- every later run then hits
                # create_pipeline's 'completed' -> skip path and reports 0
                # chunks until --force. Setting cancel here makes the stages
                # stop promptly (each polls cancel.is_set() at least once per
                # poll interval / per page), and falling through to the SAME
                # first_exc handling below as any other caught stage
                # exception marks the row 'failed' + clears the orphan WAL,
                # so a retry resumes instead of silently skipping.
                cancel.set()
                if first_exc is None:
                    first_exc = exc

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

        # Resolve collection once for all post-passes (avoids repeated API calls).
        col = t3.get_or_create_collection(collection)

        # Track post-pass success — pipeline data preserved on failure (nexus-pfmr).
        post_pass_ok = True

        # 1. Metadata enrichment from ExtractionResult.
        if not _enrich_metadata_from_extraction(
            content_hash, extraction_result, pdf_path, t3, col, collection,
            title_override=title_override,
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

            if not _update_chunk_metadata(t3, col, collection, content_hash, _tag_table_page):
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

        if post_pass_ok:
            db.delete_pipeline_data(content_hash)
        else:
            _log.warning(
                "pipeline_data_preserved",
                content_hash=content_hash,
                reason="one or more post-passes failed — data kept for retry",
            )
            # nexus-6m9zy.5 (#10): uploader_loop already called
            # db.mark_completed() -- BEFORE any post-pass ran -- the moment
            # chunks_uploaded caught up to chunks_created. Leaving the row at
            # status='completed' used to make the NEXT create_pipeline() call
            # return "skip", so pipeline_index_pdf never reached this function
            # again; since nexus-edjmu a document-identity create RESETS a
            # completed leftover instead (WAL wiped, answered "created"), which
            # would discard the very checkpoint this branch preserves and
            # re-extract everything. Move the row to 'failed' (never
            # clear_orphan_wal: that would delete the chunk/page data too) so
            # the next create_pipeline() call sees 'failed' -> 'resuming'. All
            # three stages then short-circuit near-instantly on resume
            # (everything is already uploaded), and execution reaches the
            # post-passes again for a genuine retry.
            try:
                db.mark_failed(content_hash, error="post-pass failed — kept for retry")
            except Exception:  # noqa: BLE001 — boundary catch: best-effort, mirrors the other terminal-state writes in this function
                _log.warning(
                    "pipeline_terminal_mark_failed",
                    content_hash=content_hash,
                    reason="post-pass retry re-arm",
                    exc_info=True,
                )

        # Catalog hook: register PDF in catalog (opt-in, graceful absence)
        # 2026-08-19: this used to read ``metadata["title"]`` / ``["author"]`` —
        # keys no extractor writes (``docling_title``/``pdf_title``/``pdf_author``
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
            from nexus.doc_indexer import _fence_complete, _fence_fail  # noqa: PLC0415 - deferred to avoid circular import at module load
            if total_chunks == 0:
                # MUST: zero extraction is a FAILURE, never /complete(0) — a
                # zero-chunk run would trivially satisfy the fail-closed gate
                # (referenced=0 == chunk_count=0) and stamp 'complete' on a
                # silently-failed extraction. No content-free exception exists
                # for PDFs.
                _fence_fail(doc_id, "zero chunks extracted")
            elif post_pass_ok:
                if run.writer is not None and run.finished:
                    # The writer sent every request and left the run 'indexing' (defer_completion):
                    # the post-passes above have run, so the document is whole. A refusal
                    # propagates, as the explicit stamp's did; the writer recorded it.
                    run.writer.complete()
                else:
                    # Every chunk was written by an earlier process (a retry of a run whose
                    # post-pass failed: nothing was left to upload, so no writer ran). Begin a
                    # fence for this run and stamp it, verified by the engine as before.
                    from nexus.doc_indexer import _fence_begin  # noqa: PLC0415 - deferred to avoid circular import at module load
                    _fence_begin(doc_id, content_hash, collection)
                    _fence_complete(doc_id, content_hash, total_chunks)
            else:
                _log.warning(
                    "index_run_complete_skipped_post_pass_failed",
                    doc_id=doc_id,
                    content_hash=content_hash,
                    reason="post-pass failed — fence stays 'indexing' for retry",
                )

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
) -> bool:
    """Post-pass: update chunk metadata with fields from ExtractionResult.

    Resolves source_title (docling_title → pdf_title → filename) and
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
        # The engine's merge makes the read unnecessary — sending only
        # `enrichment`'s keys leaves every other key (and any write that
        # lands in between) untouched.
        all_ids: list[str] = []
        offset = 0
        while True:
            batch = _vector_with_retry(
                col.get,
                where={"content_hash": content_hash},
                limit=300,
                offset=offset,
            )
            all_ids.extend(batch.get("ids", []))
            if len(batch.get("ids", [])) < 300:
                break
            offset += 300

        if not all_ids:
            return True

        t3.update_chunks(
            collection, all_ids, [enrichment] * len(all_ids), delete_keys=delete_keys,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort metadata enrichment; logged via log.warning, returns False
        _log.warning("metadata_enrichment_failed", content_hash=content_hash, error=str(exc))
        return False


def _update_chunk_metadata(
    t3: Any,
    col: Any,
    collection: str,
    content_hash: str,
    update_fn: Callable[[dict], bool],
) -> bool:
    """Generic post-pass: query chunks by content_hash, apply update_fn to each.

    Paginates the T3 query to handle documents with 300+ chunks.
    Returns True on success, False on failure (nexus-f8it).

    nexus-vhyar: *update_fn* mutates a copy of the row it was given, and only
    the keys it added or changed are written. The engine merges, so the rest
    of the row is untouched; writing the whole row back re-asserted every key
    as it stood at read time over any write that committed in between (the
    read-modify-write race nexus-w94eo removed from the enrichment post-pass).
    A key *update_fn* deletes is not written and so not removed; no caller
    deletes one today.
    """
    try:
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
    except Exception as exc:  # noqa: BLE001 - best-effort chunk-metadata query; logged via log.warning, returns False
        _log.warning("chunk_metadata_query_failed", content_hash=content_hash, error=str(exc))
        return False

    ids_to_update: list[str] = []
    updated_metas: list[dict] = []
    for cid, meta in zip(all_ids, all_metas):
        before = dict(meta)
        if update_fn(meta):
            delta = {k: v for k, v in meta.items() if k not in before or before[k] != v}
            if delta:
                ids_to_update.append(cid)
                updated_metas.append(delta)

    if ids_to_update:
        try:
            t3.update_chunks(collection, ids_to_update, updated_metas)
        except Exception as exc:  # noqa: BLE001 - best-effort chunk-metadata update; logged via log.warning, returns False
            _log.warning("chunk_metadata_update_failed", count=len(ids_to_update), error=str(exc))
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
