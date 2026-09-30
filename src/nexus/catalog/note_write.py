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
   shows exactly the ``(position, chash)`` rows this call wrote. The content is there but the request
   may never have committed, so it is resent once (it is idempotent: changed tags, ttl or category on
   unchanged content are applied, and the completion stamp rides it) and the resend's answer is the
   outcome (``recovered=True``); a resend that fails is "unknown" (3), never a landed note the fence
   calls unfinished. Every attempt's error counts: if any was in flight the request is settled from
   the manifest, never called "unsent". The resend is a whole new request, so a concurrent writer of
   the same document that landed between the manifest read and the resend is overwritten: the last
   request wins, as with any two racing puts.
2. **Not landed**: raises :class:`NoteWriteError`. The engine named the document in
   ``failed_doc_ids``, answered with a definitive 4xx refusal, or the connection was never made,
   and no attempt of the request was in flight. A 500 and an unexpected exception are NOT
   definitive: a 500 can follow the commit. The transaction is per document, so no piece of the
   note was added and the old manifest is intact. The one thing a refusal can leave is a metadata
   refresh: the engine refreshes the metadata of chunks whose text it already holds in a transaction
   of its own before it embeds the new ones, and a 429 from the embedder (the only 429 a
   ``write_manifest_many`` raises) arrives after that commit and before any manifest transaction
   (``CombinedWriteService`` phase 2a commits, phase 2b embeds). The 429 is still definitive: the
   manifest and the set of chunks are untouched, which is all a caller acts on, so it is not
   settled from the manifest (that could only turn a first write's refusal into "unknown").
   ``manifest_empty`` says whether the document has no manifest at all, which is what lets a caller
   remove a row it minted without deleting a concurrent writer's note.
3. **Unknown**: raises :class:`~nexus.catalog.store_hook.ManifestVerifyUncertainError`. The request
   died in flight (a timeout, a dropped connection, a gateway 5xx) and the manifest does not show it
   yet, so it may still commit (the engine cannot cancel an in-flight embed); or the manifest read
   itself failed on every attempt; or the engine refused to stamp a landed note complete
   (:class:`StampRefusedError`: recorded, the fence left ``indexing``, never ``_fence_fail``).

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


#: Who refused a note write that did not land (:attr:`NoteWriteError.refusal`,
#: :attr:`PutNoteOutcome.refusal`).
REFUSED_BY_CLIENT = "client"
REFUSED_UNREACHABLE = "unreachable"
REFUSED_BY_ENGINE = "engine"


class NoteWriteError(RuntimeError):
    """The note is CONFIRMED not to have landed.

    No piece of it was added to T3 and the document's previous manifest, if any, is intact, because
    the request is one transaction. What a refused request can leave behind is a metadata refresh on
    chunks whose text the engine already held (see the module docstring, outcome 2). ``manifest_empty`` is True only when a read of the document's
    manifest succeeded and found it empty: a row this call minted may be removed then, and only
    then (another writer's version means the row is no longer this call's to delete).

    ``refusal`` says who refused, which decides what a caller may tell the operator: the client
    itself before it sent anything (:data:`REFUSED_BY_CLIENT`: a profile mismatch, a missing Voyage
    key, a retired collection: a retry fails the same way until the operator acts), a connection that
    was never made (:data:`REFUSED_UNREACHABLE`), or the engine after it received the request
    (:data:`REFUSED_BY_ENGINE`, the only one after which a metadata refresh can have committed).

    Attributes: ``catalog_doc_id``, ``collection``, ``reason``, ``manifest_empty``, ``refusal``.
    """

    def __init__(
        self, *, catalog_doc_id: str, collection: str, reason: str, manifest_empty: bool = False,
        refusal: str = "engine",
    ) -> None:
        self.catalog_doc_id = catalog_doc_id
        self.collection = collection
        self.reason = reason
        self.manifest_empty = manifest_empty
        self.refusal = refusal
        super().__init__(f"note write of {catalog_doc_id!r} into {collection!r} did not land: {reason}")


class StampRefusedError(ManifestVerifyUncertainError):
    """The engine accepted the note's write but refused to stamp the document complete.

    The writer's rule (``multi_batch_write``): a refusal is recorded (``_record_complete_refusal``, for
    the record-level summary) and the fence is LEFT ``indexing``, so nothing fails the index run and
    no failed-document heal runs. The caller reports it as uncertain and does not call ``_fence_fail``.
    ``detail`` is the engine's refusal text alone, for a caller that words its own message.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail or message


class _UnstampedError(ManifestVerifyUncertainError):
    """The write landed and the response said the document was not stamped complete, with no engine
    refusal to name (contrast :class:`StampRefusedError`). The note exists; its state is unconfirmed."""


class _AttemptRecorder:
    """Wraps a catalog writer and remembers every error ``write_manifest_many`` raised, so a request
    is judged from ALL its attempts: the retry wrapper re-raises only the last, and a dropped
    connection followed by refused reconnects is still a request that may have reached the engine."""

    def __init__(self, cat: Any) -> None:
        self._cat = cat
        self.errors: list[BaseException] = []

    def write_manifest_many(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self._cat.write_manifest_many(*args, **kwargs)
        except Exception as exc:
            self.errors.append(exc)
            raise

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cat, name)

    def any_in_flight(self) -> bool:
        return any(_classify(e) == _IN_FLIGHT for e in self.errors)


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

    from nexus.metadata_schema import chunk_content_type_for_collection  # noqa: PLC0415 — deferred: circular-dep avoidance

    _first, manifest_metadatas = note_manifest_metadata(pieces)
    rows = note_manifest_rows(manifest_metadatas)
    chunks = _chunk_payload(
        collection, pieces, rows, title=title, tags=tags, category=category,
        session_id=session_id, source_agent=source_agent, ttl_days=ttl_days,
        catalog_doc_id=catalog_doc_id, content_type=content_type or chunk_content_type_for_collection(collection),
    )
    result = NoteWriteResult(
        catalog_doc_id=catalog_doc_id, collection=collection, chunk_ids=[r["chash"] for r in rows])
    expected = [(r["position"], r["chash"]) for r in rows]

    owns_cat = cat is None
    if owns_cat:
        from nexus.catalog.factory import make_catalog_writer  # noqa: PLC0415 — deferred to avoid circular import at module load

        cat = make_catalog_writer(priority="interactive")
    recorder = _AttemptRecorder(cat)
    try:
        try:
            out = write_one_request(
                recorder, doc_id=catalog_doc_id, collection=collection, rows=rows, chunks=chunks,
                content_hash=content_hash or None, sweep=True, dropped="optional")
        except DocumentFailedError as exc:
            if recorder.any_in_flight():
                # An earlier attempt of this request may have committed before this one was refused.
                return _settle_after_error(
                    result, expected, exc, recorder=recorder, cat=cat, rows=rows, chunks=chunks,
                    content_hash=content_hash)
            raise NoteWriteError(
                catalog_doc_id=catalog_doc_id, collection=collection,
                reason=f"{exc.reason} (the engine's own log carries its reason, event manifest_write_many_doc_failed)",
                manifest_empty=_manifest_is_empty(catalog_doc_id), refusal=REFUSED_BY_ENGINE) from exc
        except IndexRunVerifyRefused as exc:
            raise _stamp_refused(catalog_doc_id, collection, exc) from exc
        except BatchWriteFailedError as exc:
            raise ManifestVerifyUncertainError(
                f"note {catalog_doc_id} in {collection}: the engine's answer cannot be trusted "
                f"({exc.reason}); the note may have landed") from exc
        except Exception as exc:  # noqa: BLE001 — judged below from every attempt's error and the manifest
            return _settle_after_error(
                result, expected, exc, recorder=recorder, cat=cat, rows=rows, chunks=chunks,
                content_hash=content_hash)
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
#: The engine answered with a definitive 4xx refusal, so the transaction did not commit.
_REFUSED = "refused"
#: Anything else: the request may have reached the engine and may have committed or still commit
#: (a dropped connection, a timeout, a gateway 5xx, a 500 that can follow the commit, an exception
#: nobody anticipated). Settle it from the manifest.
_IN_FLIGHT = "in-flight"


def _client_side_refusals() -> tuple[type[BaseException], ...]:
    """Refusals the client raises BEFORE it sends: ``write_manifest_many`` registers the collection
    first, and registration refuses a profile that disagrees with the engine's, a voyage intent with
    no key, and a retired collection name. Nothing reached the engine, so the note is not in flight.
    (Imported at call time: ``nexus.corpus`` imports back into the catalog package.)"""
    from nexus.collection_errors import SupersededCollectionWriteError  # noqa: PLC0415 — deferred: circular-dep avoidance
    from nexus.corpus import EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError  # noqa: PLC0415 — deferred: circular-dep avoidance

    return (EmbeddingProfileMismatchError, LocalVoyageCredentialMissingError, SupersededCollectionWriteError)


def _judge(exc: BaseException) -> tuple[str, str]:
    """``(shape, refusal)`` of one failed ``write_manifest_many`` attempt.

    *shape* is one of :data:`_UNSENT`, :data:`_REFUSED`, :data:`_IN_FLIGHT`. *refusal* says who refused
    when the shape is definitive (:data:`REFUSED_BY_ENGINE` for a 4xx, else :data:`REFUSED_BY_CLIENT`
    when a node of the chain is a client-side refusal, else :data:`REFUSED_UNREACHABLE`) and is ``""``
    for an attempt in flight.

    Only a 4xx (the engine answered and refused; 408 is a timeout, so not that) and a connection that
    was never made are definitive. Everything else, unknown exceptions included, is in flight.

    The WHOLE exception chain is judged, not its first recognisable node. The httpx mixin
    (``_refreshable_client._request``) retries once inside its own ``except`` block, so the
    exception attempt 2 raises carries attempt 1's as ``__context__``, and the attempt recorder sees
    only the final one. A dropped connection whose retry was refused (or the reverse) is one request
    that may have reached the engine. Precedence over the chain: any in-flight node (an embed
    timeout, a 5xx or 408, a transport error that is not a failed connect) makes it in flight, a
    registration node included (a dropped request followed by a refused re-registration is one
    request that may have reached the engine); else any 4xx makes it refused; else it is unsent,
    which needs at least one failed connect or client-side refusal and no other transport node. A
    chain of nothing recognisable is in flight. A refusal the client raises before it sends
    (:func:`_client_side_refusals`) counts as a connection never made.
    """
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    refused = unsent = client_side = False
    while pending:
        cur = pending.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, CombinedWriteEmbedTimeoutError):
            return _IN_FLIGHT, ""
        if isinstance(cur, httpx.HTTPStatusError):
            status = cur.response.status_code
            if 400 <= status < 500 and status != 408:
                refused = True
            else:
                return _IN_FLIGHT, ""
        elif isinstance(cur, _client_side_refusals()):
            unsent = client_side = True
        elif isinstance(cur, (httpx.ConnectError, httpx.ConnectTimeout)):
            unsent = True
        elif isinstance(cur, httpx.TransportError):
            return _IN_FLIGHT, ""
        pending.extend(n for n in (cur.__cause__, cur.__context__) if n is not None)
    if refused:
        return _REFUSED, REFUSED_BY_ENGINE
    if unsent:
        return _UNSENT, REFUSED_BY_CLIENT if client_side else REFUSED_UNREACHABLE
    return _IN_FLIGHT, ""


def _classify(exc: BaseException) -> str:
    """Which of the three shapes one failed ``write_manifest_many`` attempt is (see :func:`_judge`)."""
    return _judge(exc)[0]


def _refusal_of(errors: Sequence[BaseException]) -> str:
    """Who refused a request none of whose attempts was in flight: the engine if any attempt got a 4xx
    (it received something), else the client if any attempt was its own pre-send refusal, else a
    connection that was never made."""
    origins = {_judge(e)[1] for e in errors}
    for origin in (REFUSED_BY_ENGINE, REFUSED_BY_CLIENT):
        if origin in origins:
            return origin
    return REFUSED_UNREACHABLE


def _cause_chain(exc: BaseException) -> str:
    """The exception classes of *exc*'s whole cause/context chain, outermost first, for a log line
    that no longer carries a traceback (``ConnectError <- EmbeddingProfileMismatchError``)."""
    names: list[str] = []
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        cur = pending.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        names.append(type(cur).__name__)
        pending.extend(n for n in (cur.__cause__, cur.__context__) if n is not None)
    return " <- ".join(names)


def _stamp_refused(doc: str, collection: str, exc: BaseException) -> StampRefusedError:
    """The writer's rule for a refused stamp: record it, leave the fence as it is, report unknown."""
    try:
        from nexus.mcp_infra import _record_complete_refusal  # noqa: PLC0415 — deferred: mcp_infra imports back into catalog code

        _record_complete_refusal(doc)
    except Exception as rec_exc:  # noqa: BLE001 — recording is advisory; the refusal itself propagates
        _log.warning("note_write_refusal_record_failed", doc_id=doc, error=str(rec_exc))
    return StampRefusedError(
        f"note {doc} in {collection}: the write was accepted but the engine refused to stamp it "
        f"complete: {exc}", detail=str(exc))


def _settle_after_error(
    result: NoteWriteResult, expected: list[tuple[int, str]], exc: Exception, *,
    recorder: _AttemptRecorder, cat: Any, rows: list[dict], chunks: list[dict],
    content_hash: str | None,
) -> NoteWriteResult:
    """The request failed: decide what happened, from EVERY attempt's error and, if any was in
    flight, the manifest.

    1. No attempt was in flight (only 4xx refusals and connections never made): confirmed not landed.
    2. In flight and the manifest does not show the note: unknown (it may still commit).
    3. In flight and the manifest already equals the note's rows: the content is there but the request
       may never have committed, so changed tags, ttl or category on unchanged content are not
       applied yet. Resend the same idempotent request once; its answer is the note's outcome (a
       resend that fails is unknown). A concurrent writer of the document that landed between the
       manifest read and the resend is overwritten: the last request wins.
    """
    doc = result.catalog_doc_id
    kinds = [_classify(e) for e in recorder.errors]
    if _IN_FLIGHT not in kinds and _classify(exc) != _IN_FLIGHT:
        raise NoteWriteError(
            catalog_doc_id=doc, collection=result.collection, reason=str(exc),
            manifest_empty=_manifest_is_empty(doc),
            refusal=_refusal_of([*recorder.errors, exc])) from exc
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
        "note_write_in_flight_manifest_matches", doc_id=doc, collection=result.collection,
        error=str(exc)[:300])
    try:
        out = write_one_request(
            cat, doc_id=doc, collection=result.collection, rows=rows, chunks=chunks,
            content_hash=content_hash or None, sweep=True, dropped="optional")
    except IndexRunVerifyRefused as refused:
        raise _stamp_refused(doc, result.collection, refused) from refused
    except Exception as resend_exc:  # noqa: BLE001 — the note's content is there; whether its metadata and stamp were applied is not known
        raise ManifestVerifyUncertainError(
            f"note {doc} in {result.collection} is in the manifest, but resending the request to apply "
            f"its metadata failed: {resend_exc}") from resend_exc
    _absorb(result, out)
    result.recovered = True
    result.dropped_chashes = None      # the first attempt's drop list is lost; a resend reads an empty diff
    return result


