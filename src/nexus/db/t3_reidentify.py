# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-108 Phase 2 (nexus-jc63): T3 chunk re-identification.

Re-upserts every T3 chunk under a content-derived natural ID
(``chunk_text_hash[:32]``), reusing the chunk's existing embedding so
the migration is free of Voyage calls. After all chunks for a
collection have been re-upserted, the old chunk IDs are batch-deleted
in groups of 300 (the ChromaDB write quota).

The implementation uses a two-pass design (divergence from the RDR-108
RF-6 pseudocode, which prescribed a single loop interleaving
``col.get(offset)`` and ``col.upsert``). Pass 1 paginates by offset to
collect every original chunk id with NO writes; pass 2 processes the
collected ids in batches via exact-id ``col.get(ids=batch)`` lookups,
which are immune to in-loop collection mutation. The naive single-pass
form is unsafe because mid-loop upserts add records that shift
offset-pagination semantics, causing later pages to either re-visit or
skip chunks (see ``test_multi_page_iteration_migrates_all_chunks``).

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

from concurrent.futures import Future, ThreadPoolExecutor
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

    Two-pass per-collection loop:

      Pass 1 (id discovery): paginate ``col.get(limit=300, offset=N, include=[])``
      until exhaustion, collecting every original chunk id. No writes.

      Pass 2 (process): for each batch of <=300 collected ids,
        a. ``col.get(ids=batch, include=[documents, embeddings, metadatas])``
        b. Compute ``new_id = meta["chunk_text_hash"][:32]`` per chunk.
           Skip silently if ``cid == new_id`` (already migrated).
        c. Strip ``doc_id`` / ``chunk_index`` / ``chunk_count`` from
           metadata (catalog manifest is authoritative for those).
        d. ``col.upsert(ids=new, documents=..., embeddings=..., metadatas=...)``
           — pre-computed embeddings bypass the embedding function
           (no Voyage call).

      Phase 2b (cleanup): batch-delete the collected old ids in groups
      of 300.

    The two-pass form sidesteps the offset-instability that plagues the
    naive single-pass loop (in-loop upserts add records, shifting offset
    semantics for later pages). Exact-id lookups in pass 2 are immune to
    collection mutation.

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

    # Two-pass design:
    #
    #   Pass 1: paginate by offset, collecting only the cid list. No writes
    #           happen during pass 1, so offset semantics stay valid.
    #
    #   Pass 2: process in batches of <=_PAGE_SIZE, fetching each batch via
    #           col.get(ids=batch) (exact-id lookup, not offset-based). New
    #           upserts during pass 2 don't disturb iteration because we
    #           already know the full original cid set.
    #
    # The naive "paginate-and-upsert in one loop" pattern from RDR-108 RF-6
    # is unsafe: in-loop upserts add records to the collection, shifting
    # offset semantics so later pages either re-visit migrated chunks or
    # skip un-migrated ones. The two-pass form is correctness-preserving
    # and equally idempotent / resumable.
    all_cids: list[str] = []
    offset = 0
    while True:
        page = _chroma_with_retry(
            col.get, limit=_PAGE_SIZE, offset=offset, include=[]
        )
        page_ids: list[str] = page.get("ids") or []
        if not page_ids:
            break
        all_cids.extend(page_ids)
        if len(page_ids) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE

    seen_old_ids: set[str] = set()
    seen_new_ids: set[str] = set()

    # nexus-zpnq: pipeline pass-2 fetches one batch ahead of the
    # processing+upsert of the prior batch. ChromaDB allows up to 10
    # concurrent reads + 10 concurrent writes per collection
    # (chroma_quotas.MAX_CONCURRENT_READS/WRITES); a 1-batch
    # look-ahead is well within budget. Order is preserved: the main
    # thread still processes batches in cid-list order, and the
    # ``seen_old_ids`` / ``seen_new_ids`` dedupe sets are only ever
    # touched from the main thread, so the per-collection state
    # machine stays correct under the overlap. Theoretical speedup:
    # up to 2x for I/O-bound collections; in practice modest because
    # processing time per batch is non-zero.
    cid_batches = [
        all_cids[i : i + _PAGE_SIZE]
        for i in range(0, len(all_cids), _PAGE_SIZE)
    ]

    def _fetch(batch: list[str]) -> dict:
        return _chroma_with_retry(
            col.get,
            ids=batch,
            include=["documents", "embeddings", "metadatas"],
        )

    pending_future: Future[dict] | None = None
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="reidentify-fetch",
    ) as executor:
        if cid_batches:
            pending_future = executor.submit(_fetch, cid_batches[0])

        for i, cid_batch in enumerate(cid_batches):
            assert pending_future is not None
            page = pending_future.result()
            # Kick off the NEXT fetch before processing this batch so
            # the network round-trip overlaps with the upsert below.
            if i + 1 < len(cid_batches):
                pending_future = executor.submit(
                    _fetch, cid_batches[i + 1],
                )
            else:
                pending_future = None
            page_ids = page.get("ids") or []
            page_docs = page.get("documents") or [None] * len(page_ids)
            page_embs = page.get("embeddings")
            if page_embs is None:
                page_embs = [None] * len(page_ids)
            page_metas = page.get("metadatas") or [{}] * len(page_ids)

            ids_to_upsert: list[str] = []
            docs_to_upsert: list[str] = []
            embs_to_upsert: list = []
            metas_to_upsert: list[dict] = []

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
                if new_id in seen_new_ids:
                    # Identical-text collapse (within-batch or cross-batch):
                    # still need to delete the old cid, but skip the
                    # redundant upsert so chromadb doesn't reject a duplicate.
                    continue
                seen_new_ids.add(new_id)

                # Use the canonical schema funnel (RDR-108 nexus-6l9p)
                # rather than a narrow strip set. ``_normalize_for_write``
                # drops the 3 RDR-108 Phase 3 fields (doc_id, chunk_index,
                # chunk_count) plus all pre-RDR-101-Phase-5c cargo (corpus,
                # store_type, expires_at, extraction_method, etc) and
                # bib_* placeholders. Surfaced during the Phase 5 prod
                # migration: 2 of 153 collections held legacy chunks with
                # 33+ metadata keys; the prior narrow-strip path produced
                # NumMetadataKeys quota errors on the upsert. The canonical
                # funnel brings these chunks back under the per-row quota.
                from nexus.db.t3 import _normalize_for_write
                new_meta = _normalize_for_write(meta, collection_name)
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
