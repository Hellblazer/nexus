# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``nx index repo`` oversize per-file fallback, on the RDR-223 combined writer (nexus-z0o2p.14).

A file whose chunks alone exceed one ChunkBatcher batch is refused by ``ChunkBatcher.add`` and is
written by its indexer's own per-file path: ``code_indexer.index_code_file`` (over the code cap),
``prose_indexer.index_prose_file`` (prose and RDR, over the CCE cap) and ``indexer._index_pdf_file``.
Those paths used to write the chunks with ``upsert-chunks`` (paged) and the owner rows in a later
manifest write, so a client that died between the two left chunks nobody owned. They call
:func:`write_oversize_file` instead: one document, one
:class:`~nexus.catalog.multi_batch_write.MultiBatchDocumentWriter`, every request carrying its
chunks together with their owner rows.

What the callers keep. The post-store hooks still fire once for the whole file, after the write, but
without ``manifest_write_batch_hook`` (the writer already wrote the manifest and stamped the
document complete). The early per-file ``_fence_begin`` stays: it bounds a hard kill's blast radius
to the file in flight, and the writer's own fence begin (which also snapshots the manifest the
deferred sweep works from) re-affirms it.

The embeddings a request holds together do not change for the collections where it matters:
contextual (CCE) embeddings (``docs__``/``rdr__``) and the onnx-local embedder. The engine embeds ONE
request's new chunks in ONE call, on both routes (``CombinedWriteService`` and
``PgVectorRepository.upsertChunksInternal`` each hand their request's texts to
``EmbedderRouter.embedForCollectionWithUsage`` once), and for those two families the old fallback's
requests were ``HttpVectorClient.upsert_chunks`` pages cut by ``_upsert_page_bounds(n, cap, None,
None)`` (the byte budget never applies to them). The writer cuts the file's rows into consecutive
requests of ``min(per_collection_chunk_cap(collection), 300)`` rows, the same ``cap`` and the same
boundaries; ``tests/test_rdr223_oversize_fallback.py`` pins the equality. A Voyage ``code__``
collection is the exception: the old paging also closed a page on ``_CODE_UPSERT_BYTE_BUDGET``, and
the writer cuts by count only, as the ChunkBatcher's flush already does. That embedding is a plain
(non-contextual) one, so no vector depends on which chunks share a request, and the engine's
``VoyageEmbedder`` plans sub-batches under the model's token budget, so a request of large chunks is
split there. A chunk whose chash a previous request of the run carried is not sent again, exactly as
the engine's existence partition would have skipped it on the later page.

