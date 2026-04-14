# SPDX-License-Identifier: AGPL-3.0-or-later
"""TDD tests for RDR-077 projection-quality columns + ICF hub detection.

Phase 1 (nexus-nsh): migration adds ``similarity``, ``assigned_at``,
``source_collection`` columns and ``idx_topic_assignments_source`` index to
``topic_assignments``. Later phases extend this file.
"""
from __future__ import annotations

import sqlite3


def _make_taxonomy_db() -> sqlite3.Connection:
    """Return an in-memory DB with the pre-4.3.0 taxonomy schema.

    Matches the schema that existed before the RDR-077 migration:
    legacy ``topic_assignments`` with only ``doc_id``, ``topic_id``,
    ``assigned_by``.
    """
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE topics (
            id            INTEGER PRIMARY KEY,
            label         TEXT NOT NULL,
            parent_id     INTEGER REFERENCES topics(id),
            collection    TEXT NOT NULL,
            centroid_hash TEXT,
            doc_count     INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending',
            terms         TEXT
        );
        CREATE TABLE topic_assignments (
            doc_id      TEXT NOT NULL,
            topic_id    INTEGER NOT NULL REFERENCES topics(id),
            assigned_by TEXT NOT NULL DEFAULT 'hdbscan',
            PRIMARY KEY (doc_id, topic_id)
        );
        """
    )
    return conn


class TestAddProjectionQualityColumns:
    """RDR-077 Phase 1 migration: three new columns + one new index."""

    def test_migration_adds_columns(self) -> None:
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        _add_projection_quality_columns(conn)

        cols = {
            r[1]: r[2]
            for r in conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
        }
        assert "similarity" in cols
        assert cols["similarity"] == "REAL"
        assert "assigned_at" in cols
        assert cols["assigned_at"] == "TEXT"
        assert "source_collection" in cols
        assert cols["source_collection"] == "TEXT"

    def test_migration_adds_index(self) -> None:
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        _add_projection_quality_columns(conn)

        indexes = {
            r[1] for r in conn.execute(
                "PRAGMA index_list(topic_assignments)"
            ).fetchall()
        }
        assert "idx_topic_assignments_source" in indexes

        # Verify the index covers (source_collection, assigned_by)
        index_cols = [
            r[2] for r in conn.execute(
                "PRAGMA index_info(idx_topic_assignments_source)"
            ).fetchall()
        ]
        assert index_cols == ["source_collection", "assigned_by"]

    def test_migration_idempotent(self) -> None:
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        _add_projection_quality_columns(conn)
        # Second call must be a no-op, not raise.
        _add_projection_quality_columns(conn)

        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(topic_assignments)").fetchall()
        }
        assert {"similarity", "assigned_at", "source_collection"}.issubset(cols)

    def test_migration_noop_when_columns_present(self) -> None:
        """If columns already exist (fresh install), migration is no-op."""
        from nexus.db.migrations import _add_projection_quality_columns

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE topics (id INTEGER PRIMARY KEY, label TEXT NOT NULL,
                collection TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE topic_assignments (
                doc_id            TEXT NOT NULL,
                topic_id          INTEGER NOT NULL REFERENCES topics(id),
                assigned_by       TEXT NOT NULL DEFAULT 'hdbscan',
                similarity        REAL,
                assigned_at       TEXT,
                source_collection TEXT,
                PRIMARY KEY (doc_id, topic_id)
            );
            CREATE INDEX idx_topic_assignments_source
                ON topic_assignments(source_collection, assigned_by);
            """
        )
        _add_projection_quality_columns(conn)  # must not raise

    def test_migration_noop_when_table_missing(self) -> None:
        """If ``topic_assignments`` doesn't exist yet, migration is a no-op."""
        from nexus.db.migrations import _add_projection_quality_columns

        conn = sqlite3.connect(":memory:")
        _add_projection_quality_columns(conn)  # must not raise

    def test_registered_in_migrations_list(self) -> None:
        """The new migration must be in MIGRATIONS at version 4.3.0."""
        from nexus.db.migrations import MIGRATIONS

        hits = [
            m for m in MIGRATIONS
            if m.fn.__name__ == "_add_projection_quality_columns"
        ]
        assert len(hits) == 1
        assert hits[0].introduced == "4.3.0"

    def test_preserves_existing_rows(self) -> None:
        """Legacy rows keep NULLs for new columns (no backfill)."""
        from nexus.db.migrations import _add_projection_quality_columns

        conn = _make_taxonomy_db()
        conn.execute(
            "INSERT INTO topics (id, label, collection, created_at) "
            "VALUES (1, 'foo', 'code__repo', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by) "
            "VALUES ('docA', 1, 'hdbscan')"
        )
        conn.commit()

        _add_projection_quality_columns(conn)

        row = conn.execute(
            "SELECT doc_id, topic_id, assigned_by, "
            "similarity, assigned_at, source_collection "
            "FROM topic_assignments"
        ).fetchone()
        assert row == ("docA", 1, "hdbscan", None, None, None)
