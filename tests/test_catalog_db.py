# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import sqlite3

import pytest

from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.tumbler import DocumentRecord, LinkRecord, OwnerRecord


def _make_owner(*, owner: str = "1.1", name: str = "test-repo", **kw) -> OwnerRecord:
    defaults = {
        "owner": owner,
        "name": name,
        "owner_type": "repo",
        "repo_hash": "abcd1234",
        "description": "test repo",
        "repo_root": "",
    }
    defaults.update(kw)
    return OwnerRecord(**defaults)


def _make_doc(*, tumbler: str = "1.1.1", title: str = "test.py", **kw) -> DocumentRecord:
    defaults = {
        "tumbler": tumbler,
        "title": title,
        "author": "alice",
        "year": 2026,
        "content_type": "code",
        "file_path": "src/test.py",
        "corpus": "",
        "physical_collection": "code__test",
        "chunk_count": 5,
        "head_hash": "abc123",
        "indexed_at": "2026-01-01T00:00:00Z",
        "meta": {},
    }
    defaults.update(kw)
    return DocumentRecord(**defaults)


def _make_link(
    *,
    from_t: str = "1.1.1",
    to_t: str = "1.1.2",
    link_type: str = "cites",
    **kw,
) -> LinkRecord:
    defaults = {
        "from_t": from_t,
        "to_t": to_t,
        "link_type": link_type,
        "from_span": "",
        "to_span": "",
        "created_by": "user",
        "created_at": "2026-01-01T00:00:00Z",
        "meta": {},
    }
    defaults.update(kw)
    return LinkRecord(**defaults)


