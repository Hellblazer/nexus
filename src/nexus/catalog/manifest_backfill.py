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
  - Zero-chunk-match doc: SKIPPED, counted in
    ``BackfillResult.docs_skipped_zero_chunks``, and logged as a structured
    warning naming the doc and the lookup keys tried. NEVER written as an
    empty manifest (nexus-gvmbo). ``write_manifest``/``atomic_manifest_replace``
    is an atomic DELETE+INSERT server-side (``CatalogHandler.java``); writing
    ``[]`` for a doc whose chunks simply didn't match the lookup key(s) would
    DESTROY any existing manifest for that doc. Backfill cannot distinguish
    "this doc genuinely has 0 T3 chunks" from "the lookup missed them", so it
    always skips rather than ever writing empty — a genuinely empty doc's
    manifest is created by whichever path first CREATES the doc, not backfill.
  - Chunk lookup key: matched by EITHER metadata ``doc_id`` (indexer-origin
    chunks: ``indexer.py`` stamps the tumbler under ``doc_id``) OR metadata
    ``catalog_doc_id`` (store_put-origin chunks: ``http_vector_client.py``
    stamps the tumbler under ``catalog_doc_id`` and reserves ``doc_id`` for
    the chash) (nexus-b91tv). The engine's ``where`` grammar has no ``$or``
    (``PgVectorRepository.appendWherePredicate`` fails loud on a
    ``$``-prefixed key), so both keys are queried separately and merged,
    deduped by chash.
  - taxonomy__* carve-out: skipped. Centroids use ``centroid_hash`` from
    ``topics``, not ``chunk_text_hash``. Detected by collection-name prefix.
  - Pre-RDR-053 chunks lacking ``chunk_text_hash``: FAIL LOUD with
    ``MissingChunkHashError`` (per re-gate S3 finding). Operator must
    re-index that collection or carve it out.
  - Quota compliance: paginates T3 at <=300 records per ``col.get()``
    call; ``INSERT INTO document_chunks`` batches <=300 per write.
  - Successful non-dry-run writes resync ``chunk_count`` via
    ``catalog.resync_chunk_count_cache`` (mirrors ``manifest_heal.py``'s
    ``atomic_manifest_replace`` + resync pairing) — otherwise the row stays
    at whatever stale ``chunk_count`` it had (often 0) and every gap
    detector re-flags the doc forever.
  - ``only_gapped=True`` (nexus-3n7pr G1): a repair pass over a large,
    mostly-healthy collection must touch ONLY documents that currently have
    ZERO manifest rows. Without this, ``backfill_manifest_for_collection``
    processes every document ``list_by_collection`` returns and, for each,
    calls the atomic DELETE+INSERT ``write_manifest`` — rewriting every
    healthy manifest in the collection for the sake of repairing a small
    damaged subset. The pre-pass batches ONE ``catalog.get_manifests(...)``
    call over the whole collection's doc_ids (paginated server-side, not a
    per-doc round trip) and skips any doc already present in the result,
    counted in ``BackfillResult.docs_skipped_has_manifest``. The filter is
    applied BEFORE the T3 lookup/write for each doc, and before the
    ``dry_run`` branch, so a dry run reports the same partition a real run
    would touch. Default is ``False`` — unset, behavior is unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from nexus.errors import collection_not_found_errors

from nexus.db.limits import QUOTAS

if TYPE_CHECKING:
    from nexus.catalog.catalog_protocol import CatalogReader
    from nexus.db.http_vector_client import HttpVectorClient, _ServiceCollectionStub
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


class Phase3ChunkIndexMissingError(ValueError):
    """Raised when a multi-chunk doc's T3 chunks lack ``chunk_index``.

    nexus-w5zv: post-RDR-108 Phase 3 chunks dropped ``chunk_index`` from
    metadata. Pre-fix, the backfill read ``meta.get("chunk_index", 0)``
    and every chunk landed at position=0; ``ON CONFLICT(doc_id, position)``
    in the manifest UPSERT kept only one row. The resulting manifest was
    silently wrong (a single chunk where the doc had many).

    Backfill cannot determine canonical position for a Phase-3 corpus
    without re-running the indexer; the only safe action is to fail loud
    and force a re-index. Single-chunk docs (no ordering ambiguity)
    bypass this guard.
    """

    def __init__(self, doc_id: str, collection: str, chunk_count: int) -> None:
        self.doc_id = doc_id
        self.collection = collection
        self.chunk_count = chunk_count
        super().__init__(
            f"Document {doc_id!r} in collection {collection!r} has "
            f"{chunk_count} chunks but none carry chunk_index metadata "
            f"(post-RDR-108 Phase 3 shape). Backfill cannot reconstruct "
            f"canonical chunk order from this state; re-index the "
            f"collection so the indexer writes the manifest at write "
            f"time, or carve out this doc."
        )


