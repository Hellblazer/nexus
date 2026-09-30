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

What a re-index writes. The catalog writer is wrapped in
:class:`~nexus.catalog.metadata_merging_catalog.MetadataMergingCatalog`, which sends the engine's
``metadata_merge`` mode with ``rewrite_delete_keys(metadatas)`` on every chunk-carrying request: the
old ``upsert-chunks`` semantics. A key another writer set on a stored chunk (``bib_*`` from
``nx enrich bib``) survives a re-index, and a key this writer owns and dropped from a row is
cleared. The same wrapper carries the run summary's sweep accounting.
"""
from __future__ import annotations

import structlog

from nexus.errors import CombinedWriteEmbedTimeoutError, IndexRunVerifyRefused

__all__ = [
    "OversizeWriteDeferred", "refuse_identity_less_file", "use_writer", "write_oversize_file",
]

_log = structlog.get_logger(__name__)

#: HTTP statuses of a write the engine or its gateway refused for now, not for good.
_TRANSIENT_WRITE_STATUSES = frozenset({429, 502, 503, 504})


class OversizeWriteDeferred(RuntimeError):
    """The oversize file's write hit a transient condition (a gateway or rate-limit status, the
    embed timeout, or a connectivity error the writer's own bounded retry could not outlast).

    Raised only by :func:`write_oversize_file`, with the cause chained. The fence state depends on
    where the transient hit: a failure after the writer's fence begin leaves the fence ``failed``
    (the writer's abort marks it); a failure AT the begin call leaves it ``indexing`` (abort is a
    no-op when nothing was fenced), from the caller's early ``_fence_begin``. Either way the next
    run re-indexes the file (``never_fresh`` covers both states).
    ``indexer._contain_transient_upsert`` defers the file to the next run on it and
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
    topology: it raises rather than choosing a path. Identity is tested first: a file with no
    catalog document never needs the writer, so it never reaches the batcher check either.
    """
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred: the vector client imports back into catalog code

    if not is_service_backed(db):
        return False
    if not catalog_doc_id:
        return False
    if batcher is None:
        raise RuntimeError(
            "the oversize fallback got a service-backed T3 and no ChunkBatcher: _run_index builds "
            "the batcher for every HttpVectorClient, so this is a wiring bug, not a topology")
    return True


def refuse_identity_less_file(
    db: object, catalog_doc_id: str, file_path: object, collection: str, chunk_count: int,
) -> bool:
    """Write nothing for an oversize file with no catalog document; True when refused.

    RDR-223 (nexus-z0o2p.20): a chunk is written together with its owner row, and a file with no
    catalog document has no owner. The fallback used to write such a file with the ownerless
    ``upsert-chunks`` (stored, hidden from every read by ``live(c)``, and refused by the engine
    from the paired release on). On a service-backed T3 it now records the file in the same
    identity-drop collector the flush route uses (``written=False``, so the run summary names it
    and the run fails) and the caller returns without writing or firing hooks. A non-service T3
    (the in-memory test topology) has no owner concept and keeps its write: this returns False.

    ``_run_index`` refuses such a file before it is ever dispatched, so this is the backstop for
    a file that reaches a fallback anyway (a direct caller, or a hook that lost the id mid-run).
    The caller's return value (0) reads to the progress counter as a skipped file; the drop
    collector, not that counter, is the record.
    """
    from nexus.db.http_vector_client import is_service_backed  # noqa: PLC0415 — deferred: the vector client imports back into catalog code

    if catalog_doc_id or not is_service_backed(db):
        return False
    from nexus.mcp_infra import _record_manifest_identity_drop  # noqa: PLC0415 — deferred: mcp_infra imports the indexers

    cause = "oversize_no_catalog_document"
    _record_manifest_identity_drop(
        collection, chunk_count, written=False,
        files=[{"file": str(file_path), "chunks": chunk_count, "cause": cause}],
    )
    _log.warning(
        "oversize_identity_less_file_not_written",
        file=str(file_path), collection=collection, chunks_not_written=chunk_count, cause=cause,
    )
    return True


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
    from nexus.catalog.metadata_merging_catalog import MetadataMergingCatalog  # noqa: PLC0415 — deferred: rare oversize path
    from nexus.catalog.multi_batch_write import MultiBatchDocumentWriter  # noqa: PLC0415 — deferred: multi_batch_write imports the vector client
    from nexus.mcp_infra import _manifest_chunk_rows, get_catalog_writer  # noqa: PLC0415 — deferred: mcp_infra imports the indexers
    from nexus.metadata_schema import rewrite_delete_keys  # noqa: PLC0415 — circular-dep avoidance (nexus.metadata_schema)

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
    merging = MetadataMergingCatalog(cat, collection, rewrite_delete_keys(metadatas))
    try:
        with MultiBatchDocumentWriter(
            merging, doc_id=catalog_doc_id, collection=collection,
            content_hash=content_hash, force_re_embed=force_re_embed,
        ) as writer:
            writer.add_batch(rows, chunks)
            result = writer.finish()
        merging.account_unexplained_skips(catalog_doc_id, result.sweep_skipped)
        return result
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
    writer's own bounded one).

    The rule decides on the exception that ENDED the write, not on whatever happened to be in
    flight when it was raised. A transport error counts when it is the exception itself or an
    explicit ``raise ... from`` cause of it (walked along ``__cause__``). An incidental
    ``__context__`` does not count: ``RefreshableHttpStoreMixin._request`` runs its re-resolve retry
    inside the ``except`` block of the first attempt, so a permanent error raised by the retry
    carries the first attempt's transport error as ``__context__`` and would otherwise be deferred
    as a blip instead of aborting the run. (The shared ``nexus.retry._is_connectivity_error`` reads
    ``__context__`` too, and is right to for its manifest-retry and eviction uses; this site is the
    one that turns a classification into "skip the file".) A genuine connect failure that the
    client's retry could not outlast is itself a transport error at the top level, so it still
    defers. The same rule aborts the run when the client's re-resolve gives up: it raises
    ``ServiceEndpointUnresolvableError`` (a ``RuntimeError``, no ``from``) inside the handler of
    the ``ConnectError`` that triggered it, after its bounded lease wait. That is deliberate:
    an endpoint that cannot be resolved would make every remaining file wait out the same bound.
    """
    import httpx  # noqa: PLC0415 — deferred: only the failure arm needs the type

    if isinstance(exc, CombinedWriteEmbedTimeoutError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        return resp is not None and resp.status_code in _TRANSIENT_WRITE_STATUSES
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, (httpx.TransportError, ConnectionError, TimeoutError)):
            return True
        seen.add(id(cur))
        cur = cur.__cause__
    return False
