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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import ExtractionResult, PDFExtractor
from nexus.pipeline_buffer import PipelineDB
from nexus.retry import _chroma_with_retry

_log = structlog.get_logger(__name__)

_UPLOAD_BATCH_SIZE = 128
_EMBED_BATCH_SIZE = 32
_POLL_INTERVAL = 0.1

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
    when cancel is set (propagates through MinerU batch loop).
    """
    state = db.get_pipeline_state(content_hash)
    pages_extracted_at_start = state["pages_extracted"] if state else 0

    def on_page(page_index: int, page_text: str, page_metadata: dict) -> None:
        if cancel.is_set():
            raise PipelineCancelled("pipeline cancelled")
        if page_index < pages_extracted_at_start:
            return
        db.write_page(content_hash, page_index, page_text, metadata=page_metadata)
        db.update_progress(content_hash, pages_extracted=page_index + 1)

    ext = PDFExtractor()
    try:
        result = ext.extract(pdf_path, extractor=extractor, on_page=on_page)
    except PipelineCancelled:
        return ExtractionResult(text="", metadata={"page_count": 0, "table_regions": []})

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


def _build_chunk_metadata(
    chunk: Any,
    *,
    content_hash: str,
    pdf_path: str,
    corpus: str,
    target_model: str,
    extraction_metadata: dict,
    chunk_count: int,
    now_iso: str,
) -> dict:
    """Build the full metadata dict for a chunk, matching the batch path schema.

    See ``doc_indexer._pdf_chunks`` (lines 498-527) for the canonical schema.
    """
    return {
        "source_path": pdf_path,
        "source_title": extraction_metadata.get("source_title", ""),
        "source_author": extraction_metadata.get("source_author", ""),
        "source_date": extraction_metadata.get("source_date", ""),
        "corpus": corpus,
        "store_type": "pdf",
        "page_count": extraction_metadata.get("page_count", 0),
        "page_number": chunk.metadata.get("page_number", 0),
        "section_title": "",
        "format": extraction_metadata.get("format", ""),
        "extraction_method": extraction_metadata.get("extraction_method", ""),
        "chunk_type": chunk.metadata.get("chunk_type", "text"),
        "chunk_index": chunk.chunk_index,
        "chunk_count": chunk_count,
        "chunk_start_char": chunk.metadata.get("chunk_start_char", 0),
        "chunk_end_char": chunk.metadata.get("chunk_end_char", 0),
        "embedding_model": target_model,
        "indexed_at": now_iso,
        "content_hash": content_hash,
        "pdf_subject": extraction_metadata.get("pdf_subject", ""),
        "pdf_keywords": extraction_metadata.get("pdf_keywords", ""),
        "is_image_pdf": extraction_metadata.get("is_image_pdf", False),
        "has_formulas": extraction_metadata.get("has_formulas", False),
        "bib_year": 0,
        "bib_venue": "",
        "bib_authors": "",
        "bib_citation_count": 0,
        "bib_semantic_scholar_id": "",
    }


def _embed_and_write_batch(
    chunks_to_embed: list,
    content_hash: str,
    db: PipelineDB,
    embed_fn: EmbedFn | None,
    cancel: threading.Event,
    total_embedded_so_far: int,
    *,
    pdf_path: str,
    corpus: str,
    target_model: str,
    extraction_metadata: dict,
    chunk_count: int,
    now_iso: str,
) -> int:
    """Embed a batch of chunks in sub-batches for heartbeat, then write to buffer.

    Only writes chunks that were fully embedded. Returns count written.
    """
    if not chunks_to_embed:
        return 0

    chunk_texts = [c.text for c in chunks_to_embed]
    embeddings: list[list[float]] = []

    if embed_fn is not None:
        for batch_start in range(0, len(chunk_texts), _EMBED_BATCH_SIZE):
            if cancel.is_set():
                break
            batch = chunk_texts[batch_start : batch_start + _EMBED_BATCH_SIZE]
            batch_embs, _ = embed_fn(batch, target_model)
            embeddings.extend(batch_embs)
            db.update_progress(content_hash, chunks_embedded=total_embedded_so_far + len(embeddings))

    # Only write chunks that have embeddings (or all if embed_fn is None).
    write_count = len(embeddings) if embed_fn is not None else len(chunks_to_embed)
    for i in range(write_count):
        chunk = chunks_to_embed[i]
        emb_bytes = None
        if i < len(embeddings):
            emb_bytes = struct.pack(f"{len(embeddings[i])}f", *embeddings[i])
        # Chunk ID must match batch path: {hash[:16]}_{chunk_index}
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta = _build_chunk_metadata(
            chunk,
            content_hash=content_hash,
            pdf_path=pdf_path,
            corpus=corpus,
            target_model=target_model,
            extraction_metadata=extraction_metadata,
            chunk_count=chunk_count,
            now_iso=now_iso,
        )
        db.write_chunk(content_hash, chunk.chunk_index, chunk.text, chunk_id,
                        metadata=meta, embedding=emb_bytes)

    return write_count


def chunker_loop(
    content_hash: str,
    db: PipelineDB,
    cancel: threading.Event,
    embed_fn: EmbedFn | None,
    chunk_chars: int = 1500,
    extraction_done: threading.Event | None = None,
    chunking_done: threading.Event | None = None,
    *,
    pdf_path: str = "",
    corpus: str = "",
    target_model: str = "voyage-context-3",
    doc_metadata: dict | None = None,
) -> None:
    """Incrementally chunk pages as they arrive, overlapping with extraction.

    Polls for new pages. When enough text has accumulated, chunks the stable
    prefix (all but the last chunk, whose boundary may shift). When extraction
    completes, flushes remaining chunks. Embeds in sub-batches for heartbeat.
    """
    chunker = PDFChunker(chunk_chars=chunk_chars)
    written_up_to = 0
    last_page_count = 0
    total_embedded = 0
    now_iso = datetime.now(UTC).isoformat()
    extraction_metadata = doc_metadata or {}

    def _signal_done() -> None:
        if chunking_done is not None:
            chunking_done.set()

    while not cancel.is_set():
        is_final = False
        if extraction_done is not None:
            is_final = extraction_done.is_set()
        else:
            state = db.get_pipeline_state(content_hash)
            if state and state["total_pages"] is not None and state["pages_extracted"] >= state["total_pages"]:
                is_final = True

        pages = db.read_pages(content_hash)

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

        text = "\n".join(row["page_text"] for row in pages)
        boundaries = _rebuild_boundaries(pages)
        chunk_metadata = {"page_boundaries": boundaries, "table_regions": []}
        chunks = chunker.chunk(text, chunk_metadata)

        common_kwargs = dict(
            content_hash=content_hash, db=db, embed_fn=embed_fn, cancel=cancel,
            pdf_path=pdf_path, corpus=corpus, target_model=target_model,
            extraction_metadata=extraction_metadata, chunk_count=len(chunks), now_iso=now_iso,
        )

        if is_final:
            new_chunks = chunks[written_up_to:]
            count = _embed_and_write_batch(new_chunks, total_embedded_so_far=total_embedded, **common_kwargs)
            total_embedded += count
            written_up_to += count
            db.update_progress(content_hash, chunks_created=len(chunks), chunks_embedded=total_embedded)
            _signal_done()
            return

        stable_end = max(written_up_to, len(chunks) - 1)
        new_chunks = chunks[written_up_to:stable_end]
        if new_chunks:
            count = _embed_and_write_batch(new_chunks, total_embedded_so_far=total_embedded, **common_kwargs)
            total_embedded += count
            written_up_to += count
            db.update_progress(content_hash, chunks_created=written_up_to)

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

    Uses ``T3Database.upsert_chunks_with_embeddings`` for upload and
    ``T3Database.update_chunks`` for metadata post-passes.
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

                t3.upsert_chunks_with_embeddings(collection, ids, documents, embeddings, metadatas)

                indices = [row["chunk_index"] for row in batch]
                db.mark_uploaded(content_hash, indices)
                total_uploaded += len(batch)
                db.update_progress(content_hash, chunks_uploaded=total_uploaded)

        # Done condition: chunking_done event or durable chunks_created IS NOT NULL.
        chunker_finished = chunking_done is not None and chunking_done.is_set()
        if not chunker_finished:
            state = db.get_pipeline_state(content_hash)
            if state and state["chunks_created"] is not None:
                if state["chunks_uploaded"] >= state["chunks_created"]:
                    db.mark_completed(content_hash)
                    return
            time.sleep(_POLL_INTERVAL)
            continue

        # Chunker finished — drain remaining and complete.
        if cancel.is_set():
            return
        remaining = db.read_uploadable_chunks(content_hash)
        if remaining:
            continue

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
    corpus: str = "",
    target_model: str = "voyage-context-3",
) -> int:
    """Three-stage streaming pipeline for PDFs.

    Submits ``extractor_loop``, ``chunker_loop``, and ``uploader_loop`` to a
    ``ThreadPoolExecutor(max_workers=3)``.  On first exception the cancel event
    is set and all futures are joined before cleanup (F2 fix).

    Returns total chunks indexed.
    """
    from nexus.pipeline_buffer import PIPELINE_DB_PATH

    if db is None:
        db = PipelineDB(PIPELINE_DB_PATH)

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
            pdf_path=str(pdf_path), corpus=corpus, target_model=target_model,
        )
        upload_future = pool.submit(
            uploader_loop, content_hash, db, t3, collection, cancel,
            chunking_done,
        )

        all_futures: set[Future] = {extract_future, chunk_future, upload_future}

        done, not_done = wait(all_futures, return_when=FIRST_EXCEPTION)

        for f in done:
            exc = f.exception()
            if exc is not None:
                first_exc = exc
                cancel.set()
                break

        if not_done:
            wait(not_done, return_when=ALL_COMPLETED)

    # Only assign first_exc if not already set (S4 fix).
    if first_exc is None:
        for f in all_futures:
            exc = f.exception()
            if exc is not None:
                first_exc = exc
                break

    if first_exc is not None:
        db.mark_failed(content_hash, error=str(first_exc))
        raise first_exc

    # table_regions post-pass.
    extraction_result = extract_future.result()
    table_regions = extraction_result.metadata.get("table_regions", [])
    if table_regions:
        table_pages: set[int] = {r["page"] for r in table_regions}
        _apply_table_regions(content_hash, table_pages, t3, collection)

    state = db.get_pipeline_state(content_hash)
    total_chunks = state["chunks_uploaded"] if state else 0
    db.delete_pipeline_data(content_hash)

    return total_chunks


def _apply_table_regions(
    content_hash: str, table_pages: set[int], t3: Any, collection: str,
) -> None:
    """Update chunk_type metadata for chunks on table pages (RF-14 post-pass).

    Uses ``T3Database.get_or_create_collection`` for querying and
    ``T3Database.update_chunks`` for the metadata update.
    """
    try:
        col = t3.get_or_create_collection(collection)
        result = _chroma_with_retry(
            col.get,
            where={"content_hash": content_hash},
            include=["metadatas"],
        )
    except Exception:
        _log.debug("table_regions_postpass_query_failed", content_hash=content_hash)
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
            t3.update_chunks(collection, ids_to_update, updated_metas)
        except Exception:
            _log.debug("table_regions_update_failed", count=len(ids_to_update))
