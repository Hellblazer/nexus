# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-223 (nexus-z0o2p.11 / .15): a PDF dry run is confined to a throwaway in-memory store by
construction, not by the one CLI call site that happens to build one.

The dry run is the one place a PDF path still puts chunks in a store without their owner rows, and
that is only safe because the store is discarded with the process. ``index_pdf(dry_run=True)``
given no ``t3`` resolves the ENGINE's client (``get_t3()`` in service mode), and would then send
ownerless ``upsert-chunks`` requests the engine is about to refuse (422). So every dry-run entry
refuses a store that is not in-memory, before it touches the engine, and ``_preview_upsert`` refuses
as the last line of defence.

The real-engine case ("writes nothing to the engine") is in
``tests/integration/test_rdr223_pdf_journey.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.db import make_t3
from nexus.db.inmemory_vector_store import InMemoryVectorClient
from nexus.errors import DryRunStoreError


def _throwaway():
    return make_t3(_client=InMemoryVectorClient(), _ef_override=MagicMock())


def test_preview_upsert_writes_into_an_in_memory_store() -> None:
    from nexus.doc_indexer import _preview_upsert

    t3 = _throwaway()
    _preview_upsert(t3, "docs__dry__minilm-l6-v2-384__v1", ["a" * 64], ["text"], [[0.1] * 4],
                    [{"content_hash": "h"}])
    col = t3.get_or_create_collection("docs__dry__minilm-l6-v2-384__v1")
    assert col.get(ids=["a" * 64])["ids"] == ["a" * 64]


@pytest.mark.parametrize("store", ["mock", "engine_client"])
def test_preview_upsert_refuses_a_store_that_is_not_in_memory(store) -> None:
    from nexus.db.http_vector_client import HttpVectorClient
    from nexus.doc_indexer import _preview_upsert

    t3 = MagicMock() if store == "mock" else HttpVectorClient()
    with pytest.raises(DryRunStoreError, match="in-memory"):
        _preview_upsert(t3, "docs__dry__minilm-l6-v2-384__v1", ["a" * 64], ["text"], [[0.1] * 4],
                        [{}])
    if store == "mock":
        t3.upsert_chunks_with_embeddings.assert_not_called()


@pytest.mark.parametrize("entry", ["index_pdf", "pipeline_index_pdf", "index_pdf_incremental"])
def test_every_dry_run_entry_refuses_before_it_touches_the_store(entry, tmp_path: Path) -> None:
    """The refusal is at the top of each entry: nothing has been registered, opened or read on the
    store it was given (the engine's client registers collections and reads on first contact)."""
    from nexus import doc_indexer, pipeline_stages

    engine = MagicMock(name="engine_client")
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF-1.4 dry")
    with pytest.raises(DryRunStoreError):
        if entry == "index_pdf":
            doc_indexer.index_pdf(pdf, "c", t3=engine, dry_run=True,
                                  collection_name="docs__dry__minilm-l6-v2-384__v1")
        elif entry == "pipeline_index_pdf":
            pipeline_stages.pipeline_index_pdf(
                pdf, "h", "docs__dry__minilm-l6-v2-384__v1", engine, dry_run=True, db=MagicMock())
        else:
            doc_indexer._index_pdf_incremental(
                pdf, "c", [("a" * 64, "t", {"embedding_model": "m"})], "h",
                "docs__dry__minilm-l6-v2-384__v1", engine, dry_run=True)
    assert engine.mock_calls == [], "the refused dry run did not touch the store"