# ── the caller protocol ──────────────────────────────────────────────────────

STORED = "stored"
NOT_LANDED = "not-landed"
UNCERTAIN = "uncertain"
NO_CATALOG = "no-catalog"


@dataclass
class PutNoteOutcome:
    """What :func:`put_note` did, for a producer to word its own message from.

    ``status`` is one of :data:`STORED`, :data:`NOT_LANDED` (no piece of the note was added and its
    manifest is unchanged; the catalog row this call minted is removed or the identity stamp it
    changed is put back; retry is safe), :data:`UNCERTAIN` (the note may have landed; nothing was
    rolled back) and :data:`NO_CATALOG` (registration produced no document, so nothing was written
    at all). ``reason`` carries the underlying message for the last three. ``stamp_refused`` is set
    on an :data:`UNCERTAIN` outcome whose cause is known: the engine accepted the write and refused
    the completion stamp (``stamp_detail`` is its refusal text); the document stays ``indexing``.
    ``write`` is the :class:`NoteWriteResult` when stored. ``refusal`` is set on a :data:`NOT_LANDED`
    outcome to who refused (:data:`REFUSED_BY_CLIENT`, :data:`REFUSED_UNREACHABLE`,
    :data:`REFUSED_BY_ENGINE`); ``unstamped`` on an :data:`UNCERTAIN` outcome whose write landed and
    whose completion stamp was not applied, without an engine refusal to name.
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
    stamp_refused: bool = False
    stamp_detail: str = ""
    refusal: str = ""
    unstamped: bool = False

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
       On :class:`StampRefusedError` (the engine accepted the write and refused the completion
       stamp): UNCERTAIN with ``stamp_refused`` set, and NO ``_fence_fail``: the writer's rule
       leaves the fence ``indexing`` so no failed-document heal runs; the refusal was recorded by
       :func:`write_note`, and nothing is rolled back.
       On any other :class:`~nexus.catalog.store_hook.ManifestVerifyUncertainError`, or a landed
       note the fence could not be told about: ``_fence_fail`` and nothing else, since the note may
       exist.
       On any other exception: ``_fence_fail``, remove a minted row, re-raise.

    *collection* is the full T3 collection name. Raises ``PutOversizedError`` for an over-quota
    note and ``ValueError`` for empty *content* or a bad ``ttl_days``, all before any side effect.
    """
    from nexus.catalog import store_hook as sh  # noqa: PLC0415 — module attribute lookup at call time: callers and tests patch these names

    if ttl_days is not None and ttl_days <= 0:
        raise ValueError(
            f"ttl_days={ttl_days} is invalid: omit the argument or pass None for a permanent entry "
            "— ttl_days must be a positive integer number of days (0 does NOT mean permanent; None does)")
    if not content:
        raise ValueError("put_note: 'content' is required (an empty note has nothing to store)")
    pieces = sh.note_pieces(content, collection)
    first_chash, manifest_metadatas = sh.note_manifest_metadata(pieces)
    sh.raise_if_oversized(content, doc_id=first_chash, collection=collection)
    out = PutNoteOutcome(
        status=NO_CATALOG, collection=collection, pieces=pieces, manifest_metadatas=manifest_metadatas,
        chunk_ids=[m.get("chunk_text_hash", "") for m in manifest_metadatas])

    pre_call: dict[str, str] = {}
    cause: dict[str, str] = {}
    try:
        out.catalog_doc_id, out.minted = sh.catalog_store_hook_tracked(
            title=title, doc_id=first_chash, collection_name=collection, pre_call_doc_id_out=pre_call,
            error_out=cause)
    except Exception as exc:  # noqa: BLE001 — boundary catch; failure surfaced via log.warning and the NO_CATALOG outcome
        cause["error"] = f"{type(exc).__name__}: {exc}"
        _log.warning("catalog_store_hook_failed", doc_id=first_chash, collection=collection, exc_info=True)
    if not out.catalog_doc_id:
        # catalog_store_hook_tracked swallows every exception into ("", False) and hands the cause back
        # through error_out: a caller that only said "catalog registration failed" left the operator
        # to find it in a log.
        out.reason = f"catalog registration failed: {cause.get('error') or 'no cause was reported'}"
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
            raise _UnstampedError(f"note {doc} in {collection} landed but was not stamped complete")
    except StampRefusedError as exc:
        # The writer's rule: a refused stamp leaves the fence `indexing` (no _fence_fail, so no
        # failed-document heal); it is recorded and reported as unknown.
        out.status, out.reason = UNCERTAIN, str(exc)
        out.stamp_refused, out.stamp_detail = True, exc.detail
        _log.warning(
            "store_put_stamp_refused", doc_id=out.doc_id, catalog_doc_id=doc, collection=collection,
            error=out.reason[:300])
        return out
    except ManifestVerifyUncertainError as exc:
        out.status, out.reason = UNCERTAIN, str(exc)
        out.unstamped = isinstance(exc, _UnstampedError)
        _fence_fail(doc, out.reason)
        # No exc_info, like the NOT_LANDED line below: the CLI prints these warnings to the
        # operator's terminal and the caller words the outcome itself. The cause chain keeps what
        # the traceback carried for a reader of the log.
        _log.warning(
            "store_put_manifest_verify_uncertain", doc_id=out.doc_id, catalog_doc_id=doc,
            collection=collection, error=out.reason[:300], cause_chain=_cause_chain(exc))
        return out
    except NoteWriteError as exc:
        out.status, out.reason, out.refusal = NOT_LANDED, exc.reason, exc.refusal
        _fence_fail(doc, out.reason)
        # No exc_info: a definitive refusal is a normal outcome the caller words itself (the CLI
        # prints this line to the operator's terminal), and the reason names what was refused. The
        # cause chain keeps what the traceback carried for a reader of the log.
        _log.warning(
            "store_put_note_write_failed", doc_id=out.doc_id, catalog_doc_id=doc,
            collection=collection, manifest_empty=exc.manifest_empty, refusal=exc.refusal,
            error=out.reason[:300], cause_chain=_cause_chain(exc))
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


