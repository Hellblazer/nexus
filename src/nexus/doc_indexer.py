# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document indexing pipeline: PDF and Markdown → T3 collections.

By default documents are stored in ``docs__`` collections.  Callers can
override the collection name for other prefixes (e.g. ``rdr__``).
"""
from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import structlog

_log = structlog.get_logger(__name__)

from nexus.corpus import index_model_for_collection
from nexus.db import make_t3
from nexus.retry import _chroma_with_retry, _voyage_with_retry
from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import PDFExtractor

# Type alias for the chunking callback used by _index_document.
# Receives (file_path, content_hash, target_model, now_iso, corpus) and returns
# a list of (chunk_id, document_text, metadata_dict) tuples, or an empty list.
ChunkFn = Callable[[Path, str, str, str, str], list[tuple[str, str, dict]]]

# Type alias for a local embedding function (replaces _embed_with_fallback).
# Receives (texts, model) and returns (embeddings, actual_model).
EmbedFn = Callable[[list[str], str], tuple[list[list[float]], str]]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _has_credentials() -> bool:
    from nexus.config import get_credential
    return bool(get_credential("voyage_api_key") and get_credential("chroma_api_key"))


_CCE_TOKEN_LIMIT = 32_000
_CCE_TOTAL_TOKEN_LIMIT = 120_000  # Voyage API total token limit across all inputs
# Note: per-batch limit of 32K means we never hit 120K in a single call
_CCE_MAX_TOTAL_CHUNKS = 16_000  # Voyage API limit: max 16K chunks across all inputs
_EMBED_BATCH_SIZE = 128  # Voyage AI embed() limit is 1,000; use conservative batch size
_CCE_MAX_BATCH_CHUNKS = 1000  # Voyage API limit: max 1,000 inputs per request


def _batch_chunks_for_cce(chunks: list[str]) -> list[list[str]]:
    """Split chunks into batches that each fit within the CCE token limit.

    Each batch must have >= 2 chunks (CCE requirement).  Single-leftover
    chunks are merged into the previous batch rather than dropped.
    """
    batches: list[list[str]] = []
    current: list[str] = []
    current_tokens = 0
    for chunk in chunks:
        chunk_tokens = len(chunk) // 3
        if current and (current_tokens + chunk_tokens > _CCE_TOKEN_LIMIT or len(current) >= _CCE_MAX_BATCH_CHUNKS):
            batches.append(current)
            current = [chunk]
            current_tokens = chunk_tokens
        else:
            current.append(chunk)
            current_tokens += chunk_tokens
    if current:
        # CCE requires >= 2 chunks per batch; merge singletons into previous batch
        # but only if that won't exceed the per-batch chunk limit
        if len(current) < 2 and batches and len(batches[-1]) < _CCE_MAX_BATCH_CHUNKS:
            batches[-1].extend(current)
        else:
            batches.append(current)
    return batches


def _embed_with_fallback(
    chunks: list[str],
    model: str,
    api_key: str,
    input_type: str = "document",
    timeout: float = 120.0,
) -> tuple[list[list[float]], str]:
    """Embed chunks using CCE when possible, falling back to voyage-4 on failure.

    Large documents are automatically batched into groups that fit within the
    CCE token limit.  Returns ``(embeddings, actual_model_used)`` so callers
    can record the model that produced the stored vectors in metadata.

    Note: both voyage-context-3 and voyage-4 produce 1024-dim embeddings,
    so mixed CCE/fallback batches are dimensionally compatible for ChromaDB.

    We rely on Voyage's default truncation=True. Our chunker keeps chunks well
    under model context limits, so truncation should never activate. If it does,
    the embedding is still usable (just based on truncated text).
    """
    # Filter out empty strings — Voyage AI rejects them
    chunks = [c for c in chunks if c and c.strip()]
    if not chunks:
        return [], model
    if len(chunks) >= _CCE_MAX_TOTAL_CHUNKS:
        _log.warning(
            "chunk count exceeds Voyage API limit",
            chunk_count=len(chunks),
            limit=_CCE_MAX_TOTAL_CHUNKS,
        )
    try:
        import voyageai
    except Exception as exc:
        raise ImportError(
            "voyageai is required for cloud doc indexing but is not installed. "
            "Install with: uv tool install conexus --with 'conexus[cloud]' --force"
        ) from exc
    client = voyageai.Client(api_key=api_key, timeout=timeout, max_retries=3)
    if model == "voyage-context-3":
        if len(chunks) < 2:
            # CCE requires 2+ chunks; fall back to voyage-4 (the query-time model)
            # so stored vectors are in the same embedding space as query vectors.
            model = "voyage-4"
        else:
            batches = _batch_chunks_for_cce(chunks)
            all_embeddings: list[list[float]] = []
            any_fallback = False
            for batch in batches:
                try:
                    result = _voyage_with_retry(
                        client.contextualized_embed,
                        inputs=[batch], model=model, input_type=input_type,
                    )
                    all_embeddings.extend(result.results[0].embeddings)
                except Exception as exc:
                    any_fallback = True
                    _log.warning("CCE failed for batch, falling back to voyage-4",
                                 error=str(exc), batch_size=len(batch))
                    for j in range(0, len(batch), _EMBED_BATCH_SIZE):
                        sub = batch[j:j + _EMBED_BATCH_SIZE]
                        fb = _voyage_with_retry(client.embed, texts=sub, model="voyage-4", input_type=input_type)
                        all_embeddings.extend(fb.embeddings)
            if all_embeddings:
                # Report voyage-4 if any batch fell back — forces re-index on next run
                return all_embeddings, "voyage-4" if any_fallback else model
            model = "voyage-4"
    # Standard embedding path (voyage-4 or any non-CCE model)
    all_emb: list[list[float]] = []
    for i in range(0, len(chunks), _EMBED_BATCH_SIZE):
        batch = chunks[i:i + _EMBED_BATCH_SIZE]
        result = _voyage_with_retry(client.embed, texts=batch, model=model, input_type=input_type)
        all_emb.extend(result.embeddings)
    return all_emb, model


def _index_document(
    file_path: Path,
    corpus: str,
    chunk_fn: ChunkFn,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
) -> int | list[dict]:
    """Shared indexing pipeline: credential check, staleness, embed, upsert, prune.

    *chunk_fn(file_path, content_hash, target_model, now_iso)* produces the
    per-format (chunk_id, document_text, metadata_dict) tuples.  Returns the
    number of chunks indexed, or 0 if skipped.

    When *collection_name* is provided it is used as the T3 collection name
    directly, bypassing the default ``docs__{corpus}`` derivation.  This is
    used for RDR collections (``rdr__<repo>-<hash8>``).

    When *embed_fn* is provided it replaces ``_embed_with_fallback`` and the
    Voyage AI credential check is skipped.  This supports local dry-run mode
    (ONNX / DefaultEmbeddingFunction) without requiring any API keys.

    When *return_metadata* is True, returns the prepared chunk metadatas list
    instead of a bare int.  Callers (index_pdf, index_markdown) use it to
    build format-specific summary dicts.  Default False preserves the existing
    int return type with zero overhead.
    """
    if embed_fn is None and not _has_credentials():
        return 0

    content_hash = _sha256(file_path)
    if collection_name is None:
        collection_name = f"docs__{corpus}"
    db = t3 if t3 is not None else make_t3()
    col = db.get_or_create_collection(collection_name)

    target_model = index_model_for_collection(collection_name)

    # Incremental sync: skip if file is already indexed with the same hash AND model
    existing = _chroma_with_retry(
        col.get,
        where={"source_path": str(file_path)},
        include=["metadatas"],
        limit=1,
    )
    if not force and existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        if stored_hash == content_hash and stored_model == target_model:
            return 0

    now_iso = datetime.now(UTC).isoformat()
    prepared = chunk_fn(file_path, content_hash, target_model, now_iso, corpus)
    if not prepared:
        return 0

    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas = [p[2] for p in prepared]

    if embed_fn is not None:
        embeddings, actual_model = embed_fn(documents, target_model)
    else:
        from nexus.config import get_credential, load_config
        voyage_key = get_credential("voyage_api_key")
        if not voyage_key:
            raise RuntimeError("voyage_api_key must be set — unreachable if _has_credentials() passed")
        timeout = load_config().get("voyageai", {}).get("read_timeout_seconds", 120.0)
        embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key, timeout=timeout)
    if actual_model != target_model:
        for m in metadatas:
            m["embedding_model"] = actual_model
    db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)

    # Prune stale chunks from a previous (larger) version of this file.
    # Paginate: ChromaDB Cloud returns at most 300 records per get() call.
    current_ids_set = set(ids)
    stale_ids: list[str] = []
    offset = 0
    while True:
        batch = _chroma_with_retry(
            col.get,
            where={"source_path": str(file_path)},
            include=[],
            limit=300,
            offset=offset,
        )
        batch_ids = batch.get("ids", [])
        stale_ids.extend(eid for eid in batch_ids if eid not in current_ids_set)
        if len(batch_ids) < 300:
            break
        offset += 300
    if stale_ids:
        _chroma_with_retry(col.delete, ids=stale_ids)

    if return_metadata:
        return metadatas
    return len(prepared)


def _pdf_chunks(
    pdf_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
    corpus: str,
    *,
    chunk_chars: int | None = None,
) -> list[tuple[str, str, dict]]:
    """Chunk a PDF and return (id, text, metadata) tuples.

    *chunk_chars* overrides the default chunk size (1500 chars).  When None
    the PDFChunker default is used.  Pass ``tuning.pdf_chunk_chars`` from
    TuningConfig to honour per-repo configuration.
    """
    result = PDFExtractor().extract(pdf_path)
    chunker = PDFChunker(chunk_chars=chunk_chars) if chunk_chars is not None else PDFChunker()
    chunks = chunker.chunk(result.text, result.metadata)
    if not chunks:
        return []

    # Heuristic: fewer than 20 chars per page suggests a scanned/image-only PDF.
    # Per-page normalisation avoids false positives on short-but-real documents.
    _page_count = result.metadata.get("page_count", 1) or 1
    is_image_pdf = (len(result.text) / _page_count) < 20

    prepared: list[tuple[str, str, dict]] = []
    for chunk in chunks:
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta: dict = {
            "source_path": str(pdf_path),
            "source_title": (
                result.metadata.get("docling_title", "")
                or result.metadata.get("pdf_title", "")
                or pdf_path.stem.replace("_", " ").replace("-", " ")
            ),
            "source_author": result.metadata.get("pdf_author", ""),
            "source_date": result.metadata.get("pdf_creation_date", ""),
            "corpus": corpus,
            "store_type": "pdf",
            "page_count": result.metadata.get("page_count", 0),
            "page_number": chunk.metadata.get("page_number", 0),
            "section_title": "",
            "format": result.metadata.get("format", ""),
            "extraction_method": result.metadata.get("extraction_method", ""),
            "chunk_index": chunk.chunk_index,
            "chunk_count": len(chunks),
            "chunk_start_char": chunk.metadata.get("chunk_start_char", 0),
            "chunk_end_char": chunk.metadata.get("chunk_end_char", 0),
            "embedding_model": target_model,
            "indexed_at": now_iso,
            "content_hash": content_hash,
            "pdf_subject": result.metadata.get("pdf_subject", ""),
            "pdf_keywords": result.metadata.get("pdf_keywords", ""),
            "is_image_pdf": is_image_pdf,
        }
        prepared.append((chunk_id, chunk.text, meta))
    return prepared


def _markdown_chunks(
    md_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
    corpus: str,
) -> list[tuple[str, str, dict]]:
    """Chunk a Markdown file and return (id, text, metadata) tuples."""
    raw_text = md_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw_text)
    frontmatter_len = len(raw_text) - len(body)

    base_meta: dict = {
        "source_path": str(md_path),
        "corpus": corpus,
    }
    chunks = SemanticMarkdownChunker().chunk(body, base_meta)
    if not chunks:
        return []

    prepared: list[tuple[str, str, dict]] = []
    for chunk in chunks:
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta: dict = {
            "source_path": str(md_path),
            "source_title": str(frontmatter.get("title", "")),
            "source_author": str(frontmatter.get("author", "")),
            "source_date": str(frontmatter.get("date", "")),
            "corpus": corpus,
            "store_type": "markdown",
            "page_count": 0,
            "page_number": chunk.metadata.get("page_number", 0),
            "section_title": chunk.metadata.get("header_path", ""),
            "format": "markdown",
            "extraction_method": "markdown_chunker",
            "chunk_index": chunk.chunk_index,
            "chunk_count": len(chunks),
            "chunk_start_char": chunk.metadata.get("chunk_start_char", 0) + frontmatter_len,
            "chunk_end_char": chunk.metadata.get("chunk_end_char", 0) + frontmatter_len,
            "embedding_model": target_model,
            "indexed_at": now_iso,
            "content_hash": content_hash,
        }
        prepared.append((chunk_id, chunk.text, meta))
    return prepared


def index_pdf(
    pdf_path: Path,
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
) -> int | dict:
    """Index *pdf_path* into a T3 collection.

    By default the collection is ``docs__{corpus}``.  Pass *collection_name*
    to override (e.g. ``knowledge__delos`` for external reference corpora).

    Returns the number of chunks indexed, or 0 if skipped (no credentials or
    content unchanged since last index with the same embedding model).

    Pass *embed_fn* to override the default Voyage AI embedding (e.g. a local
    ONNX function for dry-run mode).  When *embed_fn* is provided the Voyage
    credential check is bypassed.

    Pass *force=True* to bypass the staleness check and always re-index.

    When *return_metadata* is True, returns a dict instead of an int::

        {"chunks": int, "pages": list[int], "title": str, "author": str}

    Metadata is derived from chunk metadatas produced during extraction
    (no additional T3 query).  Default False preserves existing int behavior.
    """
    raw = _index_document(
        pdf_path, corpus, _pdf_chunks, t3=t3,
        collection_name=collection_name, embed_fn=embed_fn,
        force=force, return_metadata=return_metadata,
    )
    if not return_metadata:
        assert isinstance(raw, int)
        return raw
    if not isinstance(raw, list):
        return {"chunks": 0, "pages": [], "title": "", "author": ""}
    metadatas: list[dict] = raw
    return {
        "chunks": len(metadatas),
        "pages": sorted({m.get("page_number", 0) for m in metadatas}),
        "title": metadatas[0].get("source_title", "") if metadatas else "",
        "author": metadatas[0].get("source_author", "") if metadatas else "",
    }


def index_markdown(
    md_path: Path,
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    embed_fn: EmbedFn | None = None,
    force: bool = False,
    return_metadata: bool = False,
) -> int | dict:
    """Index *md_path* into a T3 collection.

    By default the collection is ``docs__{corpus}``.  Pass *collection_name*
    to override (e.g. ``rdr__<repo>-<hash8>`` for RDR documents).

    YAML frontmatter fields (title, author, date) are stored as metadata.
    Returns the number of chunks indexed, or 0 if skipped.

    Pass *embed_fn* to override the default Voyage AI embedding (e.g. a local
    ONNX function for dry-run mode).  When *embed_fn* is provided the Voyage
    credential check is bypassed.

    Pass *force=True* to bypass the staleness check and always re-index.

    When *return_metadata* is True, returns a dict instead of an int::

        {"chunks": int, "sections": int}

    *sections* is the count of chunks with a non-empty ``section_title``
    (i.e. produced under a heading).  Default False preserves existing int behavior.
    """
    raw = _index_document(
        md_path, corpus, _markdown_chunks, t3=t3,
        collection_name=collection_name, embed_fn=embed_fn,
        force=force, return_metadata=return_metadata,
    )
    if not return_metadata:
        assert isinstance(raw, int)
        return raw
    if not isinstance(raw, list):
        return {"chunks": 0, "sections": 0}
    metadatas: list[dict] = raw
    sections = sum(1 for m in metadatas if m.get("section_title", ""))
    return {"chunks": len(metadatas), "sections": sections}


def batch_index_pdfs(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    *,
    force: bool = False,
    on_file: Callable[[Path, int, float], None] | None = None,
) -> dict[str, str]:
    """Index multiple PDFs sequentially, returning per-file status.

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"skipped"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.

    Pass *force=True* to bypass the staleness check on every file.

    *on_file*, if provided, is called after each file as
    ``on_file(path, chunks, elapsed_s)`` where *chunks* is the number of
    chunks upserted (0 for skipped/failed) and *elapsed_s* is wall time.
    """
    results: dict[str, str] = {}
    for path in paths:
        count: int = 0
        t0 = time.monotonic()
        try:
            raw = index_pdf(path, corpus, t3=t3, force=force)
            count = raw if isinstance(raw, int) else 0
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:
            _log.warning("batch_index_pdfs: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
        if on_file:
            on_file(path, count, time.monotonic() - t0)
    return results


def batch_index_markdowns(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    *,
    collection_name: str | None = None,
    force: bool = False,
    on_file: Callable[[Path, int, float], None] | None = None,
) -> dict[str, str]:
    """Index multiple Markdown files sequentially, returning per-file status.

    Pass *collection_name* to override the default ``docs__{corpus}`` target
    (used for RDR collections).

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"skipped"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.

    Pass *force=True* to bypass the staleness check on every file.

    *on_file*, if provided, is called after each file as
    ``on_file(path, chunks, elapsed_s)`` where *chunks* is the number of
    chunks upserted (0 for skipped/failed) and *elapsed_s* is wall time.
    """
    results: dict[str, str] = {}
    for path in paths:
        count: int = 0
        t0 = time.monotonic()
        try:
            raw = index_markdown(path, corpus, t3=t3, collection_name=collection_name, force=force)
            count = raw if isinstance(raw, int) else 0
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:
            _log.warning("batch_index_markdowns: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
        if on_file:
            on_file(path, count, time.monotonic() - t0)
    return results