What a re-index writes. The combined write REPLACES a stored chunk's metadata where the old
``upsert-chunks`` merged it, so a key another writer set on the chunk (``bib_*`` from
``nx enrich bib``) does not survive a re-index of an oversize file. The ChunkBatcher path that
writes every file that fits one batch already behaves that way. Code and prose chunks carry no such
key; a PDF chunk can. The engine's ``metadata_merge`` option (nexus-z0o2p.13, not merged yet) is the
fix for it, and this module is the one place to send it from.
"""
from __future__ import annotations

import structlog

from nexus.errors import CombinedWriteEmbedTimeoutError, IndexRunVerifyRefused

__all__ = ["OversizeWriteDeferred", "use_writer", "write_oversize_file"]

_log = structlog.get_logger(__name__)

#: HTTP statuses of a write the engine or its gateway refused for now, not for good.
_TRANSIENT_WRITE_STATUSES = frozenset({429, 502, 503, 504})


class OversizeWriteDeferred(RuntimeError):
    """The oversize file's write hit a transient condition (a gateway or rate-limit status, the
    embed timeout, or a connectivity error the writer's own bounded retry could not outlast).

    Raised only by :func:`write_oversize_file`, with the cause chained, after the writer marked the
    fence failed. ``indexer._contain_transient_upsert`` defers the file to the next run on it and
    nothing else: an ``httpx.HTTPStatusError`` raised elsewhere in the per-file path (the doc-id
    resolver, a hook) is not a write outcome and keeps propagating.
    """

    def __init__(self, *, doc_id: str, collection: str, cause: BaseException) -> None:
        self.doc_id = doc_id
        self.collection = collection
        super().__init__(
            f"oversize write of {doc_id!r} into {collection!r} deferred on a transient error "
            f"({type(cause).__name__}: {cause})")


def use_writer(db: object, batcher: object, catalog_doc_id: str) -> bool:
    """Whether a per-file fallback writes through the combined writer.

    The writer is for a service-backed T3 (``HttpVectorClient``, every real install) and a file
    with a catalog document (a file with none has no owner row to write; nexus-z0o2p.20 counts and
    stops those). A non-service ``db`` is the in-memory test topology, which the engine's combined
    write cannot reach, and keeps the old upsert. ``_run_index`` builds the ChunkBatcher for every
    ``HttpVectorClient``, so a service-backed db with no batcher is a broken invariant, not a
    topology: it raises rather than choosing a path.
    """
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred: the vector client imports back into catalog code

    if not is_service_backed(db):
        return False
    if batcher is None:
        raise RuntimeError(
            "the oversize fallback got a service-backed T3 and no ChunkBatcher: _run_index builds "
            "the batcher for every HttpVectorClient, so this is a wiring bug, not a topology")
    return bool(catalog_doc_id)


class _SweepAccountingCat:
    """Delegates to the catalog writer and records each write response's sweep accounting for the
    run summary, as ``manifest_write_batch_hook`` did on the old path: the rows swept, and every
    sweep the engine skipped (with its reason), so a skipped sweep is never silent."""

    def __init__(self, cat, collection: str) -> None:
        self._cat = cat
        self._collection = collection

    def __getattr__(self, name: str):
        return getattr(self._cat, name)

    def write_manifest_many(self, *args, **kwargs):
        return self._note(self._cat.write_manifest_many(*args, **kwargs))

    def append_manifest_chunks(self, *args, **kwargs):
        return self._note(self._cat.append_manifest_chunks(*args, **kwargs))

    def _note(self, resp):
        if not isinstance(resp, dict):
            return resp
        from nexus.mcp_infra import _record_superseded_sweep_skip, _record_superseded_swept  # noqa: PLC0415 — deferred: mcp_infra imports the indexers

        _record_superseded_swept(int(resp.get("swept") or 0))
        for outcome in resp.get("sweep_detail") or []:
            if isinstance(outcome, dict) and outcome.get("errored"):
                _record_superseded_sweep_skip(
                    str(outcome.get("doc_id", "")), self._collection,
                    str(outcome.get("reason") or "sweep_failed"))
        return resp


def write_oversize_file(
    *,
    catalog_doc_id: str,
    content_hash: str,
    collection: str,
    ids: list[str],
    documents: list[str],
    metadatas: list[dict],
    force_re_embed: bool = False,
):
    """Write one oversize file's chunks and owner rows through the combined writer.

    *ids*, *documents* and *metadatas* are the file's chunks, index-aligned, in file order; the
    manifest position of a chunk is its index. Returns the writer's
    :class:`~nexus.catalog.multi_batch_write.DocumentWriteResult`, or ``None`` when the engine
    refused the completion stamp: the chunks landed with their owner rows, the document stays
    ``indexing``, and the refusal is already in the record-level collector the run summary reads,
    as it was when the manifest hook swallowed it. Any other failure marks the fence ``failed``
    and propagates.
    """
    from nexus.catalog.multi_batch_write import MultiBatchDocumentWriter  # noqa: PLC0415 — deferred: multi_batch_write imports the vector client
    from nexus.mcp_infra import _manifest_chunk_rows, get_catalog_writer  # noqa: PLC0415 — deferred: mcp_infra imports the indexers

    rows = _manifest_chunk_rows([(i, {**m, "chunk_index": i}) for i, m in enumerate(metadatas)])
    # First occurrence wins for a chash repeated at several positions, as the old upsert
    # (first-wins in-batch dedup) and the ChunkBatcher's chunks_payload do: the stored chunk keeps
    # the first occurrence's metadata (its line span), and every position still gets its own row.
    chunks: list[dict] = []
    seen: set[str] = set()
    for cid, text, meta in zip(ids, documents, metadatas):
        if cid in seen:
            continue
        seen.add(cid)
        chunks.append({"chash": cid, "text": text, "metadata": meta})
    cat = get_catalog_writer()
    try:
        with MultiBatchDocumentWriter(
            _SweepAccountingCat(cat, collection), doc_id=catalog_doc_id, collection=collection,
            content_hash=content_hash, force_re_embed=force_re_embed,
        ) as writer:
            writer.add_batch(rows, chunks)
            return writer.finish()
    except IndexRunVerifyRefused:
        _log.warning(
            "oversize_write_complete_refused", doc_id=catalog_doc_id, collection=collection)
        return None
    except Exception as exc:  # noqa: BLE001 — classify a write outcome; anything not transient re-raises below
        if _is_transient_write_error(exc):
            raise OversizeWriteDeferred(
                doc_id=catalog_doc_id, collection=collection, cause=exc) from exc
        raise
    finally:
        close = getattr(cat, "close", None)
        if callable(close):
            close()


def _is_transient_write_error(exc: BaseException) -> bool:
    """A write outcome worth deferring the file over: the embed timeout, a transient HTTP status,
    or a connectivity error (begin, complete and sweep-only appends carry no retry beyond the
    writer's own bounded one)."""
    import httpx  # noqa: PLC0415 — deferred: only the failure arm needs the type

    from nexus.retry import _is_connectivity_error  # noqa: PLC0415 — deferred: nexus.retry pulls in the rate brake

    if isinstance(exc, CombinedWriteEmbedTimeoutError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        return resp is not None and resp.status_code in _TRANSIENT_WRITE_STATUSES
    return _is_connectivity_error(exc)