# ── the words and the firing every producer shares ───────────────────────────


def _sentence(text: str) -> str:
    """*text* ended with a full stop, so a reason that does not end in one still reads as a sentence."""
    text = text.rstrip()
    return text if text.endswith((".", "!", "?")) else text + "."


def failure_message(outcome: PutNoteOutcome, *, subject: str, check: str = "") -> str | None:
    """The one wording of a note write that did not store, or ``None`` when it stored.

    Every producer of a note (``nx store put``, ``nx memory promote``, the recovery import and MCP
    ``store_put``) used to word each outcome itself and the four copies drifted. They now add only what
    is theirs: a prefix (``Error: store_put: ``), a suffix (promote's "The T2 entry is unchanged.") and
    *subject*, how they name the note (``notes.md``, ``content``, ``'title'``). *check* is how the
    operator looks for a note that may have landed (``nx store list``, ``store_get``).

    The message table:

    ====================================  ============================================================
    outcome                               says
    ====================================  ============================================================
    NO_CATALOG                            could not catalog, nothing written, and the cause
    NOT_LANDED, refused by the client     the client's own reason FIRST (it carries the remedy), then
                                          that nothing was sent and nothing changed, and to run it
                                          again once fixed. Never "retry is safe" (a retry fails the
                                          same way until the operator acts), "no chunk was left
                                          behind" or "could not catalog"
    NOT_LANDED, connection never made     could not store; the engine could not be reached, nothing was
                                          sent and nothing changed; retry once it is running
    NOT_LANDED, refused by the engine     could not store; not stored, no chunk left behind, earlier
                                          version unchanged, metadata-refresh qualifier, retry is safe
    UNCERTAIN, in flight                  could not confirm it landed; nothing rolled back; may already
                                          have succeeded; check before retrying
    UNCERTAIN, stamp refused              wrote it and the engine accepted it but refused to stamp it
                                          complete; stays 'indexing'; nothing rolled back
    UNCERTAIN, landed but unstamped       wrote it, the document was not stamped complete; nothing
                                          rolled back; a retry is an idempotent re-write
    any other status                      unrecognised state; nothing confirmed stored
    ====================================  ============================================================

    The metadata-refresh qualifier ("chunks whose text was already stored may have had their metadata
    refreshed") appears only where it can be true: a refresh commits inside a request the engine
    received, so it is the engine-refusal row's alone.
    """
    if outcome.status == STORED:
        return None
    col, doc_id, reason = outcome.collection, outcome.doc_id, outcome.reason
    if outcome.status == NO_CATALOG:
        return (
            f"could not catalog {subject} in {col}: {reason}. Nothing was written: a note is written "
            "together with its catalog entry, never without one.")
    if outcome.status == NOT_LANDED:
        if outcome.refusal == REFUSED_BY_CLIENT:
            return (
                f"{_sentence(reason)} Nothing was sent to the engine and nothing changed; run the "
                "command again once that is fixed.")
        if outcome.refusal == REFUSED_UNREACHABLE:
            return (
                f"could not store {subject} in {col}: {reason}. The engine could not be reached, so "
                "nothing was sent and nothing changed; retry once it is running.")
        return (
            f"could not store {subject} in {col}: {reason}. The note was not stored: its chunks and "
            "its catalog entry go in one request, so no chunk was left behind and any earlier version "
            "of the note is unchanged (chunks whose text was already stored may have had their "
            "metadata refreshed); retry is safe.")
    if outcome.status == UNCERTAIN:
        if outcome.stamp_refused:
            return (
                f"wrote {doc_id} to {col} and the engine accepted the write, but it refused to stamp "
                f"the document complete ({outcome.stamp_detail}). The document stays 'indexing'. "
                "Nothing was rolled back; a retry is an idempotent re-write.")
        if outcome.unstamped:
            return (
                f"wrote {doc_id} to {col}, but the document was not stamped complete ({reason}). "
                "Nothing was rolled back; a retry is an idempotent re-write.")
        look = f"check with {check}" if check else "check"
        return (
            f"could not confirm that {subject} landed in {col}: {reason}. Nothing was rolled back: the "
            f"write may already have succeeded; {look} before retrying (a retry is an idempotent "
            "re-write either way).")
    return (
        f"the write of {subject} to {col} ended in an unrecognised state ({outcome.status!r}); nothing "
        "was confirmed stored.")


