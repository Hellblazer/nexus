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
            store.upsert(chash="abc123", collection="code__foo")
            rows = store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall()
            assert rows == [("abc123", "code__foo")]
        finally:
            store.close()

    def test_upsert_replaces_duplicate_within_same_collection(self, tmp_path: Path) -> None:
        """Re-indexing the same chunk: INSERT OR REPLACE refreshes created_at."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="code__foo")
            first_created = store.conn.execute(
                "SELECT created_at FROM chash_index WHERE chash=?", ("abc",)
            ).fetchone()[0]
            # Re-upsert under the same (chash, collection) PK.
            store.upsert(chash="abc", collection="code__foo")
            rows = store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall()
            assert rows == [("abc", "code__foo")]  # REPLACEd, not duplicated.
            new_created = store.conn.execute(
                "SELECT created_at FROM chash_index WHERE chash=?", ("abc",)
            ).fetchone()[0]
            assert new_created >= first_created
        finally:
            store.close()

    def test_upsert_allows_same_chash_in_different_collections(self, tmp_path: Path) -> None:
        """Compound PK contract: same chunk text in two collections, both rows coexist."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="knowledge__delos")
            store.upsert(chash="abc", collection="knowledge__delos_docling")
            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            assert rows == [
                ("abc", "knowledge__delos"),
                ("abc", "knowledge__delos_docling"),
            ]
        finally:
            store.close()

    def test_lookup_returns_all_matches(self, tmp_path: Path) -> None:
        """``lookup(chash)`` returns every collection that holds the chash."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="A")
            store.upsert(chash="abc", collection="B")
            store.upsert(chash="xyz", collection="A")

            results = store.lookup("abc")
            assert sorted(r["collection"] for r in results) == ["A", "B"]
            # RDR-108 mmf5: chunk_chroma_id no longer in the row dict.
            assert "chunk_chroma_id" not in results[0]

            results = store.lookup("xyz")
            assert [r["collection"] for r in results] == ["A"]

            results = store.lookup("notfound")
            assert results == []
        finally:
            store.close()

    def test_delete_collection_removes_only_that_collections_rows(self, tmp_path: Path) -> None:
        """Phase 1.4 cascade: ``delete_collection(name)`` removes only matching rows."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="abc", collection="A")
            store.upsert(chash="abc", collection="B")
            store.upsert(chash="xyz", collection="A")

            deleted = store.delete_collection("A")
            assert deleted == 2

            remaining = sorted(store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            assert remaining == [("abc", "B")]

            # Idempotent: second delete is a 0-row no-op.
            assert store.delete_collection("A") == 0
        finally:
            store.close()


# ── Batch upsert (RDR-128 P1 kg8sj) ──────────────────────────────────────────


class TestChashIndexUpsertMany:
    """``upsert_many`` writes a whole batch in one statement+commit so the
    indexer's dual-write is a single daemon RPC, not one per chunk."""

    def test_batch_inserts_all_rows(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert_many(
                chashes=["a1", "b2", "c3"], collection="code__foo"
            )
            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            assert rows == [
                ("a1", "code__foo"), ("b2", "code__foo"), ("c3", "code__foo"),
            ]
        finally:
            store.close()

    def test_empty_list_is_noop(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert_many(chashes=[], collection="code__foo")
            assert store.conn.execute(
                "SELECT COUNT(*) FROM chash_index"
            ).fetchone()[0] == 0
        finally:
            store.close()

    def test_blank_chashes_are_skipped(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert_many(chashes=["a1", "", "  ", "b2"], collection="c")
            rows = sorted(
                r[0] for r in store.conn.execute(
                    "SELECT chash FROM chash_index"
                ).fetchall()
            )
            assert rows == ["a1", "b2"]
        finally:
            store.close()

    def test_empty_collection_raises(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            with pytest.raises(ValueError, match="collection"):
                store.upsert_many(chashes=["a1"], collection="")
        finally:
            store.close()

    def test_batch_is_insert_or_replace(self, tmp_path: Path) -> None:
        """Re-upserting an existing chash refreshes rather than erroring."""
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert_many(chashes=["a1", "b2"], collection="c")
            store.upsert_many(chashes=["a1", "c3"], collection="c")  # a1 repeats
            rows = sorted(
                r[0] for r in store.conn.execute(
                    "SELECT chash FROM chash_index"
                ).fetchall()
            )
            assert rows == ["a1", "b2", "c3"]
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
            db.chash_index.upsert(chash="abc", collection="A")
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
                store.upsert(chash="", collection="A")
        finally:
            store.close()

    def test_upsert_raises_on_empty_collection(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            with pytest.raises(ValueError, match="collection"):
                store.upsert(chash="abc", collection="")
        finally:
            store.close()


# ── registered_chashes_for_collection (RDR-108 Phase 4 / nexus-z1mu) ─────────


class TestRegisteredChashesForCollection:
    """``ChashIndex.registered_chashes_for_collection`` returns
    chash[:32] values currently registered in the chash_index routing
    table for a collection. Disambiguated from
    ``Catalog.chashes_for_collection`` (manifest-authoritative) by the
    nexus-v7mn rename. Replaces ``chunk_chroma_ids_present_in_collection``
    (removed in the same change). Used by ``compute_chash_coverage`` to
    sample missing chunks via a single set-difference."""

    def test_unknown_collection_returns_empty(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex
        store = ChashIndex(tmp_path / "t2.db")
        try:
            assert store.registered_chashes_for_collection("code__nonexistent") == set()
        finally:
            store.close()

    def test_returns_truncated_registered_chashes_for_collection(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex
        store = ChashIndex(tmp_path / "t2.db")
        try:
            full = "a" * 64
            store.upsert(chash=full, collection="code__foo")
            store.upsert(chash="b" * 64, collection="code__foo")
            store.upsert(chash="c" * 64, collection="code__bar")

            foo = store.registered_chashes_for_collection("code__foo")
            bar = store.registered_chashes_for_collection("code__bar")

            assert foo == {full[:32], ("b" * 64)[:32]}
            assert bar == {("c" * 64)[:32]}
        finally:
            store.close()

    def test_handles_truncated_storage(self, tmp_path: Path) -> None:
        """``substr`` is a no-op on inputs already <= 32 chars, so older
        rows that stored 32-char chashes round-trip unchanged."""
        from nexus.db.t2.chash_index import ChashIndex
        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="a" * 32, collection="code__foo")
            assert store.registered_chashes_for_collection("code__foo") == {"a" * 32}
        finally:
            store.close()


class TestChashIndexInitSchemaDropsLegacyColumn:
    """RDR-108 Phase 4a (nexus-mmf5): ``_init_schema`` probe-and-drop
    fast-path mirrors the version-tracked ``_drop_chash_index_chunk_chroma_id``
    migration so dev DBs that opened against the legacy 4-column shape
    converge to the post-Phase-4a 3-column shape on the next
    ``ChashIndex(...)`` construction."""

    def test_legacy_4col_table_is_dropped_to_3col_on_open(self, tmp_path) -> None:
        from nexus.db.migrations import (
            migrate_chash_index,
            migrate_chash_index_rename_doc_id,
        )
        from nexus.db.t2.chash_index import ChashIndex

        # Build the legacy 4-column shape (install + rename) directly.
        db_path = tmp_path / "t2.db"
        seed = sqlite3.connect(str(db_path))
        try:
            migrate_chash_index(seed)
            migrate_chash_index_rename_doc_id(seed)
            seed.execute(
                "INSERT INTO chash_index VALUES (?, ?, ?, ?)",
                ("aa" * 16, "code__legacy", "chunk-7", "2026-05-09T00:00:00Z"),
            )
            seed.commit()
        finally:
            seed.close()

        # Construct ChashIndex against the legacy DB. The probe-and-drop
        # in _init_schema must remove ``chunk_chroma_id`` and preserve
        # the remaining three columns + the seeded row.
        store = ChashIndex(db_path)
        try:
            cols = {
                r[1] for r in store.conn.execute(
                    "PRAGMA table_info(chash_index)"
                ).fetchall()
            }
            assert "chunk_chroma_id" not in cols
            assert cols == {"chash", "physical_collection", "created_at"}

            row = store.conn.execute(
                "SELECT chash, physical_collection, created_at FROM chash_index"
            ).fetchone()
            assert row == ("aa" * 16, "code__legacy", "2026-05-09T00:00:00Z")
        finally:
            store.close()


# ── delete_stale + is_empty encapsulation (review #1, #6) ────────────────────


class TestChashIndexEncapsulation:
    """Every connection access must go through a locked public method.
    ``resolve_chash``'s self-healing delete + ``nx doc cite``'s empty-index
    short-circuit previously bypassed ``_lock`` by touching ``.conn``
    directly — a data-race with concurrent writers.
    """

    def test_delete_stale_removes_exact_row(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="aa", collection="c1")
            store.upsert(chash="aa", collection="c2")
            removed = store.delete_stale(chash="aa", collection="c1")
            assert removed == 1
            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            assert rows == [("aa", "c2")]
        finally:
            store.close()

    def test_delete_stale_missing_row_returns_zero(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            removed = store.delete_stale(chash="absent", collection="nope")
            assert removed == 0
        finally:
            store.close()

    def test_delete_stale_acquires_lock(self, tmp_path: Path) -> None:
        """Two threads must serialize on the same lock."""
        import threading

        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            # Seed 100 rows, then have two threads racing delete_stale + upsert
            # on disjoint keys. Both must complete without DB lock errors.
            for i in range(100):
                store.upsert(chash=f"h{i:03d}", collection="race")

            errors: list[BaseException] = []

            def deleter():
                try:
                    for i in range(50):
                        store.delete_stale(
                            chash=f"h{i:03d}", collection="race",
                        )
                except BaseException as e:
                    errors.append(e)

            def upserter():
                try:
                    for i in range(100, 150):
                        store.upsert(
                            chash=f"h{i:03d}", collection="race",
                        )
                except BaseException as e:
                    errors.append(e)

            t1 = threading.Thread(target=deleter)
            t2 = threading.Thread(target=upserter)
            t1.start(); t2.start(); t1.join(); t2.join()
            assert not errors, errors
        finally:
            store.close()

    def test_is_empty_on_fresh_db(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            assert store.is_empty() is True
        finally:
            store.close()

    def test_is_empty_false_once_populated(self, tmp_path: Path) -> None:
        from nexus.db.t2.chash_index import ChashIndex

        store = ChashIndex(tmp_path / "t2.db")
        try:
            store.upsert(chash="h", collection="c")
            assert store.is_empty() is False
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
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            assert rows == [
                ("hash1", "code__foo"),
                ("hash2", "code__foo"),
                ("hash3", "code__foo"),
            ]
        finally:
            store.close()

    def test_dual_write_is_noop_when_chash_index_is_none(self, tmp_path: Path) -> None:
        """Tests + pre-Phase-1.2 call sites with no T2 plumbed must not error."""
        from nexus.db.t2.chash_index import dual_write_chash_index

        # Must not raise.
        dual_write_chash_index(None, "any", ["d1"], [{"chunk_text_hash": "h1"}])

    def test_dual_write_is_noop_when_metadatas_empty(self, tmp_path: Path) -> None:
        """Empty ``metadatas`` short-circuits before the loop. RDR-108
        Phase 4a: the helper iterates ``metadatas`` only (``ids`` is
        kept on the signature for short-circuiting and call-site
        symmetry), so an ``ids``-non-empty / ``metadatas``-empty
        mismatch must not silently iterate an empty list and pretend
        success."""
        from nexus.db.t2.chash_index import ChashIndex, dual_write_chash_index

        store = ChashIndex(tmp_path / "t2.db")
        try:
            # Caller bug: ids non-empty, metadatas empty. Must short-circuit.
            dual_write_chash_index(store, "coll", ["doc1", "doc2"], [])
            rows = store.conn.execute(
                "SELECT COUNT(*) FROM chash_index"
            ).fetchone()
            assert rows[0] == 0
        finally:
            store.close()

    def test_dual_write_skips_empty_chash_metadata(self, tmp_path: Path) -> None:
        """Some legacy / test-only metadata paths omit chunk_text_hash; don't write empty rows."""
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
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall()
            assert rows == [("hash1", "coll")]
        finally:
            store.close()

    def test_dual_write_swallows_underlying_failure(self, tmp_path: Path, caplog) -> None:
        """A single row failure must log but not abort the rest of the batch."""
        import logging
        from nexus.db.t2.chash_index import ChashIndex, dual_write_chash_index

        store = ChashIndex(tmp_path / "t2.db")
        try:
            # First row has empty chunk_text_hash that ChashIndex.upsert
            # rejects; the other two must still land.
            ids = ["doc1", "doc2", "doc3"]
            metadatas = [
                {"chunk_text_hash": ""},
                {"chunk_text_hash": "hash2"},
                {"chunk_text_hash": "hash3"},
            ]

            with caplog.at_level(logging.WARNING):
                dual_write_chash_index(store, "coll", ids, metadatas)

            # Rows with valid args still inserted.
            rows = sorted(store.conn.execute(
                "SELECT chash, physical_collection FROM chash_index"
            ).fetchall())
            assert rows == [("hash2", "coll"), ("hash3", "coll")]
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


# ── chash_dual_write_batch_hook (RDR-086 Phase 1.2; renamed in RDR-095) ──────


class TestChashDualWriteBatchEntryPoint:
    """``mcp_infra.chash_dual_write_batch_hook`` is the registered batch
    hook fired by ``HookRegistry.fire_batch`` from every CLI indexing
    write site. It opens a fresh T2Database (matching
    ``taxonomy_assign_batch_hook``'s lifecycle) and delegates.
    """

    def test_chash_dual_write_batch_populates_real_t2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.db.t2 import T2Database
        from nexus.mcp_infra import chash_dual_write_batch_hook

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr(
            "nexus.mcp_infra.default_db_path", lambda: db_path
        )

        chash_dual_write_batch_hook(
            ["doc1", "doc2"],
            "code__example",
            [],  # contents (ignored by this hook)
            None,  # embeddings (ignored by this hook)
            [
                {"chunk_text_hash": "aa11"},
                {"chunk_text_hash": "bb22"},
            ],
        )

        with T2Database(db_path) as db:
            aa = db.chash_index.lookup("aa11")
            assert len(aa) == 1
            assert aa[0]["collection"] == "code__example"
            assert "chunk_chroma_id" not in aa[0]  # column dropped (mmf5)
            assert db.chash_index.lookup("bb22")[0]["collection"] == "code__example"

    def test_chash_dual_write_batch_empty_is_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from nexus.mcp_infra import chash_dual_write_batch_hook

        db_path = tmp_path / "t2.db"
        monkeypatch.setattr(
            "nexus.mcp_infra.default_db_path", lambda: db_path
        )
        # Neither call should raise; both short-circuit before opening T2.
        chash_dual_write_batch_hook(
            [], "coll", [], None, [{"chunk_text_hash": "x"}],
        )
        chash_dual_write_batch_hook(
            ["doc1"], "coll", [], None, [],
        )

    def test_chash_dual_write_batch_swallows_outer_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If t2_ctx itself fails, the caller's T3 write must still proceed."""
        from nexus import mcp_infra

        def _boom():
            raise RuntimeError("simulated T2 open failure")

        monkeypatch.setattr(mcp_infra, "t2_ctx", _boom)
        # Must not raise.
        mcp_infra.chash_dual_write_batch_hook(
            ["doc1"], "coll", [], None, [{"chunk_text_hash": "hash1"}],
        )