@dataclass
class BackfillResult:
    """Summary of one backfill run over a single collection."""

    collection: str
    docs_processed: int = 0
    chunks_written: int = 0
    docs_skipped_no_t3: int = 0
    # nexus-w5zv: count Phase-3 docs that couldn't be backfilled because
    # chunk_index is missing from metadata (multi-chunk only). Operator
    # action: re-index the affected collection.
    docs_skipped_phase3_no_index: int = 0
    # nexus-gvmbo: count docs whose chunk lookup (both doc_id AND
    # catalog_doc_id keys) matched zero T3 chunks. Never written as an
    # empty manifest -- see the module docstring's "Zero-chunk-match doc"
    # contract. Non-vacuity: a caller pointing backfill at a collection
    # with key-mismatched chunks must SEE this count rise, not a silent 0.
    docs_skipped_zero_chunks: int = 0
    # nexus-3n7pr G1: count docs skipped by --only-gapped because they
    # already have >=1 manifest row. Non-vacuity: a caller pointing
    # --only-gapped at a fully-healthy collection must SEE this count equal
    # docs_processed's would-be value, not a silent 0.
    docs_skipped_has_manifest: int = 0
    skipped_taxonomy: bool = False


# nexus-b91tv: the two metadata keys a doc's tumbler can be stamped under.
# indexer-origin chunks use "doc_id"; store_put-origin chunks use
# "catalog_doc_id" (store_put reserves "doc_id" for the chash instead).
_DOC_LOOKUP_KEYS: tuple[str, ...] = ("doc_id", "catalog_doc_id")