def fire_note_chains(outcome: PutNoteOutcome, content: str, *, hooks: Any = None) -> None:
    """Fire the three post-store chains for a note :func:`put_note` STORED, the one way.

    The sequence MCP ``store_put`` established (RDR-223 P2.2, nexus-z0o2p.12) and ``nx store put``,
    ``nx memory promote`` and the recovery import each hand-copied: ``fire_single`` per piece, one
    ``fire_batch`` over every piece, and ``fire_document`` once with the whole *content*, carrying the
    CATALOG tumbler (nexus-w8lg1: the aspect queue's composite FK), never a chunk id. The manifest and
    the completion stamp rode the write request, so the batch chain skips the manifest hook, the same
    skip the flush-grain combined write makes. Per-hook failures are isolated by the registry.

    *hooks* is the caller's registry (MCP keeps a process-local one); without it a default registry is
    built, as every CLI path did. Only a stored note fires: any other *outcome* raises ``ValueError``
    before a hook runs, so a producer cannot fire for a write that did not land.
    """
    if outcome.status != STORED:
        raise ValueError(
            f"fire_note_chains: only a stored note fires its chains, this one is {outcome.status!r}")
    from nexus.mcp_infra import manifest_write_batch_hook  # noqa: PLC0415 — deferred: mcp_infra imports back into catalog code

    if hooks is None:
        from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — deferred to avoid an import cycle and CLI startup cost

        hooks = HookRegistry()
        install_default_hooks(hooks)
    col, pieces, doc_ids = outcome.collection, outcome.pieces, outcome.chunk_ids
    for piece_id, piece in zip(doc_ids, pieces, strict=True):
        hooks.fire_single(piece_id, col, piece)
    hooks.fire_batch(
        doc_ids, col, pieces, None, outcome.manifest_metadatas,
        catalog_doc_id=outcome.catalog_doc_id, skip_hooks={manifest_write_batch_hook},
    )
    hooks.fire_document(doc_ids[0], col, content, doc_id=outcome.catalog_doc_id)
