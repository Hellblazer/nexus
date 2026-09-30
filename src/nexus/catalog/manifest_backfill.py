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
  - chash divergence (nexus-dmf7r): the manifest row's chash is keyed on
    the T3 chunk's own id, never on ``metadata["chunk_text_hash"]``. The
    id IS the chash by construction (RDR-108/RDR-180); the metadata field
    is a redundant copy written at index time. They agree in every
    healthy state, but a rekey, a hand-patched metadata row, or an
    RDR-180-class width change could let them diverge -- and writing the
    (possibly stale) metadata copy into the manifest is exactly what
    trips ``fk_catalog_chunks_chunk`` (validated on cloud since
    engine-service-v0.1.76) with a 409, since the FK checks the id's
    referent, not the copy. Backfill asserts equality per chunk; a
    mismatch skips the WHOLE document (never guesses which value is
    correct), counted in ``BackfillResult.docs_skipped_chash_divergent``.
  - FK-409 on write (nexus-r7g3i): a manifest write whose chash has no
    matching ``nexus.chunks`` row 409s against ``fk_catalog_chunks_chunk``.
    This is caught PER DOCUMENT around the ``write_manifest`` call, counted
    in ``BackfillResult.docs_skipped_fk_409``, and the collection's
    remaining documents keep processing -- previously any per-doc write
    error propagated out of this function and was caught per-COLLECTION by
    the CLI, abandoning every unprocessed document in that collection
    without marking any of them. Any OTHER exception (not a 409) still
    propagates unchanged.
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
  - DRY-RUN COUNTS ARE AN UPPER BOUND: every skip class except
    ``docs_skipped_fk_409`` is decided before the ``dry_run`` branch, so a
    dry run reports it exactly. The FK check (``fk_catalog_chunks_chunk``)
    happens server-side only when a manifest is written, so a dry run
    cannot predict it: ``docs_processed`` and ``chunks_would_write`` can
    exceed the real run's ``docs_processed``/``chunks_written`` by the
    documents the real run skips as FK-409.
  - REVERSE notes discovery (nexus-wbfpw.7, review of nexus-wbfpw.4 round 2,
    T2 nexus/review-wbfpw4-code-r2): the census
    (``scripts/sql/manifest_less_census.sql``) classifies a chunk as
    ``legacy-unmanifested`` via TWO paths, not one. The forward path above
    is the first; the second is the "nl3fn NOTES GUARD" reverse match —
    a chunk with no live forward pointer (absent, or naming a tombstoned
    document) whose chash is instead named by a LIVE, note-shaped
    (``file_path`` empty) catalog document's OWN ``meta["doc_id"]`` (the
    same predicate :func:`nexus.indexer_utils.is_note_shaped` /
    :func:`nexus.indexer_utils.live_note_chashes` already use to protect
    such chunks from GC). Before this fix, backfill's discovery
    (``_iter_chunks_for_doc``) only ever queried a chunk's FORWARD
    metadata key, so a reverse-owned note-shaped document always matched
    zero chunks and landed in ``docs_skipped_zero_chunks`` — the census
    would keep reporting it ``legacy-unmanifested`` forever, and the
    census-zero gate (nexus-wbfpw.7's own bead) could never read zero for
    this shape.

    Discovery is materialized ONCE per collection from the SAME ``docs``
    list ``list_by_collection`` already returned (every live document in
    this collection), mirroring the census's own ``live_notes``/
    ``rev_candidates`` CTEs. For each note-shaped document, its own
    ``meta["doc_id"]`` is grouped by chash; when several live notes
    reverse-match the SAME chash, the census's EXACT tie-break applies —
    fewest manifest rows anywhere (any collection) wins, ties broken by
    the lowest tumbler (:class:`nexus.catalog.tumbler.Tumbler`'s own
    ``__lt__``, the same integer-segment ordering the engine's tumbler
    column sorts by) — via ONE batched ``catalog.get_manifests(...)`` call
    over just the candidate note tumblers (reusing the ``only_gapped``
    pre-pass's own batched call when it already covers every doc in the
    collection, never a second round trip for the same information). A
    winning candidate is only eligible when it has ZERO manifest rows in
    ANY collection — matching the census's own ``legacy-unmanifested``
    condition; a note with rows elsewhere is ``dead-owner`` territory
    (a rename-copy leftover) and backfill must not touch it.

    T3 cost (nexus-wbfpw.7 fix-round-1, correcting an earlier overclaim of
    "never a per-chunk round trip" that was true for the *document*
    discovery above but not for the *chunk* reads this needs): ONE batched
    ``col.get(ids=[...])`` call over every distinct candidate chash in the
    collection (:func:`_forward_hints_for_chashes`), never a per-candidate
    round trip. That same batched fetch's raw metadata is cached and reused
    (:func:`_fetch_chunk_by_id`'s ``cached_meta`` parameter) when a winning
    candidate is later manifested, so the winner pays no second T3 round
    trip either.

    PRECEDENCE, TENANT-WIDE (nexus-wbfpw.7 fix-round-1 CRITICAL, T2
    nexus/review-wbfpw7-code and nexus/critique-wbfpw7-code): a candidate
    chash whose own T3 chunk carries a forward pointer naming a document
    that is LIVE ANYWHERE IN THE TENANT — resolved via ONE batched
    ``catalog.resolve_many(...)`` call over every distinct forward pointer
    found, which (like the census's own ``fwd_owner`` join) returns only
    documents with ``deleted_at IS NULL`` — is dropped from reverse
    candidacy, exactly mirroring the census's own forward-wins-when-live
    rule. The FIRST fix round scoped this exclusion to only THIS
    collection's own live-doc set, which both (a) let a live forward owner
    registered under a DIFFERENT ``physical_collection`` slip through
    undetected, so a coincidental same-collection note with the identical
    chash as its own identity could be wrongly manifested as that chunk's
    owner (a genuine misattribution, not just a missed rescue), and (b)
    gave the operator no diagnostic for the gap at all. This round closes
    (a) unconditionally. For (b), only PARTIALLY: a chash whose true
    forward owner is live but registered elsewhere is reported via
    ``BackfillResult.docs_cross_collection_forward_owner_skipped`` (plus a
    structured warning naming the chash and the owner's tumbler and
    collection) ONLY when a note in this collection also claims that
    chash in reverse, because only reverse candidates are examined here.
    The general case, a forward-owned-elsewhere chunk no note claims, is
    invisible to this document-driven pass: its owner is never iterated,
    so it surfaces nowhere in this result and the counter reads 0. The
    census (``nx t3 census-manifest-less`` or the standalone SQL) is the
    only complete detector: an operator must check its legacy-unmanifested
    items for an owner registered in a different collection before relying
    on backfill, and the closing ``--require-zero legacy-unmanifested``
    run catches anything left. The write itself would be legal
    (``fk_catalog_chunks_chunk`` keys on ``(tenant_id, collection, chash)``,
    not on the owning document's own registered collection — the SAME
    schema shape the census's own rename-copy scenario already relies on),
    but reaching it requires enumerating every chunk in a T3 collection
    independent of any document that names it, a capability this
    document-driven backfill does not have and this round does not add;
    see ``scripts/sql/manifest_less_census.sql``'s own bucket comment for
    the authoritative classification this residual maps to.

    MULTI-PIECE GUARD (nexus-wbfpw.7 fix-round-1, T2
    nexus/review-wbfpw7-code Important): a note's reverse identity
    (``meta["doc_id"]``) names only ONE chunk, whatever the note's true
    piece count — ``note_manifest_metadata`` (``catalog/store_hook.py``)
    stamps a MULTI-piece note's identity from piece 0's own chash alone.
    A winning candidate is only manifested when its registered
    ``chunk_count`` is <= 1; a winner with ``chunk_count`` > 1 is a legacy
    multi-piece note with no forward pointer on any piece, and manifesting
    piece 0 alone would resync ``chunk_count`` to 1 and mark the document
    healed while pieces 1..N-1 stay orphaned with no path back — a silent,
    irreversible partial heal. Skipped instead, counted in
    ``BackfillResult.docs_reverse_multi_piece_skipped`` (plus a structured
    warning naming the doc, the chash, and the registered chunk_count).

    In the main loop, reverse discovery is consulted ONLY as the fallback
    when a document's FORWARD lookup (``_iter_chunks_for_doc``) matches
    zero chunks — the two paths are precedence-ordered exactly like the
    census (forward wins when live; reverse only rescues a document the
    forward path found nothing for), and a note-shaped document's defining
    property (nexus-cotmr) is that it carries no forward-pointing chunk in
    the first place. The reverse chunk itself is fetched by id
    (``_fetch_chunk_by_id``, reusing the discovery pass's cached metadata
    when available, else one more ``col.get(ids=[chash])``) — a
    where-filter lookup by definition finds nothing, since the chunk
    carries no forward pointer to this document — and, since a note's
    catalog identity is always single-chunk (subject to the multi-piece
    guard above), manifested at position 0. Counted separately from
    forward discovery in ``BackfillResult.docs_reverse_discovered`` (never
    folded into a forward-only counter) so a dry run's report distinguishes
    the two discovery paths, matching the census's own ``owner_path``
    column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx
import structlog
from nexus.errors import collection_not_found_errors
from nexus.indexer_utils import is_note_shaped

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


class ChashDivergentError(ValueError):
    """Raised when a T3 chunk's own id disagrees with its metadata copy
    of ``chunk_text_hash`` (nexus-dmf7r).

    The T3 chunk id IS the chash by construction (RDR-108/RDR-180) -- the
    metadata field is a redundant copy written at index time, and they
    agree in every healthy state. A divergence (a rekey, a hand-patched
    metadata row, an RDR-180-class id-width change) means the metadata
    copy no longer names the row the FK (``fk_catalog_chunks_chunk``,
    validated on cloud since engine-service-v0.1.76) actually checks
    against; writing that copy into the manifest would 409. Backfill
    cannot decide which of the two is "correct" -- it never writes a
    manifest row for a chunk it cannot verify, and the caller skips the
    whole document rather than guess.
    """

    def __init__(
        self, doc_id: str, collection: str, chunk_id: str, meta_chash: str,
    ) -> None:
        self.doc_id = doc_id
        self.collection = collection
        self.chunk_id = chunk_id
        self.meta_chash = meta_chash
        super().__init__(
            f"Document {doc_id!r} in collection {collection!r} has a T3 "
            f"chunk whose id {chunk_id!r} disagrees with its "
            f"metadata['chunk_text_hash'] copy {meta_chash!r}. The id is "
            f"the FK's actual referent; refusing to write a manifest row "
            f"keyed on the divergent copy. Skipping the document."
        )


@dataclass
class BackfillResult:
    """Summary of one backfill run over a single collection."""

    collection: str
    docs_processed: int = 0
    # True when this result came from a dry run: docs_processed and the
    # skip counters then describe the plan, not writes that happened.
    dry_run: bool = False
    chunks_written: int = 0
    # Rows a dry run WOULD write (chunks_written stays 0 in a dry run, since
    # nothing is written). A real run leaves this 0; callers report
    # chunks_would_write in dry run and chunks_written otherwise. An upper
    # bound: a server-side FK-409 at write time is not predictable (see the
    # module docstring).
    chunks_would_write: int = 0
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
    # nexus-dmf7r: count docs skipped because a T3 chunk's id (the FK's
    # actual referent) disagreed with its metadata['chunk_text_hash']
    # copy -- see ChashDivergentError. Never written. Non-vacuity: a
    # caller pointing backfill at a corpus with a rekeyed or
    # hand-patched metadata copy must SEE this count rise, not a
    # silent 0.
    docs_skipped_chash_divergent: int = 0
    # nexus-r7g3i: count docs whose write_manifest call 409'd against
    # fk_catalog_chunks_chunk (no matching nexus.chunks row for the
    # written chash). Never partially written -- the server-side write
    # is atomic and the 409 means it did not happen at all. Non-vacuity:
    # a caller pointing backfill at docs with no recoverable T3 content
    # must SEE this count rise, not a silent abort of the collection.
    docs_skipped_fk_409: int = 0
    skipped_taxonomy: bool = False
    # nexus-wbfpw.7: count docs manifested via the REVERSE notes-guard path
    # (a live note-shaped document's own meta["doc_id"] naming a chash with
    # no live forward owner) rather than the forward metadata-key path.
    # Counted regardless of dry_run -- see the module docstring's "REVERSE
    # notes discovery" contract. Non-vacuity: a caller pointing backfill at
    # a collection holding reverse-owned notes must SEE this count rise,
    # never a silent 0 that reads as "nothing to discover" when the census
    # says otherwise.
    docs_reverse_discovered: int = 0
    # nexus-wbfpw.7 fix-round-1 CRITICAL (T2 nexus/review-wbfpw7-code): count
    # reverse candidates dropped because their forward pointer names a
    # document that is LIVE somewhere in the tenant but registered under a
    # DIFFERENT physical_collection than this one -- the census's own
    # tenant-wide fwd_owner join would name that document, not any note in
    # this collection, as the chunk's true owner. Backfill cannot manifest
    # this chash into that owner from this collection-scoped, document-
    # driven pass (see the module docstring's PRECEDENCE, TENANT-WIDE
    # section) -- reported here rather than silently dropped or wrongly
    # attributed. Coverage is partial: only chashes a note in this
    # collection also claims in reverse are examined, so 0 does NOT mean
    # the tenant has no forward-owned-elsewhere legacy-unmanifested chunks;
    # the census is the complete check.
    docs_cross_collection_forward_owner_skipped: int = 0
    # nexus-wbfpw.7 fix-round-1 (T2 nexus/review-wbfpw7-code Important):
    # count reverse candidates dropped because the winning note's own
    # registered chunk_count is > 1 -- a note's reverse identity names only
    # ONE chunk regardless of true piece count, so manifesting position 0
    # alone for a legacy multi-piece note would resync chunk_count to 1 and
    # mark it healed while the remaining pieces stay orphaned forever. Never
    # partially healed -- see the module docstring's MULTI-PIECE GUARD.
    # Non-vacuity: an operator with a legacy multi-piece reverse-owned note
    # must SEE this count rise, never a silent partial heal.
    docs_reverse_multi_piece_skipped: int = 0
    # nexus-wbfpw.41 (review S2): forward-path documents skipped because the
    # matched chunk population cannot be one manifest: two matched chunks at
    # the same position, or (under only_gapped) a matched count that differs
    # from the document's registered chunk_count. The typical case is a
    # legacy note whose old and current text both still carry its tumbler --
    # the census calls EVERY such chunk legacy-unmanifested, and manifesting
    # them all would either be refused (two rows at one position) or publish
    # superseded text. Never written; the operator re-puts the note.
    docs_skipped_chunk_count_mismatch: int = 0


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
    ``chunk_text_hash``. Raises ChashDivergentError (nexus-dmf7r) if any
    matched chunk's own id disagrees with its ``chunk_text_hash`` copy.
    """
    chunks: list[dict] = []
    offset = 0
    while True:
        # nexus-wbfpw.10 (RDR-192 Step 5 amendment): the whole point of this
        # backfill is manifesting a chunk that has none yet -- by definition
        # it has no live own-collection manifest owner, so live(c) hides it
        # unless this asks for the physical scan.
        result = col.get(
            where={where_key: doc_id},
            limit=_PAGE_SIZE,
            offset=offset,
            include=["metadatas"],
            include_non_live=True,
        )
        page_ids: list[str] = result.get("ids") or []
        page_metas: list[dict] = result.get("metadatas") or []
        if not page_ids:
            break
        for cid, meta in zip(page_ids, page_metas):
            if not isinstance(meta, dict):
                meta = {}
            meta_chash = meta.get("chunk_text_hash") or ""
            if not meta_chash:
                raise MissingChunkHashError(chunk_id=cid, collection=collection)
            # nexus-dmf7r: key the manifest row on `cid` -- the T3 chunk's
            # own id, which IS the chash by construction (RDR-108/RDR-180)
            # and is what fk_catalog_chunks_chunk actually validates
            # against -- never on `meta_chash`, a redundant copy that can
            # silently diverge from the row it was copied from. Assert
            # equality rather than trusting the copy.
            if cid != meta_chash:
                raise ChashDivergentError(
                    doc_id=doc_id, collection=collection,
                    chunk_id=cid, meta_chash=meta_chash,
                )
            chunks.append({
                "chash": cid,
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


def _fetch_chunk_by_id(
    col: "_ServiceCollectionStub",
    chash: str,
    doc_id: str,
    collection: str,
    *,
    cached_meta: dict | None = None,
) -> dict | None:
    """Fetch ONE T3 chunk by its own id (nexus-wbfpw.7 REVERSE path).

    Reads stored rows whether or not they have a live owner
    (``include_non_live``, RDR-192 Step 5 amendment, nexus-wbfpw.10): a
    reverse candidate is by definition a chunk no manifest owns yet.

    Unlike :func:`_fetch_chunks_by_key`, this never filters by a forward
    metadata key -- a reverse-owned chunk by definition carries no forward
    pointer to *doc_id* (that absence, or a forward pointer to a tombstoned
    document, is exactly why the census's reverse rescue exists), so a
    ``where``-filter lookup would always find nothing. ``doc_id`` here is
    the note-shaped document whose own ``meta["doc_id"]`` named *chash* --
    used only for error attribution, never as a lookup key.

    *cached_meta* (nexus-wbfpw.7 fix-round-1, T2 nexus/review-wbfpw7-code
    Important -- "_forward_hint_for_chash is a per-candidate round trip, not
    batched"): when the caller already fetched this chash's metadata via
    :func:`_forward_hints_for_chashes`'s single batched pass, pass it here
    to validate and build the manifest row WITHOUT a second network round
    trip. ``None`` (the default) falls through to a live ``col.get(ids=
    [chash])`` -- used when the cache has no entry (the chash was not found
    during discovery, e.g. deleted since) or the caller has no cache at all.

    Returns None if the chunk no longer exists in T3 (its catalog row
    stamped this chash into ``meta["doc_id"]`` at write time, but the T3
    chunk was since deleted out from under it) -- the caller folds this
    into the same ``docs_skipped_zero_chunks`` accounting as any other
    doc whose lookup matches nothing, never writing a manifest for a chunk
    that is not there.

    Raises MissingChunkHashError if the chunk lacks ``chunk_text_hash``.
    Raises ChashDivergentError (nexus-dmf7r) if the chunk's own id
    disagrees with its ``chunk_text_hash`` copy -- same contract as the
    forward path, applied uniformly to the reverse one.
    """
    if cached_meta is not None:
        cid = chash
        meta = cached_meta
    else:
        result = col.get(ids=[chash], include=["metadatas"], include_non_live=True)
        ids: list[str] = result.get("ids") or []
        if not ids:
            return None
        metas: list[dict] = result.get("metadatas") or []
        cid = ids[0]
        meta = metas[0] if metas else {}
        if not isinstance(meta, dict):
            meta = {}
    meta_chash = meta.get("chunk_text_hash") or ""
    if not meta_chash:
        raise MissingChunkHashError(chunk_id=cid, collection=collection)
    if cid != meta_chash:
        raise ChashDivergentError(
            doc_id=doc_id, collection=collection,
            chunk_id=cid, meta_chash=meta_chash,
        )
    return {
        "chash": cid,
        # A note's catalog identity is always single-chunk
        # (store_hook.py::single_chunk_manifest_metadata) -- position 0 is
        # the only defensible value, never a guess among several.
        "position": 0,
        "line_start": meta.get("line_start"),
        "line_end": meta.get("line_end"),
        "char_start": meta.get("chunk_start_char"),
        "char_end": meta.get("chunk_end_char"),
    }


def _forward_hints_for_chashes(
    col: "_ServiceCollectionStub", chashes: list[str],
) -> tuple[dict[str, str], dict[str, dict]]:
    """ONE batched T3 fetch for every reverse-candidate chash (nexus-wbfpw.7
    fix-round-1, T2 nexus/review-wbfpw7-code Important -- replaces an O(N)
    per-chash ``col.get`` loop with one call).

    Reads stored rows whether or not they have a live owner, like
    :func:`_fetch_chunk_by_id`.

    Returns ``(forward_hint_by_chash, raw_meta_by_chash)``:
      - ``forward_hint_by_chash``: the RAW forward-pointer value
        (``catalog_doc_id``, falling back to ``doc_id``) stamped on each
        FOUND chash's own T3 chunk metadata, or ``""`` when the chunk
        carries neither key. A chash absent from this dict was not found
        in T3 at all. Used ONLY to decide reverse-candidacy eligibility in
        :func:`_reverse_note_owner_by_doc` -- deliberately NOT
        chash-validated here (no ``chunk_text_hash``/divergence check);
        the winning candidate's manifest write validates via
        :func:`_fetch_chunk_by_id`, reusing ``raw_meta_by_chash`` below.
      - ``raw_meta_by_chash``: the raw metadata dict for every chash T3
        actually has, keyed by chash -- passed to :func:`_fetch_chunk_by_id`
        as ``cached_meta`` so the winning candidate's manifest row is built
        without a second network round trip for the same chash.

    ONE ``col.get(ids=[...])`` call for every distinct candidate chash --
    :meth:`_ServiceCollectionStub.get` already paginates internally at
    ``QUOTAS.MAX_RECORDS_PER_WRITE`` on the ``ids=`` branch, so this stays
    bounded to one call per collection regardless of candidate count.
    """
    if not chashes:
        return {}, {}
    result = col.get(ids=list(chashes), include=["metadatas"], include_non_live=True)
    ids: list[str] = result.get("ids") or []
    metas: list[dict] = result.get("metadatas") or []
    if len(metas) != len(ids):
        # zip would truncate silently and drop every reverse candidate.
        raise RuntimeError(
            f"manifest backfill: the engine returned {len(ids)} ids but "
            f"{len(metas)} metadatas for a physical read of {len(chashes)} "
            "chashes; refusing to guess which candidates are missing"
        )
    forward_hints: dict[str, str] = {}
    raw_meta_by_chash: dict[str, dict] = {}
    for cid, meta in zip(ids, metas):
        if not isinstance(meta, dict):
            meta = {}
        raw_meta_by_chash[cid] = meta
        forward_hints[cid] = meta.get("catalog_doc_id") or meta.get("doc_id") or ""
    return forward_hints, raw_meta_by_chash


@dataclass
class _ReverseDiscovery:
    """Result of one collection's reverse-notes discovery pass.

    ``winner_chash_by_doc``: doc_id -> the chash it should manifest (the
    prior return type, unchanged).
    ``raw_meta_by_chash``: cached T3 metadata for every candidate chash
    found, reused by the caller to avoid re-fetching the winner's row.
    ``cross_collection_forward_owner``: count of candidates dropped because
    their forward pointer names a document LIVE elsewhere in the tenant
    (``BackfillResult.docs_cross_collection_forward_owner_skipped``).
    ``multi_piece_skipped``: count of candidates dropped because the
    winning note's registered ``chunk_count`` is > 1
    (``BackfillResult.docs_reverse_multi_piece_skipped``).
    """

    winner_chash_by_doc: dict[str, str] = field(default_factory=dict)
    raw_meta_by_chash: dict[str, dict] = field(default_factory=dict)
    cross_collection_forward_owner: int = 0
    multi_piece_skipped: int = 0


def _reverse_note_owner_by_doc(
    catalog: "CatalogReader",
    col: "_ServiceCollectionStub",
    docs: list,
    collection: str,
    *,
    manifest_counts_by_doc: "dict[str, list] | None",
) -> _ReverseDiscovery:
    """Map winning reverse-note doc_id -> the chash it should manifest.

    Mirrors ``manifest_less_census.sql``'s ``live_notes``/``rev_candidates``
    CTEs exactly: every note-shaped document in *docs* (already scoped to
    ONE physical collection, the same live set ``list_by_collection``
    returns) is grouped by its own ``meta["doc_id"]`` chash. When several
    live notes reverse-match the SAME chash, the winner is the one with
    the FEWEST manifest rows anywhere (any collection), ties broken by the
    lowest tumbler -- the identical ``ORDER BY doc_id_hex, total_count ASC,
    tumbler ASC`` the census's ``rev_candidates`` CTE applies. A winner is
    only returned when it has ZERO manifest rows anywhere -- the
    ``legacy-unmanifested`` condition; a winner with rows elsewhere is
    ``dead-owner`` territory the census does not rescue, and backfill must
    not touch it either (no counter -- this branch was already silent
    before this fix and stays silent; see
    ``test_reverse_sole_candidate_with_rows_elsewhere_is_not_granted``).

    PRECEDENCE, TENANT-WIDE (forward wins when live, exactly like the
    census -- nexus-wbfpw.7 fix-round-1 CRITICAL, T2
    nexus/review-wbfpw7-code / nexus/critique-wbfpw7-code): before any
    tie-break, every candidate chash's forward pointer
    (``catalog_doc_id``/``doc_id``, batched via
    :func:`_forward_hints_for_chashes`) is resolved via ONE
    ``catalog.resolve_many(...)`` call. ``resolve_many`` -- like the
    census's own ``fwd_owner`` join precedence -- returns only documents
    with ``deleted_at IS NULL``, so membership in its result IS "live
    ANYWHERE IN THE TENANT", not just in *docs*. A candidate whose forward
    pointer resolves this way is dropped from reverse candidacy entirely,
    regardless of which ``physical_collection`` the forward owner is
    registered under. The FIRST fix round scoped this exclusion to
    ``{str(d.tumbler) for d in docs}`` (this collection's own live-doc set
    only), which let a same-collection note wrongly claim a chash whose
    true forward owner was live but registered elsewhere -- a genuine
    misattribution the tenant-wide check closes unconditionally. When the
    live forward owner is registered under a DIFFERENT collection than
    *collection*, that is reported via
    ``_ReverseDiscovery.cross_collection_forward_owner`` (backfill still
    cannot manifest the chash into that owner from here -- see the module
    docstring's PRECEDENCE, TENANT-WIDE section for why).

    MULTI-PIECE GUARD (nexus-wbfpw.7 fix-round-1, T2
    nexus/review-wbfpw7-code Important): a winning candidate whose
    registered ``chunk_count`` is > 1 is a legacy multi-piece note with no
    forward pointer on any piece -- manifesting its reverse identity chash
    alone would resync ``chunk_count`` to 1 and silently, irreversibly
    orphan the remaining pieces. Skipped and counted in
    ``_ReverseDiscovery.multi_piece_skipped`` instead of granted.

    *manifest_counts_by_doc*, when not None, is the ``only_gapped``
    pre-pass's already-fetched ``catalog.get_manifests(...)`` result over
    EVERY doc in this collection -- a superset of the note-shaped
    candidates here, reused rather than re-fetched. When None, this
    function makes its OWN single batched ``catalog.get_manifests(...)``
    call over just the note candidates -- still bounded to once per
    collection, never a per-chunk or per-doc round trip.
    """
    note_candidates: dict[str, list] = {}
    for doc in docs:
        if not is_note_shaped(doc):
            continue
        doc_chash = (getattr(doc, "meta", None) or {}).get("doc_id", "")
        if doc_chash:
            note_candidates.setdefault(doc_chash, []).append(doc)

    if not note_candidates:
        return _ReverseDiscovery()

    if manifest_counts_by_doc is None:
        note_doc_ids = [
            str(d.tumbler) for cands in note_candidates.values() for d in cands
        ]
        manifest_counts_by_doc = catalog.get_manifests(note_doc_ids)

    live_tumblers_this_collection = {str(d.tumbler) for d in docs}

    forward_hints, raw_meta_by_chash = _forward_hints_for_chashes(
        col, list(note_candidates.keys()),
    )
    forward_tumblers = sorted({h for h in forward_hints.values() if h})
    # nexus-wbfpw.7 fix-round-1: ONE batched tenant-wide resolve, mirroring
    # the census's own fwd_owner join -- resolve_many returns only LIVE
    # (deleted_at IS NULL) documents (confirmed against
    # CatalogRepository.resolveMany), so membership here IS the tenant-wide
    # liveness check.
    live_forward_owners = catalog.resolve_many(forward_tumblers) if forward_tumblers else {}

    winner_chash_by_doc: dict[str, str] = {}
    cross_collection_forward_owner = 0
    multi_piece_skipped = 0
    for doc_chash, candidates in note_candidates.items():
        forward_hint = forward_hints.get(doc_chash, "")
        if forward_hint and forward_hint in live_forward_owners:
            # A live forward owner always wins -- this chash is never a
            # reverse candidate at all, tie-break included.
            if forward_hint not in live_tumblers_this_collection:
                owner_entry = live_forward_owners[forward_hint]
                cross_collection_forward_owner += 1
                _log.warning(
                    "manifest_backfill_reverse_cross_collection_forward_owner",
                    collection=collection,
                    chash=doc_chash,
                    forward_owner_tumbler=forward_hint,
                    forward_owner_collection=owner_entry.physical_collection,
                )
            continue
        winner = min(
            candidates,
            key=lambda d: (
                len(manifest_counts_by_doc.get(str(d.tumbler), []) or []),
                d.tumbler,
            ),
        )
        if len(manifest_counts_by_doc.get(str(winner.tumbler), []) or []) != 0:
            continue
        chunk_count = getattr(winner, "chunk_count", 0) or 0
        if chunk_count > 1:
            multi_piece_skipped += 1
            _log.warning(
                "manifest_backfill_reverse_multi_piece_skipped",
                collection=collection,
                doc_id=str(winner.tumbler),
                chash=doc_chash,
                chunk_count=chunk_count,
            )
            continue
        winner_chash_by_doc[str(winner.tumbler)] = doc_chash
    return _ReverseDiscovery(
        winner_chash_by_doc=winner_chash_by_doc,
        raw_meta_by_chash=raw_meta_by_chash,
        cross_collection_forward_owner=cross_collection_forward_owner,
        multi_piece_skipped=multi_piece_skipped,
    )


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
    Raises ChashDivergentError (nexus-dmf7r) if any chunk's id disagrees
    with its chunk_text_hash copy.
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
        BackfillResult with counts. ``docs_reverse_discovered`` counts docs
        manifested via the REVERSE notes-guard path (nexus-wbfpw.7) rather
        than the forward metadata-key path -- see the module docstring's
        "REVERSE notes discovery" contract. Counted regardless of
        ``dry_run``, and included in ``docs_processed``/``chunks_written``
        as well (it is a breakdown, not a separate total).

    Raises:
        MissingChunkHashError: if any chunk lacks ``chunk_text_hash``.

    ChashDivergentError (nexus-dmf7r) and an FK-409 on
    ``write_manifest`` (nexus-r7g3i) are both caught PER DOCUMENT inside
    this function and turned into skip counters
    (``docs_skipped_chash_divergent``, ``docs_skipped_fk_409``) rather
    than propagating -- see the module docstring's edge-case contracts.
    """
    result = BackfillResult(collection=collection_name, dry_run=dry_run)

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
    # nexus-wbfpw.7: reverse notes discovery needs the FULL, untruncated
    # live-document population for this collection -- exactly what the
    # census's own live_notes CTE scans -- captured before --only-gapped's
    # pre-pass or --limit mutate `docs` below.
    all_docs = docs

    # nexus-3n7pr G1: pre-pass, BEFORE any T3 read or write, so a repair
    # run touches only zero-manifest docs. ONE batched get_manifests() call
    # over the whole collection's doc_ids -- get_manifests keys its result
    # dict only for doc_ids that have >=1 manifest row ("missing doc_ids
    # are absent from the result, not keyed to empty list" -- see its
    # docstring), so absence from the returned dict IS the gapped signal.
    gapped_doc_ids: set[str] | None = None
    existing_manifests: dict[str, list] | None = None
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

    # nexus-wbfpw.7: reverse notes discovery, materialized ONCE per
    # collection over the full population -- see the module docstring's
    # "REVERSE notes discovery" contract. Reuses the --only-gapped
    # pre-pass's batched get_manifests() call above when it ran (it already
    # covers every doc in this collection, a superset of the note-shaped
    # candidates here) instead of a second round trip. Requires a real T3
    # collection handle (the forward-liveness check reads chunk metadata);
    # when `col is None` (collection absent in T3), every doc is about to
    # be counted docs_skipped_no_t3 below regardless, so there is nothing
    # to discover.
    reverse_discovery = (
        _reverse_note_owner_by_doc(
            catalog, col, all_docs, collection_name,
            manifest_counts_by_doc=existing_manifests,
        )
        if col is not None
        else _ReverseDiscovery()
    )
    reverse_owner_chash_by_doc: dict[str, str] = reverse_discovery.winner_chash_by_doc
    reverse_raw_meta_by_chash: dict[str, dict] = reverse_discovery.raw_meta_by_chash
    result.docs_cross_collection_forward_owner_skipped = (
        reverse_discovery.cross_collection_forward_owner
    )
    result.docs_reverse_multi_piece_skipped = reverse_discovery.multi_piece_skipped

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
        except ChashDivergentError as exc:
            # nexus-dmf7r: a T3 chunk's id disagreed with its
            # chunk_text_hash metadata copy. Never write a manifest row
            # keyed on the unverifiable copy -- skip the whole document.
            result.docs_skipped_chash_divergent += 1
            _log.warning(
                "manifest_backfill_doc_skipped_chash_divergent",
                collection=collection_name,
                doc_id=doc_id,
                chunk_id=exc.chunk_id,
                meta_chash=exc.meta_chash,
            )
            continue

        # nexus-wbfpw.7: REVERSE fallback -- only consulted when the
        # forward lookup above found nothing, and only for a document this
        # collection's census-matching tie-break (_reverse_note_owner_by_doc)
        # already picked as the winning reverse owner. Forward wins whenever
        # it finds anything, exactly like the census's own precedence.
        is_reverse = False
        if not chunks and doc_id in reverse_owner_chash_by_doc:
            reverse_chash = reverse_owner_chash_by_doc[doc_id]
            try:
                reverse_chunk = _fetch_chunk_by_id(
                    col, reverse_chash, doc_id, collection_name,
                    cached_meta=reverse_raw_meta_by_chash.get(reverse_chash),
                )
            except ChashDivergentError as exc:
                # Same contract as the forward path's ChashDivergentError
                # handling above -- never write a manifest row keyed on an
                # unverifiable copy.
                result.docs_skipped_chash_divergent += 1
                _log.warning(
                    "manifest_backfill_doc_skipped_chash_divergent",
                    collection=collection_name,
                    doc_id=doc_id,
                    chunk_id=exc.chunk_id,
                    meta_chash=exc.meta_chash,
                )
                continue
            if reverse_chunk is not None:
                chunks = [reverse_chunk]
                is_reverse = True

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

        if not is_reverse:
            positions = [c["position"] for c in chunks]
            registered = int(getattr(doc, "chunk_count", 0) or 0)
            duplicate_positions = len(set(positions)) != len(positions)
            # matched > registered only. Fewer matched than registered is the
            # repeated-piece note: identical chunk text collapses to ONE T3 row
            # by design (RDR-108), so a note of N pieces with a repeated one
            # matches N-1 unique chunks at unique positions, and the manifest
            # keeps the position gap the verb always wrote for it.
            count_mismatch = only_gapped and registered > 0 and len(chunks) > registered
            if duplicate_positions or count_mismatch:
                # nexus-wbfpw.41 (review S2). write_manifest is an atomic
                # REPLACE onto PRIMARY KEY (tenant, doc_id, position): two
                # rows at one position make the whole write fail (the
                # engine's unique-violation, seen here as a 409 and, before
                # this guard, mislabelled fk_409). And a matched population
                # larger than the registered chunk_count means chunks the
                # document no longer owns still carry its tumbler. Neither is
                # decidable here, so report and leave it for the operator.
                result.docs_skipped_chunk_count_mismatch += 1
                _log.warning(
                    "manifest_backfill_doc_skipped_chunk_count_mismatch",
                    collection=collection_name,
                    doc_id=doc_id,
                    matched=len(chunks),
                    registered_chunk_count=registered,
                    duplicate_positions=duplicate_positions,
                    chashes=[c["chash"] for c in chunks],
                )
                continue

        chunks.sort(key=lambda c: c["position"])

        if dry_run:
            result.chunks_would_write += len(chunks)
        else:
            if only_gapped and catalog.get_manifest(doc_id):
                # nexus-wbfpw.41 (review S2): the pre-pass read is minutes
                # old on a large collection, and write_manifest is an atomic
                # REPLACE with no compare-and-set. A note re-put since then
                # already has its manifest; replacing it would hide the new
                # chunk. One fresh read narrows the window to a single round
                # trip; it cannot close it (the engine has no CAS on this
                # route).
                result.docs_skipped_has_manifest += 1
                _log.info(
                    "manifest_backfill_doc_skipped_manifest_appeared",
                    collection=collection_name,
                    doc_id=doc_id,
                )
                continue
            try:
                catalog.write_manifest(doc_id, chunks, collection=collection_name)
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                if status != 409:
                    raise
                # nexus-r7g3i: fk_catalog_chunks_chunk (validated on cloud
                # since engine-service-v0.1.76) rejected this manifest
                # write -- no nexus.chunks row matches the written chash.
                # The write is atomic server-side, so nothing partial
                # landed. Previously this propagated out of the function
                # and was caught per-COLLECTION by the CLI, abandoning
                # every unprocessed document in the collection without
                # marking any of them. Skip + count instead; the
                # remainder of the collection keeps processing.
                result.docs_skipped_fk_409 += 1
                _log.warning(
                    "manifest_backfill_doc_skipped_fk_409",
                    collection=collection_name,
                    doc_id=doc_id,
                    chunk_count=len(chunks),
                    # nexus-69c94 critique S2 (substantive-critic): name the
                    # attempted chash(es) -- write_manifest sends every chunk
                    # in ONE insert and the 409 body doesn't say which row
                    # tripped the FK, so the full attempted list is the most
                    # specific signal available; an operator acting on this
                    # skip should not have to re-derive candidates by hand
                    # (asymmetric with chash_divergent's chunk_id/meta_chash
                    # logging above until this fix).
                    chashes=[c["chash"] for c in chunks],
                )
                continue
            # nexus-gvmbo (item 3, remediation-blockers addendum): mirror
            # manifest_heal.py's atomic_manifest_replace + resync pairing --
            # write_manifest alone leaves chunk_count stale (often 0), which
            # re-trips every gap detector on a doc this pass just healed.
            catalog.resync_chunk_count_cache(doc_id)
            result.chunks_written += len(chunks)

        result.docs_processed += 1
        if is_reverse:
            # nexus-wbfpw.7: counted regardless of dry_run -- a dry run must
            # report reverse-discovered counts separately from forward ones.
            result.docs_reverse_discovered += 1

        _log.debug(
            "manifest_backfill_doc",
            collection=collection_name,
            doc_id=doc_id,
            chunks=len(chunks),
            dry_run=dry_run,
            reverse=is_reverse,
        )

    return result
