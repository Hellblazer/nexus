# SPDX-License-Identifier: AGPL-3.0-or-later
"""A test double for ``doc_indexer._write_chunks_with_owner_rows`` (RDR-223, nexus-z0o2p.13).

``_index_document`` writes a document's chunks and its owner rows as ONE request to the engine. A
test whose subject is something else (chunk metadata, the staleness gate, hook firing, CLI
summaries) and that runs against a fake T3 handle cannot let that write reach the real engine: the
fake handle holds none of the chunks, and the engine refuses fake ids and model tokens. Such a test
replaces the write with this recorder and asserts on what the indexer PASSED to it; the write's own
behaviour has real-engine coverage in ``tests/integration/test_rdr223_index_document_journey.py``.
"""
from __future__ import annotations

from typing import Any

from nexus.catalog.multi_batch_write import DocumentWriteResult


class PendingStamp:
    """What a ``defer_completion`` write hands back (``doc_indexer._DeferredOwnerWrite``): the stamp
    the caller sends after its hooks. ``complete()`` logs to the recorder's shared ``events`` (so a
    test can assert the ORDER of the write, the hooks and the stamp) and raises what the test asked
    it to, as the engine's refusal or a kill would."""

    def __init__(self, recorder: "OwnerWriteRecorder", doc_id: str, result: DocumentWriteResult) -> None:
        self._rec = recorder
        self.doc_id = doc_id
        self.result = result
        self.closed = 0

    def complete(self) -> DocumentWriteResult:
        self._rec.events.append(("complete", self.doc_id))
        if self._rec.complete_raises is not None:
            raise self._rec.complete_raises
        return self.result

    def close(self) -> None:
        self.closed += 1


class OwnerWriteRecorder:
    """Records each call and answers like a write that fully succeeded."""

    def __init__(
        self, *, raises: BaseException | None = None, request_sent: bool = False,
        complete_raises: BaseException | None = None,
    ) -> None:
        #: True: the recorded write reports that it sent a request (``on_request``) before it
        #: raised, as the real writer does once it has begun writing. The real writer also reports
        #: it AFTER a request came back, so a write that returns normally has reported it too (set
        #: this for a test whose subject is what a later failure does with a registration).
        self.request_sent = request_sent
        #: Raised by the deferred stamp's ``complete()`` (an ``IndexRunVerifyRefused`` for a refusal).
        self.complete_raises = complete_raises
        #: Ordered ``("write", doc_id)`` / ``("complete", doc_id)`` entries; a test's hook stubs
        #: append their own markers to it.
        self.events: list[tuple] = []
        #: Every deferred stamp handed out.
        self.pendings: list[PendingStamp] = []
        self.calls: list[dict[str, Any]] = []
        #: Every metadata dict passed, across calls, in order. A live list: a test may
        #: hold it before the write runs.
        self.metadatas: list[dict] = []
        self.raises = raises
        self._t3: Any = None

    def forward_to(self, t3: Any) -> "OwnerWriteRecorder":
        """Also put each call's chunks into a fake T3 handle, for a test that reads them back."""
        self._t3 = t3
        return self

    def __call__(
        self, collection_name: str, doc_id: str, content_hash: str, ids: list[str],
        documents: list[str], metadatas: list[dict], *, force_re_embed: bool = False,
        batch_size: int = 0, on_progress: Any = None, on_request: Any = None,
        defer_completion: bool = False,
    ) -> "DocumentWriteResult | PendingStamp":
        self.calls.append({
            "collection_name": collection_name, "doc_id": doc_id, "content_hash": content_hash,
            "ids": list(ids), "documents": list(documents), "metadatas": metadatas,
            "force_re_embed": force_re_embed, "batch_size": batch_size,
            "defer_completion": defer_completion,
        })
        self.metadatas.extend(metadatas)
        self.events.append(("write", doc_id))
        if self.raises is not None:
            # A write that fails reports a request only when the failure left a request sent or
            # open (request_sent); a clean refusal of the first request reports nothing.
            if self.request_sent and on_request is not None:
                on_request()
            raise self.raises
        if on_request is not None:
            on_request()    # the requests came back: the real writer has reported them
        if on_progress is not None:
            total = len(ids)
            if 0 < batch_size < total:
                for start in range(batch_size, total, batch_size):
                    on_progress(start, total)
            on_progress(total, total)
        if self._t3 is not None:
            self._t3.upsert_chunks(
                collection_name, list(ids), list(documents), list(metadatas),
                force_re_embed=force_re_embed)
        result = DocumentWriteResult(
            batches=1, requests=1, chunks_written=len(ids), distinct_chashes=len(set(ids)),
            completed=not defer_completion)
        if defer_completion:
            pending = PendingStamp(self, doc_id, result)
            self.pendings.append(pending)
            return pending
        return result


def install(
    monkeypatch: Any, *, raises: BaseException | None = None, request_sent: bool = False,
    complete_raises: BaseException | None = None,
) -> OwnerWriteRecorder:
    """Replace the write with a recorder for the life of the test."""
    import nexus.doc_indexer as di

    real = di._write_chunks_with_owner_rows
    rec = OwnerWriteRecorder(raises=raises, request_sent=request_sent, complete_raises=complete_raises)
    rec.restore_real_write = lambda: monkeypatch.setattr(  # type: ignore[attr-defined]
        "nexus.doc_indexer._write_chunks_with_owner_rows", real)
    monkeypatch.setattr("nexus.doc_indexer._write_chunks_with_owner_rows", rec)
    return rec


