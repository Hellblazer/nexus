# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1 fixes from the 4.28.0 -> 5.6.0 local-mode upgrade shakeout (#1057, #1058).

* #1057 — ``rename_collection_cascade`` referenced the dropped
  ``document_aspects.source_path`` column, so *every* collection rename failed
  (``OperationalError: no such column: source_path``) on any DB whose aspect
  PK had migrated to ``doc_id`` (RDR-108 Phase 1c) with ``source_path`` dropped
  (RDR-096 P5.2). Both PK migration and the column drop are deferred until a
  catalog exists, so a DB can be in either shape — the fix resolved the
  dedup column from the live schema (``doc_id`` when present, else
  ``source_path``), matching the real PRIMARY KEY in each. (HISTORICAL:
  that SQLite cascade + probe died in nexus-i711w sub-stage A3; see the
  tombstone below.)
* #1058 — the local tier-1 (bge) embedding function pre-converted fastembed's
  numpy arrays to Python lists; chromadb >= 1.x calls ``.tolist()`` on each
  element itself, so this raised ``'list' object has no attribute 'tolist'``
  and broke *all* local-mode search. The EF now returns numpy arrays.
"""
from __future__ import annotations


# ── #1057a: dedup-column resolution — DELETED ────────────────────────────────
#
# TestRenameDedupCol (3 tests) DELETED in nexus-i711w Stage 2 sub-stage A3:
# its subject was ``_rename_dedup_col``, the live-schema PRIMARY-KEY probe
# inside the SQLite ``rename_collection_cascade`` (doc_id vs source_path,
# needed because the PK migration was catalog-deferred so a local DB could be
# in either shape). The cascade is now a pure HTTP fan-out to the engine
# stores (no sqlite3 connection, no schema probe — engine schema is
# Liquibase-managed, always doc_id), and the helper died with it. The
# surviving cascade contract is pinned by tests/test_rename_lock_t1_1.py and
# the per-store rename_collection tests in tests/db/test_http_aspects_stores.py.


# ── #1058: local tier-1 EF returns numpy arrays, not pre-converted lists ─────


class TestLocalEfReturnsNumpyArrays:
    def test_tier1_branch_returns_numpy_arrays(self) -> None:
        import numpy as np

        from nexus.db.local_ef import LocalEmbeddingFunction

        ef = LocalEmbeddingFunction.__new__(LocalEmbeddingFunction)
        ef._model_name = "BAAI/bge-base-en-v1.5"  # non-tier-0 -> tier-1 branch
        ef._dimensions = 3

        class _FakeFastembed:
            def embed(self, texts):
                for _ in texts:
                    yield np.array([0.1, 0.2, 0.3], dtype=np.float32)

        ef._ef = _FakeFastembed()

        out = ef(["one", "two"])

        assert len(out) == 2
        for vec in out:
            assert isinstance(vec, np.ndarray), f"expected np.ndarray, got {type(vec)}"
            assert hasattr(vec, "tolist")
