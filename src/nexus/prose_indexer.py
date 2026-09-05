# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Prose file indexing: semantic markdown chunking and Voyage AI CCE embedding.

Extracted from indexer.py (RDR-032).  Public API::

    index_prose_file(ctx: IndexContext, file_path: Path) -> int

Handles both Markdown files (SemanticMarkdownChunker) and plain prose
(line-based chunking via _line_chunk).  Embeds via ``ctx.embed_fn``
(local mode) or the server-side stub (service mode); non-service
cloud-mode embedding was retired (nexus-sghyo).
"""
from __future__ import annotations

import hashlib as _hl

from nexus.chunk_identity import chunk_id_from_hash as _chunk_id_from_hash
from pathlib import Path

import structlog

from nexus.index_context import IndexContext
from nexus.indexer_utils import check_staleness

_log = structlog.get_logger(__name__)


def index_prose_file(ctx: IndexContext, file_path: Path) -> int:
    """Index a single prose file into the docs__ collection.

    Uses SemanticMarkdownChunker for .md/.markdown files, _line_chunk for all
    others.  Embeds via ``ctx.embed_fn`` (local mode) or server-side
    (service mode).

    Uses ``ctx`` in place of the old 12-parameter signature.

    Returns the post-filter chunk count (chunks upserted), or 0 ONLY when
    the file is legitimately fresh (staleness check hit — content and
    embedding model unchanged). nexus-hg2dw: every OTHER zero-content
    outcome (the file cannot be decoded as UTF-8 text, or decodes fine but
    produces no usable chunks) now raises
    :class:`~nexus.errors.UnextractableContentError` instead of silently
    returning 0 — a plain 0 return was indistinguishable from a legitimate
    skip, which left a document Pass 1 had already registered fenced
    nowhere (the registration bumps ``indexed_at``; nothing ever stamps
    ``index_state``). The raise reuses ``run_file_loop``'s existing
    nexus-deyd5 per-record-survivable handling — the caller sees this as a
    named, counted skip, not a run-ending failure.
    """
    from nexus.chunker import _line_chunk  # noqa: PLC0415 — deferred import — circular-dep avoidance / heavy dep deferred
    from nexus.errors import UnextractableContentError  # noqa: PLC0415 — deferred import — circular-dep avoidance / heavy dep deferred
    from nexus.md_chunker import SemanticMarkdownChunker, classify_section_type, parse_frontmatter  # noqa: PLC0415 — deferred import — circular-dep avoidance / heavy dep deferred
    from nexus.pdf_chunker import _extract_headings  # noqa: PLC0415 — deferred import — circular-dep avoidance / heavy dep deferred

    # nexus-hg2dw: resolved up front, before the read even happens, so a
    # decode failure below (which aborts before the staleness check's own
    # later resolution used to run) can still fence-fail this document.
    # Catalog Document.doc_id (RDR-101 Phase 3 PR δ): empty string when no
    # catalog handle exists.
    catalog_doc_id = (
        ctx.doc_id_resolver(file_path) if ctx.doc_id_resolver is not None else ""
    )

    def _fence_fail_and_raise(reason: str) -> None:
        # nexus-hg2dw: distinguishes a genuine zero-content outcome (this
        # file passed — or, for the decode case, never reached — the
        # staleness check, so it is NOT the "fresh, skip" branch below)
        # from a legitimate skip. Reused nexus-deyd5 machinery: raising
        # UnextractableContentError routes through run_file_loop's EXISTING
        # per-record-survivable handling (on_skip, _skipped_files, the
        # nexus-nukn3 durable failure record) with no changes there — the
        # run summary now names this file as "could not be extracted"
        # instead of silently counting it as an indistinguishable 0.
        if catalog_doc_id:
            from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 — deferred import; test patch target
            _fence_fail(catalog_doc_id, reason)
        raise UnextractableContentError(f"{file_path}: {reason}")

    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file_path), error=type(exc).__name__)
        _fence_fail_and_raise(f"cannot decode as UTF-8 text ({type(exc).__name__})")

    content_hash = _hl.sha256(content.encode()).hexdigest()

    # Staleness check — skip if content + model unchanged. Untouched by
    # nexus-hg2dw: a file that reaches this point and reads fresh was
    # never added to the registration-time needs_fence set (indexer.py
    # _catalog_hook only tracks a genuine file_hash content change), so
    # there is nothing to reconcile for it — a plain, unfenced `return 0`
    # remains correct here.
    if not ctx.force and check_staleness(
        ctx.col, file_path, content_hash, ctx.embedding_model,
        doc_id=catalog_doc_id,
        cache=ctx.staleness_cache,
    ):
        return 0

    # nexus-7niu: per-stage timer instrumentation. Silent when
    # ``ctx.stage_timers is None`` — no overhead, no output.
    _stage = (
        ctx.stage_timers.stage if ctx.stage_timers is not None
        else _noop_stage
    )

    ext = file_path.suffix.lower()
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []
    embed_texts: list[str] = []

    if ext in (".md", ".markdown"):
        # Markdown: use SemanticMarkdownChunker (M1: uses char offsets, not line numbers)
        with _stage("chunking"):
            frontmatter, body = parse_frontmatter(content, source=str(file_path))
            frontmatter_len = len(content) - len(body)
            base_meta: dict = {"source_path": str(file_path), "corpus": ctx.corpus}
            chunks = SemanticMarkdownChunker().chunk(body, base_meta)
        if not chunks:
            _log.debug("skipped file with no chunks", path=str(file_path))
            _fence_fail_and_raise("no chunks produced from markdown content")

        from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415 — circular-dep avoidance (nexus.metadata_schema)

        for chunk in chunks:
            title = f"{file_path.relative_to(ctx.repo_path)}:chunk-{chunk.chunk_index}"
            # ``chunk_chroma_id`` is the per-chunk Chroma natural-id:
            # ``chunk_text_hash[:32]`` per RDR-108 D1 (nexus-kmb6).
            chunk_text_hash_full = _hl.sha256(chunk.text.encode()).hexdigest()
            chunk_chroma_id = _chunk_id_from_hash(chunk_text_hash_full)  # nexus-4pvho
            # RDR-101 Phase 5c dropped corpus, store_type, git_meta. Title kept.
            # RDR-108 Phase 3 dropped chunk_index, chunk_count, doc_id;
            # catalog manifest is authoritative.
            metadata = make_chunk_metadata(
                content_type="markdown",
                chunk_text_hash=chunk_text_hash_full,
                content_hash=content_hash,
                chunk_start_char=chunk.metadata.get("chunk_start_char", 0) + frontmatter_len,
                chunk_end_char=chunk.metadata.get("chunk_end_char", 0) + frontmatter_len,
                indexed_at=ctx.now_iso,
                embedding_model=ctx.embedding_model,
                title=title,
                section_title=chunk.metadata.get("header_path", ""),
                section_type=chunk.metadata.get("section_type", ""),
                tags="markdown",
                category="prose",
                frecency_score=float(ctx.score),
            )
            ids.append(chunk_chroma_id)
            documents.append(chunk.text)
            metadatas.append(metadata)
            # Embed-only prefix: helps Voyage AI locate the right context without
            # polluting stored text.  Use header_path from chunk metadata (the raw
            # field, not the stored section_title which is the same string).
            header_path = chunk.metadata.get("header_path", "")
            if header_path:
                embed_texts.append(f"## Section: {header_path}\n\n{chunk.text}")
            else:
                embed_texts.append(chunk.text)
    else:
        # Non-markdown prose: use line-based chunking
        with _stage("chunking"):
            raw_chunks = _line_chunk(content)
        if not raw_chunks:
            if not content.strip():
                _fence_fail_and_raise("empty file content")
            raw_chunks = [(1, 1, content)]

        # Detect headings across the whole file once so each line-based
        # chunk can carry section_type / section_title (matches PDF and
        # markdown paths so prose-fallback chunks aren't second-class
        # citizens for section-scoped retrieval).
        from bisect import bisect_right  # noqa: PLC0415 — deferred import — circular-dep avoidance / heavy dep deferred
        _line_offsets = [0]
        for _i, _ch in enumerate(content):
            if _ch == "\n":
                _line_offsets.append(_i + 1)
        _headings = _extract_headings(content)
        _heading_offsets = [h[0] for h in _headings]

        from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415 — circular-dep avoidance (nexus.metadata_schema)

        for ls, le, text in raw_chunks:
            title = f"{file_path.relative_to(ctx.repo_path)}:{ls}-{le}"
            # ``chunk_chroma_id`` is the per-chunk Chroma natural-id:
            # ``chunk_text_hash[:32]`` per RDR-108 D1 (nexus-kmb6).
            chunk_text_hash_full = _hl.sha256(text.encode()).hexdigest()
            chunk_chroma_id = _chunk_id_from_hash(chunk_text_hash_full)  # nexus-4pvho
            chunk_start_char = _line_offsets[ls - 1] if 0 < ls <= len(_line_offsets) else 0
            chunk_end_char = (
                _line_offsets[le] if le < len(_line_offsets) else len(content)
            )
            section_title = ""
            section_type = ""
            if _headings:
                _h_idx = bisect_right(_heading_offsets, chunk_start_char) - 1
                if _h_idx >= 0:
                    section_title = _headings[_h_idx][1]
                    section_type = classify_section_type([section_title])
            # RDR-101 Phase 5c dropped corpus, store_type, git_meta. Title kept.
            # RDR-108 Phase 3 dropped chunk_index, chunk_count, doc_id —
            # catalog manifest is authoritative.
            metadata = make_chunk_metadata(
                content_type="prose",
                chunk_text_hash=chunk_text_hash_full,
                content_hash=content_hash,
                chunk_start_char=chunk_start_char,
                chunk_end_char=chunk_end_char,
                line_start=ls,
                line_end=le,
                indexed_at=ctx.now_iso,
                embedding_model=ctx.embedding_model,
                title=title,
                section_title=section_title,
                section_type=section_type,
                tags=ext.lstrip("."),
                category="prose",
                frecency_score=float(ctx.score),
            )
            ids.append(chunk_chroma_id)
            documents.append(text)
            metadatas.append(metadata)

    if not documents:
        _fence_fail_and_raise("no documents produced")

    # For non-markdown prose, embed_texts is empty; normalise to documents so
    # the filter below can work uniformly across both paths.
    if not embed_texts:
        embed_texts = list(documents)

    # Filter empty documents before embedding (Voyage AI rejects empty strings).
    valid = [
        (i, d, m, et)
        for i, d, m, et in zip(ids, documents, metadatas, embed_texts)
        if d and d.strip()
    ]
    if not valid:
        _fence_fail_and_raise("all chunks empty after whitespace filtering")
    ids, documents, metadatas, embed_texts = map(list, zip(*valid))

    # Embed: local mode uses embed_fn; service mode embeds server-side.
    with _stage("embed"):
        if ctx.embed_fn is not None:
            embeddings = ctx.embed_fn(embed_texts)
            actual_model = ctx.embedding_model
        else:
            from nexus.db.http_vector_client import is_vector_service_mode  # noqa: PLC0415 — circular-dep avoidance (nexus.db.http_vector_client)

            if is_vector_service_mode():
                # RDR-152 Seam B stub (nexus-fsquc): the service embeds
                # server-side and HttpVectorClient discards caller
                # embeddings — a client-side CCE call here paid Voyage
                # TWICE per prose chunk since RDR-155 P4a. Mirror
                # doc_indexer's stub: placeholder embeddings, no Voyage.
                embeddings = [[] for _ in embed_texts]
                actual_model = ctx.embedding_model
            else:
                # nexus-sghyo: non-service embedding was retired — the
                # client no longer embeds via Voyage.
                raise RuntimeError(
                    "non-service embedding was retired: the client no "
                    "longer embeds via Voyage. Set NX_STORAGE_BACKEND_"
                    "VECTORS=service (the default) or unset it."
                )
    if actual_model != ctx.embedding_model:
        for m in metadatas:
            m["embedding_model"] = actual_model

    # duoak 2C (nexus-1ugqs): stage in the cross-file batcher; hooks
    # defer to the orchestrator's completion callback on batch-land.
    # add() returning False (file exceeds one batch) falls through to
    # the legacy per-file upsert — file-atomicity preserved either way.
    if ctx.batcher is not None and ctx.batcher.add(  # type: ignore[attr-defined]
        str(file_path),
        ctx.corpus,
        ids,
        documents,
        metadatas,
        context={
            "ids": ids,
            "documents": documents,
            "embeddings": embeddings,
            "metadatas": metadatas,
            "catalog_doc_id": catalog_doc_id,
            "collection": ctx.corpus,
            "hooks": ctx.hooks,
        },
    ):
        return len(ids)

    # nexus-vw594 F1: producer #6 (nx index repo, prose/rdr, legacy
    # per-file fallback — reached when the ChunkBatcher rejects the file
    # or is absent). Fence begin BEFORE the upload, mirroring
    # doc_indexer.py's single-flush producers; this path was previously
    # entirely unfenced.
    if catalog_doc_id:
        from nexus.doc_indexer import _fence_begin  # noqa: PLC0415 — deferred import; test patch target
        _fence_begin(catalog_doc_id, content_hash, ctx.corpus)

    with _stage("upload"):
        try:
            ctx.db.upsert_chunks_with_embeddings(  # type: ignore[attr-defined]
                collection_name=ctx.corpus,
                ids=ids,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
                force_re_embed=ctx.force,
            )
        except Exception as upload_exc:
            # nexus-bhlfy: mirrors commands/store.py's cotmr fix — stamp
            # 'failed' unconditionally so the fence does not wedge at
            # 'indexing' with only the 6h doctor sweep as signal.
            # _fence_fail never raises, so the re-raise below always
            # carries the original exception unmasked.
            if catalog_doc_id:
                from nexus.doc_indexer import _fence_fail  # noqa: PLC0415 — deferred import; test patch target
                _fence_fail(catalog_doc_id, str(upload_exc))
            raise

    with _stage("hooks"):
        # Post-store hook chains (RDR-095). Both single-doc and batch
        # chains fire from every storage event; the per-doc loop covers
        # single-shape consumers on CLI ingest. Own stage bucket
        # (nexus-cfc72): under concurrent indexing these serialize on
        # LockedHookRegistry, and lock-wait must not read as upload time.
        # nexus-vw594 F1: file-atomic upload above — manifest_complete
        # rides this existing call through manifest_write_batch_hook's
        # write_manifest_many completion stamp, no extra round trip.
        ctx.hooks.fire_batch(
            ids, ctx.corpus, documents, embeddings, metadatas,
            catalog_doc_id=catalog_doc_id,
            manifest_complete={catalog_doc_id: content_hash} if catalog_doc_id else None,
        )
        for _did, _doc in zip(ids, documents):
            ctx.hooks.fire_single(_did, ctx.corpus, _doc)
        # RDR-089 document-grain chain — once per prose-file boundary.
        # content="" (chunk-level scope only); hook reads source_path.
        # nexus-tdgc: forward catalog doc_id when available.
        ctx.hooks.fire_document(
            str(file_path), ctx.corpus, "",
            doc_id=catalog_doc_id,
        )

    return len(ids)


# No-op context manager used when ``ctx.stage_timers is None`` so the
# instrumented code paths stay single-shape regardless of timing mode.
# Matches the helper in ``code_indexer``; both sites avoid importing
# each other to keep this module a leaf relative to the other indexer.
from contextlib import contextmanager as _contextmanager


@_contextmanager
def _noop_stage(_name: str):
    yield
