# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one writer for a note: its pieces and its manifest in ONE request (RDR-223, nexus-z0o2p.12).

A note (MCP ``store_put``; next ``nx store put``, ``nx memory promote`` and the recovery-bundle
import) is one catalog document of a few pieces. It used to be written as one ``/store-put``
request per piece, then a separate manifest request, then compensation when a later step failed.
Between the chunk requests and the manifest request a chunk existed with no owner row, and a client
that died there left it behind. :func:`write_note` sends the pieces as the ``chunks`` array of a
single ``write_manifest_many`` request for the one document, with ``sweep`` on and the completion
stamp riding the same request. The engine writes the chunks and the owner rows in one transaction,
so a chunk of the note never lands without its owner and a failed request leaves the previous
manifest exactly as it was. The superseded-chunk sweep follows in its own transaction after the
commit, under the NOT EXISTS guard, so a chunk another document owns survives it. Two writers
replacing the same document at once each read the previous manifest before either commits, so
neither sweeps the other's pieces: the loser's pieces can stay in T3 without an owner until the
RDR-192 reaper removes them.

Caller contract:

* Register the catalog document first (:func:`~nexus.catalog.store_hook.catalog_store_hook_tracked`)
  and pass its tumbler. A note with no catalog document cannot be written at all; there is no
  ownerless fallback.
* The index fence stays the caller's: call ``doc_indexer._fence_begin`` before this and
  ``_fence_fail`` when it raises. The writer stamps the document complete in the same request when
  given a ``content_hash`` (the whole note's hash from
  :func:`~nexus.catalog.store_hook.note_content_hash`); with none it stamps nothing.
* Roll back a catalog row only when this call minted it, and only on :class:`NoteWriteError`. On
  :class:`~nexus.catalog.store_hook.ManifestVerifyUncertainError` the note may have landed: report
  the uncertainty and leave everything alone.

Outcomes, so a caller never treats every raise the same way:

1. **Landed**: returns a :class:`NoteWriteResult`. Also when the request raised (a lost
   acknowledgement, a timeout) but a retried read of the document's manifest shows exactly the
   pieces this call wrote (``recovered=True``): the request committed and only the answer was lost.
2. **Not landed**: raises :class:`NoteWriteError`. The engine named the document in
   ``failed_doc_ids``, or the request raised and the manifest read proves the note absent. The
   transaction is per document, so nothing of the note was written and the old manifest is intact.
3. **Unknown**: raises ``ManifestVerifyUncertainError``. The request raised and the manifest read
   itself failed on every attempt, or the engine refused to stamp a landed note complete.

One request means one request: a note is bounded by the 16 KiB document quota, so its pieces are
few, and a bulk-indexer chunk cap (``per_collection_chunk_cap``, which protects the local embedder's
memory) does not apply to it. More than :data:`~nexus.db.limits.QUOTAS.MAX_RECORDS_PER_WRITE`
pieces is refused before any request; the multi-batch writer
(:mod:`nexus.catalog.multi_batch_write`) is the tool for a document that large.

Only ``write_manifest_many`` is used on *cat*, so it may be the ``make_catalog_writer()`` proxy (the
closed ``CATALOG_WRITE_OPS`` whitelist).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Sequence

import structlog

from nexus.catalog.store_hook import (
    ManifestVerifyUncertainError,
    _read_manifest_chashes_with_retry,
    note_manifest_metadata,
)
from nexus.db.limits import QUOTAS

_log = structlog.get_logger(__name__)


class NoteWriteError(RuntimeError):
    """The note is CONFIRMED not to have landed.

    Nothing of it is in T3 and the document's previous manifest, if any, is intact, because the
    request is one transaction. Safe to roll back a catalog row this call minted.

    Attributes: ``catalog_doc_id``, ``collection``, ``reason``.
    """

    def __init__(self, *, catalog_doc_id: str, collection: str, reason: str) -> None:
        self.catalog_doc_id = catalog_doc_id
        self.collection = collection
        self.reason = reason
        super().__init__(f"note write of {catalog_doc_id!r} into {collection!r} did not land: {reason}")


@dataclass
class NoteWriteResult:
    """What one :func:`write_note` did.

    ``chunk_ids`` are the piece chashes in piece order (what ``put_note_pieces`` returned).
    ``dropped_chashes`` are the chashes the write dropped from the document's previous manifest,
    which the same transaction swept when nothing else owns them; ``None`` when unknown (the engine
    could not read the previous manifest, or ``recovered`` is True and the response was lost).
    ``swept`` / ``sweep_skipped`` are the engine's counts for that sweep.
    """

    catalog_doc_id: str
    collection: str
    chunk_ids: list[str] = field(default_factory=list)
    requests: int = 1
    chunks_written: int = 0
    embed_embedded: int = 0
    embed_skipped: int = 0
    chunks_deduped: int = 0
    swept: int = 0
    sweep_skipped: int = 0
    dropped_chashes: list[str] | None = None
    completed: bool = False
    recovered: bool = False


