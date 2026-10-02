# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for ``nexus.catalog.note_write.write_note`` for tests whose subject is not the write.

RDR-223 P2.2 (nexus-z0o2p.12): MCP ``store_put`` writes a note's pieces and manifest to the engine in
one request. Many older tests exercise ``store_put`` only to have a document to list, search, get or
delete, and they hold a FAKE in-memory T3 (``T3Database`` over ``InMemoryVectorClient``) whose
collection names may name a model the test engine cannot embed. For those tests the note has to land
in the fake T3, where they look for it, with its manifest in the engine catalog.

:func:`route_note_writes_to` does that with the split write this repo used before P2.2, rebuilt
here as a fixture now that no production code does it (nexus-z0o2p.32): ``t3.put`` of each piece into
the fake T3, then the manifest seeded through ``tests._catalog_fixture_ops.seed_note_manifest``.
Nothing here tests the write itself; ``tests/test_z0o2p12_note_write.py`` and ``tests/test_b6enc_store_put_ghost_compensation.py``
do, against the real engine.
"""
from __future__ import annotations

from typing import Any

import pytest


def route_note_writes_to(monkeypatch: pytest.MonkeyPatch, t3: Any) -> None:
    """Make ``store_put``'s note write land in *t3* (a fake) plus the real engine catalog."""
    from nexus.catalog import note_write, store_hook
    from tests import _catalog_fixture_ops

    def _write_note(
        *, catalog_doc_id, collection, pieces, content_hash=None, title="", tags="", category="",
        session_id="", source_agent="", ttl_days=None, content_type="prose", cat=None, stamp=True,
    ):
        ids = [
            t3.put(
                collection=collection, content=piece, title=title, tags=tags, category=category,
                session_id=session_id, source_agent=source_agent, ttl_days=ttl_days,
                catalog_doc_id=catalog_doc_id,
            )
            for piece in pieces
        ]
        _first, metadatas = store_hook.note_manifest_metadata(list(pieces))
        # The engine's owner-row foreign key needs a real chunk row for each chash.
        # Looked up at call time: a test may replace it (the greenfield promotion test does).
        _catalog_fixture_ops.seed_manifest_chunks(collection, ids)
        _catalog_fixture_ops.seed_note_manifest(catalog_doc_id, metadatas, collection=collection)
        return note_write.NoteWriteResult(
            catalog_doc_id=catalog_doc_id, collection=collection, chunk_ids=list(ids),
            chunks_written=len(ids), completed=bool(content_hash) and stamp,
        )

    monkeypatch.setattr(note_write, "write_note", _write_note)