# ── the streaming PDF pipeline's writer (RDR-223, nexus-z0o2p.11) ─────────────────────────────


class RecordingWriter:
    """Stands in for ``MultiBatchDocumentWriter`` in a test that drives ``uploader_loop`` or
    ``pipeline_index_pdf`` against fake chunk ids and a fake T3 handle, which the real engine
    would refuse. It records every call and keeps the writer's one-deep hold: a batch is SENT when
    the next one arrives (or at ``finish``), which is what the uploader's flag-and-hook ordering
    depends on. ``events`` (shared across writers of one install) is the ordered log of
    ``("add", n)``, ``("sent", n)``, ``("finish",)`` and ``("complete",)``."""

    events: list[tuple] = []
    instances: list["RecordingWriter"] = []

    def __init__(self, cat: Any, *, doc_id: str, collection: str, content_hash: str | None = None,
                 run_id: str | None = None, embedding_model: str | None = None,
                 force_re_embed: bool = False, chunk_cap: int | None = None,
                 defer_completion: bool = False) -> None:
        self.cat = cat
        self.kwargs = {
            "doc_id": doc_id, "collection": collection, "content_hash": content_hash,
            "force_re_embed": force_re_embed, "defer_completion": defer_completion,
        }
        self.batches: list[tuple[list[dict], list[dict]]] = []
        self.finished = False
        self.completed = False
        self._held: int | None = None
        type(self).instances.append(self)

    def __enter__(self) -> "RecordingWriter":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def add_batch(self, rows: Any, chunks: Any) -> None:
        n = len(self.batches)
        self.batches.append(([dict(r) for r in rows], [dict(c) for c in chunks]))
        type(self).events.append(("add", n))
        if self._held is not None:
            type(self).events.append(("sent", self._held))
        self._held = n

    def finish(self, *, allow_empty: bool = False) -> DocumentWriteResult:
        t3 = getattr(type(self), "forward_t3", None)
        if t3 is not None:
            chunks = [c for _, cs in self.batches for c in cs]
            if chunks:
                t3.upsert_chunks(
                    self.kwargs["collection"], [c["chash"] for c in chunks],
                    [c["text"] for c in chunks], [c["metadata"] for c in chunks],
                    force_re_embed=self.kwargs["force_re_embed"])
        if self._held is not None:
            type(self).events.append(("sent", self._held))
            self._held = None
        type(self).events.append(("finish",))
        self.finished = True
        rows = sum(len(r) for r, _ in self.batches)
        return DocumentWriteResult(
            batches=len(self.batches), requests=len(self.batches), chunks_written=rows,
            distinct_chashes=rows, manifest_rows=rows,
            completed=not self.kwargs["defer_completion"])

    def complete(self) -> DocumentWriteResult:
        assert self.finished, "complete() before finish()"
        type(self).events.append(("complete",))
        self.completed = True
        return DocumentWriteResult(completed=True)

    def abort(self, error: str) -> None:
        pass


class _NullCatalogWriter:
    def close(self) -> None:
        pass


def install_streaming_writer(monkeypatch: Any, *, forward_to: Any = None) -> type[RecordingWriter]:
    """Replace the multi-batch writer (and the catalog writer the streaming run would send
    through; the run's own registration keeps the real one) with :class:`RecordingWriter` for the
    life of the test. Returns a fresh recorder class holding ``events`` and ``instances``.
    *forward_to* also puts each finished document's chunks into a fake T3 handle, for a test that
    searches them afterwards."""

    class Recorder(RecordingWriter):
        events: list[tuple] = []
        instances: list[RecordingWriter] = []
        forward_t3: Any = forward_to

    import nexus.catalog.multi_batch_write as mbw
    import nexus.pipeline_stages as ps

    real_writer, real_catalog = mbw.MultiBatchDocumentWriter, ps._make_upload_catalog
    monkeypatch.setattr("nexus.catalog.multi_batch_write.MultiBatchDocumentWriter", Recorder)
    monkeypatch.setattr("nexus.pipeline_stages._make_upload_catalog", lambda: _NullCatalogWriter())

    def restore_real_writer() -> None:
        """For a test that reads the real engine back: the real writer and catalog again."""
        monkeypatch.setattr("nexus.catalog.multi_batch_write.MultiBatchDocumentWriter", real_writer)
        monkeypatch.setattr("nexus.pipeline_stages._make_upload_catalog", real_catalog)

    Recorder.restore_real_writer = staticmethod(restore_real_writer)  # type: ignore[attr-defined]
    return Recorder


# ── the dry run's throwaway store (RDR-223) ───────────────────────────────────────────────────


def throwaway_t3() -> Any:
    """The store ``nx index pdf --dry-run`` builds: a ``T3Database`` over an in-memory client, which
    is the only store a PDF dry run accepts (``doc_indexer._require_throwaway_store``). Embedding is
    refused, as in the CLI: a preview embeds nothing."""
    from unittest.mock import MagicMock

    from nexus.db import make_t3
    from nexus.db.inmemory_vector_store import InMemoryVectorClient

    return make_t3(_client=InMemoryVectorClient(), _ef_override=MagicMock())
