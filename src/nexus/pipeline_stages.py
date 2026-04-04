# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Pipeline stage functions for streaming PDF indexing (RDR-048).

Three concurrent stages connected by PipelineDB:

1. **extractor_loop** — extracts pages → ``pdf_pages`` buffer
2. **chunker_loop** — polls pages, chunks stable prefix, embeds → ``pdf_chunks``
3. **uploader_loop** — reads embedded chunks, upserts to T3 ChromaDB

After all three stages complete, the orchestrator runs post-passes to:
- Enrich chunk metadata from the ExtractionResult (title, author, etc.)
- Tag table-page chunks (table_regions post-pass)
- Correct chunk_count to the final total
- Prune stale chunks from a previous version
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

_UPLOAD_BATCH_SIZE = 128  # Conservative vs ChromaDB's 300 limit — matches _INCREMENTAL_BATCH_SIZE in doc_indexer
_EMBED_BATCH_SIZE = 32  # Smaller than batch path (128) — favours heartbeat freshness in streaming
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
    """Extract pages to PipelineDB buffer via the on_page streaming callback."""
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
    chunk_count: int,
    now_iso: str,
) -> dict:
    """Build chunk metadata with fields known at chunk time.

    Extraction-dependent fields (source_title, source_author, extraction_method,
    format, page_count, is_image_pdf, has_formulas) are set to defaults here
    and corrected by the metadata post-pass after extraction completes.
    """
    return {
        "source_path": pdf_path,
        "source_title": "",       # post-pass: from ExtractionResult
        "source_author": "",      # post-pass: from ExtractionResult
        "source_date": "",        # post-pass: from ExtractionResult
        "corpus": corpus,
        "store_type": "pdf",
        "page_count": 0,          # post-pass: from ExtractionResult
        "page_number": chunk.metadata.get("page_number", 0),
        "section_title": "",
        "format": "",             # post-pass: from ExtractionResult
        "extraction_method": "",  # post-pass: from ExtractionResult
        "chunk_type": chunk.metadata.get("chunk_type", "text"),
        "chunk_index": chunk.chunk_index,
        "chunk_count": chunk_count,  # provisional; corrected in post-pass
        "chunk_start_char": chunk.metadata.get("chunk_start_char", 0),
        "chunk_end_char": chunk.metadata.get("chunk_end_char", 0),
        "embedding_model": embedding_model,
        "indexed_at": now_iso,
        "content_hash": content_hash,
        "pdf_subject": "",        # post-pass
        "pdf_keywords": "",       # post-pass
        "is_image_pdf": False,    # post-pass
        "has_formulas": False,    # post-pass
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
    chunk_count: int,
    now_iso: str,
) -> tuple[int, str]:
    """Embed and write a batch of chunks. Returns (count_written, actual_model)."""
    if not chunks_to_embed:
        return 0, target_model

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
        emb_bytes = None
        if i < len(embeddings):
            emb_bytes = struct.pack(f"{len(embeddings[i])}f", *embeddings[i])
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta = _build_chunk_metadata(
            chunk,
            content_hash=content_hash,
            pdf_path=pdf_path,
            corpus=corpus,
            embedding_model=actual_model,
            chunk_count=chunk_count,
            now_iso=now_iso,
        )
        db.write_chunk(content_hash, chunk.chunk_index, chunk.text, chunk_id,
                        metadata=meta, embedding=emb_bytes)

    return write_count, actual_model


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
) -> None:
    """Incrementally chunk pages as they arrive, overlapping with extraction."""
    chunker = PDFChunker(chunk_chars=chunk_chars)
    # Resume: count all embedded chunks (both uploaded and not-yet-uploaded)
    # to avoid re-chunking/re-embedding work already done.
    # Note: if embed_fn failed or cancel fired mid-batch, chunks beyond the
    # last fully-embedded batch are not written at all (not NULL-embedding rows).
    # On resume, count_embedded_chunks returns the count of successfully embedded
    # rows, so the chunker re-processes from the correct position.
    written_up_to = db.count_embedded_chunks(content_hash)
    last_page_count = 0
    total_embedded = written_up_to
    now_iso = datetime.now(UTC).isoformat()

    def _signal_done() -> None:
        if chunking_done is not None:
            chunking_done.set()

    # Track current embedding model — may differ from target if embed_fn falls back.
    current_model = target_model

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

        batch_kwargs = dict(
            pdf_path=pdf_path, corpus=corpus, target_model=current_model,
            now_iso=now_iso,
        )

        if is_final:
            new_chunks = chunks[written_up_to:]
            count, actual_model = _embed_and_write_batch(
                new_chunks, content_hash, db, embed_fn, cancel,
                total_embedded, chunk_count=len(chunks), **batch_kwargs,
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
                total_embedded, chunk_count=0, **batch_kwargs,  # provisional
            )
            current_model = actual_model
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
    """Poll chunk buffer for embedded chunks and upsert to T3 ChromaDB."""
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

        chunker_finished = chunking_done is not None and chunking_done.is_set()
        if not chunker_finished:
            if chunking_done is None:
                # Resume path (no event): use durable state. Safe because on resume
                # the chunker runs to completion before the uploader starts — there
                # is no incremental chunks_created race.
                state = db.get_pipeline_state(content_hash)
                if state and state["chunks_created"] is not None:
                    if state["chunks_uploaded"] >= state["chunks_created"]:
                        db.mark_completed(content_hash)
                        return
            # Orchestrated path: wait for chunking_done event — don't trust
            # provisional chunks_created during incremental chunking.
            time.sleep(_POLL_INTERVAL)
            continue

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

    After the three stages complete, runs post-passes to:
    - Enrich chunk metadata from the ExtractionResult
    - Tag table-page chunks
    - Correct chunk_count to the final total
    - Prune stale chunks from a previous version

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

    if first_exc is None:
        for f in all_futures:
            exc = f.exception()
            if exc is not None:
                first_exc = exc
                break

    if first_exc is not None:
        db.mark_failed(content_hash, error=str(first_exc))
        raise first_exc

    # ── Post-passes (after all three stages complete) ────────────────────────

    extraction_result = extract_future.result()

    # Resolve collection once for all post-passes (avoids repeated API calls).
    col = t3.get_or_create_collection(collection)

    # 1. Metadata enrichment from ExtractionResult.
    _enrich_metadata_from_extraction(content_hash, extraction_result, pdf_path, t3, col, collection)

    # 2. table_regions post-pass.
    table_regions = extraction_result.metadata.get("table_regions", [])
    if table_regions:
        table_pages: set[int] = {r["page"] for r in table_regions}

        def _tag_table_page(meta: dict) -> bool:
            if meta.get("page_number", 0) in table_pages and meta.get("chunk_type") != "table_page":
                meta["chunk_type"] = "table_page"
                return True
            return False

        _update_chunk_metadata(t3, col, collection, content_hash, _tag_table_page)

    # 3. Stale chunk pruning.
    _prune_stale_chunks(col, str(pdf_path), content_hash)

    state = db.get_pipeline_state(content_hash)
    total_chunks = state["chunks_uploaded"] if state else 0
    db.delete_pipeline_data(content_hash)

    return total_chunks


