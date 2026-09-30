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

from typing import Any, Callable

__all__ = ["MetadataMergingCatalog"]


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
        #: Called just before each chunk-carrying request is sent (RDR-223, nexus-z0o2p.11/.15), so
        #: a caller can tell the writer has begun writing.
        self._on_request = on_request
        #: Sweeps the engine reported as errored, with a reason, across this document's responses.
        self.errored_sweeps = 0

    def write_manifest_many(self, *args: Any, **kwargs: Any) -> Any:
        if self._on_request is not None:
            self._on_request()
        return self._note(self._cat.write_manifest_many(
            *args, metadata_merge=True, metadata_delete_keys=self._delete_keys, **kwargs))

    def append_manifest_chunks(self, *args: Any, **kwargs: Any) -> Any:
        if self._on_request is not None:
            self._on_request()
        return self._note(self._cat.append_manifest_chunks(
            *args, metadata_merge=True, metadata_delete_keys=self._delete_keys, **kwargs))

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
