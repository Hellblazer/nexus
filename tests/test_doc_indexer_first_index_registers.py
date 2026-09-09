# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-204 Phase 2 client half (nexus-ft04v.16): a first index into a
brand-new collection registers it BEFORE the incremental-sync read.

The engine answers any read of a collection with no ``catalog_collections``
row with 422. ``_index_document`` and ``index_pdf`` both run an
incremental-sync pre-check READ (``col.get(where=...)``) before the
document's first write, so on the Phase 2 engine a never-seen collection
made every first ``nx index md`` / ``nx index pdf`` fail at that read.
Registration used to happen only inside the write path.

These tests pin the ORDER (register, then read) at the unit level with the
same injectable registrar the write path honours; the end-to-end proof is
the real-CLI journeys in tests/test_scenario_journeys.py,
tests/test_collection_reindex_e2e.py and
tests/test_collection_reindex_shared_client_fanout.py, which were the four
reds this fix turns green.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus import corpus as corpus_mod
from nexus import doc_indexer


@pytest.fixture(autouse=True)
def _fresh_registration_cache():
    with corpus_mod._REGISTERED_COLLECTIONS_LOCK:
        saved = set(corpus_mod._REGISTERED_COLLECTIONS)
        corpus_mod._REGISTERED_COLLECTIONS.clear()
    try:
        yield
    finally:
        with corpus_mod._REGISTERED_COLLECTIONS_LOCK:
            corpus_mod._REGISTERED_COLLECTIONS.clear()
            corpus_mod._REGISTERED_COLLECTIONS.update(saved)


def _ordered_db(events: list[str]) -> MagicMock:
    """A vector-client double whose ``col.get`` records its call, and whose
    injectable registrar records registration, so the order is observable."""
    writer = MagicMock()
    writer.register_collection.side_effect = lambda name, **kw: events.append(
        f"register:{name}"
    )
    db = MagicMock()
    db._collection_registrar = lambda: writer
    db.collection_exists.return_value = False
    col = MagicMock()

    def _get(*_a, **_kw):
        events.append("read")
        return {"metadatas": [], "ids": []}

    col.get.side_effect = _get
    db.get_or_create_collection.return_value = col
    return db


def test_index_document_registers_before_the_incremental_read(tmp_path: Path):
    events: list[str] = []
    db = _ordered_db(events)
    md = tmp_path / "a.md"
    md.write_text("# t\n\nbody\n")
    name = "docs__ft04v16-first-index__voyage-context-3__v1"
    # An empty chunk_fn keeps the tail (embed/upsert) trivial; the point
    # under test is the ordering of registration and the pre-check read.
    with patch.object(doc_indexer, "_vector_with_retry", side_effect=lambda fn, **kw: fn(**kw)), \
         patch.object(doc_indexer, "_identity_where", return_value={"x": "y"}):
        doc_indexer._index_document(
            md, "ft04v16-first-index", lambda *a, **k: [], t3=db,
            collection_name=name,
        )
    assert events, "the double saw neither a registration nor a read: the test is vacuous"
    assert events[0] == f"register:{name}", events
    assert "read" in events, events
    assert events.index(f"register:{name}") < events.index("read"), events


def test_registration_uses_the_db_registrar_the_write_path_honours(tmp_path: Path):
    """The registrar comes from the vector client (``_collection_registrar``),
    exactly as ``HttpVectorClient.put`` passes it, so a fake writer in a test
    and the real catalog writer in production are the same seam."""
    events: list[str] = []
    db = _ordered_db(events)
    md = tmp_path / "b.md"
    md.write_text("# t\n\nbody\n")
    name = "docs__ft04v16-registrar__voyage-context-3__v1"
    with patch.object(doc_indexer, "_vector_with_retry", side_effect=lambda fn, **kw: fn(**kw)), \
         patch.object(doc_indexer, "_identity_where", return_value={"x": "y"}), \
         patch("nexus.catalog.factory.make_catalog_writer", side_effect=AssertionError("default registrar must not be used when the db carries one")):
        doc_indexer._index_document(
            md, "ft04v16-registrar", lambda *a, **k: [], t3=db,
            collection_name=name,
        )
    assert events[0] == f"register:{name}", events
