# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document indexing pipeline: PDF and Markdown → T3 docs__ collections."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import structlog

_log = structlog.get_logger(__name__)

from nexus.corpus import index_model_for_collection
from nexus.db import make_t3
from nexus.md_chunker import SemanticMarkdownChunker, parse_frontmatter
from nexus.pdf_chunker import PDFChunker
from nexus.pdf_extractor import PDFExtractor

# Type alias for the chunking callback used by _index_document.
# Receives (file_path, content_hash, target_model, now_iso) and returns a list
# of (chunk_id, document_text, metadata_dict) tuples, or an empty list to skip.
ChunkFn = Callable[[Path, str, str, str], list[tuple[str, str, dict]]]


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
    """Rough token estimate: 3 characters per token (conservative; code skews short)."""
    return sum(len(c) for c in chunks) // 3


def _embed_with_fallback(
    chunks: list[str],
    model: str,
    api_key: str,
    input_type: str = "document",
) -> tuple[list[list[float]], str]:
    """Embed chunks using CCE when possible, falling back to voyage-4 on failure.

    Returns ``(embeddings, actual_model_used)`` so callers can record the model
    that produced the stored vectors in metadata — critical for staleness checks.
    """
    import voyageai
    client = voyageai.Client(api_key=api_key)
    if model == "voyage-context-3":
        if len(chunks) < 2:
            # CCE requires 2+ chunks; fall back to voyage-4 (the query-time model)
            # so stored vectors are in the same embedding space as query vectors.
            model = "voyage-4"
        else:
            estimated = _estimate_tokens(chunks)
            if estimated > 100_000:
                _log.warning("CCE skipped: estimated tokens exceed limit",
                             estimated=estimated, limit=100_000)
                model = "voyage-4"
            else:
                try:
                    result = client.contextualized_embed(
                        inputs=[chunks], model=model, input_type=input_type
                    )
                    return result.embeddings[0], model  # first (only) document's embeddings
                except Exception as exc:
                    _log.warning("CCE failed, falling back to voyage-4", error=str(exc))
                    model = "voyage-4"
    # Standard embedding path (voyage-4 or any non-CCE model)
    result = client.embed(texts=chunks, model=model, input_type=input_type)
    return result.embeddings, model


