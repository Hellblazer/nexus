# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Prose file indexing: semantic markdown chunking and Voyage AI CCE embedding.

Extracted from indexer.py (RDR-032).  Public API::

    index_prose_file(ctx: IndexContext, file_path: Path) -> int

Handles both Markdown files (SemanticMarkdownChunker) and plain prose
(line-based chunking via _line_chunk).  Delegates to
doc_indexer._embed_with_fallback for CCE-aware embedding.
"""
from __future__ import annotations

import hashlib as _hl
from pathlib import Path

import structlog

from nexus.index_context import IndexContext
from nexus.indexer_utils import check_staleness

_log = structlog.get_logger(__name__)


def index_prose_file(ctx: IndexContext, file_path: Path) -> int:
    """Index a single prose file into the docs__ collection.

    Uses SemanticMarkdownChunker for .md/.markdown files, _line_chunk for all
    others.  Embeds via _embed_with_fallback (CCE for voyage-context-3).

    Uses ``ctx`` in place of the old 12-parameter signature.

    Returns the post-filter chunk count (chunks upserted), or 0 if
    skipped (current) or failed.
    """
    from nexus.chunker import _line_chunk
    from nexus.doc_indexer import _embed_with_fallback
    from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter

    try:
        content = file_path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        _log.debug("skipped non-text file", path=str(file_path), error=type(exc).__name__)
        return 0

    content_hash = _hl.sha256(content.encode()).hexdigest()

    # Staleness check — skip if content + model unchanged
    if not ctx.force and check_staleness(ctx.col, file_path, content_hash, ctx.embedding_model):
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
            frontmatter, body = parse_frontmatter(content)
            frontmatter_len = len(content) - len(body)
            base_meta: dict = {"source_path": str(file_path), "corpus": ctx.corpus}
            chunks = SemanticMarkdownChunker().chunk(body, base_meta)
        if not chunks:
            _log.debug("skipped file with no chunks", path=str(file_path))
            return 0

        for chunk in chunks:
            title = f"{file_path.relative_to(ctx.repo_path)}:chunk-{chunk.chunk_index}"
            doc_id = _hl.sha256(f"{ctx.corpus}:{title}".encode()).hexdigest()[:32]
            metadata: dict = {
                "title": title,
                "tags": "markdown",
                "category": "prose",
                "session_id": "",
                "source_agent": "nexus-indexer",
                "store_type": "prose",
                "indexed_at": ctx.now_iso,
                "expires_at": "",
                "ttl_days": 0,
                "source_path": str(file_path),
                # M1: SemanticMarkdownChunker uses char offsets, not line numbers
                "line_start": 0,
                "line_end": 0,
                "chunk_start_char": chunk.metadata.get("chunk_start_char", 0) + frontmatter_len,
                "chunk_end_char": chunk.metadata.get("chunk_end_char", 0) + frontmatter_len,
                "section_title": chunk.metadata.get("header_path", ""),
                "section_type": chunk.metadata.get("section_type", ""),
                "frecency_score": float(ctx.score),
                "chunk_index": chunk.chunk_index,
                "chunk_count": len(chunks),
                "corpus": ctx.corpus,
                "embedding_model": ctx.embedding_model,
                "content_hash": content_hash,
                "chunk_text_hash": _hl.sha256(chunk.text.encode()).hexdigest(),
                **ctx.git_meta,
            }
            ids.append(doc_id)
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
                return 0
            raw_chunks = [(1, 1, content)]
        total_chunks = len(raw_chunks)

        for i, (ls, le, text) in enumerate(raw_chunks):
            title = f"{file_path.relative_to(ctx.repo_path)}:{ls}-{le}"
            doc_id = _hl.sha256(f"{ctx.corpus}:{title}".encode()).hexdigest()[:32]
            metadata = {
                "title": title,
                "tags": ext.lstrip("."),
                "category": "prose",
                "session_id": "",
                "source_agent": "nexus-indexer",
                "store_type": "prose",
                "indexed_at": ctx.now_iso,
                "expires_at": "",
                "ttl_days": 0,
                "source_path": str(file_path),
                "line_start": ls,
                "line_end": le,
                "frecency_score": float(ctx.score),
                "chunk_index": i,
                "chunk_count": total_chunks,
                "corpus": ctx.corpus,
                "embedding_model": ctx.embedding_model,
                "content_hash": content_hash,
                "chunk_text_hash": _hl.sha256(text.encode()).hexdigest(),
                "section_type": "",
                **ctx.git_meta,
            }
            ids.append(doc_id)
            documents.append(text)
            metadatas.append(metadata)

    if not documents:
        return 0

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
        return 0
    ids, documents, metadatas, embed_texts = map(list, zip(*valid))

    # Embed: local mode uses embed_fn; cloud uses _embed_with_fallback (CCE)
    with _stage("embed"):
        if ctx.embed_fn is not None:
            embeddings = ctx.embed_fn(embed_texts)
            actual_model = ctx.embedding_model
        else:
            embeddings, actual_model = _embed_with_fallback(
                embed_texts, ctx.embedding_model, ctx.voyage_key, timeout=ctx.timeout
            )
    if actual_model != ctx.embedding_model:
        for m in metadatas:
            m["embedding_model"] = actual_model

    with _stage("upload"):
        ctx.db.upsert_chunks_with_embeddings(  # type: ignore[attr-defined]
            collection_name=ctx.corpus,
            ids=ids,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
        )

        # Chash dual-write (RDR-086 Phase 1.2): global chash → (collection, doc_id).
        try:
            from nexus.mcp_infra import chash_dual_write_batch
            chash_dual_write_batch(ids, ctx.corpus, metadatas)
        except Exception:
            _log.debug("chash_dual_write_failed", exc_info=True)

        # Incremental taxonomy: assign chunks to nearest existing topics.
        try:
            from nexus.mcp_infra import taxonomy_assign_batch
            taxonomy_assign_batch(ids, ctx.corpus, embeddings)
        except Exception:
            _log.debug("taxonomy_incremental_assign_failed", exc_info=True)

    return len(ids)


# No-op context manager used when ``ctx.stage_timers is None`` so the
# instrumented code paths stay single-shape regardless of timing mode.
# Matches the helper in ``code_indexer``; both sites avoid importing
# each other to keep this module a leaf relative to the other indexer.
from contextlib import contextmanager as _contextmanager


@_contextmanager
def _noop_stage(_name: str):
    yield