class TestSchemaCreation:
    def test_tables_exist(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        tables = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "owners" in tables
        assert "documents" in tables
        assert "links" in tables
        assert "documents_fts" in tables

    def test_wal_mode(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_indexes_exist(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_links_from" in indexes
        assert "idx_links_to" in indexes
        assert "idx_links_type" in indexes


class TestRebuild:
    def test_rebuild_populates(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(), "1.1.2": _make_doc(tumbler="1.1.2", title="b.py")}
        links = [_make_link(from_t="1.1.1", to_t="1.1.2")]
        db.rebuild(owners, docs, links)

        assert db._conn.execute("SELECT count(*) FROM owners").fetchone()[0] == 1
        assert db._conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
        assert db._conn.execute("SELECT count(*) FROM links").fetchone()[0] == 1

    def test_rebuild_idempotent(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc()}
        links = [_make_link(from_t="1.1.1", to_t="1.1.1")]

        db.rebuild(owners, docs, links)
        db.rebuild(owners, docs, links)

        assert db._conn.execute("SELECT count(*) FROM owners").fetchone()[0] == 1
        assert db._conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert db._conn.execute("SELECT count(*) FROM links").fetchone()[0] == 1

    def test_rebuild_clears_old_data(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(), "1.1.2": _make_doc(tumbler="1.1.2", title="b.py")}
        db.rebuild(owners, docs, [])
        assert db._conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 2

        # Rebuild with fewer docs — old ones must be gone
        db.rebuild(owners, {"1.1.1": _make_doc()}, [])
        assert db._conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


class TestFTS5Search:
    def test_search_by_title(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1", title="authentication module"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="database schema"),
        }
        db.rebuild(owners, docs, [])

        results = db.search("authentication")
        assert len(results) == 1
        assert results[0]["tumbler"] == "1.1.1"

    def test_search_by_author(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1", author="alice"),
            "1.1.2": _make_doc(tumbler="1.1.2", author="bob"),
        }
        db.rebuild(owners, docs, [])

        results = db.search("bob")
        assert len(results) == 1
        assert results[0]["tumbler"] == "1.1.2"

    def test_search_no_results(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc()}
        db.rebuild(owners, docs, [])

        results = db.search("nonexistent")
        assert len(results) == 0

    def test_search_with_content_type_filter(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1", title="auth module", content_type="code"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="auth design", content_type="rdr"),
        }
        db.rebuild(owners, docs, [])

        results = db.search("auth", content_type="rdr")
        assert len(results) == 1
        assert results[0]["tumbler"] == "1.1.2"

    def test_search_special_chars_safe(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {"1.1.1": _make_doc(title="my-module (v2)")}
        db.rebuild(owners, docs, [])

        # Should not crash on FTS5 special chars
        results = db.search("my-module (v2)")
        assert isinstance(results, list)


class TestNextDocumentNumber:
    def test_empty_owner(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        db.rebuild({}, {}, [])
        assert db.next_document_number("1.1") == 1

    def test_after_inserts(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner()}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1"),
            "1.1.2": _make_doc(tumbler="1.1.2", title="b.py"),
            "1.1.3": _make_doc(tumbler="1.1.3", title="c.py"),
        }
        db.rebuild(owners, docs, [])
        assert db.next_document_number("1.1") == 4

    def test_different_owners_independent(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        owners = {"1.1": _make_owner(), "1.2": _make_owner(owner="1.2", name="other")}
        docs = {
            "1.1.1": _make_doc(tumbler="1.1.1"),
            "1.2.1": _make_doc(tumbler="1.2.1", title="x.py"),
            "1.2.2": _make_doc(tumbler="1.2.2", title="y.py"),
        }
        db.rebuild(owners, docs, [])
        assert db.next_document_number("1.1") == 2
        assert db.next_document_number("1.2") == 3


class TestUniqueConstraint:
    def test_unique_constraint_prevents_duplicate_link_insert(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        db._conn.execute(
            "INSERT INTO links (from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("1.1.1", "1.1.2", "cites", "", "", "user", "2026-01-01", "{}"),
        )
        # Second insert with same (from, to, type) should be silently ignored
        db._conn.execute(
            "INSERT OR IGNORE INTO links (from_tumbler, to_tumbler, link_type, from_span, to_span, "
            "created_by, created_at, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("1.1.1", "1.1.2", "cites", "", "", "other", "2026-02-01", "{}"),
        )
        count = db._conn.execute("SELECT count(*) FROM links").fetchone()[0]
        assert count == 1

    def test_rebuild_deduplicates_links_with_insert_or_ignore(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        dup1 = _make_link(from_t="1.1.1", to_t="1.1.2", link_type="cites")
        dup2 = _make_link(from_t="1.1.1", to_t="1.1.2", link_type="cites", created_by="other")
        db.rebuild({}, {}, [dup1, dup2])
        count = db._conn.execute("SELECT count(*) FROM links").fetchone()[0]
        assert count == 1

    def test_composite_index_created_by_type_exists(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_links_created_by_type" in indexes

    def test_unique_index_exists(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        indexes = {
            row[0]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_links_unique" in indexes


class TestClose:
    def test_close(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        db.close()
        with pytest.raises(Exception):
            db._conn.execute("SELECT 1")


class TestNexus7vuwOwnerNameUniqueRelaxation:
    """nexus-7vuw: a repo and a curator with the same name must coexist.

    Pre-fix the ``owners`` table had a single-column ``UNIQUE(name)``
    that caused INSERT OR REPLACE on the second registration to
    silently obliterate the first row (SQLite resolves UNIQUE conflicts
    by deleting the conflicting row before inserting the new one).
    The visible symptom: ``Catalog.owner_for_repo(repo_hash)`` returned
    None even though events.jsonl carried the OwnerRegistered event,
    causing the indexer to fall back to path-derived collection naming
    while a peer code path used the catalog tumbler, splitting RDR
    chunks across two conformant collections.
    """

    def test_repo_and_curator_with_same_name_coexist(self, tmp_path):
        db = CatalogDB(tmp_path / ".catalog.db")
        # Register a repo owner.
        db._conn.execute(
            "INSERT INTO owners "
            "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("1.1", "nexus", "repo", "571b8edd", "Git repository: nexus",
             "/Users/hal/git/nexus"),
        )
        # Register a curator owner with the same name.
        db._conn.execute(
            "INSERT INTO owners "
            "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("1.2", "nexus", "curator", "", "", ""),
        )
        rows = db._conn.execute(
            "SELECT tumbler_prefix, name, owner_type, repo_hash "
            "FROM owners ORDER BY tumbler_prefix"
        ).fetchall()
        assert rows == [
            ("1.1", "nexus", "repo", "571b8edd"),
            ("1.2", "nexus", "curator", ""),
        ]

    def test_same_name_same_type_still_collides(self, tmp_path):
        """Composite UNIQUE(name, owner_type) keeps the within-type
        collision check that protected register_owner from creating
        duplicate curators with the same name.
        """
        db = CatalogDB(tmp_path / ".catalog.db")
        db._conn.execute(
            "INSERT INTO owners "
            "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("1.1", "papers", "curator", "", "", ""),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute(
                "INSERT INTO owners "
                "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("1.2", "papers", "curator", "", "", ""),
            )

    def test_legacy_unique_name_index_migrated(self, tmp_path):
        """Pre-fix DBs (with the legacy single-column UNIQUE(name) auto-
        index) get the table rebuilt to the new composite UNIQUE on
        first open, preserving existing rows.
        """
        # Hand-craft a pre-fix DB: build the legacy schema, insert one
        # row, then close. The migration in CatalogDB.__init__ should
        # detect the auto-index and rebuild.
        db_path = tmp_path / ".catalog.db"
        legacy = sqlite3.connect(str(db_path))
        legacy.execute(
            "CREATE TABLE owners ("
            "    tumbler_prefix TEXT PRIMARY KEY, "
            "    name TEXT NOT NULL UNIQUE, "
            "    owner_type TEXT NOT NULL, "
            "    repo_hash TEXT, "
            "    description TEXT, "
            "    repo_root TEXT DEFAULT ''"
            ")"
        )
        legacy.execute(
            "INSERT INTO owners VALUES (?, ?, ?, ?, ?, ?)",
            ("1.1", "nexus", "repo", "571b8edd", "Git repository: nexus", ""),
        )
        legacy.commit()
        legacy.close()

        # Open through CatalogDB; migration runs.
        db = CatalogDB(db_path)
        rows = db._conn.execute(
            "SELECT tumbler_prefix, name, owner_type FROM owners"
        ).fetchall()
        assert rows == [("1.1", "nexus", "repo")]
        # Verify the legacy single-column UNIQUE auto-index is gone.
        legacy_indexes = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='owners' AND name LIKE 'sqlite_autoindex_owners_%' "
            "AND sql IS NULL"
        ).fetchall()
        for (idx_name,) in legacy_indexes:
            cols = db._conn.execute(
                f"PRAGMA index_info({idx_name!r})"
            ).fetchall()
            assert len(cols) > 1 or cols[0][2] != "name", (
                f"Legacy single-column UNIQUE(name) auto-index "
                f"{idx_name!r} survived migration: {cols}"
            )
        # And the post-migration repo+curator coexistence holds.
        db._conn.execute(
            "INSERT INTO owners "
            "(tumbler_prefix, name, owner_type, repo_hash, description, repo_root) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("1.2", "nexus", "curator", "", "", ""),
        )
        assert db._conn.execute(
            "SELECT COUNT(*) FROM owners WHERE name = 'nexus'"
        ).fetchone()[0] == 2
