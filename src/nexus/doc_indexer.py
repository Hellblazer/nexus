# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document indexing pipeline: PDF and Markdown → T3 docs__ collections."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from nexus.db import make_t3
from nexus.db.t3 import T3Database
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


def index_pdf(pdf_path: Path, corpus: str) -> int:
    """Index *pdf_path* into the T3 ``docs__{corpus}`` collection.

    Returns the number of chunks indexed, or 0 if skipped (no credentials or
    content unchanged since last index).
    """
    if not _has_credentials():
        return 0

    content_hash = _sha256(pdf_path)
    collection_name = f"docs__{corpus}"
    db = make_t3()
    col = db.get_or_create_collection(collection_name)

    # Incremental sync: skip if file is already indexed with the same hash
    existing = col.get(
        where={"source_path": str(pdf_path)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"] and existing["metadatas"][0].get("content_hash") == content_hash:
        return 0

    # Remove stale chunks for this file (different hash)
    stale = col.get(where={"source_path": str(pdf_path)}, include=[])
    if stale["ids"]:
        col.delete(ids=stale["ids"])

    # Extract → chunk → upsert
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
            "embedding_model": "voyage-4",
            "indexed_at": now_iso,
            "content_hash": content_hash,
        }
        ids.append(chunk_id)
        documents.append(chunk.text)
        metadatas.append(meta)

    db.upsert_chunks(collection=collection_name, ids=ids, documents=documents, metadatas=metadatas)
    return len(chunks)


def index_markdown(md_path: Path, corpus: str) -> int:
    """Index *md_path* into the T3 ``docs__{corpus}`` collection.

    YAML frontmatter fields (title, author, date) are stored as metadata.
    Returns the number of chunks indexed, or 0 if skipped.
    """
    if not _has_credentials():
        return 0

    content_hash = _sha256(md_path)
    collection_name = f"docs__{corpus}"
    db = make_t3()
    col = db.get_or_create_collection(collection_name)

    # Incremental sync
    existing = col.get(
        where={"source_path": str(md_path)},
        include=["metadatas"],
        limit=1,
    )
    if existing["metadatas"] and existing["metadatas"][0].get("content_hash") == content_hash:
        return 0

    stale = col.get(where={"source_path": str(md_path)}, include=[])
    if stale["ids"]:
        col.delete(ids=stale["ids"])

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
            "embedding_model": "voyage-4",
            "indexed_at": now_iso,
            "content_hash": content_hash,
        }
        ids.append(chunk_id)
        documents.append(chunk.text)
        metadatas.append(meta)

    db.upsert_chunks(collection=collection_name, ids=ids, documents=documents, metadatas=metadatas)
    return len(chunks)