def note_manifest_rows(manifest_metadatas: Sequence[dict]) -> list[dict]:
    """Manifest rows for a note, from :func:`~nexus.catalog.store_hook.note_manifest_metadata`.

    Same fields ``store_put_manifest_direct`` wrote: ``chash``, ``position`` (the piece's
    ``chunk_index``, else its index), ``chunk_index``, the note-relative character span.
    """
    rows = [
        {
            "chash": m.get("chunk_text_hash", ""),
            "position": int(m.get("chunk_index", i)),
            "chunk_index": m.get("chunk_index"),
            "line_start": m.get("line_start") or None,
            "line_end": m.get("line_end") or None,
            "char_start": m.get("chunk_start_char") or None,
            "char_end": m.get("chunk_end_char") or None,
        }
        for i, m in enumerate(manifest_metadatas or [])
    ]
    return [r for r in rows if r["chash"]]


def _chunk_payload(
    collection: str, pieces: Sequence[str], rows: Sequence[dict], *,
    title: str, tags: str, category: str, session_id: str, source_agent: str,
    ttl_days: int | None, catalog_doc_id: str, content_type: str,
) -> list[dict]:
    """The ``chunks`` array: one ``{chash, text, metadata}`` per DISTINCT piece.

    The metadata is what ``HttpVectorClient.put`` stamped on the single piece it wrote (the same
    ``make_chunk_metadata`` factory, ``catalog_doc_id`` included), so a note reads back exactly as
    one written the old way. Identical pieces collapse to one chunk, as identical text always did.
    """
    from nexus.corpus import index_model_for_collection  # noqa: PLC0415 — deferred: nexus.corpus imports back into catalog
    from nexus.metadata_schema import make_chunk_metadata  # noqa: PLC0415 — deferred: circular-dep avoidance

    model = index_model_for_collection(collection)
    now_iso = datetime.now(UTC).isoformat()
    out: list[dict] = []
    seen: set[str] = set()
    for piece, row in zip(pieces, rows, strict=True):
        chash = row["chash"]
        if chash in seen:
            continue
        seen.add(chash)
        meta = make_chunk_metadata(
            content_type=content_type,
            chunk_text_hash=chash,
            content_hash=chash,
            chunk_start_char=0,
            chunk_end_char=len(piece),
            indexed_at=now_iso,
            embedding_model=model,
            title=title,
            tags=tags,
            category=category,
            ttl_days=ttl_days,
            source_agent=source_agent,
            session_id=session_id,
        )
        if catalog_doc_id:
            meta["catalog_doc_id"] = catalog_doc_id
        out.append({"chash": chash, "text": piece, "metadata": meta})
    return out


def write_note(
    *,
    catalog_doc_id: str,
    collection: str,
    pieces: Sequence[str],
    content_hash: str | None = None,
    title: str = "",
    tags: str = "",
    category: str = "",
    session_id: str = "",
    source_agent: str = "",
    ttl_days: int | None = None,
    content_type: str = "prose",
    cat: Any = None,
) -> NoteWriteResult:
    """Write *pieces* as the chunks of *catalog_doc_id*, and its manifest, in one request.

    *collection* is the full T3 collection name. *pieces* come from
    :func:`~nexus.catalog.store_hook.note_pieces`; the manifest rows are derived from them exactly
    as :func:`~nexus.catalog.store_hook.note_manifest_metadata` does. *content_hash* is the whole
    note's hash (:func:`~nexus.catalog.store_hook.note_content_hash`): given, the document is
    stamped complete in the same request; ``None`` stamps nothing. *content_type* is the chunk
    metadata's content type: a note is prose (a ``knowledge__`` collection's), and the name is never
    parsed for it. *cat* is a catalog writer; by default one is made for the call and closed after it.

    Raises ``ValueError`` before any request for a missing document or collection, no pieces, more
    pieces than one request may carry, or ``ttl_days`` that is not a positive integer. See the
    module docstring for the three outcomes and what each obliges the caller to do.
    """
    if not catalog_doc_id:
        raise ValueError("write_note: 'catalog_doc_id' is required (a note is never written ownerless)")
    if not collection:
        raise ValueError("write_note: 'collection' is required")
    pieces = list(pieces)
    if not pieces or not all(pieces):
        raise ValueError("write_note: a note needs at least one non-empty piece")
    if len(pieces) > QUOTAS.MAX_RECORDS_PER_WRITE:
        raise ValueError(
            f"write_note: {len(pieces)} pieces exceed the {QUOTAS.MAX_RECORDS_PER_WRITE} one request "
            "carries; use the multi-batch writer (nexus.catalog.multi_batch_write)")
    if ttl_days is not None and ttl_days <= 0:
        raise ValueError(
            f"ttl_days={ttl_days} is invalid: omit the argument or pass None for a permanent entry "
            "— ttl_days must be a positive integer number of days (0 does NOT mean permanent; None does)")

    _first, manifest_metadatas = note_manifest_metadata(pieces)
    rows = note_manifest_rows(manifest_metadatas)
    chunks = _chunk_payload(
        collection, pieces, rows, title=title, tags=tags, category=category,
        session_id=session_id, source_agent=source_agent, ttl_days=ttl_days,
        catalog_doc_id=catalog_doc_id, content_type=content_type,
    )
    result = NoteWriteResult(
        catalog_doc_id=catalog_doc_id, collection=collection, chunk_ids=[r["chash"] for r in rows])
    expected = {r["chash"] for r in rows}

    owns_cat = cat is None
    if owns_cat:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load

        cat = make_catalog_writer(priority="interactive")
    try:
        try:
            resp = cat.write_manifest_many(
                [(catalog_doc_id, rows)],
                complete={catalog_doc_id: content_hash} if content_hash else None,
                sweep=True, chunks=chunks, collection=collection,
            )
        except Exception as exc:  # noqa: BLE001 — the request may have committed with only its answer lost; arbitrated by reading the manifest
            return _arbitrate_after_error(result, expected, exc, cat=cat, content_hash=content_hash)
        resp = resp if isinstance(resp, dict) else {}
        _absorb_response(result, resp, stamped=bool(content_hash))
        return result
    finally:
        if owns_cat:
            try:
                cat.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup
                pass


