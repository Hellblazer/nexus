# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one catalog-writer wrapper the RDR-223 per-file writers share (nexus-z0o2p.13, .14).

:class:`~nexus.catalog.multi_batch_write.MultiBatchDocumentWriter` owns the request sequence and
knows nothing about metadata modes or the run summary. A caller that writes a document's chunks
with it wraps its catalog writer in :class:`MetadataMergingCatalog`, which adds two things to every
chunk-carrying request:

* the engine's metadata MERGE mode (``metadata_merge=True`` plus the caller's
  ``metadata_delete_keys``). The combined write REPLACES a stored chunk's metadata unless asked to
  merge; the old ``upsert-chunks`` merged. Merging keeps the keys another writer set on the chunk
  (``bib_*`` from ``nx enrich bib``) across a re-index, while
  :func:`nexus.metadata_schema.rewrite_delete_keys` names the keys THIS writer owns and dropped
  from a row so they are cleared;
* the run summary's sweep accounting, as ``manifest_write_batch_hook`` carried it on the old path:
  each response's swept count, and every sweep the engine reported as errored (with its reason), so
  a skipped sweep is never silent (nexus-39upx).

Every other attribute is the wrapped writer's.
"""
from __future__ import annotations

import json
from typing import Any, Callable

__all__ = ["MetadataMergingCatalog"]


def _may_have_written(exc: BaseException) -> bool:
    """False only when EVERY exception in *exc*'s cause/context chain positively means its attempt
    wrote nothing; an unknown failure is treated as possibly written, since the cost of a wrong
    "written" is a leftover failed registration a rerun heals, and the cost of a wrong "not written"
    is a rollback of chunks that landed.

    The whole chain is judged, not the outermost exception. The refreshable client retries once from
    inside its own ``except`` block, so when attempt 1 committed and its response was reset, and the
    retry then fails cleanly (a connect error while the service restarts, a 401), the exception that
    propagates is the retry's, with attempt 1's as its ``__context__``. The retry's clean refusal says
    nothing about attempt 1. Same rule as ``note_write._judge``."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        cur = pending.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if _attempt_may_have_written(cur):
            return True
        pending.extend(n for n in (cur.__cause__, cur.__context__) if n is not None)
    return False


def _attempt_may_have_written(exc: BaseException) -> bool:
    """One exception on its own: False only for a failure that positively means nothing was written."""
    import httpx  # noqa: PLC0415 — deferred: keeps the module import light

    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return not (400 <= exc.response.status_code < 500 and exc.response.status_code != 408)
    if isinstance(exc, json.JSONDecodeError):  # a ValueError, but of the ANSWER: the write happened
        return True
    return not isinstance(exc, (ValueError, TypeError))


class MetadataMergingCatalog:
    """Wrap a catalog writer for one document's multi-batch write. *collection* names the T3
    collection for the summary; *delete_keys* is ``rewrite_delete_keys(metadatas)`` of the rows
    being written."""

    def __init__(
        self, cat: Any, collection: str, delete_keys: list[str],
        on_request: "Callable[[], None] | None" = None,
    ) -> None:
        self._cat = cat
        self._collection = collection
        self._delete_keys = list(delete_keys)
        #: Called when a chunk-carrying request has, or may have, written chunks (see :meth:`_send`).
        self._on_request = on_request
        #: Sweeps the engine reported as errored, with a reason, across this document's responses.
        self.errored_sweeps = 0

    def write_manifest_many(self, *args: Any, **kwargs: Any) -> Any:
        docs = args[0] if args else kwargs.get("docs")
        return self._send(lambda: self._cat.write_manifest_many(
            *args, metadata_merge=True, metadata_delete_keys=self._delete_keys, **kwargs),
            doc_ids=[d[0] for d in docs or ()])

    def append_manifest_chunks(self, *args: Any, **kwargs: Any) -> Any:
        return self._send(lambda: self._cat.append_manifest_chunks(
            *args, metadata_merge=True, metadata_delete_keys=self._delete_keys, **kwargs),
            doc_ids=[])

    def _send(self, send: "Callable[[], Any]", *, doc_ids: list[str]) -> Any:
        """Run one chunk-carrying request and tell the caller whether it may have written.

        The hook reports what a caller that would undo a fresh registration needs to know: that
        chunks are, or may be, in the store. It fires AFTER the request, and only when that is
        true. A request that never reached the engine (a connect error, the client's own argument
        checks) or that the engine refused (a 4xx other than 408) wrote nothing, and a freshly
        minted document whose first request ended that way is a phantom registration the caller
        must roll back, so reporting it would leave a failed document with zero chunks. A request
        that answered with every document in ``failed_doc_ids`` was rolled back by the engine, for
        the same reason. Everything else (a read timeout, a dropped connection, a 5xx or 408, an
        answer the client could not parse or trust) leaves the outcome open and counts as written.
        """
        try:
            resp = send()
        except BaseException as exc:
            if _may_have_written(exc):
                self._report()
            raise
        failed = set(resp.get("failed_doc_ids") or ()) if isinstance(resp, dict) else set()
        if not (doc_ids and set(doc_ids) <= failed):
            self._report()
        return self._note(resp)

    def _report(self) -> None:
        if self._on_request is not None:
            self._on_request()

    def _note(self, resp: Any) -> Any:
        if not isinstance(resp, dict):
            return resp
        from nexus.mcp_infra import _record_superseded_sweep_skip, _record_superseded_swept  # noqa: PLC0415 — deferred: mcp_infra imports the indexers

        _record_superseded_swept(int(resp.get("swept") or 0))
        for outcome in resp.get("sweep_detail") or ():
            if isinstance(outcome, dict) and outcome.get("errored"):
                self.errored_sweeps += 1
                _record_superseded_sweep_skip(
                    str(outcome.get("doc_id", "")), self._collection,
                    str(outcome.get("reason") or "sweep_failed"))
        return resp

    def account_unexplained_skips(self, doc_id: str, sweep_skipped: int) -> None:
        """Record the sweeps the engine skipped but gave no reason for (an older response shape
        without ``sweep_detail``): *sweep_skipped* is the writer result's count, of which
        :attr:`errored_sweeps` already carry a reason."""
        if sweep_skipped > self.errored_sweeps:
            from nexus.mcp_infra import _record_superseded_sweep_skip  # noqa: PLC0415 — deferred: mcp_infra imports the indexers

            _record_superseded_sweep_skip(doc_id, self._collection, "sweep_failed")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cat, name)