def _index_document(
    file_path: Path,
    corpus: str,
    chunk_fn: ChunkFn,
    t3: Any = None,
) -> int:
    """Shared indexing pipeline: credential check, staleness, embed, upsert, prune.

    *chunk_fn(file_path, content_hash, target_model, now_iso)* produces the
    per-format (chunk_id, document_text, metadata_dict) tuples.  Returns the
    number of chunks indexed, or 0 if skipped.
    """
    if not _has_credentials():
        return 0

    content_hash = _sha256(file_path)
    collection_name = f"docs__{corpus}"
    db = t3 if t3 is not None else make_t3()
    col = db.get_or_create_collection(collection_name)

    target_model = index_model_for_collection(collection_name)

    # Incremental sync: skip if file is already indexed with the same hash AND model
    existing = col.get(
        where={"source_path": str(file_path)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"]:
        stored_hash = existing["metadatas"][0].get("content_hash", "")
        stored_model = existing["metadatas"][0].get("embedding_model", "")
        if stored_hash == content_hash and stored_model == target_model:
            return 0

    now_iso = datetime.now(UTC).isoformat()
    prepared = chunk_fn(file_path, content_hash, target_model, now_iso)
    if not prepared:
        return 0

    ids = [p[0] for p in prepared]
    documents = [p[1] for p in prepared]
    metadatas = [p[2] for p in prepared]

    from nexus.config import get_credential
    voyage_key = get_credential("voyage_api_key")
    if not voyage_key:
        raise RuntimeError("voyage_api_key must be set — unreachable if _has_credentials() passed")
    embeddings, actual_model = _embed_with_fallback(documents, target_model, voyage_key)
    if actual_model != target_model:
        for m in metadatas:
            m["embedding_model"] = actual_model
    db.upsert_chunks_with_embeddings(collection_name, ids, documents, embeddings, metadatas)

    # Prune stale chunks from a previous (larger) version of this file
    current_ids_set = set(ids)
    all_existing = col.get(where={"source_path": str(file_path)}, include=[])
    stale_ids = [eid for eid in all_existing["ids"] if eid not in current_ids_set]
    if stale_ids:
        col.delete(ids=stale_ids)

    return len(prepared)


def _pdf_chunks(
    pdf_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
) -> list[tuple[str, str, dict]]:
    """Chunk a PDF and return (id, text, metadata) tuples."""
    result = PDFExtractor().extract(pdf_path)
    chunks = PDFChunker().chunk(result.text, result.metadata)
    if not chunks:
        return []

    prepared: list[tuple[str, str, dict]] = []
    for chunk in chunks:
        chunk_id = f"{content_hash[:16]}_{chunk.chunk_index}"
        meta: dict = {
            "source_path": str(pdf_path),
            "source_title": "",
            "source_author": "",
            "source_date": "",
            "corpus": "",  # filled by caller context via collection_name
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
        prepared.append((chunk_id, chunk.text, meta))
    return prepared


def _markdown_chunks(
    md_path: Path,
    content_hash: str,
    target_model: str,
    now_iso: str,
) -> list[tuple[str, str, dict]]:
    """Chunk a Markdown file and return (id, text, metadata) tuples."""
    raw_text = md_path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(raw_text)
    frontmatter_len = len(raw_text) - len(body)

    base_meta: dict = {
        "source_path": str(md_path),
        "corpus": "",  # filled by caller context via collection_name
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
            "corpus": "",  # filled by caller context via collection_name
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


def index_pdf(pdf_path: Path, corpus: str, t3: Any = None) -> int:
    """Index *pdf_path* into the T3 ``docs__{corpus}`` collection.

    Returns the number of chunks indexed, or 0 if skipped (no credentials or
    content unchanged since last index with the same embedding model).
    """
    def _chunk_fn(
        path: Path, content_hash: str, target_model: str, now_iso: str
    ) -> list[tuple[str, str, dict]]:
        prepared = _pdf_chunks(path, content_hash, target_model, now_iso)
        for _, _, meta in prepared:
            meta["corpus"] = corpus
        return prepared

    return _index_document(pdf_path, corpus, _chunk_fn, t3=t3)


def index_markdown(md_path: Path, corpus: str, t3: Any = None) -> int:
    """Index *md_path* into the T3 ``docs__{corpus}`` collection.

    YAML frontmatter fields (title, author, date) are stored as metadata.
    Returns the number of chunks indexed, or 0 if skipped.
    """
    def _chunk_fn(
        path: Path, content_hash: str, target_model: str, now_iso: str
    ) -> list[tuple[str, str, dict]]:
        prepared = _markdown_chunks(path, content_hash, target_model, now_iso)
        for _, _, meta in prepared:
            meta["corpus"] = corpus
        return prepared

    return _index_document(md_path, corpus, _chunk_fn, t3=t3)


def batch_index_pdfs(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
) -> dict[str, str]:
    """Index multiple PDFs sequentially, returning per-file status.

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.
    """
    results: dict[str, str] = {}
    for path in paths:
        try:
            count = index_pdf(path, corpus, t3=t3)
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:
            _log.warning("batch_index_pdfs: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
    return results


def batch_index_markdowns(
    paths: list[Path],
    corpus: str,
    t3: Any = None,
) -> dict[str, str]:
    """Index multiple Markdown files sequentially, returning per-file status.

    Returns dict mapping ``str(path)`` -> ``"indexed"`` | ``"failed"``.
    Failures are logged and do not abort the remaining paths.
    """
    results: dict[str, str] = {}
    for path in paths:
        try:
            count = index_markdown(path, corpus, t3=t3)
            results[str(path)] = "indexed" if count else "skipped"
        except Exception as e:
            _log.warning("batch_index_markdowns: failed", path=str(path), error=str(e))
            results[str(path)] = "failed"
    return results
