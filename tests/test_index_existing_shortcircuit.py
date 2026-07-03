# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-h8rf6.4: chash-existence short-circuit before (server-side) embedding.

The 6.2.0 shakeout measured full-run service-mode indexing at ~4.7 files/min:
every chunk was sent to /v1/vectors/upsert-chunks and embedded via Voyage even
when its chash already existed in the collection (chunks are content-addressed
— an existing chash means IDENTICAL text, so the stored embedding is already
correct by construction). ``_upsert_skip_reembed`` pre-filters via
``existing_ids`` and sends existing chashes down the metadata-only
``update_chunks`` path (no embed), only new chashes down the full upsert.

Correctness invariants pinned here:
  - metadata is still REFRESHED for existing chashes (the pre-optimization
    upsert's ON CONFLICT DO UPDATE refreshed it; skipping outright would
    strand stale source_path/indexed_at)
  - a failed/empty existence probe falls back to the FULL upsert (the
    pre-optimization behavior — the probe is an optimization, never a gate)
  - non-service mode is untouched (local embeddings were already computed
    by the time the upsert runs; nothing to save)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.db.http_vector_client import HttpVectorClient
from nexus.doc_indexer import _upsert_skip_reembed

_COLL = "code__nexus-1-1__voyage-code-3__v1"
_IDS = ["aa" * 16, "bb" * 16, "cc" * 16]
_DOCS = ["doc-a", "doc-b", "doc-c"]
_EMB = [[], [], []]
_METAS = [{"source_path": "a.py"}, {"source_path": "b.py"}, {"source_path": "c.py"}]


def _service_db(monkeypatch, existing: set[str]) -> MagicMock:
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    db = MagicMock(spec=HttpVectorClient)
    db.existing_ids.return_value = existing
    return db


def test_mixed_existing_and_new_splits_the_batch(monkeypatch):
    db = _service_db(monkeypatch, existing={_IDS[0], _IDS[2]})
    sent = _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)
    assert sent == 1  # only the new chash goes to the embed path
    db.upsert_chunks_with_embeddings.assert_called_once()
    args = db.upsert_chunks_with_embeddings.call_args[0]
    assert args[1] == [_IDS[1]]
    assert args[2] == ["doc-b"]
    assert args[4] == [{"source_path": "b.py"}]
    # existing chashes get the metadata-only refresh, aligned by index
    db.update_chunks.assert_called_once_with(
        _COLL, [_IDS[0], _IDS[2]],
        [{"source_path": "a.py"}, {"source_path": "c.py"}],
    )


def test_all_existing_skips_upsert_entirely(monkeypatch):
    db = _service_db(monkeypatch, existing=set(_IDS))
    sent = _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)
    assert sent == 0
    db.upsert_chunks_with_embeddings.assert_not_called()
    db.update_chunks.assert_called_once_with(_COLL, _IDS, _METAS)


def test_none_existing_full_upsert_no_update(monkeypatch):
    db = _service_db(monkeypatch, existing=set())
    sent = _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)
    assert sent == 3
    db.upsert_chunks_with_embeddings.assert_called_once_with(
        _COLL, _IDS, _DOCS, _EMB, _METAS,
    )
    db.update_chunks.assert_not_called()


def test_probe_failure_falls_back_to_full_upsert(monkeypatch):
    # existing_ids resolves failures to set() internally; a raising probe
    # (non-Http db shapes, unexpected errors) must ALSO degrade to full
    # upsert — the probe is an optimization, never a gate.
    db = _service_db(monkeypatch, existing=set())
    db.existing_ids.side_effect = RuntimeError("probe exploded")
    sent = _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)
    assert sent == 3
    db.upsert_chunks_with_embeddings.assert_called_once()
    db.update_chunks.assert_not_called()


def test_non_service_mode_is_untouched(monkeypatch):
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: False,
    )
    db = MagicMock(spec=HttpVectorClient)
    sent = _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)
    assert sent == 3
    db.existing_ids.assert_not_called()
    db.upsert_chunks_with_embeddings.assert_called_once_with(
        _COLL, _IDS, _DOCS, _EMB, _METAS,
    )


def test_db_without_existing_ids_shape_falls_back(monkeypatch):
    # Non-Http db object without the probe method (test stubs, legacy
    # T3Database shapes) must not crash — full upsert.
    monkeypatch.setattr(
        "nexus.db.http_vector_client.is_vector_service_mode", lambda: True,
    )
    class Bare:
        def __init__(self):
            self.upserts = []
        def upsert_chunks_with_embeddings(self, *a):
            self.upserts.append(a)
    db = Bare()
    sent = _upsert_skip_reembed(db, _COLL, _IDS, _DOCS, _EMB, _METAS)
    assert sent == 3
    assert len(db.upserts) == 1


def test_empty_ids_noop(monkeypatch):
    db = _service_db(monkeypatch, existing=set())
    assert _upsert_skip_reembed(db, _COLL, [], [], [], []) == 0
    db.upsert_chunks_with_embeddings.assert_not_called()
