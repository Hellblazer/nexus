# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-086 Phase 1.2: T2 ``ChashIndex`` domain store.

Contract tests for the new store — basic upsert, compound-PK semantics,
delete cascade, lookup API, concurrent writes, and facade wiring.
The store lives at ``src/nexus/db/t2/chash_index.py`` and is registered
on ``T2Database`` as ``db.chash_index``.
"""
from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


# ── ChashIndex store — isolated ──────────────────────────────────────────────


class TestChashIndexStoreBasics:
    """Core upsert/lookup contract on a fresh DB."""

    def test_init_creates_table(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            tables = {
                r[0] for r in store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "chash_index" in tables
        finally:
            store.close()

    def test_upsert_inserts_new_row(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc123", collection="code__foo", doc_id="d1")
            rows = store.conn.execute(
                "SELECT chash, physical_collection, doc_id FROM chash_index"
            ).fetchall()
            assert rows == [("abc123", "code__foo", "d1")]
        finally:
            store.close()

    def test_upsert_replaces_duplicate_within_same_collection(self, tmp_path: Path) -> None:
        """Re-indexing the same file: INSERT OR REPLACE updates doc_id + created_at."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="code__foo", doc_id="d1")
            first_created = store.conn.execute(
                "SELECT created_at FROM chash_index WHERE chash=?", ("abc",)
            ).fetchone()[0]
            # Second write with different doc_id, same (chash, collection).
            store.upsert(chash="abc", collection="code__foo", doc_id="d2")
            rows = store.conn.execute(
                "SELECT chash, physical_collection, doc_id FROM chash_index"
            ).fetchall()
            assert rows == [("abc", "code__foo", "d2")]  # REPLACEd, not duplicated.
            new_created = store.conn.execute(
                "SELECT created_at FROM chash_index WHERE chash=?", ("abc",)
            ).fetchone()[0]
            assert new_created >= first_created
        finally:
            store.close()

    def test_upsert_allows_same_chash_in_different_collections(self, tmp_path: Path) -> None:
        """Compound PK contract — same chunk text in two collections: both rows coexist."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="knowledge__delos",         doc_id="d1")
            store.upsert(chash="abc", collection="knowledge__delos_docling", doc_id="d2")
            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection, doc_id FROM chash_index"
            ).fetchall())
            assert rows == [
                ("abc", "knowledge__delos",         "d1"),
                ("abc", "knowledge__delos_docling", "d2"),
            ]
        finally:
            store.close()

    def test_lookup_returns_all_matches(self, tmp_path: Path) -> None:
        """``lookup(chash)`` returns every (collection, doc_id) pair for the chash."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="A", doc_id="d1")
            store.upsert(chash="abc", collection="B", doc_id="d2")
            store.upsert(chash="xyz", collection="A", doc_id="d3")

            results = store.lookup("abc")
            assert sorted((r["collection"], r["doc_id"]) for r in results) == [
                ("A", "d1"),
                ("B", "d2"),
            ]

            results = store.lookup("xyz")
            assert [(r["collection"], r["doc_id"]) for r in results] == [("A", "d3")]

            results = store.lookup("notfound")
            assert results == []
        finally:
            store.close()

    def test_delete_collection_removes_only_that_collections_rows(self, tmp_path: Path) -> None:
        """Phase 1.4 cascade: ``delete_collection(name)`` removes only matching rows."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="A", doc_id="d1")
            store.upsert(chash="abc", collection="B", doc_id="d2")
            store.upsert(chash="xyz", collection="A", doc_id="d3")

            deleted = store.delete_collection("A")
            assert deleted == 2

            remaining = sorted(store.conn.execute(
                "SELECT chash, physical_collection, doc_id FROM chash_index"
            ).fetchall())
            assert remaining == [("abc", "B", "d2")]

            # Idempotent: second delete is a 0-row no-op.
            assert store.delete_collection("A") == 0
        finally:
            store.close()


# ── Concurrency ──────────────────────────────────────────────────────────────


class TestChashIndexConcurrency:
    """Pipeline workers call ``upsert`` from a 3-worker ThreadPoolExecutor.

    Mirrors the concurrent-write pattern in ``pipeline_stages.py``.
    """

    def test_concurrent_upserts_no_corruption(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        N = 1000
        try:
            def _write(i: int) -> None:
                store.upsert(
                    chash=f"chash_{i:04d}",
                    collection=f"coll_{i % 8}",
                    doc_id=f"doc_{i}",
                )

            with ThreadPoolExecutor(max_workers=3) as ex:
                list(ex.map(_write, range(N)))

            count = store.conn.execute(
                "SELECT COUNT(*) FROM chash_index"
            ).fetchone()[0]
            assert count == N, f"expected {N} rows, got {count}"
        finally:
            store.close()


# ── Facade wiring ────────────────────────────────────────────────────────────


class TestT2DatabaseFacadeWiring:
    """``T2Database`` constructs a ``ChashIndex`` at ``db.chash_index``
    and tears it down in close()."""

    def test_t2_exposes_chash_index_attribute(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "t2.db")
        try:
            assert hasattr(db, "chash_index"), "T2Database must expose db.chash_index"
            # Reachable through the facade + functional.
            db.chash_index.upsert(chash="abc", collection="A", doc_id="d1")
            results = db.chash_index.lookup("abc")
            assert results and results[0]["collection"] == "A"
        finally:
            db.close()

    def test_close_tears_down_chash_index(self, tmp_path: Path) -> None:
        from nexus.db.t2 import T2Database

        db = T2Database(tmp_path / "t2.db")
        conn = db.chash_index.conn
        db.close()
        # After close, the connection is unusable.
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")


# ── Error behaviour ──────────────────────────────────────────────────────────


class TestChashIndexErrors:
    """The store raises cleanly on bad input; callers can catch and log."""

    def test_upsert_raises_on_empty_chash(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            with pytest.raises(ValueError, match="chash"):
                store.upsert(chash="", collection="A", doc_id="d1")
        finally:
            store.close()

    def test_upsert_raises_on_empty_collection(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            with pytest.raises(ValueError, match="collection"):
                store.upsert(chash="abc", collection="", doc_id="d1")
        finally:
            store.close()

    def test_upsert_raises_on_empty_doc_id(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            with pytest.raises(ValueError, match="doc_id"):
                store.upsert(chash="abc", collection="A", doc_id="")
        finally:
            store.close()


# ── dual_write_chash_index helper ────────────────────────────────────────────


class TestDualWriteHelper:
    """``dual_write_chash_index(chash_index, collection, ids, metadatas)``
    is called by each of the six indexing write sites after a T3 upsert.
    It must be best-effort — T2 failure logs and does not re-raise.
    """

    def test_dual_write_populates_chash_index(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex, dual_write_chash_index

        store = ChashIndex(tmp_path / "t2.db")
        try:
            ids = ["doc1", "doc2", "doc3"]
            metadatas = [
                {"chunk_text_hash": "hash1", "other": "x"},
                {"chunk_text_hash": "hash2"},
                {"chunk_text_hash": "hash3"},
            ]
            dual_write_chash_index(store, "code__foo", ids, metadatas)

            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection, doc_id FROM chash_index"
            ).fetchall())
            assert rows == [
                ("hash1", "code__foo", "doc1"),
                ("hash2", "code__foo", "doc2"),
                ("hash3", "code__foo", "doc3"),
            ]
        finally:
            store.close()

    def test_dual_write_is_noop_when_chash_index_is_none(self, tmp_path: Path) -> None:
        """Tests + pre-Phase-1.2 call sites with no T2 plumbed must not error."""
        from nexus.db.t2.chash_index import dual_write_chash_index

        # Must not raise.
        dual_write_chash_index(None, "any", ["d1"], [{"chunk_text_hash": "h1"}])

    def test_dual_write_skips_empty_chash_metadata(self, tmp_path: Path) -> None:
        """Some legacy / test-only metadata paths omit chunk_text_hash — don't write empty rows."""
        from nexus.db.t2.chash_index import ChashIndex, dual_write_chash_index

        store = ChashIndex(tmp_path / "t2.db")
        try:
            ids = ["doc1", "doc2", "doc3"]
            metadatas = [
                {"chunk_text_hash": "hash1"},
                {"chunk_text_hash": ""},              # explicit empty
                {"not_a_chunk_text_hash_field": "x"}, # missing key
            ]
            dual_write_chash_index(store, "coll", ids, metadatas)

            rows = store.conn.execute(
                "SELECT chash, doc_id FROM chash_index"
            ).fetchall()
            assert rows == [("hash1", "doc1")]
        finally:
            store.close()

    def test_dual_write_swallows_underlying_failure(self, tmp_path: Path, caplog) -> None:
        """A single row failure must log but not abort the rest of the batch."""
        import logging
        from nexus.db.t2.chash_index import ChashIndex, dual_write_chash_index

        store = ChashIndex(tmp_path / "t2.db")
        try:
            # First row has invalid args that ChashIndex.upsert rejects;
            # second and third must still land.
            ids = ["", "doc2", "doc3"]
            metadatas = [
                {"chunk_text_hash": "hash1"},
                {"chunk_text_hash": "hash2"},
                {"chunk_text_hash": "hash3"},
            ]

            with caplog.at_level(logging.WARNING):
                dual_write_chash_index(store, "coll", ids, metadatas)

            # Rows with valid args still inserted.
            rows = sorted(store.conn.execute(
                "SELECT chash, doc_id FROM chash_index"
            ).fetchall())
            assert rows == [("hash2", "doc2"), ("hash3", "doc3")]
        finally:
            store.close()

    def test_dual_write_upserts_idempotent(self, tmp_path: Path) -> None:
        """Re-indexing the same file runs the helper again; INSERT OR REPLACE semantics."""
        from nexus.db.t2.chash_index import ChashIndex, dual_write_chash_index

        store = ChashIndex(tmp_path / "t2.db")
        try:
            ids = ["doc1"]
            metadatas = [{"chunk_text_hash": "hash1"}]
            dual_write_chash_index(store, "coll", ids, metadatas)
            # Second call with same data — must not error, must keep exactly 1 row.
            dual_write_chash_index(store, "coll", ids, metadatas)

            count = store.conn.execute(
                "SELECT COUNT(*) FROM chash_index"
            ).fetchone()[0]
            assert count == 1
        finally:
            store.close()


