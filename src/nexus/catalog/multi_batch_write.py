# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one writer for a document larger than one request (RDR-223, bead nexus-z0o2p.10).

Every multi-batch path (streaming PDF, ``_index_document``, the ``nx index repo`` oversize
fallbacks, ``nx store put``, ``nx memory promote``, import) writes its document through
:class:`MultiBatchDocumentWriter` so that a chunk never lands without an owner row and a killed
client leaves a document that is whole up to some batch, never chunks without an owner.

The protocol (RDR-223 Technical Design 1):

* **One request** (one batch that fits the chunk cap): one ``write_manifest_many`` carrying the
  chunks, sweep on, and the completion stamp riding the same call (``write_many`` stamps the
  document complete against that request's own row count). The fence is optional here. A FENCED
  single request begins with the snapshot too (below) and reports its drop list from it, so a
  resent ``write_many`` still reports correctly; an UNFENCED one (a note with no ``content_hash``)
  reports the ``write_many`` response's own list, which can under-report after a resend (the
  resend reads the manifest its first attempt replaced). The sweep itself runs server-side either
  way.
* **Several requests** (a second batch, or one batch larger than the chunk cap). ``content_hash``
  is REQUIRED: the fence is what keeps a stale ``complete`` stamp off a half-replaced manifest.

  1. ``begin_index_run(snapshot_manifest=True)``: the fence, and the document's PRE-RUN manifest
     (distinct chashes in position order, and the row count), read by the engine in the same
     transaction as the stamp. ``write_manifest_many`` never clears a prior ``complete``, so
     without the fence a rerun that dies partway would leave the old stamp on a half-replaced
     document and the next run would skip it. The snapshot, not the first batch's response, is
     the source of the deferred sweep: a first batch whose response is lost and resent reads the
     manifest its first attempt already replaced and would report nothing dropped.
  2. Batch 1: ``write_manifest_many`` with the chunks, sweep OFF and NO ``complete``. A sweep on a
     first batch would delete the previous run's chunks before the later batches land.
  3. Batches 2..N: ``append_manifest_chunks`` with the chunks inline: chunk rows and owner rows in
     one transaction.
  4. The LAST append carries the drop list (snapshot chashes minus every chash this run wrote, so
     an unchanged re-index sends no sweep at all) as ``sweep_chashes``, at most 300 per request; a
     longer list continues in trailing sweep-only appends. Nothing is ever swept before the last
     data batch has landed. The list is hedged against a concurrent writer: the snapshot UNION the
     drop list batch 1's response reported (when it carries one), minus what the run wrote.
  5. ``complete_index_run(doc_id, content_hash, manifest ROW count)``, after the last append and
     its sweeps. The engine compares the count with ``count(*)`` over the manifest rows, so a
     chash used at two positions counts twice; positions are unique per run (the writer refuses a
     repeat), so the row count is the number of distinct positions written. Batch 1 carried no
     ``complete``, so a client killed after batch 1 leaves a document that is not stamped and is
     replaced by the next run.

A crash at any point leaves every chunk this run wrote with an owner row. What a crash before the
last append leaves is the previous run's dropped chunks, ownerless and hidden from search. Nothing
on the client removes them, and the RDR-192 reaper (nexus-2x9xa, not built) is scoped to
``knowledge__`` collections, so for ``docs__``/``code__``/``rdr__`` they stay until ``nx t3 gc``;
nexus-2x9xa carries the coverage decision. That is the accepted cost of not sweeping early. A RERUN
sweeps what its own snapshot shows: the crashed run's chunks that are in the manifest (the crash
replaced the manifest with batches 1..k) and are absent from the rerun, but NOT the tail of the run
before the crash, which the crashed run's batch 1 already dropped from the manifest and the rerun's
snapshot therefore no longer contains.

One request, one body of code: :func:`write_one_request` (the ``write_manifest_many`` for one document
and the judgement of its answer: failed document, chunks dropped as unreferenced, a refused stamp,
the dropped-list checks, the retry) and :func:`complete_document` (the stamp, retried) are module
functions. This writer's single-request and multi-request paths call them, and so does the note
writer (:mod:`nexus.catalog.note_write`), which sends one request per note and adds only what a
note needs on top: settling a request that died in flight from the manifest.

Batches are buffered one deep: whether a batch is the first of several, the only one, or the last
is not known until the next batch arrives or :meth:`finish` is called. ``finish(allow_empty=True)``
with no batch at all writes an EMPTY manifest (``write_manifest_many`` with no rows, sweep on,
completion count 0 when fenced): a re-index that yields no chunks must still clear the old
manifest. A bare ``finish()`` with no batch raises, so a caller that lost its batches to a bug does
not silently wipe the document.

