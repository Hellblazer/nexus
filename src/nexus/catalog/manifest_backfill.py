# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-108 Phase 1b (nexus-j43k): document_chunks manifest backfill.

Reads existing T3 chunk metadata for a collection and writes one row
per (doc_id, chunk_index) into the ``document_chunks`` manifest table.
After this runs, the catalog can answer "what chunks compose this Document
and in what order?" without consulting T3 chunk metadata.

The backfill is idempotent: re-running overwrites the manifest with the
same content. A crash mid-run leaves partial manifest data; re-running
resolves it.

Edge-case contracts:
  - Zero-chunk doc: produces an empty manifest row-set. Valid, not an error.
  - taxonomy__* carve-out: skipped. Centroids use ``centroid_hash`` from
    ``topics``, not ``chunk_text_hash``. Detected by collection-name prefix.
  - Pre-RDR-053 chunks lacking ``chunk_text_hash``: FAIL LOUD with
    ``MissingChunkHashError`` (per re-gate S3 finding). Operator must
    re-index that collection or carve it out.
  - Quota compliance: paginates T3 at <=300 records per ``col.get()``
    call; ``INSERT INTO document_chunks`` batches <=300 per write.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from nexus.db.chroma_quotas import QUOTAS

if TYPE_CHECKING:
    from nexus.catalog.catalog import Catalog
    from nexus.db.t3 import T3Database

_log = structlog.get_logger(__name__)

_TAXONOMY_PREFIX = "taxonomy__"
_PAGE_SIZE = QUOTAS.MAX_RECORDS_PER_WRITE  # 300


class MissingChunkHashError(ValueError):
    """Raised when a T3 chunk is missing ``chunk_text_hash``.

    Per the RDR-108 re-gate S3 finding, pre-RDR-053 chunks that lack
    ``chunk_text_hash`` must FAIL LOUD so the operator knows to re-index
    the collection rather than silently skipping data.
    """

    def __init__(self, chunk_id: str, collection: str) -> None:
        self.chunk_id = chunk_id
        self.collection = collection
        super().__init__(
            f"Chunk {chunk_id!r} in collection {collection!r} has no "
            f"chunk_text_hash. Re-index this collection before running "
            f"backfill, or explicitly carve it out."
        )


@dataclass
class BackfillResult:
    """Summary of one backfill run over a single collection."""

    collection: str
    docs_processed: int = 0
    chunks_written: int = 0
    docs_skipped_no_t3: int = 0
    skipped_taxonomy: bool = False


def _iter_chunks_for_doc(
    col: "chromadb.Collection",
    doc_id: str,
    collection: str,
) -> list[dict]:
    """Paginate T3 and collect chunk metadata for one doc_id.

    Returns a list of chunk dicts with keys:
      chash, position, line_start, line_end, char_start, char_end

    Raises MissingChunkHashError if any chunk lacks chunk_text_hash.
    """
    from chromadb import Collection  # noqa: PLC0415 — defer heavy import

    chunks: list[dict] = []
    offset = 0
    while True:
        result = col.get(
            where={"doc_id": doc_id},
            limit=_PAGE_SIZE,
            offset=offset,
            include=["metadatas"],
        )
        page_ids: list[str] = result.get("ids") or []
        page_metas: list[dict] = result.get("metadatas") or []
        if not page_ids:
            break
        for cid, meta in zip(page_ids, page_metas):
            if not isinstance(meta, dict):
                meta = {}
            chash = meta.get("chunk_text_hash") or ""
            if not chash:
                raise MissingChunkHashError(chunk_id=cid, collection=collection)
            chunk_index = int(meta.get("chunk_index", 0) or 0)
            chunks.append({
                "chash": chash,
                "position": chunk_index,
                "line_start": meta.get("line_start"),
                "line_end": meta.get("line_end"),
                "char_start": meta.get("chunk_start_char"),
                "char_end": meta.get("chunk_end_char"),
            })
        if len(page_ids) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return chunks


def backfill_manifest_for_collection(
    catalog: "Catalog",
    t3: "T3Database",
    collection_name: str,
    *,
    dry_run: bool = True,
    limit: int = 0,
) -> BackfillResult:
    """Backfill the document_chunks manifest for one T3 collection.

    Iterates catalog documents whose ``physical_collection`` matches
    ``collection_name``, reads T3 chunk metadata per doc_id (paginating
    at <=300), then calls ``catalog.write_manifest(doc_id, chunks)``
    for each document.

    Args:
        catalog: The Catalog instance (SQLite + JSONL).
        t3: T3Database instance for ChromaDB access.
        collection_name: Name of the T3 collection to backfill.
        dry_run: If True, compute but do not write manifest rows.
        limit: If > 0, process at most this many documents.

    Returns:
        BackfillResult with counts.

    Raises:
        MissingChunkHashError: if any chunk lacks ``chunk_text_hash``.
    """
    result = BackfillResult(collection=collection_name)

    # taxonomy__* carve-out: centroids use centroid_hash, not chunk_text_hash.
    if collection_name.startswith(_TAXONOMY_PREFIX):
        _log.info(
            "manifest_backfill_skipped_taxonomy",
            collection=collection_name,
        )
        result.skipped_taxonomy = True
        return result

    # Fetch the T3 collection handle (returns without error if not found).
    try:
        col = t3._client_for(collection_name).get_collection(collection_name)
    except Exception:
        # Collection doesn't exist in T3; treat as zero-chunk for all docs.
        col = None

    # Get docs from catalog for this collection.
    docs = catalog.list_by_collection(collection_name)
    if limit > 0:
        docs = docs[:limit]

    for doc in docs:
        doc_id = str(doc.tumbler)
        if col is None:
            chunks: list[dict] = []
        else:
            chunks = _iter_chunks_for_doc(col, doc_id, collection_name)

        chunks.sort(key=lambda c: c["position"])

        if not dry_run:
            catalog.write_manifest(doc_id, chunks)
            result.chunks_written += len(chunks)

        result.docs_processed += 1

        _log.debug(
            "manifest_backfill_doc",
            collection=collection_name,
            doc_id=doc_id,
            chunks=len(chunks),
            dry_run=dry_run,
        )

    return result
