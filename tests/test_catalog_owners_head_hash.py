# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 1.5b: owners.head_hash column + migration (nexus-tts0d.2).

Adds a nullable ``head_hash TEXT`` column to the ``owners`` table so the
catalog can carry per-repo git HEAD identity (previously held by
``~/.config/nexus/repos.json``).  A1-verdict mitigation: switching to
``documents.source_mtime`` is NOT equivalent because a repo HEAD can
advance without any tracked file's mtime changing (remote-only merge,
ff-only pull of tag-only commits, merge with changes in submodule /
untracked dir).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from nexus.db.t2.catalog import CatalogStore


class TestCatalogOwnersHeadHashSchema:
    def test_fresh_install_has_column(self, tmp_path: Path) -> None:
        """New databases include owners.head_hash with NULL default."""
        store = CatalogStore(tmp_path / "cat.db")
        cols = {
            r[1]: r
            for r in store._conn.execute("PRAGMA table_info(owners)").fetchall()
        }
        assert "head_hash" in cols
        # cid, name, type, notnull, dflt_value, pk
        _, _, col_type, notnull, _, _ = cols["head_hash"]
        assert col_type == "TEXT"
        assert notnull == 0  # nullable

    def test_migration_adds_column_to_pre_tts0d_db(self, tmp_path: Path) -> None:
        """ALTER-on-open must patch a pre-migration owners table that lacks
        head_hash. Verifies the try/SELECT guard runs the ALTER instead of
        silently skipping it."""
        db_path = tmp_path / "cat.db"
        conn = sqlite3.connect(str(db_path))
        # Legacy schema — no head_hash column. Matches the post-nexus-7vuw
        # composite-unique shape (the most recent owners schema before
        # nexus-tts0d.2).
        conn.execute(
            """
            CREATE TABLE owners (
                tumbler_prefix TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                owner_type TEXT NOT NULL,
                repo_hash TEXT,
                description TEXT,
                repo_root TEXT DEFAULT '',
                UNIQUE(name, owner_type)
            )
            """
        )
        conn.execute(
            "INSERT INTO owners (tumbler_prefix, name, owner_type, repo_hash, "
            "description, repo_root) VALUES "
            "('1.1', 'nexus', 'repo', 'abc123', '', '/path/to/nexus')"
        )
        conn.commit()
        conn.close()

        # Re-open via CatalogStore — migration should kick in.
        store = CatalogStore(db_path)
        cols = [
            r[1] for r in store._conn.execute("PRAGMA table_info(owners)").fetchall()
        ]
        assert "head_hash" in cols
        # Legacy row survives; pre-existing rows get the NULL default.
        row = store._conn.execute(
            "SELECT name, head_hash FROM owners WHERE tumbler_prefix = ?",
            ("1.1",),
        ).fetchone()
        assert row == ("nexus", None)

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """Re-opening an already-migrated DB is a no-op (no exception, no
        duplicate column add).  The idempotency contract every CatalogStore
        ALTER migration honours via the SELECT-then-except guard."""
        db_path = tmp_path / "cat.db"
        # First open creates the schema with the column.
        store1 = CatalogStore(db_path)
        store1._conn.close()
        # Second open should be a no-op.
        store2 = CatalogStore(db_path)
        cols = [
            r[1] for r in store2._conn.execute("PRAGMA table_info(owners)").fetchall()
        ]
        assert cols.count("head_hash") == 1


class TestCatalogOwnersHeadHashWrites:
    def test_head_hash_column_accepts_writes(self, tmp_path: Path) -> None:
        """Direct UPDATE writes a value; reads round-trip."""
        store = CatalogStore(tmp_path / "cat.db")
        store._conn.execute(
            "INSERT INTO owners (tumbler_prefix, name, owner_type, repo_root) "
            "VALUES ('1.5', 'sample', 'repo', '/tmp/sample')"
        )
        store._conn.execute(
            "UPDATE owners SET head_hash = ? WHERE tumbler_prefix = ?",
            ("deadbeef" * 5, "1.5"),
        )
        store._conn.commit()
        row = store._conn.execute(
            "SELECT head_hash FROM owners WHERE tumbler_prefix = ?", ("1.5",)
        ).fetchone()
        assert row == ("deadbeef" * 5,)