# ── chash_dual_write_batch — mcp_infra entry point (RDR-086 Phase 1.2) ───────


class TestChashDualWriteBatchEntryPoint:
    """``mcp_infra.chash_dual_write_batch`` is what each of the seven
    indexing write sites actually calls. It opens a fresh T2Database
    (matching ``taxonomy_assign_batch``'s lifecycle) and delegates.
    """

    def test_chash_dual_write_batch_populates_real_t2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db.t2 import T2Database
        from nexus.mcp_infra import chash_dual_write_batch

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr(
            "nexus.mcp_infra.default_db_path", lambda: db_path
        )

        chash_dual_write_batch(
            ["doc1", "doc2"],
            "code__example",
            [
                {"chunk_text_hash": "aa11"},
                {"chunk_text_hash": "bb22"},
            ],
        )

        with T2Database(db_path) as db:
            assert db.chash_index.lookup("aa11") == [
                {"collection": "code__example", "doc_id": "doc1",
                 "created_at": db.chash_index.lookup("aa11")[0]["created_at"]},
            ]
            assert db.chash_index.lookup("bb22")[0]["doc_id"] == "doc2"

    def test_chash_dual_write_batch_empty_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.mcp_infra import chash_dual_write_batch

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr(
            "nexus.mcp_infra.default_db_path", lambda: db_path
        )
        # Neither call should raise; both short-circuit before opening T2.
        chash_dual_write_batch([], "coll", [{"chunk_text_hash": "x"}])
        chash_dual_write_batch(["doc1"], "coll", [])

    def test_chash_dual_write_batch_swallows_outer_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If t2_ctx itself fails, the caller's T3 write must still proceed."""
        from nexus import mcp_infra

        def _boom():
            raise RuntimeError("simulated T2 open failure")

        monkeypatch.setattr(mcp_infra, "t2_ctx", _boom)
        # Must not raise.
        mcp_infra.chash_dual_write_batch(
            ["doc1"], "coll", [{"chunk_text_hash": "hash1"}]
        )
