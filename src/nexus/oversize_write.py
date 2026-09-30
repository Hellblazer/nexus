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

The embeddings a request holds together do not change, which is what keeps contextual (CCE)
embeddings stable. The engine embeds ONE request's new chunks in ONE call, on both routes
(``CombinedWriteService`` and ``PgVectorRepository.upsertChunksInternal`` each hand their request's
texts to ``EmbedderRouter.embedForCollectionWithUsage`` once), and the old fallback's requests were
``HttpVectorClient.upsert_chunks`` pages cut by ``_upsert_page_bounds(n, cap, None, None)`` (the
byte budget never applies to CCE). The writer cuts the file's rows into consecutive requests of
``min(per_collection_chunk_cap(collection), 300)`` rows, the same ``cap`` and the same boundaries;
``tests/test_rdr223_oversize_fallback.py`` pins the equality. A chunk whose chash a previous request
of the run carried is not sent again, exactly as the engine's existence partition would have
skipped it on the later page.

What a re-index writes. The combined write REPLACES a stored chunk's metadata where the old
``upsert-chunks`` merged it, so a key another writer set on the chunk (``bib_*`` from
``nx enrich bib``) does not survive a re-index of an oversize file. The ChunkBatcher path that
writes every file that fits one batch already behaves that way. Code and prose chunks carry no such
key; a PDF chunk can. The engine's ``metadata_merge`` option (nexus-z0o2p.13, not merged yet) is the
fix for it, and this module is the one place to send it from.
"""
from __future__ import annotations

import structlog

from nexus.errors import IndexRunVerifyRefused

__all__ = ["write_oversize_file"]

_log = structlog.get_logger(__name__)


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
    chunks = [
        {"chash": cid, "text": text, "metadata": meta}
        for cid, text, meta in zip(ids, documents, metadatas)
    ]
    cat = get_catalog_writer()
    try:
        with MultiBatchDocumentWriter(
            _SweepAccountingCat(cat, collection), doc_id=catalog_doc_id, collection=collection, content_hash=content_hash,
            force_re_embed=force_re_embed,
        ) as writer:
            writer.add_batch(rows, chunks)
            return writer.finish()
    except IndexRunVerifyRefused:
        _log.warning(
            "oversize_write_complete_refused", doc_id=catalog_doc_id, collection=collection)
        return None
    finally:
        close = getattr(cat, "close", None)
        if callable(close):
            close()
