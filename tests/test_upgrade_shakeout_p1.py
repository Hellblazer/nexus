# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1 fixes from the 4.28.0 -> 5.6.0 local-mode upgrade shakeout (#1057, #1058).

* #1057 — ``rename_collection_cascade`` referenced the dropped
  ``document_aspects.source_path`` column, so *every* collection rename failed
  (``OperationalError: no such column: source_path``) on any DB whose aspect
  PK had migrated to ``doc_id`` (RDR-108 Phase 1c) with ``source_path`` dropped
  (RDR-096 P5.2). Both PK migration and the column drop are deferred until a
  catalog exists, so a DB can be in either shape — the cascade now resolves the
  dedup column from the live schema (``doc_id`` when present, else
  ``source_path``), matching the real PRIMARY KEY in each.
* #1058 — the local tier-1 (bge) embedding function pre-converted fastembed's
  numpy arrays to Python lists; chromadb >= 1.x calls ``.tolist()`` on each
  element itself, so this raised ``'list' object has no attribute 'tolist'``
  and broke *all* local-mode search. The EF now returns numpy arrays.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import os

import pytest


# ── #1057a: dedup-column resolution picks the live PRIMARY KEY ───────────────


class TestRenameDedupCol:
    def _table(self, *cols: str) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(f"CREATE TABLE t ({', '.join(c + ' TEXT' for c in cols)})")
        return conn

    def test_prefers_doc_id_when_present(self) -> None:
        from nexus.db.t2 import _rename_dedup_col

        conn = self._table("collection", "doc_id", "source_uri")
        assert _rename_dedup_col(conn, "t") == "doc_id"

    def test_falls_back_to_source_path_when_no_doc_id(self) -> None:
        from nexus.db.t2 import _rename_dedup_col

        conn = self._table("collection", "source_path", "source_uri")
        assert _rename_dedup_col(conn, "t") == "source_path"

    def test_raises_when_neither_present(self) -> None:
        from nexus.db.t2 import _rename_dedup_col

        conn = self._table("collection", "source_uri")
        with pytest.raises(RuntimeError, match="neither doc_id nor source_path"):
            _rename_dedup_col(conn, "t")


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
