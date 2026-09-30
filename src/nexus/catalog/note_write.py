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
commit, under the NOT EXISTS guard, so a chunk another document owns survives it.

Two layers:

* :func:`write_note` is the request and the judgement of its outcome.
* :func:`put_note` is the whole caller protocol every note producer needs: register the catalog
  document, begin the index-run fence, write, then act on the outcome (fail the fence, remove the
  row this call minted or put back the identity stamp it put on a row it reconciled onto). The
  producers differ only in how they word the result, so they call this one function and map
  :class:`PutNoteOutcome` to their own message.

Outcomes of :func:`write_note`, so a caller never treats every raise the same way:

1. **Landed**: returns a :class:`NoteWriteResult`. Also when the request raised on the transport (a
   lost acknowledgement, a timeout, a gateway 5xx) but a retried read of the document's manifest
   shows exactly the ``(position, chash)`` rows this call wrote (``recovered=True``): the request
   committed and only the answer was lost. The completion stamp is asked for again then; if that
   cannot be confirmed the outcome is "unknown" (3), never a landed note the fence calls unfinished.
2. **Not landed**: raises :class:`NoteWriteError`. The engine named the document in
   ``failed_doc_ids``, answered with a definitive refusal (a 4xx, a 500), or the connection was
   never made. The transaction is per document, so nothing of the note was written and the old
   manifest is intact. ``manifest_empty`` says whether the document has no manifest at all, which is
   what lets a caller remove a row it minted without deleting a concurrent writer's note.
3. **Unknown**: raises :class:`~nexus.catalog.store_hook.ManifestVerifyUncertainError`. The request
   died in flight (a timeout, a dropped connection, a gateway 5xx) and the manifest does not show it
   yet, so it may still commit (the engine cannot cancel an in-flight embed); or the manifest read
   itself failed on every attempt; or the engine refused to stamp a landed note complete.

One request means one request: a note is bounded by the 16 KiB document quota, so its pieces are
few, and a bulk-indexer chunk cap (``per_collection_chunk_cap``, which protects the local embedder's
memory) does not apply to it. More than :data:`~nexus.db.limits.QUOTAS.MAX_RECORDS_PER_WRITE`
pieces is refused before any request; the multi-batch writer
(:mod:`nexus.catalog.multi_batch_write`) is the tool for a document that large.

Two writers replacing the same document at once are serialized by the engine: it takes the
document's write locks before it reads the previous manifest (``CatalogRepository.writeManifestMany``),
so the second reads the first's manifest, drops its chunks and sweeps them. Nothing is left in T3
without an owner. That needs the engine build that carries the ordering fix; an older engine leaves the
loser's pieces hidden by live(c) until the RDR-192 reaper removes them.

Only ``write_manifest_many`` and ``complete_index_run`` are used on *cat*, so it may be the
``make_catalog_writer()`` proxy (the closed ``CATALOG_WRITE_OPS`` whitelist).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Sequence

import httpx
import structlog

from nexus.catalog.multi_batch_write import (
    DocumentFailedError,
    OneRequestResult,
    complete_document,
    write_one_request,
)
from nexus.catalog.store_hook import (
    ManifestVerifyUncertainError,
    _read_manifest_rows_with_retry,
    note_manifest_metadata,
)
from nexus.db.limits import QUOTAS
from nexus.errors import BatchWriteFailedError, CombinedWriteEmbedTimeoutError, IndexRunVerifyRefused

_log = structlog.get_logger(__name__)


class NoteWriteError(RuntimeError):
    """The note is CONFIRMED not to have landed.

    Nothing of it is in T3 and the document's previous manifest, if any, is intact, because the
    request is one transaction. ``manifest_empty`` is True only when a read of the document's
    manifest succeeded and found it empty: a row this call minted may be removed then, and only
    then (another writer's version means the row is no longer this call's to delete).

    Attributes: ``catalog_doc_id``, ``collection``, ``reason``, ``manifest_empty``.
    """

    def __init__(
        self, *, catalog_doc_id: str, collection: str, reason: str, manifest_empty: bool = False,
    ) -> None:
        self.catalog_doc_id = catalog_doc_id
        self.collection = collection
        self.reason = reason
        self.manifest_empty = manifest_empty
        super().__init__(f"note write of {catalog_doc_id!r} into {collection!r} did not land: {reason}")


