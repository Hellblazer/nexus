# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one writer for a document larger than one request (RDR-223, bead nexus-z0o2p.10).

Every multi-batch path (streaming PDF, ``_index_document``, the ``nx index repo`` oversize
fallbacks, ``nx store put``, ``nx memory promote``, import) writes its document through
:class:`MultiBatchDocumentWriter` so that a chunk never lands without an owner row and a killed
client leaves a document that is whole up to some batch, never chunks without an owner.

The protocol (RDR-223 Technical Design 1):

* **One batch**: one ``write_manifest_many`` carrying the chunks, sweep on, and the completion
  stamp riding the same call (``write_many`` stamps the document complete against that request's
  own row count).
* **Several batches**:

  1. ``begin_index_run`` (when a ``content_hash`` is given): the fence. ``write_manifest_many``
     never clears a prior ``complete``, so without it a rerun that dies partway would leave the
     old stamp on a half-replaced document and the next run would skip it.
  2. Batch 1: ``write_manifest_many`` with the chunks, sweep OFF and NO ``complete``. A sweep on a
     first batch would delete the previous run's chunks before the later batches land. Its
     ``dropped_chashes`` (the previous manifest minus batch 1) are kept.
  3. Batches 2..N: ``append_manifest_chunks`` with the chunks inline: chunk rows and owner rows in
     one transaction.
  4. The LAST append carries the kept list as ``sweep_chashes``, minus every chash this run wrote
     (so an unchanged re-index sends no sweep at all), at most 300 per request; a longer list
     continues in trailing sweep-only appends. Nothing is ever swept before the last data batch
     has landed.
  5. ``complete_index_run(doc_id, content_hash, distinct chash count)``, after the last append and
     its sweeps. Batch 1 carried no ``complete``, so a client killed after batch 1 leaves a
     document that is not stamped and is replaced by the next run.

A crash at any point leaves every chunk this run wrote with an owner row. What a crash before the
last append leaves is the previous run's dropped chunks, ownerless and hidden until the RDR-192
reaper removes them; that is the accepted cost of not sweeping early.

Batches are buffered one deep: whether a batch is the first of several, the only one, or the last
is not known until the next batch arrives or :meth:`finish` is called.

Every combined-write request is clamped to ``min(per_collection_chunk_cap(collection), 300)``
chunks, whatever ``NX_ONNX_LOCAL_UPSERT_CHUNK_CAP`` says: ``append_many`` caps chunks at 300 on the
engine and ``append`` follows, and the per-collection cap is the embed-memory / gateway-timeout
bound every other writer obeys. A batch larger than that is split into consecutive requests, the
chunk payload of each carrying only the chunks its own rows reference that no earlier request of
this run already carried.

Only write ops are used, so ``cat`` may be the ``get_catalog_writer()`` proxy (the closed
``CATALOG_WRITE_OPS`` whitelist); the writer never reads the catalog. A request that fails
propagates its exception unchanged (a killed process is the model) and poisons the writer;
:meth:`abort` marks the fence failed for a caller that survives the failure.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

import structlog

from nexus.catalog.http_catalog_client import MANIFEST_APPEND_SWEEP_CHASHES_CAP
from nexus.db.limits import QUOTAS
from nexus.errors import IndexRunVerifyRefused, NexusError

_log = structlog.get_logger(__name__)


class RepeatedPositionError(ValueError):
    """A position was written twice in one run.

    A revisited position with a different chash drops the old chash from the manifest outside
    ``sweep_chashes`` (the drop list is computed against the previous manifest, not this run's
    earlier batches), so the writer refuses the batch before sending it.
    """


class BatchWriteFailedError(NexusError):
    """A batch of a multi-batch write did not land, or the engine's answer cannot be trusted.

    Attributes: ``doc_id``, ``batch`` (1-based index of the batch or step that failed).
    """

    def __init__(self, *, doc_id: str, batch: int, reason: str) -> None:
        self.doc_id = doc_id
        self.batch = batch
        self.reason = reason
        super().__init__(f"multi-batch write of {doc_id!r} failed at batch {batch}: {reason}")