def _enrich_metadata_from_extraction(
    content_hash: str,
    result: ExtractionResult,
    pdf_path: Path,
    t3: Any,
    col: Any,
    collection: str,
) -> None:
    """Post-pass: update chunk metadata with fields from ExtractionResult.

    Resolves source_title (docling_title → pdf_title → filename), source_author,
    extraction_method, format, page_count, is_image_pdf, has_formulas — matching
    the batch path in doc_indexer._pdf_chunks.

    Cannot use ``_update_chunk_metadata`` because ``chunk_count`` requires
    knowing the total number of chunks (``len(all_ids)``), which is only
    available after the full paginated query.
    """
    meta = result.metadata
    page_count = meta.get("page_count", 0) or 1
    text_len = len(result.text) if result.text else 0

    source_title = (
        meta.get("docling_title", "")
        or meta.get("pdf_title", "")
        or pdf_path.stem.replace("_", " ").replace("-", " ")
    )

    enrichment = {
        "source_title": source_title,
        "source_author": meta.get("pdf_author", ""),
        "source_date": meta.get("pdf_creation_date", ""),
        "extraction_method": meta.get("extraction_method", ""),
        "format": meta.get("format", ""),
        "page_count": meta.get("page_count", 0),
        "pdf_subject": meta.get("pdf_subject", ""),
        "pdf_keywords": meta.get("pdf_keywords", ""),
        "is_image_pdf": (text_len / page_count) < 20 if page_count else False,
        "has_formulas": meta.get("formula_count", 0) > 0,
    }

    # Also correct chunk_count to the final total.
    try:
        all_ids: list[str] = []
        all_metas: list[dict] = []
        offset = 0
        while True:
            batch = _chroma_with_retry(
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

        if not all_ids:
            return

        chunk_count = len(all_ids)
        updated_metas = [{**m, **enrichment, "chunk_count": chunk_count} for m in all_metas]

        t3.update_chunks(collection, all_ids, updated_metas)
    except Exception as exc:
        _log.warning("metadata_enrichment_failed", content_hash=content_hash, error=str(exc))


def _update_chunk_metadata(
    t3: Any,
    col: Any,
    collection: str,
    content_hash: str,
    update_fn: Callable[[dict], bool],
) -> None:
    """Generic post-pass: query chunks by content_hash, apply update_fn to each.

    Paginates the T3 query to handle documents with 300+ chunks.
    """
    try:
        all_ids: list[str] = []
        all_metas: list[dict] = []
        offset = 0
        while True:
            batch = _chroma_with_retry(
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
    except Exception:
        _log.debug("chunk_metadata_query_failed", content_hash=content_hash)
        return

    ids_to_update: list[str] = []
    updated_metas: list[dict] = []
    for cid, meta in zip(all_ids, all_metas):
        if update_fn(meta):
            ids_to_update.append(cid)
            updated_metas.append(meta)

    if ids_to_update:
        try:
            t3.update_chunks(collection, ids_to_update, updated_metas)
        except Exception:
            _log.debug("chunk_metadata_update_failed", count=len(ids_to_update))


def _prune_stale_chunks(
    col: Any, pdf_path: str, content_hash: str,
) -> None:
    """Delete chunks from T3 that belong to a previous version of the same PDF."""
    try:
        stale_ids: list[str] = []
        offset = 0

        # Use full content_hash from metadata to distinguish current vs stale.
        while True:
            batch = _chroma_with_retry(
                col.get,
                where={"source_path": pdf_path},
                include=["metadatas"],
                limit=300,
                offset=offset,
            )
            batch_ids = batch.get("ids", [])
            batch_metas = batch.get("metadatas", [])
            for eid, meta in zip(batch_ids, batch_metas):
                if meta.get("content_hash") != content_hash:
                    stale_ids.append(eid)
            if len(batch_ids) < 300:
                break
            offset += 300

        if stale_ids:
            _chroma_with_retry(col.delete, ids=stale_ids)
            _log.info("stale_chunks_pruned", count=len(stale_ids), pdf_path=pdf_path)
    except Exception:
        _log.debug("stale_prune_failed", pdf_path=pdf_path)