@dataclass
class NoteWriteResult:
    """What one :func:`write_note` did.

    ``chunk_ids`` are the piece chashes in piece order (what ``put_note_pieces`` returned).
    ``dropped_chashes`` are the chashes the write dropped from the document's previous manifest,
    which the engine swept after the commit when nothing else owns them; ``None`` when unknown (the
    engine could not read the previous manifest, or ``recovered`` is True and the response was lost).
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


def _content_type_for(collection: str) -> str:
    """The chunk metadata content type ``HttpVectorClient.put`` derived from the collection prefix."""
    for prefix, content_type in (
        ("code__", "code"), ("docs__", "prose"), ("rdr__", "markdown"), ("knowledge__", "prose"),
    ):
        if collection.startswith(prefix):
            return content_type
    return "prose"


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
    content_type: str | None = None,
    cat: Any = None,
) -> NoteWriteResult:
    """Write *pieces* as the chunks of *catalog_doc_id*, and its manifest, in one request.

    *collection* is the full T3 collection name. *pieces* come from
    :func:`~nexus.catalog.store_hook.note_pieces`; the manifest rows are derived from them exactly
    as :func:`~nexus.catalog.store_hook.note_manifest_metadata` does. *content_hash* is the whole
    note's hash (:func:`~nexus.catalog.store_hook.note_content_hash`): given, the document is
    stamped complete in the same request; ``None`` stamps nothing. *content_type* is the chunk
    metadata's content type, by default the one the collection prefix implies. *cat* is a catalog
    writer; by default one is made for the call and closed after it.

    The request is :func:`~nexus.catalog.multi_batch_write.write_one_request`, the primitive the
    multi-batch writer's single-request path uses too, so the checks on the engine's answer are one
    body of code: 429, 503 with Retry-After and connectivity errors are retried (tripping the shared
    rate brake), a ``CombinedWriteEmbedTimeoutError`` never is (a retry would start a second
    uncancelled embed), and a document the engine names in ``failed_doc_ids``, chunks it dropped as
    unreferenced and a refused stamp are judged there.

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
        catalog_doc_id=catalog_doc_id, content_type=content_type or _content_type_for(collection),
    )
    result = NoteWriteResult(
        catalog_doc_id=catalog_doc_id, collection=collection, chunk_ids=[r["chash"] for r in rows])
    expected = [(r["position"], r["chash"]) for r in rows]

    owns_cat = cat is None
    if owns_cat:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load

        cat = make_catalog_writer(priority="interactive")
    try:
        try:
            out = write_one_request(
                cat, doc_id=catalog_doc_id, collection=collection, rows=rows, chunks=chunks,
                content_hash=content_hash or None, sweep=True, dropped="optional")
        except DocumentFailedError as exc:
            raise NoteWriteError(
                catalog_doc_id=catalog_doc_id, collection=collection,
                reason=f"{exc.reason} (see manifest_write_many_doc_failed in the log for its reason)",
                manifest_empty=_manifest_is_empty(catalog_doc_id)) from exc
        except IndexRunVerifyRefused as exc:
            # The rows and chunks committed; only the stamp was refused. Rolling the document back
            # now would delete a manifest the engine holds, so this is "unknown", not "failed".
            raise ManifestVerifyUncertainError(
                f"note {catalog_doc_id} in {collection} landed but the engine refused to stamp it "
                f"complete: {exc}") from exc
        except BatchWriteFailedError as exc:
            raise ManifestVerifyUncertainError(
                f"note {catalog_doc_id} in {collection}: the engine's answer cannot be trusted "
                f"({exc.reason}); the note may have landed") from exc
        except Exception as exc:  # noqa: BLE001 — judged below from the exception and the manifest
            return _settle_after_error(result, expected, exc, cat=cat, content_hash=content_hash)
        _absorb(result, out)
        return result
    finally:
        if owns_cat:
            try:
                cat.close()
            except Exception:  # noqa: BLE001 — best-effort handle cleanup
                pass


def _absorb(result: NoteWriteResult, out: OneRequestResult) -> None:
    result.chunks_written = out.chunks_written
    result.embed_embedded = out.embed_embedded
    result.embed_skipped = out.embed_skipped
    result.chunks_deduped = out.chunks_deduped
    result.swept = out.swept
    result.sweep_skipped = out.sweep_skipped
    result.dropped_chashes = out.dropped
    result.completed = out.completed


def _manifest_is_empty(doc: str) -> bool:
    """True only when the manifest read succeeded and found no row: unknown reads as not empty."""
    try:
        return not _read_manifest_rows_with_retry(doc, context="note write emptiness check")
    except ManifestVerifyUncertainError:
        return False


#: The request never reached the engine, so nothing of it can commit.
_UNSENT = "unsent"
#: The engine answered with a definitive refusal, so the transaction did not commit.
_REFUSED = "refused"
#: The request may have reached the engine and may still commit: settle it from the manifest.
_IN_FLIGHT = "in-flight"

