# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-108 Phase 2 (nexus-jc63): T3 chunk re-identification.

Re-upserts every T3 chunk under a content-derived natural ID
(``chunk_text_hash[:32]``), reusing the chunk's existing embedding so
the migration is free of Voyage calls. After all chunks for a
collection have been re-upserted, the old chunk IDs are batch-deleted
in groups of 300 (the ChromaDB write quota).

The loop is idempotent (re-running on a fully-migrated collection is
a zero-write no-op) and crash-resumable (a partial run can be
re-invoked safely; the filter naturally skips already-migrated chunks
and the un-deleted old IDs from the prior crash get re-collected on
resume).

Edge-case contracts:
  - ``taxonomy__*`` collections are skipped: centroids use
    ``centroid_hash`` from the ``topics`` table, not
    ``chunk_text_hash``. Detected by collection-name prefix.
  - Pre-RDR-053 chunks lacking ``chunk_text_hash``: FAIL LOUD with
    :class:`MissingChunkHashError` (per RDR-108 re-gate S3). Operator
    must re-index that collection or carve it out.
  - Quota compliance: paginates T3 at <=300 records per ``col.get()``
    call; ``col.delete()`` batches at <=300 ids per call.
  - Embedding reuse: ``col.upsert(..., embeddings=...)`` accepts a
    pre-computed vector and bypasses the embedding function (RF-2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from chromadb.errors import NotFoundError as _ChromaNotFoundError

from nexus.db.chroma_quotas import QUOTAS
from nexus.retry import _chroma_with_retry

if TYPE_CHECKING:
    from nexus.db.t3 import T3Database

_log = structlog.get_logger(__name__)

_TAXONOMY_PREFIX = "taxonomy__"
_PAGE_SIZE = QUOTAS.MAX_RECORDS_PER_WRITE  # 300
_STRIPPED_META_FIELDS = frozenset({"doc_id", "chunk_index", "chunk_count"})


class MissingChunkHashError(ValueError):
    """Raised when a T3 chunk lacks ``chunk_text_hash``.

    Phase 2 cannot derive a natural ID without this field. Per the
    RDR-108 re-gate S3 finding, pre-RDR-053 chunks must FAIL LOUD so
    the operator knows to re-index the collection rather than silently
    losing data through a degraded fallback.
    """

    def __init__(self, chunk_id: str, collection: str) -> None:
        self.chunk_id = chunk_id
        self.collection = collection
        super().__init__(
            f"Chunk {chunk_id!r} in collection {collection!r} has no "
            f"chunk_text_hash. Re-index this collection from source "
            f"before running 'nx t3 reidentify', or explicitly carve "
            f"it out."
        )


@dataclass
class ReidentifyResult:
    """Summary of one re-identification run over a single collection."""

    collection: str
    chunks_examined: int = 0
    chunks_migrated: int = 0
    chunks_already_migrated: int = 0
    chunks_deleted: int = 0
    skipped_taxonomy: bool = False


def reidentify_collection(
    t3: "T3Database",
    collection_name: str,
    *,
    dry_run: bool = True,
) -> ReidentifyResult:
    """Re-upsert every chunk in ``collection_name`` under chunk_text_hash[:32].

    Per-collection paginated loop following RDR-108 RF-6:

      1. ``col.get(limit=300, offset=N, include=[documents, embeddings, metadatas])``
      2. For each chunk, compute ``new_id = meta["chunk_text_hash"][:32]``.
         Skip silently if ``cid == new_id`` (already migrated).
      3. Strip ``doc_id`` / ``chunk_index`` / ``chunk_count`` from metadata
         (the catalog manifest is now authoritative for those fields).
      4. ``col.upsert(ids=new, documents=..., embeddings=..., metadatas=...)``
         — pre-computed embeddings bypass the embedding function (no Voyage
         call).
      5. After the get-loop completes, batch-delete the collected old IDs
         in groups of 300.

    Args:
        t3: T3Database for ChromaDB access.
        collection_name: Name of the T3 collection to re-identify.
        dry_run: If True, count what would change but do not write or delete.

    Returns:
        :class:`ReidentifyResult` with counts.

    Raises:
        :class:`MissingChunkHashError`: if any chunk lacks ``chunk_text_hash``.
    """
    result = ReidentifyResult(collection=collection_name)

    if collection_name.startswith(_TAXONOMY_PREFIX):
        _log.info(
            "reidentify_skipped_taxonomy",
            collection=collection_name,
        )
        result.skipped_taxonomy = True
        return result

    try:
        col = t3._client_for(collection_name).get_collection(collection_name)
    except _ChromaNotFoundError:
        _log.info(
            "reidentify_collection_absent",
            collection=collection_name,
        )
        return result

    seen_old_ids: set[str] = set()
    offset = 0
    while True:
        page = _chroma_with_retry(
            col.get,
            limit=_PAGE_SIZE,
            offset=offset,
            include=["documents", "embeddings", "metadatas"],
        )
        page_ids: list[str] = page.get("ids") or []
        if not page_ids:
            break

        page_docs = page.get("documents") or [None] * len(page_ids)
        page_embs = page.get("embeddings")
        if page_embs is None:
            page_embs = [None] * len(page_ids)
        page_metas = page.get("metadatas") or [{}] * len(page_ids)

        ids_to_upsert: list[str] = []
        docs_to_upsert: list[str] = []
        embs_to_upsert: list = []
        metas_to_upsert: list[dict] = []
        # Dedupe within-page identical-text collisions: chromadb.upsert
        # rejects duplicate IDs within a single call. Two chunks in the
        # same page sharing chunk_text_hash[:32] mean identical text, so
        # the second upsert would carry the same content anyway.
        seen_new_ids_in_page: set[str] = set()

        for cid, doc, emb, meta in zip(
            page_ids, page_docs, page_embs, page_metas
        ):
            result.chunks_examined += 1
            meta = meta if isinstance(meta, dict) else {}
            chash = meta.get("chunk_text_hash") or ""
            if not chash:
                raise MissingChunkHashError(
                    chunk_id=cid, collection=collection_name
                )
            new_id = chash[:32]
            if cid == new_id:
                result.chunks_already_migrated += 1
                continue

            seen_old_ids.add(cid)
            if new_id in seen_new_ids_in_page:
                # Identical-text collapse: still need to delete the old
                # cid, but skip the redundant upsert.
                continue
            seen_new_ids_in_page.add(new_id)

            new_meta = {
                k: v for k, v in meta.items()
                if k not in _STRIPPED_META_FIELDS
            }
            ids_to_upsert.append(new_id)
            docs_to_upsert.append(doc)
            embs_to_upsert.append(emb)
            metas_to_upsert.append(new_meta)

        if ids_to_upsert and not dry_run:
            _chroma_with_retry(
                col.upsert,
                ids=ids_to_upsert,
                documents=docs_to_upsert,
                embeddings=embs_to_upsert,
                metadatas=metas_to_upsert,
            )

        result.chunks_migrated += len(ids_to_upsert)

        if len(page_ids) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    if not dry_run and seen_old_ids:
        old_ids_list = list(seen_old_ids)
        for start in range(0, len(old_ids_list), _PAGE_SIZE):
            batch = old_ids_list[start : start + _PAGE_SIZE]
            _chroma_with_retry(col.delete, ids=batch)
            result.chunks_deleted += len(batch)

    _log.info(
        "reidentify_collection_complete",
        collection=collection_name,
        chunks_examined=result.chunks_examined,
        chunks_migrated=result.chunks_migrated,
        chunks_already_migrated=result.chunks_already_migrated,
        chunks_deleted=result.chunks_deleted,
        dry_run=dry_run,
    )

    return result
