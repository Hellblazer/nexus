# SPDX-License-Identifier: AGPL-3.0-or-later
"""Several documents written page by page, each chunk together with its owner row (RDR-223, bead
nexus-z0o2p.19: the ``.nxexp`` import).

:mod:`nexus.catalog.multi_batch_write` writes ONE document as N batches. An import cannot use it: the
export is in chash-page order, so one page holds rows of many documents and one document has rows on
many pages. This module is the same protocol driven from the other side: a page arrives holding rows
for several documents and is written as a few requests, and every document is finished (swept and
stamped) on ITS OWN last page, not at the end of the stream.

The caller knows each document's record count beforehand (the import reads its file once to count
them) and says so with :meth:`MultiDocumentImportWriter.register_document`; a document's last page is
the one that brings its received rows up to that count.

Per page, for the documents it holds rows of:

* **Fence.** Documents met for the first time: ``begin_index_run_many(snapshot_manifest=True)``
  (``index_state`` becomes ``indexing``; the answer carries each document's PRE-RUN manifest, read in
  the same transaction as its stamp). The snapshot, not a ``write_many`` response, is the source of
  the deferred sweep: a first response that is lost and resent reads the manifest its first attempt
  already replaced and would report nothing dropped.
* **First request of a fresh document.** One ``write_manifest_many`` (a replace) carrying its rows and
  chunks. A document whose only request this is (its first page is also its last) is written with
  ``sweep`` on and ``complete`` in the same request; the engine sweeps what the replace dropped and
  stamps it. Every other first request is written with sweep off and no stamp (a sweep there would
  delete the previous manifest's chunks before the later pages land). Fresh and single-request
  documents travel in separate requests because ``sweep`` is per request.
* **Later requests, and every request of a RESUMED document.** One ``append_manifest_many`` carrying
  rows and chunks. A resumed document is one this same file left ``indexing`` or ``failed`` (the caller
  decides); its whole manifest is upserted BY POSITION from the same file in the same order, so the
  append form drops nothing, needs no sweep, and never leaves a chunk of the earlier run ownerless.
* **Last page of a multi-request document.** The same ``append_many`` carries the document's deferred
  sweep (``sweep_chashes``: its snapshot minus every chash the run wrote for it, at most 300) and its
  completion stamp (``complete``: content hash and manifest ROW count, verified by the engine in the
  document's own transaction). A sweep list longer than 300 continues in trailing sweep-only appends
  and the stamp rides the last of them. Nothing is swept before the last data page has landed, and a
  killed client never reaches it.

Chunks travel with their vectors when the caller supplies them (``embedding``), so the engine embeds
nothing; ``embedding_model`` then names the model that produced them. Every chunk in a request is
referenced by a row of that request, so a request that fails writes no chunk without an owner. If the
engine nevertheless embeds (``embed_embedded`` above zero) the supplied vectors were ignored and the
import raises: the byte-identical property is lost.

A refused stamp (``complete_refused``) leaves ``index_state`` as ``begin`` left it, ``indexing``, and
is recorded for the record-level summary as the single-document writer does; it is not turned into
``failed``. A document the engine fails in place is marked failed, gets no further rows, and is
reported; the other documents of the request are unaffected. A request that raises propagates;
:meth:`abort` marks the fences of the documents still open ``failed`` for a caller that survives it
(a killed process needs nothing: the fence stays ``indexing`` and the next run resumes the document).

No old-engine fallbacks: a response missing a field this protocol needs is an error, never a degrade.

Only write ops are used, so ``cat`` may be the ``make_catalog_writer()`` proxy (the closed
``CATALOG_WRITE_OPS`` whitelist).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

import structlog

from nexus.catalog.http_catalog_client import (
    MANIFEST_APPEND_MANY_MAX_DOCS,
    MANIFEST_APPEND_SWEEP_CHASHES_CAP,
)
from nexus.errors import BatchWriteFailedError

__all__ = ["FinishResult", "MultiDocumentImportWriter", "PageWriteResult"]

_log = structlog.get_logger(__name__)


@dataclass
class PageWriteResult:
    """What one :meth:`MultiDocumentImportWriter.write_page` did.

    ``written`` are the documents whose rows landed in this page; ``failed`` maps a document that
    did not land (this page or an earlier one) to the reason; ``finished`` are the documents whose
    last request landed in this page with their stamp accepted. The counters sum the engine's
    answers over the page's requests.
    """

    written: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    finished: list[str] = field(default_factory=list)
    chunks_written: int = 0
    embed_embedded: int = 0
    vectors_supplied: int = 0
    vector_mismatches: int = 0
    sweep_skipped: int = 0


@dataclass
class FinishResult:
    """What :meth:`MultiDocumentImportWriter.finish` found: the documents stamped ``complete`` over
    the whole run, and those that were not (failed, a refused stamp, or never brought to their last
    page), with the reason."""

    completed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class _Doc:
    __slots__ = ("positions", "tail", "total", "resume", "received", "begun", "written", "failed",
                 "prior", "wrote", "sweep_rest", "done", "stamped", "refusal", "collisions")

    def __init__(self, total: int, max_position: int, resume: bool) -> None:
        self.positions: set[int] = set()     # every position claimed
        self.tail = max_position + 1         # where a colliding row goes: past every legitimate one
        self.total = total                   # rows the caller will send over the run
        self.resume = resume
        self.received = 0                    # manifest rows that landed: the completion stamp's count
        self.begun = False
        self.written = False                 # the first request landed
        self.failed: str | None = None
        self.prior: list[str] = []           # the pre-run manifest's distinct chashes (fresh docs)
        self.wrote: set[str] | None = None   # chashes the run wrote; tracked only while `prior` is non-empty
        self.sweep_rest: list[str] = []      # sweep chashes not yet sent (past the 300 cap)
        self.done = False                    # the last request landed
        self.stamped = False
        self.refusal: str | None = None
        self.collisions = 0


class MultiDocumentImportWriter:
    """Write several documents page by page. See the module docstring.

    *cat* is a catalog writer. *content_hash* stamps the fence and the completion (any stable,
    non-empty identity of this run's source; the import uses a hash of the file). *embedding_model*
    is required as soon as any chunk carries an ``embedding``. *force_re_embed* makes every payload
    chunk land with its supplied vector even when the collection already holds the chash (the engine
    otherwise keeps the stored vector and only counts the difference). *metadata_merge* writes a
    stored chash's metadata as ``stored || incoming`` instead of replacing it, so keys another
    document's enrichment set on a shared chunk survive.
    """

    def __init__(
        self,
        cat: Any,
        *,
        collection: str,
        content_hash: str,
        embedding_model: str | None = None,
        force_re_embed: bool = False,
        metadata_merge: bool = False,
        run_id: str | None = None,
    ) -> None:
        if not collection:
            raise ValueError("MultiDocumentImportWriter: 'collection' is required")
        if not content_hash:
            raise ValueError(
                "MultiDocumentImportWriter: 'content_hash' is required: the index-run fence is what "
                "keeps a stale 'complete' stamp off a half-written document")
        self._cat = cat
        self._collection = collection
        self._content_hash = content_hash
        self._embedding_model = embedding_model
        self._force_re_embed = force_re_embed
        self._metadata_merge = metadata_merge
        self._run_id = run_id or uuid.uuid4().hex
        self._docs: dict[str, _Doc] = {}
        self._rows_landed = 0
        self._finished = False

    # ── public ────────────────────────────────────────────────────────────────

    @property
    def rows_landed(self) -> int:
        """Manifest rows written (each with its chunk) over the run, failed documents' earlier
        pages included: those rows are owned."""
        return self._rows_landed

    def register_document(
        self, doc_id: str, *, total_rows: int, max_position: int, resume: bool = False,
    ) -> None:
        """Declare a document the run will write: *total_rows* rows over the whole run (its last page
        is the one that brings it there), *max_position* the highest position the caller will claim
        for it, and whether it is a *resume* (written with the append form throughout). Registering
        again is a no-op: a document reached through two owner groups is declared by both with its
        combined figures."""
        if total_rows < 1:
            raise ValueError(f"register_document({doc_id!r}): total_rows must be positive, got {total_rows}")
        self._docs.setdefault(doc_id, _Doc(total_rows, max_position, resume))

    def failure(self, doc_id: str) -> str | None:
        """Why *doc_id* failed, or None."""
        st = self._docs.get(doc_id)
        return st.failed if st else None

    def failures(self) -> dict[str, str]:
        """Every document that failed (a begin, a write, a sweep) or whose stamp was refused, with
        the reason."""
        out: dict[str, str] = {}
        for d, st in self._docs.items():
            if st.failed is not None:
                out[d] = st.failed
            elif st.refusal is not None:
                out[d] = st.refusal
        return out

    def progress(self) -> tuple[int, int]:
        """``(documents stamped complete, documents begun)`` so far."""
        return (sum(1 for st in self._docs.values() if st.stamped),
                sum(1 for st in self._docs.values() if st.begun))

    def claim_position(self, doc_id: str, wanted: int) -> int:
        """Reserve a manifest position for *doc_id*: *wanted* when free, else the next position past
        the highest the document will ever have (its registered ``max_position``), in arrival order.
        A legitimate row is therefore never moved; only a row that claims a position already taken
        goes to the tail. An append upserts BY POSITION, so a repeated position would silently
        replace another chunk's row. Deterministic for a given file, which a resumed run relies on."""
        st = self._docs[doc_id]
        pos = wanted
        if isinstance(pos, bool) or not isinstance(pos, int) or pos < 0 or pos in st.positions:
            pos = st.tail
            st.tail += 1
            st.collisions += 1
            (_log.warning if st.collisions == 1 else _log.debug)(
                "nxexp_import_position_collision", doc_id=doc_id, wanted=wanted, assigned=pos,
                collisions=st.collisions)
        st.positions.add(pos)
        return pos

    def write_page(
        self, rows_by_doc: Mapping[str, Sequence[dict]], chunks: Mapping[str, dict],
    ) -> PageWriteResult:
        """Write one page. *rows_by_doc* maps a registered document to its manifest rows (``chash``
        and a claimed ``position``); *chunks* maps a chash to its ``{chash, text, metadata
        [, embedding]}`` payload. A chash with a row and no payload entry is one the collection
        already holds (the engine's foreign key refuses the row otherwise, failing that document
        alone)."""
        self._require_usable()
        result = PageWriteResult()
        active: dict[str, list[dict]] = {}
        for doc_id, rows in rows_by_doc.items():
            st = self._docs.get(doc_id)
            if st is None:
                raise ValueError(f"MultiDocumentImportWriter.write_page: {doc_id!r} was not registered")
            if st.failed is not None:
                result.failed[doc_id] = st.failed
                continue
            if not rows:
                continue
            if st.done:
                self._fail_doc(
                    doc_id, f"rows arrived after its last page (expected {st.total} rows, "
                            f"{st.received} landed): two owner groups resolved to one document",
                    result)
                continue
            for i, r in enumerate(rows):
                pos = r.get("position")
                if isinstance(pos, bool) or not isinstance(pos, int) or pos not in st.positions:
                    raise ValueError(
                        f"MultiDocumentImportWriter.write_page: {doc_id!r} rows[{i}] position "
                        f"{pos!r} was not claimed through claim_position")
                if not r.get("chash"):
                    raise ValueError(f"MultiDocumentImportWriter.write_page: {doc_id!r} rows[{i}] needs a 'chash'")
            active[doc_id] = [dict(r) for r in rows]
        if not active:
            return result
        self._begin(list(active), result)
        active = {d: r for d, r in active.items() if self._docs[d].failed is None}
        fresh = {d: r for d, r in active.items() if not (self._docs[d].written or self._docs[d].resume)}
        later = {d: r for d, r in active.items() if self._docs[d].written or self._docs[d].resume}
        if fresh:
            self._write_first(fresh, chunks, result)
        if later:
            self._append(later, chunks, result)
        return result

    def finish(self) -> FinishResult:
        """The run's verdict per document. Every document is swept and stamped on its own last page,
        so this sends nothing: a document begun and not stamped is reported with the reason (failed,
        stamp refused, or its last page never arrived)."""
        self._require_usable()
        out = FinishResult()
        for doc_id, st in self._docs.items():
            if st.failed is not None:
                out.failed[doc_id] = st.failed
            elif st.refusal is not None:
                out.failed[doc_id] = st.refusal
            elif st.stamped:
                out.completed.append(doc_id)
            elif st.begun:
                out.failed[doc_id] = (
                    f"never reached its last page ({st.received} of {st.total} rows landed); "
                    "it stays indexing and the next run of the same file finishes it")
        self._finished = True
        return out

    def abort(self, error: str) -> None:
        """Mark the fence of every document begun and neither stamped, refused nor failed ``failed``.
        Best effort, for a caller that survives a failed run; it stops at the first fence call that
        fails (the engine is the likely cause, and one more call per document would only repeat it:
        the documents stay ``indexing``, which the next run of the same file resumes). A no-op once
        :meth:`finish` returned."""
        if self._finished:
            return
        for doc_id, st in self._docs.items():
            if not st.begun or st.stamped or st.refusal is not None or st.failed is not None:
                continue
            st.failed = error
            try:
                self._cat.fail_index_run(doc_id, error)
            except Exception as exc:  # noqa: BLE001 — best-effort fence marking must never mask the original failure
                _log.warning("nxexp_import_abort_fail_index_run_failed", doc_id=doc_id, error=str(exc))
                break

    # ── requests ──────────────────────────────────────────────────────────────

    def _require_usable(self) -> None:
        if self._finished:
            raise ValueError("MultiDocumentImportWriter: already finished")

    def _retrying(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """nexus.retry's bounded manifest-write retry: connectivity errors, and a rate-limit answer
        paces the shared brake. A ``CombinedWriteEmbedTimeoutError`` is never retried."""
        from nexus.retry import _manifest_write_with_retry  # noqa: PLC0415 — deferred: nexus.retry pulls in the rate brake
        return _manifest_write_with_retry(fn, *args, **kwargs)

    def _fail_doc(self, doc_id: str, reason: str, result: PageWriteResult | None = None) -> None:
        st = self._docs[doc_id]
        st.failed = reason
        if result is not None:
            result.failed[doc_id] = reason
        if not st.begun:
            return                          # never fenced; nothing to mark
        try:
            self._cat.fail_index_run(doc_id, reason)
        except Exception as exc:  # noqa: BLE001 — a document already failed must not lose the others over its fence mark
            _log.warning("nxexp_import_fail_index_run_failed", doc_id=doc_id, error=str(exc))

    def _begin(self, doc_ids: list[str], result: PageWriteResult) -> None:
        todo = [d for d in doc_ids if not self._docs[d].begun]
        if not todo:
            return
        resp = self._retrying(
            self._cat.begin_index_run_many,
            [{"doc_id": d, "content_hash": self._content_hash, "run_id": self._run_id} for d in todo],
            self._collection, snapshot_manifest=True)
        # begin_index_run_many answers {} on a 404: an engine without the fence route. A write
        # without the fence is not safe (a stale 'complete' stamp could outlive a half-written
        # document), so that is an error here, not a degrade.
        if not isinstance(resp, dict) or "failed_doc_ids" not in resp or not isinstance(resp.get("snapshots"), dict):
            raise BatchWriteFailedError(
                doc_id=todo[0], batch=0,
                reason="begin_index_run_many returned no 'failed_doc_ids' and 'snapshots'; the engine "
                       "has no index-run fence route or no manifest snapshot, and a write without "
                       "them is not safe")
        failed = {str(d) for d in (resp.get("failed_doc_ids") or ())}
        snapshots = resp["snapshots"]
        for d in todo:
            st = self._docs[d]
            if d in failed:
                # Its begin did not land, so it was never fenced; nothing to mark.
                st.failed = "the engine could not begin its index run"
                result.failed[d] = st.failed
                continue
            snap = snapshots.get(d)
            prior = snap.get("prior_chashes") if isinstance(snap, dict) else None
            count = snap.get("prior_count") if isinstance(snap, dict) else None
            if (not isinstance(prior, list) or isinstance(count, bool) or not isinstance(count, int)
                    or count < len(prior) or (prior and count == 0)):
                st.begun = True            # the stamp is committed the moment begin answers
                raise BatchWriteFailedError(
                    doc_id=d, batch=0,
                    reason=f"begin_index_run_many returned no usable pre-run manifest "
                           f"(prior_chashes={type(prior).__name__}, prior_count={count!r})")
            st.begun = True
            if prior and not st.resume:
                st.prior = [str(c) for c in prior]
                st.wrote = set()

    @staticmethod
    def _payload(rows_by_doc: Mapping[str, Sequence[dict]], chunks: Mapping[str, dict]) -> list[dict]:
        """The chunks these rows reference, once each, in row order."""
        out: list[dict] = []
        seen: set[str] = set()
        for rows in rows_by_doc.values():
            for r in rows:
                c = r["chash"]
                if c in seen:
                    continue
                seen.add(c)
                chunk = chunks.get(c)
                if chunk is not None:
                    out.append(chunk)
        return out

    def _is_last(self, st: _Doc, n_rows: int) -> bool:
        return st.received + n_rows >= st.total

    def _land(self, doc_id: str, rows: Sequence[dict], result: PageWriteResult) -> None:
        st = self._docs[doc_id]
        st.written = True
        st.received += len(rows)
        self._rows_landed += len(rows)
        result.written.append(doc_id)
        if st.wrote is not None:
            st.wrote.update(r["chash"] for r in rows)

    def _account(self, resp: dict, result: PageWriteResult, doc_ids: Sequence[str]) -> None:
        result.chunks_written += int(resp.get("chunks_written") or 0)
        result.embed_embedded += int(resp.get("embed_embedded") or 0)
        result.vectors_supplied += int(resp.get("vectors_supplied") or 0)
        result.vector_mismatches += int(resp.get("vector_mismatches") or 0)
        result.sweep_skipped += int(resp.get("sweep_skipped") or 0)

    def _check_no_embeds(self, resp: dict, doc_ids: Sequence[str]) -> None:
        """The import supplies a vector with every chunk, so the engine embeds nothing. A count
        above zero means it ignored the vectors and re-embedded the text: the exported vectors are
        not what was stored, and the import must not go on as if they were."""
        n = int(resp.get("embed_embedded") or 0)
        if n:
            raise BatchWriteFailedError(
                doc_id=next(iter(doc_ids), ""), batch=0,
                reason=f"the engine embedded {n} chunk(s) although every chunk carried its exported "
                       "vector; the supplied vectors were ignored and the stored ones are NOT the export's")

    def _finish_stamp(self, doc_id: str, refused: Mapping[str, dict], result: PageWriteResult) -> None:
        """Record the outcome of a stamp that rode a request this document landed in."""
        st = self._docs[doc_id]
        st.done = True
        r = refused.get(doc_id)
        if r is None:
            st.stamped = True
            result.finished.append(doc_id)
            return
        st.refusal = (
            f"the engine refused the completion stamp (referenced={r.get('referenced')}, "
            f"missing={r.get('missing')}, expected rows={r.get('chunk_count')}); it stays indexing")
        try:
            from nexus.mcp_infra import _record_complete_refusal  # noqa: PLC0415 — deferred: mcp_infra imports back into catalog code
            _record_complete_refusal(doc_id)
        except Exception as exc:  # noqa: BLE001 — recording is advisory; the refusal itself is reported
            _log.warning("nxexp_import_refusal_record_failed", doc_id=doc_id, error=str(exc))

    @staticmethod
    def _refused_map(resp: dict) -> dict[str, dict]:
        return {str(r.get("doc_id")): r for r in (resp.get("complete_refused") or ()) if isinstance(r, dict)}

    def _write_first(
        self, first: dict[str, list[dict]], chunks: Mapping[str, dict], result: PageWriteResult,
    ) -> None:
        """The first request of each fresh document (a replace). Documents whose only request this is
        are written with sweep on and their stamp; the others with neither."""
        single = {d: r for d, r in first.items() if self._is_last(self._docs[d], len(r))}
        multi = {d: r for d, r in first.items() if d not in single}
        for group, is_single in ((multi, False), (single, True)):
            if not group:
                continue
            payload = self._payload(group, chunks)
            stamp = {d: self._content_hash for d in group} if is_single else None
            resp = self._retrying(
                self._cat.write_manifest_many, list(group.items()), stamp, sweep=is_single,
                chunks=payload or None, collection=self._collection,
                force_re_embed=self._force_re_embed, embedding_model=self._embedding_model,
                metadata_merge=self._metadata_merge)
            resp = resp if isinstance(resp, dict) else {}
            failed = {str(d) for d in (resp.get("failed_doc_ids") or ())}
            refused = self._refused_map(resp)
            self._account(resp, result, list(group))
            for doc_id, rows in group.items():
                if doc_id in failed:
                    self._fail_doc(doc_id, "the engine reported the document in failed_doc_ids", result)
                    continue
                self._land(doc_id, rows, result)
                if is_single:
                    self._finish_stamp(doc_id, refused, result)
            self._check_no_embeds(resp, list(group))

    def _append(
        self, later: dict[str, list[dict]], chunks: Mapping[str, dict], result: PageWriteResult,
    ) -> None:
        """One append_many for the documents already open (and the resumed ones), carrying the last
        page's deferred sweep and stamp for the documents this page finishes."""
        cap = MANIFEST_APPEND_SWEEP_CHASHES_CAP
        sweeps: dict[str, list[str]] = {}
        stamps: dict[str, tuple[str, int]] = {}
        rests: dict[str, list[str]] = {}
        last_docs: set[str] = set()
        for doc_id, rows in later.items():
            st = self._docs[doc_id]
            if not self._is_last(st, len(rows)):
                continue
            last_docs.add(doc_id)
            sweep: list[str] = []
            if st.prior and not st.resume:
                wrote = (st.wrote or set()) | {r["chash"] for r in rows}
                seen: set[str] = set()
                for c in st.prior:
                    if c in wrote or c in seen:
                        continue
                    seen.add(c)
                    sweep.append(c)
            if sweep:
                sweeps[doc_id] = sweep[:cap]
            if len(sweep) > cap:
                rests[doc_id] = sweep[cap:]
            else:
                stamps[doc_id] = (self._content_hash, st.received + len(rows))
        payload = self._payload(later, chunks)
        resp = self._retrying(
            self._cat.append_manifest_many, list(later.items()), collection=self._collection,
            chunks=payload or None, sweep_chashes=sweeps or None, complete=stamps or None,
            force_re_embed=self._force_re_embed, embedding_model=self._embedding_model,
            metadata_merge=self._metadata_merge)
        resp = resp if isinstance(resp, dict) else {}
        failed = {str(d) for d in (resp.get("failed_doc_ids") or ())}
        if payload:
            # The engine counts payload chunks no row of the request referenced and drops them
            # (neither embedded nor inserted). The payload holds only referenced chunks, so any
            # count is content that silently did not land; an answer without it is an engine that
            # does not know the check, not a zero.
            if "chunks_unreferenced" not in resp:
                raise BatchWriteFailedError(
                    doc_id=next(iter(later)), batch=2,
                    reason="the append_many response carried no 'chunks_unreferenced'; the engine "
                           "cannot say whether every chunk was written")
            unref = int(resp["chunks_unreferenced"] or 0)
            if unref and not failed:
                raise BatchWriteFailedError(
                    doc_id=next(iter(later)), batch=2,
                    reason=f"the engine dropped {unref} chunk(s) as referenced by no row of the request")
        refused = self._refused_map(resp)
        self._account(resp, result, list(later))
        for doc_id, rows in later.items():
            if doc_id in failed:
                self._fail_doc(doc_id, "the engine reported the document in failed_doc_ids", result)
                continue
            self._land(doc_id, rows, result)
            if doc_id in rests:
                self._docs[doc_id].sweep_rest = rests[doc_id]
            elif doc_id in last_docs:
                self._finish_stamp(doc_id, refused, result)
        self._check_no_embeds(resp, list(later))
        self._trailing_sweeps(result)

    def _trailing_sweeps(self, result: PageWriteResult) -> None:
        """Sweep-only appends for the documents whose drop list outran the per-request cap; the last
        of a document's carries its completion stamp."""
        cap = MANIFEST_APPEND_SWEEP_CHASHES_CAP
        while True:
            pending = [(d, st) for d, st in self._docs.items() if st.sweep_rest and st.failed is None]
            if not pending:
                return
            batch = pending[:MANIFEST_APPEND_MANY_MAX_DOCS]
            sweeps: dict[str, list[str]] = {}
            stamps: dict[str, tuple[str, int]] = {}
            final: set[str] = set()
            for d, st in batch:
                sweeps[d] = st.sweep_rest[:cap]
                rest = st.sweep_rest[cap:]
                st.sweep_rest = rest
                if not rest:
                    final.add(d)
                    stamps[d] = (self._content_hash, st.received)
            resp = self._retrying(
                self._cat.append_manifest_many, [(d, []) for d, _ in batch], collection=self._collection,
                sweep_chashes=sweeps, complete=stamps or None)
            resp = resp if isinstance(resp, dict) else {}
            failed = {str(d) for d in (resp.get("failed_doc_ids") or ())}
            refused = self._refused_map(resp)
            result.sweep_skipped += int(resp.get("sweep_skipped") or 0)
            for d, _ in batch:
                if d in failed:
                    self._docs[d].sweep_rest = []
                    self._fail_doc(d, "the engine could not run the deferred sweep", result)
                elif d in final:
                    self._finish_stamp(d, refused, result)
