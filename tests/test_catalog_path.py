# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-060 path rationalization: OwnerRecord.repo_root + DDL migration."""
from __future__ import annotations

import json
import sqlite3

import pytest

from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.tumbler import OwnerRecord, _filter_fields


class TestOwnerRecordRepoRoot:
    """OwnerRecord repo_root field basics."""

    def test_default_repo_root_is_empty_string(self):
        rec = OwnerRecord(owner="1.1", name="r", owner_type="repo", repo_hash="h", description="d")
        assert rec.repo_root == ""

    def test_explicit_repo_root(self):
        rec = OwnerRecord(
            owner="1.1", name="r", owner_type="repo", repo_hash="h",
            description="d", repo_root="/home/user/repo",
        )
        assert rec.repo_root == "/home/user/repo"

    def test_jsonl_roundtrip_with_repo_root(self):
        rec = OwnerRecord(
            owner="1.1", name="r", owner_type="repo", repo_hash="h",
            description="d", repo_root="/tmp/repo",
        )
        serialized = json.dumps(rec.__dict__)
        deserialized = json.loads(serialized)
        rec2 = OwnerRecord(**_filter_fields(OwnerRecord, deserialized))
        assert rec2.repo_root == "/tmp/repo"

    def test_jsonl_backwards_compat_without_repo_root(self):
        """Old JSONL entries without repo_root should deserialize with default ''."""
        old_data = {"owner": "1.1", "name": "r", "owner_type": "repo", "repo_hash": "h", "description": "d"}
        rec = OwnerRecord(**_filter_fields(OwnerRecord, old_data))
        assert rec.repo_root == ""


class TestCatalogDBMigration:
    """DDL migration: existing DBs get repo_root column added."""

    def test_new_db_has_repo_root_column(self, tmp_path):
        db = CatalogDB(tmp_path / "catalog.db")
        # Should be able to query repo_root without error
        db.execute("SELECT repo_root FROM owners LIMIT 0")
        db.close()

    def test_migration_adds_repo_root_to_existing_db(self, tmp_path):
        """Simulate an existing DB without repo_root, then open with new CatalogDB."""
        db_path = tmp_path / "catalog.db"
        # Create old-schema DB manually
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE owners (
                tumbler_prefix TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                owner_type TEXT NOT NULL,
                repo_hash TEXT,
                description TEXT
            )
        """)
        conn.execute("INSERT INTO owners VALUES ('1.1', 'old-repo', 'repo', 'hash1', 'desc')")
        conn.commit()
        conn.close()

        # Open with new CatalogDB — should migrate
        db = CatalogDB(db_path)
        row = db.execute("SELECT repo_root FROM owners WHERE tumbler_prefix = '1.1'").fetchone()
        assert row[0] == ""  # default empty string
        db.close()

    def test_rebuild_stores_repo_root(self, tmp_path):
        db = CatalogDB(tmp_path / "catalog.db")
        owner = OwnerRecord(
            owner="1.1", name="test-repo", owner_type="repo",
            repo_hash="abc", description="test", repo_root="/home/user/repo",
        )
        db.rebuild(owners={"1.1": owner}, documents={}, links=[])
        row = db.execute("SELECT repo_root FROM owners WHERE tumbler_prefix = '1.1'").fetchone()
        assert row[0] == "/home/user/repo"
        db.close()

    def test_rebuild_stores_empty_repo_root(self, tmp_path):
        db = CatalogDB(tmp_path / "catalog.db")
        owner = OwnerRecord(
            owner="1.1", name="test-repo", owner_type="repo",
            repo_hash="abc", description="test",
        )
        db.rebuild(owners={"1.1": owner}, documents={}, links=[])
        row = db.execute("SELECT repo_root FROM owners WHERE tumbler_prefix = '1.1'").fetchone()
        assert row[0] == ""
        db.close()
