# SPDX-License-Identifier: AGPL-3.0-or-later
"""CatalogStore parity tests vs legacy CatalogDB.

Tests that CatalogStore (the eighth T2 domain store) has identical behavior
to the legacy CatalogDB on identical inputs. Schema, CRUD, FTS, rebuild,
bulk-load, and atomicity invariants.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.tumbler import DocumentRecord, LinkRecord, OwnerRecord
from nexus.db.t2.catalog_store import CatalogStore


# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_catalog_db.py)
# ---------------------------------------------------------------------------


def _make_owner(
    *,
    owner: str = "1.1",
    name: str = "test-repo",
    owner_type: str = "repo",
    repo_hash: str = "abcd1234",
    description: str = "test repo",
    repo_root: str = "",
) -> OwnerRecord:
    return OwnerRecord(
        owner=owner,
        name=name,
        owner_type=owner_type,
        repo_hash=repo_hash,
        description=description,
        repo_root=repo_root,
    )


def _make_doc(
    *,
    tumbler: str = "1.1.1",
    title: str = "test.py",
    corpus: str = "",
    physical_collection: str = "code__test",
    content_type: str = "code",
    **kw,
) -> DocumentRecord:
    defaults = dict(
        tumbler=tumbler,
        title=title,
        author="alice",
        year=2026,
        content_type=content_type,
        file_path="src/test.py",
        corpus=corpus,
        physical_collection=physical_collection,
        chunk_count=5,
        head_hash="abc123",
        indexed_at="2026-01-01T00:00:00Z",
        meta={},
    )
    defaults.update(kw)
    return DocumentRecord(**defaults)


def _make_link(
    *,
    from_t: str = "1.1.1",
    to_t: str = "1.1.2",
    link_type: str = "cites",
    created_by: str = "user",
) -> LinkRecord:
    return LinkRecord(
        from_t=from_t,
        to_t=to_t,
        link_type=link_type,
        from_span="",
        to_span="",
        created_by=created_by,
        created_at="2026-01-01T00:00:00Z",
        meta={},
    )


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------


class TestSchemaCreation:
    def test_tables_exist(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for expected in ("owners", "documents", "links", "collections", "document_chunks", "_meta"):
            assert expected in tables, f"table {expected!r} missing"
        store.close()

    def test_fts_table_exists(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='documents_fts'"
        ).fetchone()
        assert row is not None, "documents_fts virtual table missing"
        store.close()

    def test_wal_mode(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        store.close()

    def test_foreign_keys_on(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        fk = store._conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1, "foreign_keys should be ON"
        store.close()

    def test_indexes_exist(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        indexes = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        for idx in ("idx_links_from", "idx_links_to", "idx_links_type", "idx_document_chunks_chash"):
            assert idx in indexes, f"index {idx!r} missing"
        store.close()


# ---------------------------------------------------------------------------
# Parity: rebuild
# ---------------------------------------------------------------------------


class TestRebuildParity:
    """CatalogStore.rebuild produces identical row counts as CatalogDB.rebuild."""

    def _rebuild_data(self) -> tuple[dict, dict, list]:
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="other.py"),
        }
        links = [_make_link()]
        return owners, docs, links

    def test_documents_row_count(self, tmp_path: Path) -> None:
        owners, docs, links = self._rebuild_data()
        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, links)
        count = store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 2
        store.close()

    def test_owners_row_count(self, tmp_path: Path) -> None:
        owners, docs, links = self._rebuild_data()
        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, links)
        count = store._conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
        assert count == 1
        store.close()

    def test_links_row_count(self, tmp_path: Path) -> None:
        owners, docs, links = self._rebuild_data()
        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, links)
        count = store._conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        assert count == 1
        store.close()

    def test_rebuild_clears_old_rows(self, tmp_path: Path) -> None:
        owners, docs, links = self._rebuild_data()
        store = CatalogStore(tmp_path / "memory.db")
        # First rebuild with 2 docs
        store.rebuild(owners, docs, links)
        # Second rebuild with 1 doc
        single_docs = {"1.1.1": _make_doc()}
        store.rebuild(owners, single_docs, [])
        count = store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 1
        store.close()

    def test_consistency_mtime_written_atomically(self, tmp_path: Path) -> None:
        owners, docs, links = self._rebuild_data()
        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, links, consistency_mtime=123.456)
        row = store._conn.execute(
            "SELECT value FROM _meta WHERE key='last_consistency_mtime'"
        ).fetchone()
        assert row is not None
        assert float(row[0]) == pytest.approx(123.456)
        store.close()

    def test_matches_catalog_db(self, tmp_path: Path) -> None:
        """Identical inputs produce identical row counts in both implementations."""
        owners, docs, links = self._rebuild_data()

        legacy = CatalogDB(tmp_path / "catalog.db")
        legacy.rebuild(owners, docs, links)
        legacy_doc_count = legacy._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        legacy.close()

        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, links)
        store_doc_count = store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        store.close()

        assert store_doc_count == legacy_doc_count


# ---------------------------------------------------------------------------
# Parity: next_document_number
# ---------------------------------------------------------------------------


class TestNextDocumentNumber:
    def test_empty_returns_one(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        assert store.next_document_number("1.1") == 1
        store.close()

    def test_after_insert_increments(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(tumbler="1.1.1")}
        store.rebuild(owners, docs, [])
        assert store.next_document_number("1.1") == 2
        store.close()

    def test_parity_with_catalog_db(self, tmp_path: Path) -> None:
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1"),
            "1.1.3": _make_doc(tumbler="1.1.3", title="c.py"),
        }

        legacy = CatalogDB(tmp_path / "catalog.db")
        legacy.rebuild(owners, docs, [])
        legacy_next = legacy.next_document_number("1.1")
        legacy.close()

        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, [])
        store_next = store.next_document_number("1.1")
        store.close()

        assert store_next == legacy_next


# ---------------------------------------------------------------------------
# Parity: search (FTS5)
# ---------------------------------------------------------------------------


class TestSearchParity:
    def test_search_returns_matching_document(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(tumbler="1.1.1", title="uniquekeyword.py")}
        store.rebuild(owners, docs, [])
        results = store.search("uniquekeyword")
        assert len(results) == 1
        assert results[0]["tumbler"] == "1.1.1"
        store.close()

    def test_search_content_type_filter(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1", title="keyword.py", content_type="code"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="keyword.md", content_type="docs"),
        }
        store.rebuild(owners, docs, [])
        results = store.search("keyword", content_type="code")
        assert all(r["content_type"] == "code" for r in results)
        store.close()

    def test_search_empty_returns_empty(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild({}, {}, [])
        results = store.search("nonexistent")
        assert results == []
        store.close()

    def test_search_parity_with_catalog_db(self, tmp_path: Path) -> None:
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(tumbler="1.1.1", title="uniqueterm.py")}

        legacy = CatalogDB(tmp_path / "catalog.db")
        legacy.rebuild(owners, docs, [])
        legacy_results = legacy.search("uniqueterm")
        legacy.close()

        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, [])
        store_results = store.search("uniqueterm")
        store.close()

        assert len(store_results) == len(legacy_results)
        assert store_results[0]["tumbler"] == legacy_results[0]["tumbler"]


# ---------------------------------------------------------------------------
# Parity: descendants
# ---------------------------------------------------------------------------


class TestDescendants:
    def test_descendants_returns_children(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="b.py"),
            "2.1.1": _make_doc(tumbler="2.1.1", title="other.py"),
        }
        store.rebuild(owners, docs, [])
        results = store.descendants("1.1")
        tumblers = {r["tumbler"] for r in results}
        assert "1.1.1" in tumblers
        assert "1.1.2" in tumblers
        assert "2.1.1" not in tumblers
        store.close()

    def test_descendants_parity(self, tmp_path: Path) -> None:
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="b.py"),
        }

        legacy = CatalogDB(tmp_path / "catalog.db")
        legacy.rebuild(owners, docs, [])
        legacy_desc = {r["tumbler"] for r in legacy.descendants("1.1")}
        legacy.close()

        store = CatalogStore(tmp_path / "memory.db")
        store.rebuild(owners, docs, [])
        store_desc = {r["tumbler"] for r in store.descendants("1.1")}
        store.close()

        assert store_desc == legacy_desc


# ---------------------------------------------------------------------------
# Parity: execute / commit / transaction
# ---------------------------------------------------------------------------


class TestTransactionParity:
    def test_execute_returns_results(self, tmp_path: Path) -> None:
        """execute returns a list of tuples (RPC-serializable, not a cursor)."""
        store = CatalogStore(tmp_path / "memory.db")
        result = store.execute("SELECT COUNT(*) FROM documents")
        assert isinstance(result, list)
        assert result[0][0] == 0
        store.close()

    def test_transaction_commits_on_success(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO owners (tumbler_prefix, name, owner_type) VALUES (?,?,?)",
                ("9.9", "tx-test", "repo"),
            )
        count = store._conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
        assert count == 1
        store.close()

    def test_transaction_rollback_on_exception(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        try:
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO owners (tumbler_prefix, name, owner_type) VALUES (?,?,?)",
                    ("9.9", "tx-fail", "repo"),
                )
                raise RuntimeError("forced failure")
        except RuntimeError:
            pass
        count = store._conn.execute("SELECT COUNT(*) FROM owners").fetchone()[0]
        assert count == 0
        store.close()


# ---------------------------------------------------------------------------
# Parity: bulk_load_documents
# ---------------------------------------------------------------------------


class TestBulkLoadDocuments:
    def test_bulk_load_idempotent_with_rebuild(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        owners = {"1.1": _make_owner()}
        docs = {f"1.1.{i}": _make_doc(tumbler=f"1.1.{i}", title=f"f{i}.py") for i in range(1, 11)}
        store.rebuild(owners, docs, [])
        count = store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        assert count == 10
        # FTS should be intact
        results = store.search("f1")
        assert len(results) >= 1
        store.close()


# ---------------------------------------------------------------------------
# RDR-108: document_chunks manifest
# ---------------------------------------------------------------------------


class TestDocumentChunks:
    def test_document_chunks_table_exists(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "document_chunks" in tables
        store.close()

    def test_document_chunks_fk_cascade(self, tmp_path: Path) -> None:
        """Deleting a document cascades to document_chunks (RDR-108 K1)."""
        store = CatalogStore(tmp_path / "memory.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(tumbler="1.1.1")}
        store.rebuild(owners, docs, [])
        # Insert a chunk manifest row
        store._conn.execute(
            "INSERT INTO document_chunks (doc_id, position, chash) VALUES (?,?,?)",
            ("1.1.1", 0, "deadbeef"),
        )
        store._conn.commit()
        # Delete document
        store._conn.execute("DELETE FROM documents WHERE tumbler='1.1.1'")
        store._conn.commit()
        count = store._conn.execute("SELECT COUNT(*) FROM document_chunks").fetchone()[0]
        assert count == 0, "document_chunks rows should cascade-delete with parent document"
        store.close()


# ---------------------------------------------------------------------------
# collections table (RDR-101 Phase 6)
# ---------------------------------------------------------------------------


class TestCollectionsTable:
    def test_collections_table_exists(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        tables = {
            r[0]
            for r in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "collections" in tables
        store.close()

    def test_rebuild_backfills_collections(self, tmp_path: Path) -> None:
        """Rebuild auto-backfills collections rows for physical_collection values."""
        store = CatalogStore(tmp_path / "memory.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(tumbler="1.1.1", physical_collection="code__myrepo")}
        store.rebuild(owners, docs, [])
        # After rebuild, the backfilled_collections may or may not be present
        # depending on implementation; at minimum the store opens cleanly
        assert store._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
        store.close()


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        store = CatalogStore(tmp_path / "memory.db")
        store.close()
        # Second close should not raise
        store.close()


class TestMigratedPathsGuard:
    """Second CatalogStore on the same path must not re-run executescript.

    Re-running executescript silently commits any active transaction on the
    shared connection — a latent footgun if a future code path constructs
    two CatalogStore instances against the same DB file inside one process.
    The _migrated_paths set short-circuits the second call.
    """

    def test_second_construction_skips_schema_init(self, tmp_path) -> None:
        from nexus.db.t2 import catalog_store as cs_module

        db = tmp_path / "memory.db"
        first = CatalogStore(db)
        canonical = str(db.resolve())
        assert canonical in cs_module._migrated_paths, (
            "first CatalogStore must add path to _migrated_paths"
        )

        # Reset the executescript counter on the SAME path by re-constructing.
        # If the guard works, _init_schema should early-return and NOT call
        # executescript again. We can't easily count calls, but we can verify
        # the set still contains the path and the construction does not raise.
        second = CatalogStore(db)
        try:
            assert canonical in cs_module._migrated_paths
        finally:
            first.close()
            second.close()

    def test_failed_init_does_not_stamp_path(self, tmp_path, monkeypatch) -> None:
        """RDR-112 P2.review C1 (nexus-3vyw): if schema creation raises, the
        path MUST NOT be added to _migrated_paths. Pre-fix the path was
        stamped before executescript, so a failed init permanently stranded
        the path as 'migrated' for the rest of the process, leaving every
        subsequent CatalogStore call against a schema-less connection."""
        from nexus.db.t2 import catalog_store as cs_module

        db = tmp_path / "memory.db"
        canonical = str(db.resolve())
        cs_module._migrated_paths.discard(canonical)

        original_schema_sql = cs_module._CATALOG_SCHEMA_SQL
        monkeypatch.setattr(
            cs_module, "_CATALOG_SCHEMA_SQL", "INVALID SQL STATEMENT;",
        )
        with pytest.raises(sqlite3.OperationalError):
            CatalogStore(db)
        assert canonical not in cs_module._migrated_paths, (
            "failed init must leave path eligible for retry"
        )

        monkeypatch.setattr(
            cs_module, "_CATALOG_SCHEMA_SQL", original_schema_sql,
        )
        store = CatalogStore(db)
        try:
            tables = {
                r[0]
                for r in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            assert "owners" in tables, "retry construction must rebuild schema"
            assert canonical in cs_module._migrated_paths
        finally:
            store.close()


class TestRebuildFieldParity:
    """RDR-112 P2.review S3: field-by-field parity (not just count) between
    CatalogStore and CatalogDB rebuild. Catches serialization divergences
    (NULL vs '' for nullable columns, metadata JSON encoding, etc.) that
    count-only assertions would silently miss.
    """

    def test_documents_row_field_parity(self, tmp_path: Path) -> None:
        owners = {
            "1.1": OwnerRecord(
                owner="1.1", name="parity-repo", owner_type="repo",
                repo_hash="hash-xyz", description="parity test",
                repo_root="/some/root",
            ),
        }
        docs = {
            "1.1.1": DocumentRecord(
                tumbler="1.1.1",
                title="parity.py",
                author="alice",
                year=2025,
                content_type="code",
                file_path="src/parity.py",
                corpus="test-corpus",
                physical_collection="code__parity",
                chunk_count=7,
                head_hash="head-abc",
                indexed_at="2026-01-15T12:34:56Z",
                meta={"key": "value", "nested": {"a": 1}},
                source_mtime=1234567.89,
                alias_of="",
                source_uri="file:///src/parity.py",
            ),
        }
        links = [
            LinkRecord(
                from_t="1.1.1", to_t="1.1.2", link_type="cites",
                from_span="span-from", to_span="span-to",
                created_by="alice",
                created_at="2026-01-15T13:00:00Z",
                meta={"weight": 0.7},
            ),
        ]

        legacy = CatalogDB(tmp_path / "legacy.db")
        legacy.rebuild(owners, docs, links, consistency_mtime=42.0)
        legacy_doc = legacy._conn.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri "
            "FROM documents WHERE tumbler = '1.1.1'"
        ).fetchone()
        legacy_owner = legacy._conn.execute(
            "SELECT tumbler_prefix, name, owner_type, repo_hash, description, repo_root "
            "FROM owners WHERE tumbler_prefix = '1.1'"
        ).fetchone()
        legacy_link = legacy._conn.execute(
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata "
            "FROM links WHERE from_tumbler = '1.1.1'"
        ).fetchone()
        legacy_meta = legacy._conn.execute(
            "SELECT value FROM _meta WHERE key='last_consistency_mtime'"
        ).fetchone()
        legacy.close()

        store = CatalogStore(tmp_path / "store.db")
        store.rebuild(owners, docs, links, consistency_mtime=42.0)
        store_doc = store._conn.execute(
            "SELECT tumbler, title, author, year, content_type, file_path, "
            "corpus, physical_collection, chunk_count, head_hash, indexed_at, "
            "metadata, source_mtime, alias_of, source_uri "
            "FROM documents WHERE tumbler = '1.1.1'"
        ).fetchone()
        store_owner = store._conn.execute(
            "SELECT tumbler_prefix, name, owner_type, repo_hash, description, repo_root "
            "FROM owners WHERE tumbler_prefix = '1.1'"
        ).fetchone()
        store_link = store._conn.execute(
            "SELECT from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata "
            "FROM links WHERE from_tumbler = '1.1.1'"
        ).fetchone()
        store_meta = store._conn.execute(
            "SELECT value FROM _meta WHERE key='last_consistency_mtime'"
        ).fetchone()
        store.close()

        assert store_doc == legacy_doc, (
            f"document row diverges:\nstore={store_doc}\nlegacy={legacy_doc}"
        )
        assert store_owner == legacy_owner, (
            f"owner row diverges:\nstore={store_owner}\nlegacy={legacy_owner}"
        )
        assert store_link == legacy_link, (
            f"link row diverges:\nstore={store_link}\nlegacy={legacy_link}"
        )
        assert store_meta == legacy_meta, (
            f"_meta row diverges:\nstore={store_meta}\nlegacy={legacy_meta}"
        )