def _fetch_chunks_by_key(
    col: "_ServiceCollectionStub",
    doc_id: str,
    collection: str,
    where_key: str,
) -> list[dict]:
    """Paginate T3 for chunks whose ``metadata[where_key] == doc_id``.

    Returns raw chunk dicts (chash, position, line_start, line_end,
    char_start, char_end, chunk_index_present). Does not merge across
    keys, dedup, or apply the Phase-3 chunk_index check -- the caller
    (:func:`_iter_chunks_for_doc`) does that over the UNION of both keys'
    results so the check sees the doc's true chunk population, not one
    key's partial slice.

    Raises MissingChunkHashError if any matched chunk lacks
    ``chunk_text_hash``.
    """
    chunks: list[dict] = []
    offset = 0
    while True:
        result = col.get(
            where={where_key: doc_id},
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
            chunks.append({
                "chash": chash,
                "position": int(meta.get("chunk_index", 0) or 0),
                "line_start": meta.get("line_start"),
                "line_end": meta.get("line_end"),
                "char_start": meta.get("chunk_start_char"),
                "char_end": meta.get("chunk_end_char"),
                "chunk_index_present": "chunk_index" in meta,
            })
        if len(page_ids) < _PAGE_SIZE:
            break
        offset += _PAGE_SIZE
    return chunks


def _iter_chunks_for_doc(
    col: "_ServiceCollectionStub",
    doc_id: str,
    collection: str,
) -> list[dict]:
    """Paginate T3 and collect chunk metadata for one doc_id.

    Queries BOTH lookup keys in ``_DOC_LOOKUP_KEYS`` (nexus-b91tv: the
    engine's where-grammar has no ``$or``, so this is two queries, not
    one compound filter) and merges the results, deduped by chash so a
    chunk carrying both keys is never double-counted.

    Returns a list of chunk dicts with keys:
      chash, position, line_start, line_end, char_start, char_end

    Raises MissingChunkHashError if any chunk lacks chunk_text_hash.
    """
    merged: dict[str, dict] = {}
    chunk_index_seen = 0
    for where_key in _DOC_LOOKUP_KEYS:
        for chunk in _fetch_chunks_by_key(col, doc_id, collection, where_key):
            chash = chunk["chash"]
            if chash in merged:
                continue
            # nexus-w5zv: track explicit chunk_index presence so multi-chunk
            # Phase-3 docs (where chunk_index was dropped from metadata) fail
            # loud rather than collapse every chunk to position=0. Counted
            # once per unique chash, over the merged set of both keys.
            if chunk.pop("chunk_index_present"):
                chunk_index_seen += 1
            merged[chash] = chunk

    chunks = list(merged.values())
    if len(chunks) > 1 and chunk_index_seen == 0:
        raise Phase3ChunkIndexMissingError(
            doc_id=doc_id, collection=collection, chunk_count=len(chunks),
        )
    return chunks


def backfill_manifest_for_collection(
    catalog: "CatalogReader",
    t3: "T3Database | HttpVectorClient",
    collection_name: str,
    *,
    dry_run: bool = True,
    limit: int = 0,
    only_gapped: bool = False,
) -> BackfillResult:
    """Backfill the document_chunks manifest for one T3 collection.

    Iterates catalog documents whose ``physical_collection`` matches
    ``collection_name``, reads T3 chunk metadata per doc_id (paginating
    at <=300), then calls ``catalog.write_manifest(doc_id, chunks, collection=...)``
    for each document.

    Args:
        catalog: The Catalog instance (SQLite + JSONL).
        t3: T3Database or HttpVectorClient instance for T3 access
            (GH #1373 sibling bug: production's ``make_t3()`` returns
            ``HttpVectorClient``, which has no ``_client_for``).
        collection_name: Name of the T3 collection to backfill.
        dry_run: If True, compute but do not write manifest rows.
        limit: If > 0, process at most this many documents.
        only_gapped: If True (nexus-3n7pr G1), skip any document that
            already has >=1 manifest row -- determined via ONE batched
            ``catalog.get_manifests(...)`` pre-pass over the collection's
            doc_ids, never a per-doc lookup. Skipped docs are counted in
            ``BackfillResult.docs_skipped_has_manifest`` and never reach
            the T3 read or the ``write_manifest`` call, in either dry-run
            or real-run mode.

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

    # Fetch the T3 collection handle (K10: only catch NotFoundError;
    # let quota errors, auth failures, and other exceptions propagate
    # so the operator sees the real cause instead of a silent empty manifest).
    col = None
    try:
        col = t3.get_collection(collection_name)
    except collection_not_found_errors():
        # Collection doesn't exist in T3 — all docs will be counted as skipped.
        col = None

    # Get docs from catalog for this collection.
    docs = catalog.list_by_collection(collection_name)

    # nexus-3n7pr G1: pre-pass, BEFORE any T3 read or write, so a repair
    # run touches only zero-manifest docs. ONE batched get_manifests() call
    # over the whole collection's doc_ids -- get_manifests keys its result
    # dict only for doc_ids that have >=1 manifest row ("missing doc_ids
    # are absent from the result, not keyed to empty list" -- see its
    # docstring), so absence from the returned dict IS the gapped signal.
    gapped_doc_ids: set[str] | None = None
    if only_gapped and docs:
        candidate_ids = [str(doc.tumbler) for doc in docs]
        existing_manifests = catalog.get_manifests(candidate_ids)
        gapped_doc_ids = {
            doc_id for doc_id in candidate_ids if doc_id not in existing_manifests
        }
        # ``limit`` bounds the GAPPED set under --only-gapped (critique of the
        # 3n7pr plan, T2 [22623]): applying it to the raw tumbler-ordered list
        # first could pick N healthy docs and process zero gapped ones, so a
        # canary run would exit 0 without ever exercising the write path.
        # Healthy docs outside the limited window are still counted as
        # skipped-has-manifest so the partition stays visible.
        if limit > 0:
            kept: list = []
            gapped_kept = 0
            for doc in docs:
                if str(doc.tumbler) in gapped_doc_ids:
                    if gapped_kept < limit:
                        kept.append(doc)
                        gapped_kept += 1
                else:
                    kept.append(doc)
            docs = kept
    elif limit > 0:
        docs = docs[:limit]

    for doc in docs:
        doc_id = str(doc.tumbler)

        if gapped_doc_ids is not None and doc_id not in gapped_doc_ids:
            # Already has >=1 manifest row -- --only-gapped skips it before
            # touching T3 or calling the atomic DELETE+INSERT write_manifest.
            result.docs_skipped_has_manifest += 1
            _log.debug(
                "manifest_backfill_doc_skipped_has_manifest",
                collection=collection_name,
                doc_id=doc_id,
            )
            continue

        if col is None:
            # S-2: collection absent in T3 — count as skipped, not processed.
            result.docs_skipped_no_t3 += 1
            _log.debug(
                "manifest_backfill_doc_skipped_no_t3",
                collection=collection_name,
                doc_id=doc_id,
            )
            continue

        try:
            chunks = _iter_chunks_for_doc(col, doc_id, collection_name)
        except Phase3ChunkIndexMissingError:
            # nexus-w5zv: Phase-3 multi-chunk doc has no recoverable
            # position metadata. Skip rather than silently collapse to
            # position=0. Operator must re-index.
            result.docs_skipped_phase3_no_index += 1
            _log.warning(
                "manifest_backfill_doc_skipped_phase3_no_chunk_index",
                collection=collection_name,
                doc_id=doc_id,
            )
            continue

        if not chunks:
            # nexus-gvmbo: never write an empty manifest. write_manifest is
            # an atomic DELETE+INSERT server-side -- writing [] for a doc
            # whose chunks simply didn't match either lookup key would
            # DESTROY any existing manifest for that doc. Skip and count
            # instead; the operator sees exactly which doc and which keys
            # were tried.
            result.docs_skipped_zero_chunks += 1
            _log.warning(
                "manifest_backfill_doc_skipped_zero_chunks",
                collection=collection_name,
                doc_id=doc_id,
                lookup_keys=_DOC_LOOKUP_KEYS,
            )
            continue

        chunks.sort(key=lambda c: c["position"])

        if not dry_run:
            catalog.write_manifest(doc_id, chunks, collection=collection_name)
            # nexus-gvmbo (item 3, remediation-blockers addendum): mirror
            # manifest_heal.py's atomic_manifest_replace + resync pairing --
            # write_manifest alone leaves chunk_count stale (often 0), which
            # re-trips every gap detector on a doc this pass just healed.
            catalog.resync_chunk_count_cache(doc_id)
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