Every combined-write request is clamped to ``min(per_collection_chunk_cap(collection), 300)``
chunks, whatever ``NX_ONNX_LOCAL_UPSERT_CHUNK_CAP`` says: ``append_many`` caps chunks at 300 on the
engine and ``append`` follows, and the per-collection cap is the embed-memory / gateway-timeout
bound every other writer obeys. A batch larger than that is split into consecutive requests, the
chunk payload of each carrying only the chunks its own rows reference that no earlier request of
this run already carried.

``defer_completion=True`` (nexus-z0o2p.11, the streaming PDF pipeline) runs everything above EXCEPT
the completion stamp: ``finish()`` leaves the run ``indexing`` and the caller stamps it later with
:meth:`complete`. The streaming pipeline enriches every chunk's metadata after the last chunk has
landed (title, author, extraction method) and must not have the document stamped complete before
that pass succeeded: a process killed between the stamp and the pass would leave a document that
looks complete and is missing its enrichment, and the next run would skip it. A single-request
document then writes its ``write_many`` with no ``complete`` and :meth:`complete` sends
``complete_index_run``, the same stamp a multi-request document ends with.

Only write ops are used, so ``cat`` may be the ``get_catalog_writer()`` proxy (the closed
``CATALOG_WRITE_OPS`` whitelist); the writer never reads the catalog. A request that fails
propagates its exception unchanged (a killed process is the model) and poisons the writer;
:meth:`abort` marks the fence failed for a caller that survives the failure, and the writer is a
context manager that does so on an exception.

Every request (the fence begin, the first or only ``write_manifest_many``, every append and
sweep-only append, the completion stamp) goes through ``nexus.retry``'s manifest-write retry: a
bounded number of attempts on connectivity errors, and a rate-limit answer (429, or a 503 with
Retry-After) trips the shared ``RateLimitBrake`` so every writer in the process backs off together.
A ``CombinedWriteEmbedTimeoutError`` is never retried (a retry would start an uncancelled duplicate
embed). A resent first ``write_manifest_many`` is safe for a multi-request document because its
sweep comes from the begin snapshot; for an UNFENCED single request the resend makes the response's
own drop list empty, so ``DocumentWriteResult.dropped`` can under-report there (the sweep itself ran
server-side on the first attempt).

The fence after a failure. ``abort()`` (and the ``with`` block) marks the run ``failed`` EXCEPT
when the completion stamp already succeeded (nothing to undo) or the engine REFUSED the stamp
(``IndexRunVerifyRefused``): a refusal leaves ``index_state`` exactly as ``begin`` left it,
``indexing`` (IndexRunFenceTest pins that engine contract, and ``doc_indexer._fence_complete`` lets
the refusal propagate past every ``_fence_fail`` site for the same reason), and the writer RECORDS
it in ``mcp_infra``'s refusal collector, the one the record-level summary reads. ``abort()`` acts at
most once, and a caller that already failed the fence itself calls :meth:`mark_fence_handled` so the
``with`` block does not fail it again.

ONE WRITER PER DOCUMENT AT A TIME, and a writer is not thread-safe: there is no lock, on the
document or in the writer. Two writers on one document interleave their manifests. The fence is a
record of intent, not a lock, and it begins with the writer's FIRST request, which for a streaming
caller is after extraction and the first chunk batch: what keeps two runs of one document apart is
the caller's own claim (the streaming pipeline's row is refused while another run's heartbeat is
fresh), not the catalog fence. No chunk is written outside the fence: the begin precedes the first
data request.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import structlog

from nexus.catalog.http_catalog_client import MANIFEST_APPEND_SWEEP_CHASHES_CAP
from nexus.db.limits import QUOTAS
from nexus.errors import BatchWriteFailedError, EngineOlderThanClientError, IndexRunVerifyRefused

__all__ = [
    "BatchWriteFailedError",
    "DocumentFailedError",
    "DocumentWriteResult",
    "MultiBatchDocumentWriter",
    "OneRequestResult",
    "RepeatedPositionError",
    "complete_document",
    "first_chunk_per_chash",
    "retrying",
    "write_document",
    "write_one_request",
]

_log = structlog.get_logger(__name__)


# ── the one-request primitives ───────────────────────────────────────────────
#
# One ``write_manifest_many`` for one document, and the completion stamp, judged once. The writer's
# single-request path and the note writer (:mod:`nexus.catalog.note_write`) both call these, so the
# checks on the engine's answer cannot drift between them: the failed-document check, the
# unreferenced-chunk check, the refused-stamp mapping, the dropped-list validation and the retry.


class DocumentFailedError(BatchWriteFailedError):
    """The engine answered a write with the document in ``failed_doc_ids``: its per-document
    transaction rolled back, so none of the request landed. A subclass of
    :class:`BatchWriteFailedError` so the writer's callers see one type; the note writer tells it
    apart because "the engine said no" is a confirmed failure while any other bad answer is not."""


