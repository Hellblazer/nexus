# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Pipeline stage functions for streaming PDF indexing (RDR-048).

Three concurrent stages connected by PipelineDB:

1. **extractor_loop** — extracts pages → ``pdf_pages`` buffer
2. **chunker_loop** — reads pages, chunks, embeds → ``pdf_chunks`` buffer
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

# Poll interval for chunker and uploader loops (RF-11).
_POLL_INTERVAL = 0.5

# Type alias matching doc_indexer.EmbedFn.
EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]


# ── Stage 1: Extractor ──────────────────────────────────────────────────────


def extractor_loop(
    pdf_path: Path,
    content_hash: str,
    db: PipelineDB,
    cancel: threading.Event,
    extractor: str = "auto",
) -> ExtractionResult:
    """Extract pages to PipelineDB buffer via the on_page streaming callback.

    Returns the ``ExtractionResult`` (needed for ``table_regions`` post-pass).
    """
    state = db.get_pipeline_state(content_hash)
    pages_extracted_at_start = state["pages_extracted"] if state else 0

    def on_page(page_index: int, page_text: str, page_metadata: dict) -> None:
        if cancel.is_set():
            return
        if page_index < pages_extracted_at_start:
            return  # already in buffer from a previous run
        db.write_page(content_hash, page_index, page_text, metadata=page_metadata)
        db.update_progress(content_hash, pages_extracted=page_index + 1)

    ext = PDFExtractor()
    result = ext.extract(pdf_path, extractor=extractor, on_page=on_page)

    # Signal extraction complete: set total_pages.
    page_count = result.metadata.get("page_count", 0)
    db.update_progress(content_hash, total_pages=page_count)

    return result


# ── Stage 2: Chunker ────────────────────────────────────────────────────────


def chunker_loop(
    content_hash: str,
    db: PipelineDB,
    cancel: threading.Event,
    embed_fn: EmbedFn | None,
    chunk_chars: int = 1500,
) -> None:
    """Poll page buffer, chunk accumulated text, embed, write to chunk buffer.

    Waits for extraction to complete (``total_pages`` set and
    ``pages_extracted == total_pages``), then chunks the full text once.
    Embedding is performed via *embed_fn*; chunks + embeddings are written
    to ``pdf_chunks`` via INSERT OR IGNORE (idempotent on resume).
    """
    # Poll until extraction is complete.
    while not cancel.is_set():
        state = db.get_pipeline_state(content_hash)
        if state and state["total_pages"] is not None and state["pages_extracted"] >= state["total_pages"]:
            break
        time.sleep(_POLL_INTERVAL)

    if cancel.is_set():
        return

    # Read all pages, join text (C1 contract).
    pages = db.read_pages(content_hash)
    if not pages:
        db.update_progress(content_hash, chunks_created=0, chunks_embedded=0)
        return

    text = "\n".join(row["page_text"] for row in pages)

    # Reconstruct page_boundaries from page metadata.
    page_boundaries: list[dict] = []
    pos = 0
    for row in pages:
        meta = json.loads(row["metadata_json"]) if isinstance(row["metadata_json"], str) else row["metadata_json"]
        page_boundaries.append({
            "page_number": meta.get("page_number", row["page_index"] + 1),
            "start_char": pos,
            "page_text_length": len(row["page_text"]) + 1,
        })
        pos += len(row["page_text"]) + 1

    extraction_metadata = {"page_boundaries": page_boundaries, "table_regions": []}

    chunker = PDFChunker(chunk_chars=chunk_chars)
    chunks = chunker.chunk(text, extraction_metadata)

    if not chunks:
        db.update_progress(content_hash, chunks_created=0, chunks_embedded=0)
        return

    # Embed all chunks.
    chunk_texts = [c.text for c in chunks]
    embeddings: list[list[float]] = []
    if embed_fn is not None and chunk_texts:
        embeddings, _ = embed_fn(chunk_texts, "voyage-context-3")

    # Write chunks + embeddings to buffer, refreshing heartbeat periodically.
    for i, chunk in enumerate(chunks):
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
        # Heartbeat every 32 chunks to prevent stale-pipeline detection.
        if (i + 1) % 32 == 0:
            db.update_progress(content_hash, chunks_created=i + 1)

    db.update_progress(
        content_hash,
        chunks_created=len(chunks),
        chunks_embedded=len(embeddings),
    )


# ── Stage 3: Uploader ───────────────────────────────────────────────────────


def uploader_loop(
    content_hash: str,
    db: PipelineDB,
    t3: Any,
    collection: str,
    cancel: threading.Event,
) -> None:
    """Poll chunk buffer for embedded chunks and upsert to T3 ChromaDB.

    Batches upserts into groups of ``_UPLOAD_BATCH_SIZE`` (128). Marks each
    batch as uploaded after successful upsert. Sets pipeline status to
    ``'completed'`` when all chunks are uploaded and extraction is done.
    """
    total_uploaded = 0

    while not cancel.is_set():
        chunks = db.read_uploadable_chunks(content_hash)

        if chunks:
            # Process in batches.
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

        # Check done condition.
        state = db.get_pipeline_state(content_hash)
        if state:
            extraction_done = (
                state["total_pages"] is not None
                and state["pages_extracted"] >= state["total_pages"]
            )
            # chunks_created is NULL until the chunker sets it explicitly.
            chunking_done = state["chunks_created"] is not None
            all_uploaded = chunking_done and state["chunks_uploaded"] >= state["chunks_created"]
            if extraction_done and all_uploaded:
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
    """Three-stage streaming pipeline for large PDFs.

    Submits ``extractor_loop``, ``chunker_loop``, and ``uploader_loop`` to a
    ``ThreadPoolExecutor(max_workers=3)``.  On first exception the cancel event
    is set and all futures are joined before cleanup (F2 fix).

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
    first_exc: BaseException | None = None

    with ThreadPoolExecutor(max_workers=3) as pool:
        extract_future = pool.submit(
            extractor_loop, pdf_path, content_hash, db, cancel, extractor,
        )
        chunk_future = pool.submit(
            chunker_loop, content_hash, db, cancel, embed_fn,
        )
        upload_future = pool.submit(
            uploader_loop, content_hash, db, t3, collection, cancel,
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

    # Check for additional exceptions from the remaining futures.
    if first_exc is None:
        for f in all_futures:
            exc = f.exception()
            if exc is not None:
                first_exc = exc
                break

    if first_exc is not None:
        db.mark_failed(content_hash, error=str(first_exc))
        raise first_exc

    # Success: get chunk count and clean up buffer.
    state = db.get_pipeline_state(content_hash)
    total_chunks = state["chunks_uploaded"] if state else 0
    db.delete_pipeline_data(content_hash)

    return total_chunks