def _absorb_response(result: NoteWriteResult, resp: dict, *, stamped: bool) -> None:
    doc = result.catalog_doc_id
    if doc in (resp.get("failed_doc_ids") or ()):
        raise NoteWriteError(
            catalog_doc_id=doc, collection=result.collection,
            reason="the engine reported the document in failed_doc_ids "
                   "(see manifest_write_many_doc_failed in the log for its reason)")
    result.chunks_written = int(resp.get("chunks_written") or 0)
    result.embed_embedded = int(resp.get("embed_embedded") or 0)
    result.embed_skipped = int(resp.get("embed_skipped") or 0)
    result.chunks_deduped = int(resp.get("chunks_deduped") or 0)
    result.swept = int(resp.get("swept") or 0)
    result.sweep_skipped = int(resp.get("sweep_skipped") or 0)
    dropped = resp.get("dropped_chashes")
    if isinstance(dropped, dict) and doc in dropped and doc not in (resp.get("dropped_unknown") or ()):
        result.dropped_chashes = list(dropped[doc] or ())
    for refused in resp.get("complete_refused") or ():
        if refused.get("doc_id") == doc:
            # The rows and chunks committed; only the stamp was refused. Rolling the document back
            # now would delete a manifest the engine holds, so this is "unknown", not "failed".
            raise ManifestVerifyUncertainError(
                f"note {doc} in {result.collection} landed but the engine refused to stamp it complete: {refused}")
    result.completed = stamped


def _arbitrate_after_error(
    result: NoteWriteResult, expected: set[str], exc: Exception, *, cat: Any, content_hash: str | None,
) -> NoteWriteResult:
    """The request raised: decide from the document's manifest whether it committed.

    A note that landed on the strength of this read alone may not have been stamped complete (an
    unchanged re-put whose request never committed reads the same as one that did), so the stamp is
    asked for again; the engine checks it against the manifest it holds.
    """
    doc = result.catalog_doc_id
    try:
        landed = _read_manifest_chashes_with_retry(doc, context="note write exception arbitration")
    except ManifestVerifyUncertainError as verify_exc:
        _log.warning(
            "note_write_exception_unarbitrated", doc_id=doc, collection=result.collection,
            error=str(exc)[:300])
        raise ManifestVerifyUncertainError(
            f"{verify_exc}; the note write itself had raised: {exc}") from verify_exc
    if landed != expected:
        raise NoteWriteError(
            catalog_doc_id=doc, collection=result.collection, reason=str(exc)) from exc
    _log.warning(
        "note_write_exception_but_landed", doc_id=doc, collection=result.collection,
        error=str(exc)[:300])
    result.recovered = True
    if content_hash:
        try:
            cat.complete_index_run(doc, content_hash, len(expected))
            result.completed = True
        except Exception:  # noqa: BLE001 — the note is there; an unstamped fence is diagnostic, and the caller's own fail path must not fire for a landed note
            _log.warning(
                "note_write_recovered_complete_stamp_failed", doc_id=doc,
                collection=result.collection, exc_info=True)
    return result
