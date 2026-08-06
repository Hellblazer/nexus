# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Pipeline stage functions for streaming PDF indexing (RDR-048).

Three concurrent stages connected by the engine-backed pipeline buffer
(``HttpPipelineDB`` over ``nexus.pdf_pipeline`` — RDR-186 .16; the local
``pipeline.db`` SQLite buffer is retired):

1. **extractor_loop** — extracts pages → ``pdf_pages`` buffer
2. **chunker_loop** — polls pages, chunks stable prefix, embeds → ``pdf_chunks``
3. **uploader_loop** — reads embedded chunks, upserts to T3

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

from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import ExtractionResult, PDFExtractor
from nexus.db.http_pipeline_client import HttpPipelineDB
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
    """
    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415  — circular-dep avoidance (nexus.metadata_schema)

    # RDR-101 Phase 5c dropped corpus, store_type, git_meta. Title kept.
    # RDR-108 Phase 3 dropped chunk_index, chunk_count, doc_id.
    return make_chunk_metadata(
        content_type="pdf",
        chunk_text_hash=hashlib.sha256(chunk.text.encode()).hexdigest(),
        content_hash=content_hash,
        chunk_start_char=chunk.metadata.get("chunk_start_char", 0),
        chunk_end_char=chunk.metadata.get("chunk_end_char", 0),
        page_number=chunk.metadata.get("page_number", 0),
        indexed_at=now_iso,
        embedding_model=embedding_model,
        title="",                 # post-pass: from ExtractionResult
        source_author="",         # post-pass: from ExtractionResult
        section_title=chunk.metadata.get("section_title", ""),
        section_type=chunk.metadata.get("section_type", ""),
        tags="pdf",
        category="paper",
    )


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
            # the chunk up; the uploader struct.unpacks ``b""`` to ``[]`` and
            # ``HttpVectorClient.upsert_chunks_with_embeddings`` discards the
            # empty vector and embeds from the chunk text. Mirrors the batch
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
    chunker = PDFChunker(chunk_chars=chunk_chars)
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
) -> None:
    """Poll chunk buffer for embedded chunks and upsert to T3 ChromaDB.

    *catalog_doc_id* (RDR-108 Phase 3) — catalog ``Document.tumbler``
    string for the document this pipeline run is processing. Threaded
    through to ``HookRegistry.fire_batch`` so ``manifest_write_batch_hook``
    can write the manifest without having to read it from chunk metadata
    (which Phase 3 retired).
    """
    total_uploaded = 0

    while not cancel.is_set():
        chunks = db.read_uploadable_chunks(content_hash, limit=_UPLOAD_BATCH_SIZE)

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

                # RDR-108 Phase 3: inject the per-row global chunk_index
                # from T2 into the metadata blob BEFORE firing the batch
                # chain. The blob in T2 was stamped via
                # ``make_chunk_metadata`` (post-Phase-3, no chunk_index),
                # so the manifest hook would otherwise default to a
                # batch-local enumeration index — wrong for multi-batch
                # streaming where each batch resets to 0. T3 has already
                # been upserted with the post-Phase-3 metadata; mutating
                # the local copy now is safe and only affects the hook
                # payload. ``row["chunk_index"]`` is the chunker's
                # canonical global ordering.
                for _i, row in enumerate(batch):
                    metadatas[_i]["chunk_index"] = row["chunk_index"]

                # Post-store hook chains (RDR-095). Both single-doc and
                # batch chains fire from every storage event; the per-doc
                # loop covers single-shape consumers on CLI ingest.
                if hooks is None:
                    from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 - deferred to avoid circular import at module load
                    hooks = HookRegistry()
                    install_default_hooks(hooks)
                hooks.fire_batch(
                    ids, collection, documents, embeddings, metadatas,
                    catalog_doc_id=catalog_doc_id,
                )
                for _did, _doc in zip(ids, documents):
                    hooks.fire_single(_did, collection, _doc)

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
        remaining = db.read_uploadable_chunks(content_hash, limit=1)
        if remaining:
            continue

        state = db.get_pipeline_state(content_hash)
        if state and state["chunks_created"] is not None and state["chunks_uploaded"] >= state["chunks_created"]:
            db.mark_completed(content_hash)
            return

        time.sleep(_POLL_INTERVAL)


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
            writer.update(
                existing.tumbler,
                physical_collection=collection_name,
                chunk_count=chunk_count,
                indexed_at=datetime.now(UTC).isoformat(),
                source_mtime=source_mtime,
            )
        else:
            # nexus-u8n4r: refuse a brand-new registration when
            # ``file_path_str`` (always absolute — see the resolve()
            # above) sits under an agent worktree or system temp dir,
            # unless this owner's own repo_root is itself rooted there.
            # See ``nexus.repo_identity.should_skip_ephemeral_registration``.
            from nexus.repo_identity import (  # noqa: PLC0415 - circular-dep avoidance (nexus.repo_identity)
                owner_repo_root_best_effort,
                should_skip_ephemeral_registration,
            )
            _owner_repo_root = owner_repo_root_best_effort(reader, owner)
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
            writer.register(
                owner=owner, title=effective_title, content_type="paper",
                author=author, year=year, corpus=corpus,
                physical_collection=collection_name,
                chunk_count=chunk_count,
                file_path=file_path_str,
                source_mtime=source_mtime,
                source_uri=source_uri,
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
    doc_id: str = "",
    hooks: "HookRegistry | None" = None,
    source_uri: str = "",
    allow_degraded_extraction: bool = False,
) -> int:
    """Three-stage streaming pipeline for PDFs.

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
    if not doc_id:
        from nexus.doc_indexer import _register_or_lookup_doc_id  # noqa: PLC0415 - deferred to avoid circular import at module load
        doc_id = _register_or_lookup_doc_id(
            pdf_path, corpus,
            content_type="paper",
            physical_collection=collection,
        )

    # nexus-9ji: --force must break the partial-ingest deadlock. Both
    # pipeline-buffer state and T3 orphan chunks can independently block
    # re-ingest; wipe both before the pre-flight.
    if force:
        db.delete_pipeline_data(content_hash)
        try:
            col = t3.get_or_create_collection(collection)
            col.delete(where={"content_hash": content_hash})
        except Exception as exc:  # noqa: BLE001 - best-effort orphan cleanup; logged via log.warning
            _log.warning(
                "force_t3_orphan_cleanup_failed",
                content_hash=content_hash,
                collection=collection,
                error=str(exc),
            )

    # Pre-flight: check if pipeline should run before resolving credentials.
    result = db.create_pipeline(content_hash, str(pdf_path), collection)
    if result == "skip":
        # nexus-lcmbp: "skip" now means ONLY "already completed" — a
        # fresh-heartbeat 'running' row raises PipelineConflictRunning
        # from create_pipeline() above instead of reaching this branch.
        _log.info("pipeline_skip", content_hash=content_hash, reason="already completed")
        return 0

    # Resolve embed_fn from credentials when not provided (matches batch path).
    if embed_fn is None:
        from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415  — circular-dep avoidance (nexus.db.http_vector_client)
        if is_vector_service_mode():
            # nexus-9n1u3 / RDR-152 Seam B: leave embed_fn=None — the service
            # embeds server-side at upload time. The embed stage writes a
            # non-NULL empty-blob sentinel and the uploader routes through
            # HttpVectorClient.upsert_chunks_with_embeddings (JVM embeds).
            # Mirrors the batch path (doc_indexer._index_pdf_document).
            pass
        else:
            from nexus.config import get_credential, load_config  # noqa: PLC0415 - deferred to avoid circular import at module load
            voyage_key = get_credential("voyage_api_key")
            if voyage_key:
                from nexus.doc_indexer import _embed_with_fallback  # noqa: PLC0415 - deferred to avoid circular import at module load
                timeout = load_config().get("voyageai", {}).get("read_timeout_seconds", 120.0)
                embed_fn = lambda texts, model: _embed_with_fallback(texts, model, voyage_key, timeout=timeout)
            else:
                try:
                    db.mark_failed(content_hash, error="voyage_api_key not configured")
                except Exception:  # noqa: BLE001 — boundary catch: the RuntimeError below must propagate, not a /fail transport error
                    _log.warning(
                        "pipeline_terminal_mark_failed",
                        content_hash=content_hash,
                        exc_info=True,
                    )
                raise RuntimeError(
                    "voyage_api_key not configured — cannot embed for streaming "
                    "pipeline (set a Voyage key, or use service mode for "
                    "server-side embedding)"
                )

    # nexus-5xn3k.4 RUNFENCE C2: commit the fence BEFORE the first chunk
    # upsert (memo §3.5 T0) — right before the executor starts the
    # uploader stage. Gated on doc_id per the no-catalog ingest contract.
    if doc_id:
        from nexus.doc_indexer import _fence_begin  # noqa: PLC0415 - deferred to avoid circular import at module load
        _fence_begin(doc_id, content_hash, collection)

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
        try:
            db.mark_failed(content_hash, error=str(first_exc))
            db.clear_orphan_wal(content_hash)
        except Exception:  # noqa: BLE001 — boundary catch: terminal-state bookkeeping must never mask first_exc
            _log.warning(
                "pipeline_terminal_mark_failed",
                content_hash=content_hash,
                original_error=str(first_exc),
                exc_info=True,
            )
        # nexus-5xn3k.4: _fence_fail never raises, so first_exc propagation
        # below cannot be masked by a fence-write failure.
        if doc_id:
            from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 - deferred to avoid circular import at module load
            _fence_fail(doc_id, str(first_exc))
        raise first_exc

    # ── Post-passes (after all three stages complete) ────────────────────────

    extraction_result = extract_future.result()

    # Resolve collection once for all post-passes (avoids repeated API calls).
    col = t3.get_or_create_collection(collection)

    # Track post-pass success — pipeline data preserved on failure (nexus-pfmr).
    post_pass_ok = True

    # 1. Metadata enrichment from ExtractionResult.
    if not _enrich_metadata_from_extraction(content_hash, extraction_result, pdf_path, t3, col, collection):
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

    # Catalog hook: register PDF in catalog (opt-in, graceful absence)
    title = (
        extraction_result.title
        if hasattr(extraction_result, "title") and extraction_result.title
        else extraction_result.metadata.get("title", "")
        if hasattr(extraction_result, "metadata")
        else ""
    ) or pdf_path.stem
    author = extraction_result.metadata.get("author", "") if hasattr(extraction_result, "metadata") else ""
    # Extract year from pdf_creation_date or explicit year field
    year_raw = 0
    if hasattr(extraction_result, "metadata"):
        year_raw = extraction_result.metadata.get("year", 0)
        if not year_raw:
            creation_date = extraction_result.metadata.get("pdf_creation_date", "")
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

    # RDR-089 document-grain chain — once per PDF boundary at the streaming
    # pipeline tail. content="" (PDF text streamed not retained); the hook
    # reads source_path itself per the P0.1 content-sourcing contract.
    # nexus-tdgc: _catalog_pdf_hook ran above so the catalog entry now
    # exists; resolve the doc_id and forward it to the document chain.
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 - deferred to avoid circular import at module load
    from nexus.doc_indexer import _lookup_existing_doc_id  # noqa: PLC0415 - deferred to avoid circular import at module load
    _cat = make_catalog_reader()
    hooks.fire_document(
        str(pdf_path), collection, "",
        doc_id=_lookup_existing_doc_id(_cat, str(pdf_path), corpus),
    )

    # nexus-5xn3k.4 RUNFENCE C2: the fence tail — after every possible
    # manifest touch (including the fire_document hook above), before
    # returning. Gated on doc_id per the no-catalog ingest contract.
    if doc_id:
        from nexus.doc_indexer import _fence_complete, _fence_fail  # noqa: PLC0415 - deferred to avoid circular import at module load
        if total_chunks == 0:
            # MUST: zero extraction is a FAILURE, never /complete(0) — a
            # zero-chunk run would trivially satisfy the fail-closed gate
            # (referenced=0 == chunk_count=0) and stamp 'complete' on a
            # silently-failed extraction. No content-free exception exists
            # for PDFs.
            _fence_fail(doc_id, "zero chunks extracted")
        elif post_pass_ok:
            _fence_complete(doc_id, content_hash, total_chunks)
        else:
            _log.warning(
                "index_run_complete_skipped_post_pass_failed",
                doc_id=doc_id,
                content_hash=content_hash,
                reason="post-pass failed — fence stays 'indexing' for retry",
            )

    return total_chunks