@dataclass
class DocumentWriteResult:
    """What one :meth:`MultiBatchDocumentWriter.finish` did.

    ``batches`` counts data requests (a batch larger than the chunk cap is several); ``requests``
    adds the sweep-only appends (not the fence or the completion stamp). ``distinct_chashes`` is the post-dedup chash count the completion stamp claims.
    ``sweep_deferred_unknown`` is True when the engine could not read the previous manifest, so
    the deferred sweep was omitted and the previous run's chunks are left to the reaper.
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
    sweep_deferred_unknown: bool = False
    completed: bool = False


Batch = tuple[Sequence[dict], Sequence[dict]]


class MultiBatchDocumentWriter:
    """Write one document as N batches with the RDR-223 protocol. See the module docstring.

    *cat* is a catalog writer (``get_catalog_writer()`` or ``HttpCatalogClient``). *content_hash*
    turns on the index-run fence and the completion stamp; without it the caller stamps (or not).
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
    ) -> None:
        if not doc_id:
            raise ValueError("MultiBatchDocumentWriter: 'doc_id' is required")
        if not collection:
            raise ValueError("MultiBatchDocumentWriter: 'collection' is required")
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
        self._pending: tuple[list[dict], list[dict]] | None = None
        self._carried: set[str] = set()      # chashes whose chunk payload an earlier request carried
        self._sent = 0                       # batches sent so far
        self._positions: set[int] = set()
        self._run_chashes: dict[str, None] = {}      # distinct, insertion-ordered
        self._dropped: list[str] = []
        self._dropped_unknown = False
        self._fenced = False
        self._failed = False
        self._finished = False
        self._result = DocumentWriteResult()

    # ── public ────────────────────────────────────────────────────────────────

    def add_batch(self, rows: Sequence[dict], chunks: Sequence[dict]) -> None:
        """Add the next batch: *rows* are manifest rows (``chash`` and an explicit integer
        ``position``), *chunks* the ``{chash, text, metadata[, embedding]}`` payload those rows
        reference. Sends the previously buffered batch (it is now known not to be the last)."""
        self._require_usable()
        rows_l, by_chash = self._validate(rows, chunks)
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

    def finish(self) -> DocumentWriteResult:
        """Send the last batch, the deferred sweep and the completion stamp."""
        self._require_usable()
        if self._pending is None:
            raise ValueError("MultiBatchDocumentWriter.finish: no batch was added")
        try:
            self._send_pending(last=True)
        except BaseException:
            self._failed = True
            raise
        self._finished = True
        self._result.distinct_chashes = len(self._run_chashes)
        return self._result

    def abort(self, error: str) -> None:
        """Mark the index run failed, if a fence was begun. Best effort; for a caller that
        survives a failed write (a killed process needs nothing: the fence stays ``indexing``)."""
        if not self._fenced or self._content_hash is None:
            return
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
        return rows_l, {c["chash"]: c for c in chunks_l}

    # ── requests ──────────────────────────────────────────────────────────────

    def _begin_fence(self) -> None:
        if self._content_hash is None or self._fenced:
            return
        self._cat.begin_index_run(
            self._doc_id, self._content_hash, self._run_id, self._collection)
        self._fenced = True

    def _send_pending(self, *, last: bool) -> None:
        rows, chunks = self._pending  # type: ignore[misc]
        self._pending = None
        n = self._sent + 1
        try:
            if n == 1:
                self._begin_fence()
            if n == 1 and last:
                self._send_only_batch(rows, chunks)
            elif n == 1:
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

    def _write_many(
        self, rows: list[dict], chunks: list[dict], *, sweep: bool, complete: dict | None,
    ) -> dict:
        resp = self._cat.write_manifest_many(
            [(self._doc_id, rows)], complete=complete, sweep=sweep, chunks=chunks or None,
            collection=self._collection, force_re_embed=self._force_re_embed,
            embedding_model=self._embedding_model)
        resp = resp if isinstance(resp, dict) else {}
        if self._doc_id in (resp.get("failed_doc_ids") or ()):
            raise BatchWriteFailedError(
                doc_id=self._doc_id, batch=self._sent + 1,
                reason="the engine reported the document in failed_doc_ids")
        self._account(resp)
        self._check_unreferenced(resp, self._sent + 1)
        return resp

    def _check_unreferenced(self, resp: dict, batch: int) -> None:
        """The engine counts payload chunks no row of the request referenced and drops them
        (neither embedded nor inserted). The writer sends only referenced chunks, so any count is
        content that silently did not land."""
        n = int(resp.get("chunks_unreferenced") or 0)
        if n:
            raise BatchWriteFailedError(
                doc_id=self._doc_id, batch=batch,
                reason=f"the engine dropped {n} chunk(s) as referenced by no row of the request")

    def _send_only_batch(self, rows: list[dict], chunks: list[dict]) -> None:
        complete = {self._doc_id: self._content_hash} if self._content_hash else None
        resp = self._write_many(rows, chunks, sweep=True, complete=complete)
        for refused in resp.get("complete_refused") or ():
            if refused.get("doc_id") == self._doc_id:
                referenced = int(refused.get("referenced") or 0)
                missing = int(refused.get("missing") or 0)
                raise IndexRunVerifyRefused(
                    doc_id=self._doc_id, referenced=referenced,
                    present=referenced - missing, missing=missing,
                    chunk_count=int(refused.get("chunk_count") or 0))
        self._result.swept += int(resp.get("swept") or 0)
        self._result.sweep_skipped += int(resp.get("sweep_skipped") or 0)
        self._result.completed = complete is not None

    def _send_first_of_many(self, rows: list[dict], chunks: list[dict]) -> None:
        resp = self._write_many(rows, chunks, sweep=False, complete=None)
        unknown = self._doc_id in (resp.get("dropped_unknown") or ())
        dropped_map = resp.get("dropped_chashes")
        if unknown:
            self._dropped_unknown = True
            self._result.sweep_deferred_unknown = True
            _log.warning(
                "multi_batch_dropped_unknown_sweep_omitted",
                doc_id=self._doc_id, collection=self._collection)
            return
        if not isinstance(dropped_map, dict) or self._doc_id not in dropped_map:
            raise BatchWriteFailedError(
                doc_id=self._doc_id, batch=1,
                reason="the write_many response carried neither a 'dropped_chashes' entry nor a "
                       "'dropped_unknown' marker for the document; the drop list cannot be "
                       "guessed (empty would strand the previous run's chunks, sweeping now "
                       "would delete them under the later batches)")
        dropped = list(dropped_map[self._doc_id] or ())
        counts = resp.get("dropped_count")
        if not isinstance(counts, dict) or self._doc_id not in counts:
            raise BatchWriteFailedError(
                doc_id=self._doc_id, batch=1,
                reason="the write_many response carried no 'dropped_count' entry for the document; "
                       "without it a truncated 'dropped_chashes' list cannot be told from a "
                       "complete one")
        if int(counts[self._doc_id]) != len(dropped):
            raise BatchWriteFailedError(
                doc_id=self._doc_id, batch=1,
                reason=f"dropped_count says {counts[self._doc_id]} but dropped_chashes lists "
                       f"{len(dropped)}; the list is truncated or corrupt")
        self._dropped = dropped

    def _sweep_list(self) -> list[str]:
        """The kept drop list minus every chash this run wrote, de-duplicated, in order."""
        if self._dropped_unknown:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for c in self._dropped:
            if c in self._run_chashes or c in seen:
                continue
            seen.add(c)
            out.append(c)
        return out

    def _send_append(self, n: int, rows: list[dict], chunks: list[dict], *, last: bool) -> None:
        cap = MANIFEST_APPEND_SWEEP_CHASHES_CAP
        sweep = self._sweep_list() if last else []
        first, rest = sweep[:cap], sweep[cap:]
        resp = self._cat.append_manifest_chunks(
            self._doc_id, rows, collection=self._collection, chunk_payload=chunks or None,
            sweep_chashes=first or None, force_re_embed=self._force_re_embed,
            embedding_model=self._embedding_model)
        resp = resp if isinstance(resp, dict) else {}
        self._account(resp)
        self._check_unreferenced(resp, n)
        self._result.swept += int(resp.get("swept") or 0)
        self._result.sweep_skipped += int(resp.get("sweep_skipped") or 0)
        if not last:
            return
        for i in range(0, len(rest), cap):
            part = rest[i:i + cap]
            sresp = self._cat.append_manifest_chunks(
                self._doc_id, [], collection=self._collection, sweep_chashes=part)
            sresp = sresp if isinstance(sresp, dict) else {}
            self._result.requests += 1
            self._result.swept += int(sresp.get("swept") or 0)
            self._result.sweep_skipped += int(sresp.get("sweep_skipped") or 0)
        if self._content_hash is not None:
            # The distinct, post-dedup chash count of the manifest this run wrote, which is what
            # the engine compares against the stored manifest (a mismatch is a 409).
            self._cat.complete_index_run(
                self._doc_id, self._content_hash, len(self._run_chashes))
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
) -> DocumentWriteResult:
    """Write *batches* (``(rows, chunks)`` pairs) as one document; see
    :class:`MultiBatchDocumentWriter`. On a failure the fence is marked failed and the exception
    propagates."""
    w = MultiBatchDocumentWriter(
        cat, doc_id=doc_id, collection=collection, content_hash=content_hash, run_id=run_id,
        embedding_model=embedding_model, force_re_embed=force_re_embed)
    try:
        for rows, chunks in batches:
            w.add_batch(rows, chunks)
        return w.finish()
    except BaseException as exc:
        w.abort(f"{type(exc).__name__}: {exc}")
        raise
