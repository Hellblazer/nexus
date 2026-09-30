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


class OwnerWriteRecorder:
    """Records each call and answers like a write that fully succeeded."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
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
    ) -> DocumentWriteResult:
        self.calls.append({
            "collection_name": collection_name, "doc_id": doc_id, "content_hash": content_hash,
            "ids": list(ids), "documents": list(documents), "metadatas": metadatas,
            "force_re_embed": force_re_embed,
        })
        self.metadatas.extend(metadatas)
        if self.raises is not None:
            raise self.raises
        if self._t3 is not None:
            self._t3.upsert_chunks(
                collection_name, list(ids), list(documents), list(metadatas),
                force_re_embed=force_re_embed)
        return DocumentWriteResult(
            batches=1, requests=1, chunks_written=len(ids), distinct_chashes=len(set(ids)),
            completed=True)


def install(monkeypatch: Any, *, raises: BaseException | None = None) -> OwnerWriteRecorder:
    """Replace the write with a recorder for the life of the test."""
    import nexus.doc_indexer as di

    real = di._write_chunks_with_owner_rows
    rec = OwnerWriteRecorder(raises=raises)
    rec.restore_real_write = lambda: monkeypatch.setattr(  # type: ignore[attr-defined]
        "nexus.doc_indexer._write_chunks_with_owner_rows", real)
    monkeypatch.setattr("nexus.doc_indexer._write_chunks_with_owner_rows", rec)
    return rec
