# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document indexing pipeline: PDF and Markdown → T3 docs__ collections."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from nexus.corpus import index_model_for_collection
from nexus.db import make_t3
from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import PDFExtractor


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def _has_credentials() -> bool:
    from nexus.config import get_credential
    return bool(get_credential("voyage_api_key") and get_credential("chroma_api_key"))


def _estimate_tokens(chunks: list[str]) -> int:
    """Rough token estimate: 4 characters per token."""
    return sum(len(c) for c in chunks) // 4


def _embed_with_fallback(
    chunks: list[str],
    model: str,
    api_key: str,
    input_type: str = "document",
) -> list[list[float]]:
    """Embed chunks, falling back to voyage-4 if CCE fails."""
    import voyageai
    if model == "voyage-context-3" and len(chunks) >= 2:
        estimated = _estimate_tokens(chunks)
        if estimated > 100_000:
            # Too large for CCE — fall back to standard
            model = "voyage-4"
        else:
            try:
                client = voyageai.Client(api_key=api_key)
                result = client.contextualized_embed(
                    inputs=[chunks], model=model, input_type=input_type
                )
                return result.embeddings[0]  # first (only) document's embeddings
            except Exception:
                model = "voyage-4"  # fall back to standard
    # Standard embedding path
    client = voyageai.Client(api_key=api_key)
    result = client.embed(texts=chunks, model=model, input_type=input_type)
    return result.embeddings


def index_pdf(pdf_path: Path, corpus: str, t3: Any = None) -> int:
    """Index *pdf_path* into the T3 ``docs__{corpus}`` collection.

    Returns the number of chunks indexed, or 0 if skipped (no credentials or
    content unchanged since last index with the same embedding model).
    """
    if not _has_credentials():
        return 0

    content_hash = _sha256(pdf_path)
    collection_name = f"docs__{corpus}"
    db = t3 if t3 is not None else make_t3()
    col = db.get_or_create_collection(collection_name)

    target_model = index_model_for_collection(collection_name)

    # Incremental sync: skip if file is already indexed with the same hash AND model
    existing = col.get(
        where={"source_path": str(pdf_path)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        if stored_hash == content_hash and stored_model == target_model:
            return 0

    # Extract → chunk → upsert (idempotent: chunk IDs are deterministic)
    result = PDFExtractor().extract(pdf_path)
    chunks = PDFChunker().chunk(result.text, result.metadata)
    if not chunks:
        return 0

    now_iso = datetime.now(UTC).isoformat()
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

    for chunk in chunks:
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta: dict = {
            "source_path": str(pdf_path),
            "source_title": "",
            "source_author": "",
            "source_date": "",
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
        }
        ids.append(chunk_id)
        documents.append(chunk.text)
        metadatas.append(meta)

    if target_model == "voyage-context-3":
        from nexus.config import get_credential
        voyage_key = get_credential("voyage_api_key")
        embeddings = _embed_with_fallback(documents, target_model, voyage_key)
        db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)
    else:
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)

    # Prune stale chunks from a previous (larger) version of this file
    current_ids_set = set(ids)
    all_existing = col.get(where={"source_path": str(pdf_path)}, include=[])
    stale_ids = [eid for eid in all_existing["ids"] if eid not in current_ids_set]
    if stale_ids:
        col.delete(ids=stale_ids)

    return len(chunks)


def index_markdown(md_path: Path, corpus: str, t3: Any = None) -> int:
    """Index *md_path* into the T3 ``docs__{corpus}`` collection.

    YAML frontmatter fields (title, author, date) are stored as metadata.
    Returns the number of chunks indexed, or 0 if skipped.
    """
    if not _has_credentials():
        return 0

    content_hash = _sha256(md_path)
    collection_name = f"docs__{corpus}"
    db = t3 if t3 is not None else make_t3()
    col = db.get_or_create_collection(collection_name)

    target_model = index_model_for_collection(collection_name)

    # Incremental sync: skip if file is already indexed with same hash AND model
    existing = col.get(
        where={"source_path": str(md_path)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        if stored_hash == content_hash and stored_model == target_model:
            return 0

    # Parse frontmatter then chunk
    raw_text = md_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw_text)
    # Offset added to chunk char positions so they reference the original file,
    # not the body-only text (frontmatter is stripped before chunking).
    frontmatter_len = len(raw_text) - len(body)

    base_meta: dict = {
        "source_path": str(md_path),
        "corpus": corpus,
    }
    chunks = SemanticMarkdownChunker().chunk(body, base_meta)
    if not chunks:
        return 0

    now_iso = datetime.now(UTC).isoformat()
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict] = []

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
        ids.append(chunk_id)
        documents.append(chunk.text)
        metadatas.append(meta)

    if target_model == "voyage-context-3":
        from nexus.config import get_credential
        voyage_key = get_credential("voyage_api_key")
        embeddings = _embed_with_fallback(documents, target_model, voyage_key)
        db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)
    else:
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)

    # Prune stale chunks from a previous (larger) version of this file
    current_ids_set = set(ids)
    all_existing = col.get(where={"source_path": str(md_path)}, include=[])
    stale_ids = [eid for eid in all_existing["ids"] if eid not in current_ids_set]
    if stale_ids:
        col.delete(ids=stale_ids)

    return len(chunks)


def batch_index_pdfs(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    batch_size: int = 4,
) -> dict[str, str]:
    """Index multiple PDFs, batching CCE calls for efficiency.

    Returns dict mapping path -> status ("indexed", "skipped", "failed").
    """
    _log = structlog.get_logger()
    results: dict[str, str] = {}
    for path in paths:
        try:
            index_pdf(path, corpus, t3=t3)
            results[str(path)] = "indexed"
        except Exception as e:
            _log.warning("batch_index_pdfs: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
    return results


def batch_index_markdowns(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
    batch_size: int = 4,
) -> dict[str, str]:
    """Index multiple Markdown files, batching CCE calls for efficiency.

    Returns dict mapping path -> status ("indexed", "skipped", "failed").
    """
    _log = structlog.get_logger()
    results: dict[str, str] = {}
    for path in paths:
        try:
            index_markdown(path, corpus, t3=t3)
            results[str(path)] = "indexed"
        except Exception as e:
            _log.warning("batch_index_markdowns: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
    return results