#: A gateway answer says nothing about what the engine did with the request; any other status is the
#: engine's own, definitive answer.
_GATEWAY_STATUSES = frozenset({502, 503, 504})


def _classify(exc: BaseException) -> str:
    """Which of the three shapes a failed ``write_manifest_many`` is (see the constants above)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, CombinedWriteEmbedTimeoutError):
            return _IN_FLIGHT
        if isinstance(cur, httpx.HTTPStatusError):
            return _IN_FLIGHT if cur.response.status_code in _GATEWAY_STATUSES else _REFUSED
        if isinstance(cur, (httpx.ConnectError, httpx.ConnectTimeout)):
            return _UNSENT
        if isinstance(cur, (httpx.TransportError, ConnectionError, TimeoutError)):
            return _IN_FLIGHT
        cur = cur.__cause__ or cur.__context__
    return _REFUSED


def _settle_after_error(
    result: NoteWriteResult, expected: list[tuple[int, str]], exc: Exception, *,
    cat: Any, content_hash: str | None,
) -> NoteWriteResult:
    """The request raised: decide what happened, from the exception and, if in flight, the manifest.

    A note that landed on the strength of the manifest read alone may not have been stamped complete
    (an unchanged re-put whose request never committed reads the same as one that did), so the stamp
    is asked for again, with the manifest ROW count the engine verifies against (a repeated piece is
    one chunk but two rows).
    """
    doc = result.catalog_doc_id
    kind = _classify(exc)
    if kind != _IN_FLIGHT:
        raise NoteWriteError(
            catalog_doc_id=doc, collection=result.collection, reason=str(exc),
            manifest_empty=_manifest_is_empty(doc)) from exc
    try:
        landed = _read_manifest_rows_with_retry(doc, context="note write exception arbitration")
    except ManifestVerifyUncertainError as verify_exc:
        _log.warning(
            "note_write_exception_unarbitrated", doc_id=doc, collection=result.collection,
            error=str(exc)[:300])
        raise ManifestVerifyUncertainError(
            f"{verify_exc}; the note write itself had raised: {exc}") from verify_exc
    if landed != expected:
        _log.warning(
            "note_write_in_flight_not_visible", doc_id=doc, collection=result.collection,
            error=str(exc)[:300])
        raise ManifestVerifyUncertainError(
            f"note {doc} in {result.collection}: the write request failed in flight ({exc}) and the "
            "manifest does not show it, but it may still commit") from exc
    _log.warning(
        "note_write_exception_but_landed", doc_id=doc, collection=result.collection,
        error=str(exc)[:300])
    result.recovered = True
    if content_hash:
        try:
            complete_document(cat, doc_id=doc, content_hash=content_hash, manifest_rows=len(expected))
        except Exception as stamp_exc:  # noqa: BLE001 — reported as unknown below, never as a finished note
            _log.warning(
                "note_write_recovered_complete_stamp_failed", doc_id=doc,
                collection=result.collection, exc_info=True)
            raise ManifestVerifyUncertainError(
                f"note {doc} in {result.collection} landed (the write's answer was lost) but could not "
                f"be stamped complete: {stamp_exc}") from stamp_exc
        result.completed = True
    return result


# ── the caller protocol ──────────────────────────────────────────────────────

STORED = "stored"
NOT_LANDED = "not-landed"
UNCERTAIN = "uncertain"
NO_CATALOG = "no-catalog"


@dataclass
class PutNoteOutcome:
    """What :func:`put_note` did, for a producer to word its own message from.

    ``status`` is one of :data:`STORED`, :data:`NOT_LANDED` (nothing was written; the catalog row this
    call minted is removed or the identity stamp it changed is put back; retry is safe),
    :data:`UNCERTAIN` (the note may have landed; nothing was rolled back) and :data:`NO_CATALOG`
    (registration produced no document, so nothing was written at all). ``reason`` carries the
    underlying message for the last three. ``write`` is the :class:`NoteWriteResult` when stored.
    """

    status: str
    collection: str
    pieces: list[str] = field(default_factory=list)
    manifest_metadatas: list[dict] = field(default_factory=list)
    chunk_ids: list[str] = field(default_factory=list)
    catalog_doc_id: str = ""
    minted: bool = False
    reason: str = ""
    write: NoteWriteResult | None = None

    @property
    def doc_id(self) -> str:
        """The first chunk's id: the note's natural id, what ``store_put`` reports."""
        return self.chunk_ids[0] if self.chunk_ids else ""


def put_note(
    *,
    content: str,
    collection: str,
    title: str = "",
    tags: str = "",
    category: str = "",
    session_id: str = "",
    source_agent: str = "",
    ttl_days: int | None = None,
    content_type: str | None = None,
    cat: Any = None,
) -> PutNoteOutcome:
    """Register, fence, write and settle one note. The whole caller protocol in one place.

    1. Split *content* into pieces and refuse an over-quota note before minting anything.
    2. Register the catalog document (reconciling onto an existing (collection, title) row).
       Registration that yields no document is :data:`NO_CATALOG`: nothing has been written and
       nothing can be, since a note is never written ownerless.
    3. ``doc_indexer._fence_begin`` (advisory), then :func:`write_note` with the whole note's hash so
       the completion stamp rides the request.
    4. On :class:`NoteWriteError`: ``_fence_fail``; remove the row this call minted, but only when
       its manifest is empty (a concurrent writer's version means the row is no longer ours), or,
       for a row this call reconciled onto, put back the identity stamp it changed.
       On :class:`~nexus.catalog.store_hook.ManifestVerifyUncertainError`, or a landed note the fence
       could not be told about: ``_fence_fail`` and nothing else, since the note may exist.
       On any other exception: ``_fence_fail``, remove a minted row, re-raise.

    *collection* is the full T3 collection name. Raises ``PutOversizedError`` for an over-quota
    note and ``ValueError`` for a bad ``ttl_days``, both before any side effect.
    """
    from nexus.catalog import store_hook as sh  # noqa: PLC0415 — module attribute lookup at call time: callers and tests patch these names

    if ttl_days is not None and ttl_days <= 0:
        raise ValueError(
            f"ttl_days={ttl_days} is invalid: omit the argument or pass None for a permanent entry "
            "— ttl_days must be a positive integer number of days (0 does NOT mean permanent; None does)")
    pieces = sh.note_pieces(content, collection)
    first_chash, manifest_metadatas = sh.note_manifest_metadata(pieces)
    sh.raise_if_oversized(content, doc_id=first_chash, collection=collection)
    out = PutNoteOutcome(
        status=NO_CATALOG, collection=collection, pieces=pieces, manifest_metadatas=manifest_metadatas,
        chunk_ids=[m.get("chunk_text_hash", "") for m in manifest_metadatas])

    pre_call: dict[str, str] = {}
    try:
        out.catalog_doc_id, out.minted = sh.catalog_store_hook_tracked(
            title=title, doc_id=first_chash, collection_name=collection, pre_call_doc_id_out=pre_call)
    except Exception:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning and the NO_CATALOG outcome
        _log.warning("catalog_store_hook_failed", doc_id=first_chash, collection=collection, exc_info=True)
    if not out.catalog_doc_id:
        out.reason = "catalog registration failed"
        return out

    from nexus.doc_indexer import _fence_begin, _fence_fail  # noqa: PLC0415 — deferred import; tests patch these names

    doc = out.catalog_doc_id
    content_hash = sh.note_content_hash(content, manifest_metadatas)
    _fence_begin(doc, content_hash, collection)
    try:
        write = write_note(
            catalog_doc_id=doc, collection=collection, pieces=pieces, content_hash=content_hash,
            title=title, tags=tags, category=category, session_id=session_id,
            source_agent=source_agent, ttl_days=ttl_days, content_type=content_type, cat=cat)
        if not write.completed:
            raise ManifestVerifyUncertainError(
                f"note {doc} in {collection} landed but was not stamped complete")
    except ManifestVerifyUncertainError as exc:
        out.status, out.reason = UNCERTAIN, str(exc)
        _fence_fail(doc, out.reason)
        _log.warning(
            "store_put_manifest_verify_uncertain", doc_id=out.doc_id, catalog_doc_id=doc,
            collection=collection, error=out.reason[:300], exc_info=True)
        return out
    except NoteWriteError as exc:
        out.status, out.reason = NOT_LANDED, exc.reason
        _fence_fail(doc, out.reason)
        _log.warning(
            "store_put_note_write_failed", doc_id=out.doc_id, catalog_doc_id=doc,
            collection=collection, manifest_empty=exc.manifest_empty, error=out.reason[:300],
            exc_info=True)
        if out.minted:
            if exc.manifest_empty:
                sh.rollback_minted_catalog_entry(doc, original_error=out.reason)
        else:
            sh.restore_pre_call_stamp(doc, pre_call.get("doc_id", ""), out.doc_id)
        return out
    except Exception as exc:
        _fence_fail(doc, str(exc))
        if out.minted:
            sh.rollback_minted_catalog_entry(doc, original_error=str(exc))
        raise
    out.status, out.write = STORED, write
    return out