def retrying(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call a request with nexus.retry's bounded retry: connectivity errors, and a rate-limit
    answer paces the shared brake. A ``CombinedWriteEmbedTimeoutError`` has no transport error in
    its chain, so it is not retried (a retry would start an uncancelled duplicate embed)."""
    from nexus.retry import _manifest_write_with_retry  # noqa: PLC0415 — deferred: nexus.retry pulls in the rate brake
    return _manifest_write_with_retry(fn, *args, **kwargs)


def first_chunk_per_chash(chunks: Sequence[dict]) -> dict[str, dict]:
    """``{chash: chunk}``, FIRST occurrence wins. A document that repeats a chunk's text under
    different metadata writes one chunk, and the old ``upsert`` and the ChunkBatcher both keep the
    first; a last-wins dict here would silently change which metadata a repeated chunk carries."""
    out: dict[str, dict] = {}
    for c in chunks:
        out.setdefault(c["chash"], c)
    return out


def check_unreferenced(resp: dict, *, doc_id: str, batch: int, required: bool) -> None:
    """The engine counts payload chunks no row of the request referenced and drops them (neither
    embedded nor inserted). The writer sends only referenced chunks, so any count is content that
    silently did not land. ``append`` and ``append_many`` always report it; a response without it
    there is an engine that does not know the check, not a zero."""
    if "chunks_unreferenced" not in resp:
        if required:
            raise BatchWriteFailedError(
                doc_id=doc_id, batch=batch,
                reason="the append response carried no 'chunks_unreferenced'; "
                       "the engine cannot say whether every chunk was written")
        return
    n = int(resp["chunks_unreferenced"] or 0)
    if n:
        raise BatchWriteFailedError(
            doc_id=doc_id, batch=batch,
            reason=f"the engine dropped {n} chunk(s) as referenced by no row of the request")


@dataclass
class OneRequestResult:
    """The judged answer to one :func:`write_one_request`.

    ``dropped`` / ``dropped_count`` are the chashes the write dropped from the document's previous
    manifest (the list the sweep ran on) and their number; ``dropped_unknown`` is True when the
    engine could not read the previous manifest. ``completed`` is True when the completion stamp
    was asked for and not refused.
    """

    resp: dict
    chunks_written: int = 0
    embed_embedded: int = 0
    embed_skipped: int = 0
    chunks_deduped: int = 0
    swept: int = 0
    sweep_skipped: int = 0
    dropped: list[str] | None = None
    dropped_count: int | None = None
    dropped_unknown: bool = False
    completed: bool = False


def write_one_request(
    cat: Any,
    *,
    doc_id: str,
    collection: str,
    rows: list[dict],
    chunks: list[dict],
    content_hash: str | None = None,
    sweep: bool = True,
    embedding_model: str | None = None,
    force_re_embed: bool = False,
    batch: int = 1,
    dropped: str = "required",
    metadata_merge: bool = False,
) -> OneRequestResult:
    """Send ONE ``write_manifest_many`` for *doc_id* and judge the answer.

    Raises :class:`DocumentFailedError` when the engine names the document in ``failed_doc_ids``,
    :class:`BatchWriteFailedError` when the answer cannot be trusted (chunks dropped as
    unreferenced; in ``dropped="required"`` mode a missing or inconsistent drop list), and
    :class:`~nexus.errors.IndexRunVerifyRefused` when the engine refused the completion stamp (the
    rows and chunks committed). Anything the request itself raises propagates unchanged.

    *content_hash* rides the same request as the completion stamp. *dropped* is ``"required"`` (the
    response must carry a ``dropped_chashes`` and ``dropped_count`` entry for the document that agree,
    or a ``dropped_unknown`` marker) or ``"optional"`` (take the list if the response has one).

    *metadata_merge* asks the engine to MERGE a stored chunk's metadata with the incoming one
    (``stored || incoming``) instead of replacing it; it is sent only when True, so a catalog writer
    that wraps this call and adds the mode itself (``MetadataMergingCatalog``) is unaffected.
    """
    chunks = list(first_chunk_per_chash(chunks).values())
    mode = {"metadata_merge": True} if metadata_merge else {}
    resp = retrying(
        cat.write_manifest_many,
        [(doc_id, rows)], complete={doc_id: content_hash} if content_hash is not None else None,
        sweep=sweep, chunks=chunks or None, collection=collection,
        force_re_embed=force_re_embed, embedding_model=embedding_model, **mode)
    resp = resp if isinstance(resp, dict) else {}
    if doc_id in (resp.get("failed_doc_ids") or ()):
        raise DocumentFailedError(
            doc_id=doc_id, batch=batch, reason="the engine reported the document in failed_doc_ids")
    out = OneRequestResult(
        resp=resp,
        chunks_written=int(resp.get("chunks_written") or 0),
        embed_embedded=int(resp.get("embed_embedded") or 0),
        embed_skipped=int(resp.get("embed_skipped") or 0),
        chunks_deduped=int(resp.get("chunks_deduped") or 0),
        swept=int(resp.get("swept") or 0),
        sweep_skipped=int(resp.get("sweep_skipped") or 0),
    )
    check_unreferenced(resp, doc_id=doc_id, batch=batch, required=False)
    for refused in resp.get("complete_refused") or ():
        if refused.get("doc_id") == doc_id:
            referenced = int(refused.get("referenced") or 0)
            missing = int(refused.get("missing") or 0)
            raise IndexRunVerifyRefused(
                doc_id=doc_id, referenced=referenced, present=referenced - missing,
                missing=missing, chunk_count=int(refused.get("chunk_count") or 0))
    out.completed = content_hash is not None
    dropped_map = resp.get("dropped_chashes")
    if doc_id in (resp.get("dropped_unknown") or ()):
        out.dropped_unknown = True
    elif dropped == "optional":
        if isinstance(dropped_map, dict) and isinstance(dropped_map.get(doc_id), list):
            out.dropped = [str(c) for c in dropped_map[doc_id]]
    else:
        counts = resp.get("dropped_count")
        if (not isinstance(dropped_map, dict) or doc_id not in dropped_map
                or not isinstance(counts, dict) or doc_id not in counts):
            # An engine that predates the drop list answers without it. Not a corrupt answer: the
            # remedy is an upgrade, so say so instead of failing the same cryptic way per record.
            raise EngineOlderThanClientError(
                f"multi-batch write of {doc_id!r} failed at batch {batch}: the write_many response "
                "carried neither dropped_chashes and dropped_count entries nor a dropped_unknown "
                "marker for the document")
        listed = list(dropped_map[doc_id] or ())
        if int(counts[doc_id]) != len(listed):
            raise BatchWriteFailedError(
                doc_id=doc_id, batch=batch,
                reason=f"dropped_count says {counts[doc_id]} but dropped_chashes "
                       f"lists {len(listed)}; the list is truncated or corrupt")
        out.dropped = listed
        out.dropped_count = len(listed)
    return out


def complete_document(
    cat: Any, *, doc_id: str, content_hash: str, manifest_rows: int, batch: int = 1,
) -> None:
    """Ask the engine to stamp *doc_id* complete (retried like every idempotent request).

    *manifest_rows* is the row count the engine verifies against ``count(*)`` over the manifest:
    positions written, not distinct chashes (a chash used at two positions counts twice). Raises
    :class:`~nexus.errors.IndexRunVerifyRefused` when the engine refuses, and
    :class:`BatchWriteFailedError` when it has no index-run fence route (the document was NOT
    stamped)."""
    done = retrying(cat.complete_index_run, doc_id, content_hash, manifest_rows)
    if done is None:
        raise BatchWriteFailedError(
            doc_id=doc_id, batch=batch,
            reason="complete_index_run answered 404: the engine has no index-run fence route, "
                   "so the document was NOT stamped")


class RepeatedPositionError(ValueError):
    """A position was written twice in one run.

    A revisited position with a different chash drops the old chash from the manifest outside
    ``sweep_chashes`` (the drop list is computed from the pre-run manifest, not this run's earlier
    batches), so the writer refuses the batch before sending it.
    """


@dataclass
class DocumentWriteResult:
    """What one :meth:`MultiBatchDocumentWriter.finish` did.

    ``batches`` counts data requests (a batch larger than the chunk cap is several); ``requests``
    adds the sweep-only appends (not the fence or the completion stamp). ``distinct_chashes`` is
    the number of distinct chashes the run wrote; ``manifest_rows`` the row count the completion
    stamp claims.

    ``dropped`` and ``dropped_count`` are the chashes the write dropped from the document's
    previous manifest, and their number. One request: the ``write_many`` response's own
    ``dropped_chashes`` / ``dropped_count`` (the list the sweep ran on). Several requests: the
    swept set, i.e. the pre-run snapshot (plus batch 1's own list, when it carried one) minus what
    the run wrote. A fenced one-request write reports the same way; an unfenced one reports the
    ``write_many`` response and can under-report after a resend. ``dropped_unknown`` is True (and
    both are ``None``) when the engine could not read the previous manifest of an unfenced
    one-request write, so the drop list does not exist.
    """

    batches: int = 0
    requests: int = 0
    chunks_written: int = 0
    embed_embedded: int = 0
    embed_skipped: int = 0
    chunks_deduped: int = 0
    swept: int = 0
    sweep_skipped: int = 0
    distinct_chashes: int = 0
    manifest_rows: int = 0
    dropped: list[str] | None = None
    dropped_count: int | None = None
    dropped_unknown: bool = False
    completed: bool = False


Batch = tuple[Sequence[dict], Sequence[dict]]


class MultiBatchDocumentWriter:
    """Write one document as N batches with the RDR-223 protocol. See the module docstring.

    *cat* is a catalog writer (``get_catalog_writer()`` or ``HttpCatalogClient``). *content_hash*
    turns on the index-run fence and the completion stamp; it is required as soon as the document
    takes more than one request. Without it the caller stamps (or not). With *defer_completion*
    the writer never stamps: :meth:`complete` does, after :meth:`finish`.
    """

    def __init__(
        self,
        cat: Any,
        *,
        doc_id: str,
        collection: str,
        content_hash: str | None = None,
        run_id: str | None = None,
        embedding_model: str | None = None,
        force_re_embed: bool = False,
        chunk_cap: int | None = None,
        defer_completion: bool = False,
    ) -> None:
        if not doc_id:
            raise ValueError("MultiBatchDocumentWriter: 'doc_id' is required")
        if not collection:
            raise ValueError("MultiBatchDocumentWriter: 'collection' is required")
        if content_hash is not None and not content_hash:
            raise ValueError("MultiBatchDocumentWriter: 'content_hash' must be non-empty when given")
        if defer_completion and content_hash is None:
            raise ValueError(
                "MultiBatchDocumentWriter: defer_completion needs a 'content_hash' (it is the "
                "completion stamp that is deferred)")
        if chunk_cap is None:
            from nexus.db.http_vector_client import per_collection_chunk_cap  # noqa: PLC0415 — deferred: the vector client imports back into catalog code
            chunk_cap = per_collection_chunk_cap(collection)
        if chunk_cap < 1:
            raise ValueError(f"MultiBatchDocumentWriter: chunk_cap must be positive, got {chunk_cap}")
        #: The most chunks (and rows) one request carries.
        self._cap = min(chunk_cap, QUOTAS.MAX_RECORDS_PER_WRITE)
        self._cat = cat
        self._doc_id = doc_id
        self._collection = collection
        self._content_hash = content_hash
        self._run_id = run_id or uuid.uuid4().hex
        self._embedding_model = embedding_model
        self._force_re_embed = force_re_embed
        self._defer = defer_completion
        self._pending: tuple[list[dict], list[dict]] | None = None
        self._carried: set[str] = set()      # chashes whose chunk payload an earlier request carried
        self._sent = 0                       # data requests sent so far
        self._positions: set[int] = set()
        self._run_chashes: dict[str, None] = {}      # distinct, insertion-ordered
        self._prior: list[str] | None = None         # the pre-run manifest's distinct chashes
        self._batch1_dropped: list[str] = []         # batch 1's own drop list (a hedge, never the source)
        self._fenced = False
        self._failed = False
        self._finished = False
        self._refused = False        # the engine refused the completion stamp
        self._aborted = False        # the fence was already failed (by abort() or the caller)
        self._result = DocumentWriteResult()

    def __enter__(self) -> "MultiBatchDocumentWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            return
        if issubclass(exc_type, IndexRunVerifyRefused):
            # A refusal raised inside the block by the caller's own completion stamp: same policy
            # as a refusal the writer met itself.
            self._note_refusal()
            return
        self.abort(f"{exc_type.__name__}: {exc}")

    # ── public ────────────────────────────────────────────────────────────────

    def add_batch(self, rows: Sequence[dict], chunks: Sequence[dict]) -> None:
        """Add the next batch: *rows* are manifest rows (``chash`` and an explicit integer
        ``position``), *chunks* the ``{chash, text, metadata[, embedding]}`` payload those rows
        reference. Sends the previously buffered request (it is now known not to be the last).

        Raises ``ValueError`` BEFORE anything is sent when the document would take a second
        request and no ``content_hash`` was given."""
        self._require_usable()
        rows_l, by_chash = self._validate(rows, chunks)
        parts = -(-len(rows_l) // self._cap)
        if self._content_hash is None and (self._pending is not None or self._sent > 0 or parts > 1):
            raise ValueError(
                f"MultiBatchDocumentWriter.add_batch: {self._doc_id!r} would take more than one "
                "request (a second batch, or a batch over the chunk cap of "
                f"{self._cap}) and no 'content_hash' was given: the index-run fence is what keeps "
                "a stale 'complete' stamp off a half-replaced manifest")
        self._positions.update(r["position"] for r in rows_l)
        self._run_chashes.update(dict.fromkeys(r["chash"] for r in rows_l))
        for start in range(0, len(rows_l), self._cap):
            part = rows_l[start:start + self._cap]
            payload: list[dict] = []
            for r in part:
                c = by_chash.get(r["chash"])
                if c is not None and r["chash"] not in self._carried:
                    self._carried.add(r["chash"])
                    payload.append(c)
            if self._pending is not None:
                self._send_pending(last=False)
            self._pending = (part, payload)

    def finish(self, *, allow_empty: bool = False) -> DocumentWriteResult:
        """Send the last request, the deferred sweep and the completion stamp. With no batch at
        all this raises ``ValueError`` unless ``allow_empty=True``, which writes an empty manifest
        (sweep on, completion count 0 when fenced)."""
        self._require_usable()
        if self._pending is None:
            if not allow_empty:
                raise ValueError(
                    "MultiBatchDocumentWriter.finish: no batch was added; pass allow_empty=True "
                    "to write an empty manifest (which clears the document's old chunks)")
            self._pending = ([], [])
        try:
            self._send_pending(last=True)
        except BaseException:
            self._failed = True
            raise
        self._finished = True
        self._result.distinct_chashes = len(self._run_chashes)
        self._result.manifest_rows = len(self._positions)
        return self._result

    def complete(self) -> DocumentWriteResult:
        """Stamp the run complete. Only for a ``defer_completion`` writer, after :meth:`finish`.

        Raises :class:`~nexus.errors.IndexRunVerifyRefused` when the engine refuses the stamp
        (recorded for the run summary; the fence stays ``indexing``) and
        :class:`BatchWriteFailedError` when the engine has no fence route. A second call after the
        stamp landed does nothing."""
        if not self._defer:
            raise ValueError(
                "MultiBatchDocumentWriter.complete: only a defer_completion writer is stamped "
                "by the caller; this one stamps itself in finish()")
        if not self._finished:
            raise ValueError("MultiBatchDocumentWriter.complete: call finish() first")
        if self._result.completed:
            return self._result
        if self._failed:
            raise self._fail(self._sent, "an earlier request of this writer failed; the writer "
                                         "refuses to stamp the document")
        try:
            self._stamp(self._sent)    # complete_document + the refusal record
        except BaseException:
            self._failed = True
            raise
        return self._result

    def mark_fence_handled(self) -> None:
        """Tell the writer the caller already failed (or deliberately left) the index run, so
        :meth:`abort` and the ``with`` block do not fail it again."""
        self._aborted = True

    def abort(self, error: str) -> None:
        """Mark the index run failed, if a fence was begun. Best effort; for a caller that
        survives a failed write (a killed process needs nothing: the fence stays ``indexing``).

        A no-op once the stamp landed, after the engine refused the stamp (the fence stays
        ``indexing``, see the module docstring) and after the first call. A deferred-completion
        writer that finished but was never stamped is still open, so it is failed."""
        stamped = self._result.completed
        if (stamped or (self._finished and not self._defer) or self._refused or self._aborted
                or not self._fenced or self._content_hash is None):
            return
        self._aborted = True
        try:
            self._cat.fail_index_run(self._doc_id, error)
        except Exception as exc:  # noqa: BLE001 — best-effort fence marking must never mask the original failure
            _log.warning("multi_batch_abort_fail_index_run_failed", doc_id=self._doc_id, error=str(exc))

    # ── validation ────────────────────────────────────────────────────────────

    def _require_usable(self) -> None:
        if self._failed:
            raise BatchWriteFailedError(
                doc_id=self._doc_id, batch=self._sent,
                reason="an earlier request of this writer failed; the writer refuses more work")
        if self._finished:
            raise ValueError("MultiBatchDocumentWriter: already finished")

    def _validate(
        self, rows: Sequence[dict], chunks: Sequence[dict],
    ) -> tuple[list[dict], dict[str, dict]]:
        rows_l = [dict(r) for r in rows]
        chunks_l = list(chunks)
        if not rows_l:
            raise ValueError("MultiBatchDocumentWriter.add_batch: a batch needs at least one row")
        seen: set[int] = set()
        for i, r in enumerate(rows_l):
            pos = r.get("position")
            if isinstance(pos, bool) or not isinstance(pos, int) or pos < 0:
                raise ValueError(
                    f"MultiBatchDocumentWriter.add_batch: rows[{i}] needs an explicit "
                    f"non-negative integer 'position', got {pos!r}")
            if not r.get("chash"):
                raise ValueError(f"MultiBatchDocumentWriter.add_batch: rows[{i}] needs a 'chash'")
            if pos in seen or pos in self._positions:
                raise RepeatedPositionError(
                    f"position {pos} of {self._doc_id!r} is written twice in one run "
                    f"(rows[{i}], chash {r['chash'][:12]}...)")
            seen.add(pos)
        referenced = {r["chash"] for r in rows_l}
        for i, c in enumerate(chunks_l):
            if c.get("chash") not in referenced:
                raise ValueError(
                    f"MultiBatchDocumentWriter.add_batch: chunks[{i}] (chash "
                    f"{str(c.get('chash'))[:12]}...) is referenced by no row of this batch; "
                    "it would be written without an owner")
        return rows_l, first_chunk_per_chash(chunks_l)

    # ── requests ──────────────────────────────────────────────────────────────

    def _retrying(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """:func:`retrying`, for the requests this class sends itself."""
        return retrying(fn, *args, **kwargs)

    def _note_refusal(self) -> None:
        """The engine refused the completion stamp: record it for the record-level summary and
        leave the fence as ``begin`` left it."""
        if self._refused:
            return
        self._refused = True
        try:
            from nexus.mcp_infra import _record_complete_refusal  # noqa: PLC0415 — deferred: mcp_infra imports back into catalog code
            _record_complete_refusal(self._doc_id)
        except Exception as exc:  # noqa: BLE001 — recording is advisory; the refusal itself propagates
            _log.warning("multi_batch_refusal_record_failed", doc_id=self._doc_id, error=str(exc))

    def _fail(self, batch: int, reason: str) -> BatchWriteFailedError:
        return BatchWriteFailedError(doc_id=self._doc_id, batch=batch, reason=reason)

    def _begin_fence(self, *, snapshot: bool) -> None:
        if self._content_hash is None or self._fenced:
            return
        resp = self._retrying(
            self._cat.begin_index_run, self._doc_id, self._content_hash, self._run_id,
            self._collection, **({"snapshot_manifest": True} if snapshot else {}))
        if resp is None:
            raise self._fail(1, "the engine has no index-run fence route (begin_index_run "
                                "answered 404); a write without the fence is not safe")
        # The stamp is committed the moment begin answers, so an unusable snapshot below must
        # still be abort()-able (index_state 'failed', not left 'indexing').
        self._fenced = True
        if snapshot:
            prior = resp.get("prior_chashes")
            count = resp.get("prior_count")
            if (not isinstance(prior, list) or isinstance(count, bool)
                    or not isinstance(count, int) or count < len(prior)
                    or (prior and count == 0)):
                detail = ("begin_index_run(snapshot_manifest=True) returned no usable "
                          f"pre-run manifest (prior_chashes={type(prior).__name__}, "
                          f"prior_count={count!r})")
                if not isinstance(prior, list) or isinstance(count, bool) or not isinstance(count, int):
                    # The snapshot fields are absent or untyped: an engine that predates them.
                    raise EngineOlderThanClientError(
                        f"multi-batch write of {self._doc_id!r} failed at batch 1: {detail}")
                raise self._fail(1, detail)   # present but self-contradicting: a corrupt answer
            self._prior = [str(c) for c in prior]

    def _send_pending(self, *, last: bool) -> None:
        rows, chunks = self._pending  # type: ignore[misc]
        self._pending = None
        n = self._sent + 1
        try:
            if n == 1:
                self._begin_fence(snapshot=True)
                if last:
                    self._send_only_request(rows, chunks)
                else:
                    self._send_first_of_many(rows, chunks)
            else:
                self._send_append(n, rows, chunks, last=last)
        except BaseException:
            self._failed = True
            raise
        self._sent = n
        self._result.batches = n
        self._result.requests += 1

    def _account(self, resp: dict) -> None:
        r = self._result
        r.chunks_written += int(resp.get("chunks_written") or 0)
        r.embed_embedded += int(resp.get("embed_embedded") or 0)
        r.embed_skipped += int(resp.get("embed_skipped") or 0)
        r.chunks_deduped += int(resp.get("chunks_deduped") or 0)

    def _check_unreferenced(self, resp: dict, batch: int, *, required: bool) -> None:
        check_unreferenced(resp, doc_id=self._doc_id, batch=batch, required=required)

    def _write_many(
        self, rows: list[dict], chunks: list[dict], *, sweep: bool, content_hash: str | None,
        dropped: str,
    ) -> OneRequestResult:
        out = write_one_request(
            self._cat, doc_id=self._doc_id, collection=self._collection, rows=rows, chunks=chunks,
            content_hash=content_hash, sweep=sweep, embedding_model=self._embedding_model,
            force_re_embed=self._force_re_embed, batch=self._sent + 1, dropped=dropped)
        self._account(out.resp)
        return out

    def _send_only_request(self, rows: list[dict], chunks: list[dict]) -> None:
        try:
            out = self._write_many(
                rows, chunks, sweep=True,
                # A deferred-completion writer sends no stamp with the write: complete() does.
                content_hash=None if self._defer else self._content_hash,
                dropped="optional" if self._prior is not None else "required")
        except IndexRunVerifyRefused:
            self._note_refusal()
            raise
        self._result.swept += out.swept
        self._result.sweep_skipped += out.sweep_skipped
        self._result.completed = out.completed
        # The drop list the sweep ran on, for callers that report it (nexus-wbfpw.25).
        if self._prior is not None:
            # Fenced: the snapshot (union this response's own list) minus what the run wrote,
            # correct even when the transport resent the write_many.
            if out.dropped is not None:
                self._batch1_dropped = out.dropped
            dropped = self._sweep_list()
            self._result.dropped = dropped
            self._result.dropped_count = len(dropped)
            return
        if out.dropped_unknown:
            self._result.dropped_unknown = True
            return
        self._result.dropped = out.dropped
        self._result.dropped_count = out.dropped_count

    def _send_first_of_many(self, rows: list[dict], chunks: list[dict]) -> None:
        # Sweep OFF and no `complete`. The response's dropped_chashes is NOT the source of the
        # deferred sweep (the begin snapshot is): a resent first batch reads the manifest its
        # first attempt already replaced.
        out = self._write_many(rows, chunks, sweep=False, content_hash=None, dropped="optional")
        # A hedge for a concurrent writer that changed the manifest between the snapshot and this
        # write; on a resend it is empty, which is why it is never the source.
        if out.dropped is not None:
            self._batch1_dropped = out.dropped

    def _sweep_list(self) -> list[str]:
        """The pre-run snapshot, then anything batch 1's own response added, minus every chash
        this run wrote, de-duplicated, in order."""
        seen: set[str] = set()
        out: list[str] = []
        for c in [*(self._prior or ()), *self._batch1_dropped]:
            if c in self._run_chashes or c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out

    def _send_append(self, n: int, rows: list[dict], chunks: list[dict], *, last: bool) -> None:
        cap = MANIFEST_APPEND_SWEEP_CHASHES_CAP
        sweep = self._sweep_list() if last else []
        first, rest = sweep[:cap], sweep[cap:]
        resp = self._retrying(
            self._cat.append_manifest_chunks, self._doc_id, rows, collection=self._collection,
            chunk_payload=chunks or None, sweep_chashes=first or None,
            force_re_embed=self._force_re_embed, embedding_model=self._embedding_model)
        resp = resp if isinstance(resp, dict) else {}
        self._account(resp)
        self._check_unreferenced(resp, n, required=bool(chunks))
        self._result.swept += int(resp.get("swept") or 0)
        self._result.sweep_skipped += int(resp.get("sweep_skipped") or 0)
        if not last:
            return
        for i in range(0, len(rest), cap):
            sresp = self._retrying(
                self._cat.append_manifest_chunks, self._doc_id, [], collection=self._collection,
                sweep_chashes=rest[i:i + cap])
            sresp = sresp if isinstance(sresp, dict) else {}
            self._result.requests += 1
            self._result.swept += int(sresp.get("swept") or 0)
            self._result.sweep_skipped += int(sresp.get("sweep_skipped") or 0)
        self._result.dropped = sweep
        self._result.dropped_count = len(sweep)
        if self._content_hash is not None and not self._defer:
            self._stamp(n)

    def _stamp(self, batch: int) -> None:
        """The completion stamp, after the last data request and its sweeps."""
        # The engine compares this with count(*) over the manifest ROWS. Positions are unique
        # in a run, so that is the number of positions written, not the distinct chashes.
        try:
            complete_document(
                self._cat, doc_id=self._doc_id, content_hash=self._content_hash,
                manifest_rows=len(self._positions), batch=batch)
        except IndexRunVerifyRefused:
            self._note_refusal()
            raise
        self._result.completed = True


def write_document(
    cat: Any,
    batches: Iterable[Batch],
    *,
    doc_id: str,
    collection: str,
    content_hash: str | None = None,
    run_id: str | None = None,
    embedding_model: str | None = None,
    force_re_embed: bool = False,
    chunk_cap: int | None = None,
) -> DocumentWriteResult:
    """Write *batches* (``(rows, chunks)`` pairs) as one document; see
    :class:`MultiBatchDocumentWriter`. On a failure the fence is marked failed and the exception
    propagates."""
    with MultiBatchDocumentWriter(
        cat, doc_id=doc_id, collection=collection, content_hash=content_hash, run_id=run_id,
        embedding_model=embedding_model, force_re_embed=force_re_embed, chunk_cap=chunk_cap,
    ) as w:
        for rows, chunks in batches:
            w.add_batch(rows, chunks)
        return w.finish()