def _enrich_metadata_from_extraction(
    content_hash: str,
    result: ExtractionResult,
    pdf_path: Path,
    t3: Any,
    col: Any,
    collection: str,
) -> bool:
    """Post-pass: update chunk metadata with fields from ExtractionResult.

    Resolves source_title (docling_title → pdf_title → filename) and
    source_author — matching the batch path in doc_indexer._pdf_chunks.

    RDR-108 Phase 3: ``chunk_count`` was retired from the chunk schema
    (catalog ``document_chunks`` manifest carries it at document scope),
    so the post-pass no longer has to correct chunk_count after the fact.

    Returns True on success, False on failure (nexus-pfmr).
    """
    meta = result.metadata
    page_count = meta.get("page_count", 0) or 1
    text_len = len(result.text) if result.text else 0

    source_title = (
        meta.get("docling_title", "")
        or meta.get("pdf_title", "")
        or pdf_path.stem.replace("_", " ").replace("-", " ")
    )

    # `title`, `source_author`, (nexus-1oguj) `extraction_method`, and
    # (nexus-wi1uv round-2) `quality_gate_overridden` are the only
    # extraction-dependent fields in ALLOWED_TOP_LEVEL — the other fields
    # below (source_date, format, page_count, pdf_subject, pdf_keywords,
    # is_image_pdf, has_formulas) are dropped by metadata_schema.normalize()
    # so writing them costs cycles for no payload. Keep this dict minimal.
    #
    # nexus-wi1uv round-2 CORRECTION: the historical comment here claimed
    # "t3.update_chunks re-runs metadata_schema.normalize() before
    # writing" for the empty-extraction_method case. VERIFIED FALSE for
    # the production path: HttpVectorClient.update_chunks (src/nexus/db/
    # http_vector_client.py) posts the raw metadata dict straight to
    # /v1/vectors/update-metadata with no client-side normalize() call —
    # only the LOCAL/in-memory T3Database.update_chunks normalizes, which
    # is not the serving path in either mode post-RDR-155-P4a. Harmless
    # for extraction_method (a PDF's value is never empty by the time
    # this post-pass runs, so the drop-when-empty branch was dead code
    # via THIS call site regardless), but NOT harmless for quality_gate_
    # overridden, whose False default is the COMMON case for every
    # streamed PDF — merging it in unconditionally would silently stamp
    # every healthy document with an explicit False, defeating the
    # sparse-key design in metadata_schema.normalize(). So: build this
    # dict conditionally instead of trusting a downstream normalize that
    # does not run on this path.
    enrichment = {
        "title": source_title,
        "source_author": meta.get("pdf_author", ""),
        "extraction_method": meta.get("extraction_method", ""),
    }
    if meta.get("quality_gate_overridden", False):
        enrichment["quality_gate_overridden"] = True

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

        if not all_ids:
            return True

        updated_metas = [{**m, **enrichment} for m in all_metas]

        t3.update_chunks(collection, all_ids, updated_metas)
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
        if update_fn(meta):
            ids_to_update.append(cid)
            updated_metas.append(meta)

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
