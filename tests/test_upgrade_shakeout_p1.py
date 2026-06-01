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


# ── #1057b: cascade works on the MIGRATED schema (the reported repro) ────────


class TestRenameMigratedSchema:
    def test_rename_with_aspect_row_succeeds_no_source_path_error(self, tmp_path: Path) -> None:
        """With a catalog present the aspect PK migrates to doc_id and
        source_path is dropped; a rename touching document_aspects must NOT
        raise 'no such column: source_path' (#1057). doc_id is globally unique
        so the collision-defense is a no-op here — the fix is purely that it
        references a column that exists."""
        from nexus.catalog.catalog import Catalog
        from nexus.db.t2 import T2Database

        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        Catalog(cat_dir, cat_dir / ".catalog.db")  # presence triggers the PK migration

        db = T2Database(tmp_path / "memory.db")
        try:
            cols = {
                r[1]
                for r in db.document_aspects.conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            assert "doc_id" in cols, "catalog present should have migrated the PK to doc_id"
            assert "source_path" not in cols, "RDR-096 P5.2 should have dropped source_path"

            db.document_aspects.conn.execute(
                "INSERT INTO document_aspects "
                "(doc_id, collection, extracted_at, model_version, extractor_name) "
                "VALUES ('doc-1', 'code__old', 't', 'm', 'x')"
            )
            db.document_aspects.conn.commit()

            db.rename_collection_cascade(old="code__old", new="code__new")

            rows = db.document_aspects.conn.execute(
                "SELECT doc_id, collection FROM document_aspects"
            ).fetchall()
            assert rows == [("doc-1", "code__new")]
        finally:
            db.close()


# ── #1057c: cascade still works + dedups on the UNMIGRATED schema ────────────


class TestRenameUnmigratedSchema:
    def _aspect(self, collection: str, source_path: str):
        from nexus.db.t2.document_aspects import AspectRecord

        return AspectRecord(
            collection=collection,
            source_path=source_path,
            problem_formulation="pf",
            proposed_method="pm",
            confidence=1.0,
            extracted_at="2026-01-01T00:00:00Z",
            model_version="m1",
            extractor_name="x1",
        )

    def test_rename_moves_row_and_dedups_on_source_path(self, tmp_path: Path) -> None:
        """No catalog -> PK migration deferred -> (collection, source_path) PK,
        no doc_id column. The cascade must dedup on source_path: a source_path
        present in both old and new collapses to one row after the rename."""
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        try:
            cols = {
                r[1]
                for r in db.document_aspects.conn.execute(
                    "PRAGMA table_info(document_aspects)"
                ).fetchall()
            }
            assert "source_path" in cols and "doc_id" not in cols, "expected unmigrated schema"

            db.document_aspects.upsert(self._aspect("code__old", "/p/shared.py"))
            db.document_aspects.upsert(self._aspect("code__new", "/p/shared.py"))
            db.document_aspects.upsert(self._aspect("code__old", "/p/only-old.py"))

            db.rename_collection_cascade(old="code__old", new="code__new")

            rows = sorted(
                db.document_aspects.conn.execute(
                    "SELECT collection, source_path FROM document_aspects"
                ).fetchall()
            )
            # shared.py collapsed to one new-collection row; only-old.py moved.
            assert rows == [
                ("code__new", "/p/only-old.py"),
                ("code__new", "/p/shared.py"),
            ]
        finally:
            db.close()

    def test_rename_with_aspect_queue_rows_succeeds(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "memory.db")
        try:
            q = db.aspect_queue
            q.enqueue("code__old", "/p/a.py")
            q.enqueue("code__new", "/p/a.py")  # source_path collision across collections
            db.rename_collection_cascade(old="code__old", new="code__new")
            rows = q.conn.execute(
                "SELECT collection, source_path FROM aspect_extraction_queue"
            ).fetchall()
            assert rows == [("code__new", "/p/a.py")]
        finally:
            db.close()


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
