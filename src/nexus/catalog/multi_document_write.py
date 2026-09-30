# SPDX-License-Identifier: AGPL-3.0-or-later
"""Several documents written page by page, each chunk together with its owner row (RDR-223, bead
nexus-z0o2p.19: the ``.nxexp`` import).

:mod:`nexus.catalog.multi_batch_write` writes ONE document as N batches. An import cannot use it: the
export is in chash-page order, so one page holds rows of many documents and one document has rows on
many pages. This module is the same protocol driven from the other side: a page arrives holding rows
for several documents and is written as at most two requests.

Per page:

* documents met for the first time: ``begin_index_run_many`` (the fence: ``index_state`` becomes
  ``indexing``), then ONE ``write_manifest_many`` carrying their rows and chunks, sweep OFF and no
  ``complete`` stamp. The response's ``dropped_chashes`` for each document (what the replace dropped
  from its previous manifest) is kept.
* documents already open: ONE ``append_manifest_many`` carrying their rows and chunks.

Chunks travel with their vectors when the caller supplies them (``embedding``), so the engine embeds
nothing; ``embedding_model`` then names the model that produced them. Every chunk in a request is
referenced by a row of that request, so a request that fails writes no chunk without an owner.

:meth:`MultiDocumentImportWriter.finish` closes the run after the last page:

1. **The deferred sweep.** Each document's kept drop list, minus every chash the run wrote for it,
   goes out as ``sweep_chashes`` on that document's LAST append: a trailing sweep-only
   ``append_many`` (at most 300 chashes per document per request; a longer list continues in further
   requests). It is a trailing request rather than a field of the last data append because a
   streaming caller does not know which page is a document's last until the stream ends (that is the
   caller's choice to record: the import reads the file once). Nothing is swept before the last data
   append has landed, and a killed client never reaches it.
2. **The completion stamp.** ``complete_index_run(doc, content_hash, manifest ROW count)`` per
   document, after its sweep. The engine compares the count with ``count(*)`` over the document's
   manifest rows. A refused stamp (:class:`~nexus.errors.IndexRunVerifyRefused`) leaves
   ``index_state`` as ``begin`` left it, ``indexing``, and is recorded for the record-level summary
   exactly as the single-document writer does; it is not turned into ``failed``.

A document that fails (the engine lists it in ``failed_doc_ids``) is marked failed, gets no further
rows, and is reported; the other documents of the request are unaffected. A request that raises
propagates: :meth:`abort` marks the fences of the documents still open ``failed`` for a caller that
survives the failure (a killed process needs nothing: the fence stays ``indexing``).

No old-engine fallbacks: a response missing a field this protocol needs is an error, never a
degrade.

Only write ops are used, so ``cat`` may be the ``get_catalog_writer()`` /
``make_catalog_writer()`` proxy (the closed ``CATALOG_WRITE_OPS`` whitelist).
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
from nexus.errors import BatchWriteFailedError, IndexRunVerifyRefused

__all__ = ["FinishResult", "MultiDocumentImportWriter", "PageWriteResult"]

_log = structlog.get_logger(__name__)


@dataclass
class PageWriteResult:
    """What one :meth:`MultiDocumentImportWriter.write_page` did.

    ``written`` are the documents whose rows landed in this page; ``failed`` maps a document that
    did not land (this page or an earlier one) to the reason. The counters sum the engine's answers
    over the page's requests.
    """

    written: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    chunks_written: int = 0
    embed_embedded: int = 0
    vectors_supplied: int = 0


@dataclass
class FinishResult:
    """What :meth:`MultiDocumentImportWriter.finish` did: the documents stamped ``complete`` and
    those that were not (a failed sweep, a refused or unroutable stamp), with the reason."""

    completed: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class _Doc:
    __slots__ = ("positions", "rows", "begun", "written", "failed", "dropped", "dropped_known",
                 "wrote_chashes", "stamped", "refused")

    def __init__(self) -> None:
        self.positions: set[int] = set()     # every position claimed (a claim is written or dropped)
        self.rows = 0                        # manifest rows that landed: the completion stamp's count
        self.begun = False
        self.written = False                 # the first (replace) request landed
        self.failed: str | None = None
        self.dropped: list[str] = []         # the replace's drop list; the deferred sweep's source
        self.dropped_known = True
        self.wrote_chashes: set[str] | None = None   # tracked only while `dropped` is non-empty
        self.stamped = False
        self.refused = False


class MultiDocumentImportWriter:
    """Write several documents page by page. See the module docstring.

    *cat* is a catalog writer. *content_hash* stamps the fence and the completion (any stable,
    non-empty identity of this run's source; the import uses a hash of the file). *embedding_model*
    is required as soon as any chunk carries an ``embedding``. *chunk_cap* bounds the chunks and rows
    of one request (the caller pages at the same number).
    """

    def __init__(
        self,
        cat: Any,
        *,
        collection: str,
        content_hash: str,
        embedding_model: str | None = None,
        force_re_embed: bool = False,
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

    def failure(self, doc_id: str) -> str | None:
        """Why *doc_id* failed, or None."""
        st = self._docs.get(doc_id)
        return st.failed if st else None

    def failures(self) -> dict[str, str]:
        """Every document that failed (a begin, a write or a sweep), with the reason."""
        return {d: st.failed for d, st in self._docs.items() if st.failed is not None}

    def is_open(self, doc_id: str) -> bool:
        """True once *doc_id*'s first request landed and it has not failed."""
        st = self._docs.get(doc_id)
        return bool(st and st.written and st.failed is None)

    def claim_position(self, doc_id: str, wanted: int) -> int:
        """Reserve a manifest position for *doc_id*: *wanted* when free, else the smallest free
        position above every one claimed so far. Rows must be built from claimed positions only;
        an append upserts BY position, so a repeated one would silently replace another chunk's row."""
        st = self._docs.setdefault(doc_id, _Doc())
        pos = wanted
        if pos in st.positions or pos < 0:
            pos = max(st.positions, default=-1) + 1
            _log.warning("nxexp_import_position_collision", doc_id=doc_id, wanted=wanted, assigned=pos)
        st.positions.add(pos)
        return pos

    def write_page(
        self, rows_by_doc: Mapping[str, Sequence[dict]], chunks: Mapping[str, dict],
    ) -> PageWriteResult:
        """Write one page. *rows_by_doc* maps a document to its manifest rows (``chash`` and a
        claimed ``position``); *chunks* maps a chash to its ``{chash, text, metadata[, embedding]}``
        payload. A chash with a row and no payload entry is one the collection already holds (the
        engine's foreign key refuses the row otherwise, failing that document alone)."""
        self._require_usable()
        result = PageWriteResult()
        first: dict[str, list[dict]] = {}
        later: dict[str, list[dict]] = {}
        for doc_id, rows in rows_by_doc.items():
            st = self._docs.setdefault(doc_id, _Doc())
            if st.failed is not None:
                result.failed[doc_id] = st.failed
                continue
            if not rows:
                continue
            for i, r in enumerate(rows):
                pos = r.get("position")
                if isinstance(pos, bool) or not isinstance(pos, int) or pos not in st.positions:
                    raise ValueError(
                        f"MultiDocumentImportWriter.write_page: {doc_id!r} rows[{i}] position "
                        f"{pos!r} was not claimed through claim_position")
                if not r.get("chash"):
                    raise ValueError(f"MultiDocumentImportWriter.write_page: {doc_id!r} rows[{i}] needs a 'chash'")
            (later if st.written else first)[doc_id] = [dict(r) for r in rows]
        if first:
            self._begin(list(first), result)
            first = {d: r for d, r in first.items() if self._docs[d].failed is None}
        if first:
            self._write_first(first, chunks, result)
        if later:
            self._append(later, chunks, result)
        return result

    def finish(self) -> FinishResult:
        """The deferred sweeps, then the completion stamps. See the module docstring."""
        self._require_usable()
        out = FinishResult()
        open_docs = [d for d, st in self._docs.items() if st.written and st.failed is None]
        self._sweep(open_docs, out)
        for doc_id in open_docs:
            st = self._docs[doc_id]
            if st.failed is not None:
                out.failed[doc_id] = st.failed
                continue
            self._stamp(doc_id, st, out)
        self._finished = True
        return out

    def abort(self, error: str) -> None:
        """Mark the fence of every document begun and neither stamped, refused nor failed ``failed``.
        Best effort, for a caller that survives a failed run. A no-op once :meth:`finish` returned."""
        if self._finished:
            return
        for doc_id, st in self._docs.items():
            if not st.begun or st.stamped or st.refused or st.failed is not None:
                continue
            st.failed = error
            try:
                self._cat.fail_index_run(doc_id, error)
            except Exception as exc:  # noqa: BLE001 — best-effort fence marking must never mask the original failure
                _log.warning("nxexp_import_abort_fail_index_run_failed", doc_id=doc_id, error=str(exc))

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
            self._collection)
        # begin_index_run_many answers {} on a 404: an engine without the fence route. A write
        # without the fence is not safe (a stale 'complete' stamp could outlive a half-written
        # document), so that is an error here, not a degrade.
        if not isinstance(resp, dict) or "failed_doc_ids" not in resp:
            raise BatchWriteFailedError(
                doc_id=todo[0], batch=0,
                reason="begin_index_run_many returned no 'failed_doc_ids'; the engine has no index-run "
                       "fence route, and a write without the fence is not safe")
        failed = {str(d) for d in (resp.get("failed_doc_ids") or ())}
        for d in todo:
            if d in failed:
                # Its begin did not land, so it was never fenced; nothing to mark.
                self._docs[d].failed = "the engine could not begin its index run"
                result.failed[d] = self._docs[d].failed  # type: ignore[assignment]
            else:
                self._docs[d].begun = True

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

    def _land(self, doc_id: str, rows: Sequence[dict], result: PageWriteResult) -> None:
        st = self._docs[doc_id]
        st.written = True
        st.rows += len(rows)
        self._rows_landed += len(rows)
        result.written.append(doc_id)
        if st.wrote_chashes is not None:
            st.wrote_chashes.update(r["chash"] for r in rows)

    def _write_first(
        self, first: dict[str, list[dict]], chunks: Mapping[str, dict], result: PageWriteResult,
    ) -> None:
        payload = self._payload(first, chunks)
        resp = self._retrying(
            self._cat.write_manifest_many, list(first.items()), sweep=False,
            chunks=payload or None, collection=self._collection,
            force_re_embed=self._force_re_embed, embedding_model=self._embedding_model)
        resp = resp if isinstance(resp, dict) else {}
        failed = {str(d) for d in (resp.get("failed_doc_ids") or ())}
        dropped = resp.get("dropped_chashes")
        counts = resp.get("dropped_count")
        unknown = {str(d) for d in (resp.get("dropped_unknown") or ())}
        result.chunks_written += int(resp.get("chunks_written") or 0)
        result.embed_embedded += int(resp.get("embed_embedded") or 0)
        result.vectors_supplied += int(resp.get("vectors_supplied") or 0)
        for doc_id, rows in first.items():
            if doc_id in failed:
                self._fail_doc(doc_id, "the engine reported the document in failed_doc_ids", result)
                continue
            st = self._docs[doc_id]
            if doc_id in unknown:
                # Its previous manifest could not be read after the commit: the drop list is
                # UNKNOWN, not empty. Nothing can be swept for it.
                st.dropped_known = False
                _log.warning("nxexp_import_dropped_unknown", doc_id=doc_id, collection=self._collection)
            else:
                if (not isinstance(dropped, dict) or doc_id not in dropped
                        or not isinstance(counts, dict) or doc_id not in counts):
                    raise BatchWriteFailedError(
                        doc_id=doc_id, batch=1,
                        reason="the write_many response carried neither dropped_chashes and "
                               "dropped_count entries nor a dropped_unknown marker for the document")
                lst = [str(c) for c in (dropped[doc_id] or ())]
                if int(counts[doc_id]) != len(lst):
                    raise BatchWriteFailedError(
                        doc_id=doc_id, batch=1,
                        reason=f"dropped_count says {counts[doc_id]} but dropped_chashes lists "
                               f"{len(lst)}; the list is truncated or corrupt")
                st.dropped = lst
                if lst:
                    st.wrote_chashes = set()
            self._land(doc_id, rows, result)

    def _append(
        self, later: dict[str, list[dict]], chunks: Mapping[str, dict], result: PageWriteResult,
    ) -> None:
        payload = self._payload(later, chunks)
        resp = self._retrying(
            self._cat.append_manifest_many, list(later.items()), collection=self._collection,
            chunks=payload or None, force_re_embed=self._force_re_embed,
            embedding_model=self._embedding_model)
        resp = resp if isinstance(resp, dict) else {}
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
            failed_ids = {str(d) for d in (resp.get("failed_doc_ids") or ())}
            if unref and not failed_ids:
                raise BatchWriteFailedError(
                    doc_id=next(iter(later)), batch=2,
                    reason=f"the engine dropped {unref} chunk(s) as referenced by no row of the request")
        failed = {str(d) for d in (resp.get("failed_doc_ids") or ())}
        result.chunks_written += int(resp.get("chunks_written") or 0)
        result.embed_embedded += int(resp.get("embed_embedded") or 0)
        result.vectors_supplied += int(resp.get("vectors_supplied") or 0)
        for doc_id, rows in later.items():
            if doc_id in failed:
                self._fail_doc(doc_id, "the engine reported the document in failed_doc_ids", result)
                continue
            self._land(doc_id, rows, result)

    # ── closing ───────────────────────────────────────────────────────────────

    def _sweep(self, open_docs: list[str], out: FinishResult) -> None:
        """Send each open document's drop list (minus what the run wrote) as ``sweep_chashes``, at
        most 300 per document per request, in sweep-only appends after the last data append."""
        cap = MANIFEST_APPEND_SWEEP_CHASHES_CAP
        pending: dict[str, list[str]] = {}
        for doc_id in open_docs:
            st = self._docs[doc_id]
            if not st.dropped:
                continue
            wrote = st.wrote_chashes or set()
            seen: set[str] = set()
            lst = []
            for c in st.dropped:
                if c in wrote or c in seen:
                    continue
                seen.add(c)
                lst.append(c)
            if lst:
                pending[doc_id] = lst
        while pending:
            batch = list(pending.items())[:MANIFEST_APPEND_MANY_MAX_DOCS]
            sweeps = {d: lst[:cap] for d, lst in batch}
            resp = self._retrying(
                self._cat.append_manifest_many, [(d, []) for d, _ in batch],
                collection=self._collection, sweep_chashes=sweeps)
            failed = {str(d) for d in ((resp or {}).get("failed_doc_ids") or ())}
            for d, lst in batch:
                rest = lst[cap:]
                if d in failed:
                    self._docs[d].failed = "the engine could not run the deferred sweep"
                    out.failed[d] = self._docs[d].failed  # type: ignore[assignment]
                    del pending[d]
                elif rest:
                    pending[d] = rest
                else:
                    del pending[d]

    def _stamp(self, doc_id: str, st: _Doc, out: FinishResult) -> None:
        try:
            done = self._retrying(
                self._cat.complete_index_run, doc_id, self._content_hash, st.rows)
        except IndexRunVerifyRefused as exc:
            st.refused = True
            try:
                from nexus.mcp_infra import _record_complete_refusal  # noqa: PLC0415 — deferred: mcp_infra imports back into catalog code
                _record_complete_refusal(doc_id)
            except Exception as rec_exc:  # noqa: BLE001 — recording is advisory; the refusal itself is reported below
                _log.warning("nxexp_import_refusal_record_failed", doc_id=doc_id, error=str(rec_exc))
            out.failed[doc_id] = f"the engine refused the completion stamp: {exc}"
            return
        if done is None:
            out.failed[doc_id] = ("complete_index_run answered 404: the engine has no index-run fence "
                                  "route, so the document was NOT stamped")
            return
        st.stamped = True
        out.completed.append(doc_id)
