# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Pipeline stage functions for streaming PDF indexing (RDR-048).

Three concurrent stages connected by PipelineDB:

1. **extractor_loop** — extracts pages → ``pdf_pages`` buffer
2. **chunker_loop** — polls pages, chunks stable prefix, embeds → ``pdf_chunks``
3. **uploader_loop** — reads embedded chunks, upserts to T3 ChromaDB
"""
from __future__ import annotations

import json
import struct
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait, ALL_COMPLETED, FIRST_EXCEPTION
from pathlib import Path
from typing import Any

import structlog

from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import ExtractionResult, PDFExtractor
from nexus.pipeline_buffer import PipelineDB

_log = structlog.get_logger(__name__)

# Upload batch size — matches _INCREMENTAL_BATCH_SIZE in doc_indexer.py.
_UPLOAD_BATCH_SIZE = 128

# Embedding batch size — embed in small batches so heartbeat stays fresh.
_EMBED_BATCH_SIZE = 32

# Poll interval for uploader loop (checks for new chunks to upload).
_POLL_INTERVAL = 0.1

# Type alias matching doc_indexer.EmbedFn.
EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]


class PipelineCancelled(Exception):
    """Raised inside on_page to abort extraction when cancel is set."""


# ── Stage 1: Extractor ──────────────────────────────────────────────────────


def extractor_loop(
    pdf_path: Path,
    content_hash: str,
    db: PipelineDB,
    cancel: threading.Event,
    extractor: str = "auto",
    extraction_done: threading.Event | None = None,
) -> ExtractionResult:
    """Extract pages to PipelineDB buffer via the on_page streaming callback.

    Raises ``PipelineCancelled`` inside on_page to abort extraction early
    when cancel is set (S1 fix — propagates through MinerU batch loop).
    Sets *extraction_done* when all pages are written and ``total_pages`` is set.
    Returns the ``ExtractionResult`` (needed for ``table_regions`` post-pass).
    """
    state = db.get_pipeline_state(content_hash)
    pages_extracted_at_start = state["pages_extracted"] if state else 0

    def on_page(page_index: int, page_text: str, page_metadata: dict) -> None:
        if cancel.is_set():
            raise PipelineCancelled("pipeline cancelled")
        if page_index < pages_extracted_at_start:
            return  # already in buffer from a previous run
        db.write_page(content_hash, page_index, page_text, metadata=page_metadata)
        db.update_progress(content_hash, pages_extracted=page_index + 1)

    ext = PDFExtractor()
    try:
        result = ext.extract(pdf_path, extractor=extractor, on_page=on_page)
    except PipelineCancelled:
        # Clean cancellation — return a minimal result so the orchestrator
        # can still read whatever was extracted before cancel.
        return ExtractionResult(text="", metadata={"page_count": 0, "table_regions": []})

    # Signal extraction complete: set total_pages, then notify waiters.
    page_count = result.metadata.get("page_count", 0)
    db.update_progress(content_hash, total_pages=page_count)
    if extraction_done is not None:
        extraction_done.set()

    return result


# ── Stage 2: Chunker ────────────────────────────────────────────────────────


def _rebuild_boundaries(pages: list[dict]) -> list[dict]:
    """Reconstruct page_boundaries from buffered page rows."""
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


def _embed_batch(
    chunks_to_embed: list,
    content_hash: str,
    db: PipelineDB,
    embed_fn: EmbedFn | None,
    cancel: threading.Event,
    written_so_far: int,
) -> int:
    """Embed and write a batch of chunks. Returns count of chunks written."""
    if not chunks_to_embed:
        return 0

    chunk_texts = [c.text for c in chunks_to_embed]
    embeddings: list[list[float]] = []

    # Embed in sub-batches for heartbeat freshness (S2 fix).
    if embed_fn is not None:
        for batch_start in range(0, len(chunk_texts), _EMBED_BATCH_SIZE):
            if cancel.is_set():
                break
            batch = chunk_texts[batch_start : batch_start + _EMBED_BATCH_SIZE]
            batch_embs, _ = embed_fn(batch, "voyage-context-3")
            embeddings.extend(batch_embs)
            db.update_progress(content_hash, chunks_embedded=written_so_far + len(embeddings))

    for i, chunk in enumerate(chunks_to_embed):
        emb_bytes = None
        if i < len(embeddings):
            emb_bytes = struct.pack(f"{len(embeddings[i])}f", *embeddings[i])
        chunk_id = f"{content_hash}-{chunk.chunk_index}"
        db.write_chunk(
            content_hash,
            chunk.chunk_index,
            chunk.text,
            chunk_id,
            metadata=chunk.metadata,
            embedding=emb_bytes,
        )

    return len(chunks_to_embed)


def chunker_loop(
    content_hash: str,
    db: PipelineDB,
    cancel: threading.Event,
    embed_fn: EmbedFn | None,
    chunk_chars: int = 1500,
    extraction_done: threading.Event | None = None,
    chunking_done: threading.Event | None = None,
) -> None:
    """Incrementally chunk pages as they arrive, overlapping with extraction.

    Polls for new pages. When enough text has accumulated, chunks the stable
    prefix (all but the last chunk, whose boundary may shift when more pages
    arrive). When extraction completes, does a final pass to flush remaining
    chunks. Embeds in batches of ``_EMBED_BATCH_SIZE`` for heartbeat freshness.
    """
    chunker = PDFChunker(chunk_chars=chunk_chars)
    written_up_to = 0  # chunk indices [0, written_up_to) are done
    last_page_count = 0
    total_embedded = 0

    def _signal_done() -> None:
        if chunking_done is not None:
            chunking_done.set()

    while not cancel.is_set():
        # Check if extraction is finished.
        is_final = False
        if extraction_done is not None:
            is_final = extraction_done.is_set()
        else:
            state = db.get_pipeline_state(content_hash)
            if state and state["total_pages"] is not None and state["pages_extracted"] >= state["total_pages"]:
                is_final = True

        pages = db.read_pages(content_hash)

        # No new pages and extraction not done — wait.
        if len(pages) == last_page_count and not is_final:
            if extraction_done is not None:
                extraction_done.wait(timeout=0.5)
            else:
                time.sleep(_POLL_INTERVAL)
            continue

        last_page_count = len(pages)

        if not pages:
            if is_final:
                db.update_progress(content_hash, chunks_created=0, chunks_embedded=0)
                _signal_done()
                return
            continue

        # Join text and chunk (C1 contract).
        text = "\n".join(row["page_text"] for row in pages)
        boundaries = _rebuild_boundaries(pages)
        extraction_metadata = {"page_boundaries": boundaries, "table_regions": []}
        chunks = chunker.chunk(text, extraction_metadata)

        if is_final:
            # Final pass: embed and write ALL remaining chunks.
            new_chunks = chunks[written_up_to:]
            count = _embed_batch(new_chunks, content_hash, db, embed_fn, cancel, total_embedded)
            total_embedded += count
            written_up_to += count
            db.update_progress(content_hash, chunks_created=len(chunks), chunks_embedded=total_embedded)
            _signal_done()
            return

        # Incremental pass: write stable prefix (all but last chunk).
        stable_end = max(written_up_to, len(chunks) - 1)
        new_chunks = chunks[written_up_to:stable_end]
        if new_chunks:
            count = _embed_batch(new_chunks, content_hash, db, embed_fn, cancel, total_embedded)
            total_embedded += count
            written_up_to += count
            db.update_progress(content_hash, chunks_created=written_up_to)

    # Cancelled — signal done so uploader can exit.
    _signal_done()


# ── Stage 3: Uploader ───────────────────────────────────────────────────────


def uploader_loop(
    content_hash: str,
    db: PipelineDB,
    t3: Any,
    collection: str,
    cancel: threading.Event,
    chunking_done: threading.Event | None = None,
) -> None:
    """Poll chunk buffer for embedded chunks and upsert to T3 ChromaDB.

    Batches upserts into groups of ``_UPLOAD_BATCH_SIZE`` (128). Marks each
    batch as uploaded after successful upsert. Sets pipeline status to
    ``'completed'`` when chunking is done and all chunks are uploaded.
    """
    total_uploaded = 0

    while not cancel.is_set():
        chunks = db.read_uploadable_chunks(content_hash)

        if chunks:
            for batch_start in range(0, len(chunks), _UPLOAD_BATCH_SIZE):
                if cancel.is_set():
                    return
                batch = chunks[batch_start : batch_start + _UPLOAD_BATCH_SIZE]

                ids = [row["chunk_id"] for row in batch]
                documents = [row["chunk_text"] for row in batch]
                embeddings = [
                    list(struct.unpack(f"{len(row['embedding']) // 4}f", row["embedding"]))
                    for row in batch
                ]
                metadatas = [
                    json.loads(row["metadata_json"]) if isinstance(row["metadata_json"], str) else row["metadata_json"]
                    for row in batch
                ]

                t3.upsert(
                    collection_name=collection,
                    ids=ids,
                    documents=documents,
                    embeddings=embeddings,
                    metadatas=metadatas,
                )

                indices = [row["chunk_index"] for row in batch]
                db.mark_uploaded(content_hash, indices)
                total_uploaded += len(batch)
                db.update_progress(content_hash, chunks_uploaded=total_uploaded)

        # Done condition (C1 fix): require chunking_done event OR durable
        # chunks_created IS NOT NULL. Never use >= 0 fallback.
        chunker_finished = chunking_done is not None and chunking_done.is_set()
        if not chunker_finished:
            # Fallback for resume path: check durable state.
            state = db.get_pipeline_state(content_hash)
            if state and state["chunks_created"] is not None:
                # chunks_created was explicitly set by chunker. Check if
                # all chunks are uploaded.
                if state["chunks_uploaded"] >= state["chunks_created"]:
                    db.mark_completed(content_hash)
                    return
            time.sleep(_POLL_INTERVAL)
            continue

        # Chunker finished — drain remaining chunks and complete.
        # Re-read to catch any chunks written between our last read and the event.
        remaining = db.read_uploadable_chunks(content_hash)
        if remaining:
            continue  # loop back to upload them

        state = db.get_pipeline_state(content_hash)
        if state and state["chunks_created"] is not None and state["chunks_uploaded"] >= state["chunks_created"]:
            db.mark_completed(content_hash)
            return

        time.sleep(_POLL_INTERVAL)


# ── Orchestrator ─────────────────────────────────────────────────────────────


def pipeline_index_pdf(
    pdf_path: Path,
    content_hash: str,
    collection: str,
    t3: Any,
    *,
    db: PipelineDB | None = None,
    embed_fn: EmbedFn | None = None,
    extractor: str = "auto",
) -> int:
    """Three-stage streaming pipeline for PDFs.

    Submits ``extractor_loop``, ``chunker_loop``, and ``uploader_loop`` to a
    ``ThreadPoolExecutor(max_workers=3)``.  On first exception the cancel event
    is set and all futures are joined before cleanup (F2 fix).

    After upload, applies table_regions post-pass (S3 fix) to tag chunks on
    table pages with ``chunk_type=table_page``.

    Returns total chunks indexed.
    """
    from nexus.pipeline_buffer import PIPELINE_DB_PATH

    if db is None:
        db = PipelineDB(PIPELINE_DB_PATH)

    # Pre-flight: register pipeline.
    result = db.create_pipeline(content_hash, str(pdf_path), collection)
    if result == "skip":
        _log.info("pipeline_skip", content_hash=content_hash, reason="already completed or running")
        return 0

    cancel = threading.Event()
    extraction_done = threading.Event()
    chunking_done = threading.Event()
    first_exc: BaseException | None = None

    with ThreadPoolExecutor(max_workers=3) as pool:
        extract_future = pool.submit(
            extractor_loop, pdf_path, content_hash, db, cancel, extractor,
            extraction_done,
        )
        chunk_future = pool.submit(
            chunker_loop, content_hash, db, cancel, embed_fn,
            extraction_done=extraction_done, chunking_done=chunking_done,
        )
        upload_future = pool.submit(
            uploader_loop, content_hash, db, t3, collection, cancel,
            chunking_done,
        )

        all_futures: set[Future] = {extract_future, chunk_future, upload_future}

        # F2 fix: wait for first exception, then cancel and join all.
        done, not_done = wait(all_futures, return_when=FIRST_EXCEPTION)

        for f in done:
            exc = f.exception()
            if exc is not None:
                first_exc = exc
                cancel.set()
                break

        if not_done:
            wait(not_done, return_when=ALL_COMPLETED)

    # S4 fix: only assign first_exc if not already set.
    if first_exc is None:
        for f in all_futures:
            exc = f.exception()
            if exc is not None:
                first_exc = exc
                break

    if first_exc is not None:
        db.mark_failed(content_hash, error=str(first_exc))
        raise first_exc

    # S3 fix: table_regions post-pass.
    extraction_result = extract_future.result()
    table_regions = extraction_result.metadata.get("table_regions", [])
    if table_regions:
        table_pages: set[int] = {r["page"] for r in table_regions}
        _apply_table_regions(content_hash, table_pages, t3, collection)

    # Success: get chunk count and clean up buffer.
    state = db.get_pipeline_state(content_hash)
    total_chunks = state["chunks_uploaded"] if state else 0
    db.delete_pipeline_data(content_hash)

    return total_chunks


def _apply_table_regions(
    content_hash: str, table_pages: set[int], t3: Any, collection: str,
) -> None:
    """Update chunk_type metadata for chunks on table pages (RF-14 post-pass)."""
    # Fetch all chunks for this document from T3.
    prefix = content_hash[:16]
    try:
        result = t3.get(
            collection_name=collection,
            where={"content_hash": content_hash},
            include=["metadatas"],
        )
    except Exception:
        _log.debug("table_regions_postpass_failed", content_hash=content_hash)
        return

    ids_to_update: list[str] = []
    updated_metas: list[dict] = []
    for cid, meta in zip(result.get("ids", []), result.get("metadatas", [])):
        page = meta.get("page_number", 0)
        if page in table_pages and meta.get("chunk_type") != "table_page":
            meta["chunk_type"] = "table_page"
            ids_to_update.append(cid)
            updated_metas.append(meta)

    if ids_to_update:
        try:
            t3.update(collection_name=collection, ids=ids_to_update, metadatas=updated_metas)
        except Exception:
            _log.debug("table_regions_update_failed", count=len(ids_to_update))
