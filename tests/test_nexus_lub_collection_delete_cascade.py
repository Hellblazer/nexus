# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-lub regression — `nx collection delete` must cascade-purge
all taxonomy state tied to the deleted collection.

Four tables carry per-collection rows:
  * ``topics`` (keyed by ``collection``)
  * ``topic_assignments`` (via topic_id FK, plus ``source_collection``)
  * ``topic_links`` (via from/to topic_id FK)
  * ``taxonomy_meta`` (keyed by ``collection``)

Pre-fix behavior: `nx collection delete` removed the Chroma collection
but left all four orphaned — `nx taxonomy status` continued to list the
deleted collection with its pre-delete topic count; hub detection
traversed orphan edges inflating ICF denominators.

Post-fix contract: `CatalogTaxonomy.purge_collection(name)` removes
every row tied to *name* transactionally, returns a count dict so the
CLI can report what was cleaned.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def seeded_taxonomy(tmp_path: Path):
    """Open a real T2Database on disk and seed two collections with
    topics, assignments, and cross-collection links so the cascade
    path is exercised, not mocked."""
    from nexus.db.t2 import T2Database

    db_path = tmp_path / "memory.db"
    db = T2Database(db_path)
    tax = db.taxonomy

    # --- Seed collection A (to be deleted) ---
    t_a1 = tax.conn.execute(
        "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-04-16T00:00:00Z')",
        ("A-Topic-1", "docs__doomed", "h1", 5, "[]"),
    ).lastrowid
    t_a2 = tax.conn.execute(
        "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-04-16T00:00:00Z')",
        ("A-Topic-2", "docs__doomed", "h2", 3, "[]"),
    ).lastrowid

    # --- Seed collection B (must survive) ---
    t_b1 = tax.conn.execute(
        "INSERT INTO topics (label, collection, centroid_hash, doc_count, terms, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-04-16T00:00:00Z')",
        ("B-Topic-1", "docs__keepme", "h3", 8, "[]"),
    ).lastrowid

    # --- Assignments: mix source_collection and topic_id ownership ---
    # Native A assignment (doc in A, topic in A)
    tax.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, source_collection) "
        "VALUES (?, ?, ?, ?)",
        ("doomed:doc1:0", t_a1, "hdbscan", "docs__doomed"),
    )
    # Projection of doomed chunks into B's topic
    tax.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, source_collection) "
        "VALUES (?, ?, ?, ?)",
        ("doomed:doc2:0", t_b1, "projection", "docs__doomed"),
    )
    # Projection of B chunks into A's topic (must also be purged — doomed
    # topic_id → NULL FK residue left behind otherwise)
    tax.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, source_collection) "
        "VALUES (?, ?, ?, ?)",
        ("keepme:doc1:0", t_a2, "projection", "docs__keepme"),
    )
    # Native B assignment — must survive
    tax.conn.execute(
        "INSERT INTO topic_assignments (doc_id, topic_id, assigned_by, source_collection) "
        "VALUES (?, ?, ?, ?)",
        ("keepme:doc2:0", t_b1, "hdbscan", "docs__keepme"),
    )

    # --- topic_links: A→B, B→A, A→A, B→B ---
    tax.conn.execute(
        "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
        "VALUES (?, ?, ?, ?)",
        (t_a1, t_b1, 2, "[]"),
    )
    tax.conn.execute(
        "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
        "VALUES (?, ?, ?, ?)",
        (t_b1, t_a1, 1, "[]"),
    )
    tax.conn.execute(
        "INSERT INTO topic_links (from_topic_id, to_topic_id, link_count, link_types) "
        "VALUES (?, ?, ?, ?)",
        (t_a1, t_a2, 3, "[]"),  # A→A, both doomed
    )

    # --- taxonomy_meta ---
    tax.conn.execute(
        "INSERT INTO taxonomy_meta (collection, last_discover_at) "
        "VALUES (?, ?)",
        ("docs__doomed", "2026-04-14T12:00:00Z"),
    )
    tax.conn.execute(
        "INSERT INTO taxonomy_meta (collection, last_discover_at) "
        "VALUES (?, ?)",
        ("docs__keepme", "2026-04-14T12:00:00Z"),
    )
    tax.conn.commit()
    yield db, tax
    db.close()


class TestPurgeCollection:
    """Unit tests for the new purge_collection method."""

    def test_purge_removes_topics_for_collection(self, seeded_taxonomy):
        db, tax = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")
        assert counts["topics"] == 2

        remaining = tax.conn.execute(
            "SELECT COUNT(*) FROM topics WHERE collection = ?",
            ("docs__doomed",),
        ).fetchone()[0]
        assert remaining == 0

        # Survivor untouched
        surv = tax.conn.execute(
            "SELECT COUNT(*) FROM topics WHERE collection = ?",
            ("docs__keepme",),
        ).fetchone()[0]
        assert surv == 1

    def test_purge_removes_assignments_by_topic_and_source(self, seeded_taxonomy):
        db, tax = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")

        # Seeded 4 assignments; 3 reference doomed (native + 2 projections).
        # Only the native-B assignment (topic_id=B, source=B) should survive.
        assert counts["assignments"] == 3
        remaining_total = tax.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments"
        ).fetchone()[0]
        assert remaining_total == 1

        # No assignment should reference a doomed source_collection
        leftover = tax.conn.execute(
            "SELECT COUNT(*) FROM topic_assignments WHERE source_collection = ?",
            ("docs__doomed",),
        ).fetchone()[0]
        assert leftover == 0

    def test_purge_removes_links_touching_doomed_topics(self, seeded_taxonomy):
        db, tax = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")

        # 3 seeded links; all 3 touch a doomed topic (A→B, B→A, A→A).
        assert counts["links"] == 3
        remaining = tax.conn.execute("SELECT COUNT(*) FROM topic_links").fetchone()[0]
        assert remaining == 0

    def test_purge_removes_taxonomy_meta_row(self, seeded_taxonomy):
        db, tax = seeded_taxonomy
        counts = tax.purge_collection("docs__doomed")

        assert counts["meta"] == 1
        remaining = tax.conn.execute(
            "SELECT COUNT(*) FROM taxonomy_meta WHERE collection = ?",
            ("docs__doomed",),
        ).fetchone()[0]
        assert remaining == 0
        # Survivor meta row untouched
        surv = tax.conn.execute(
            "SELECT COUNT(*) FROM taxonomy_meta WHERE collection = ?",
            ("docs__keepme",),
        ).fetchone()[0]
        assert surv == 1

    def test_purge_is_transactional(self, seeded_taxonomy):
        """If any step fails mid-cascade, the whole purge rolls back.

        sqlite3.Connection.execute is read-only at the C-API level so
        we sabotage via a wrapper that masquerades as the real conn.
        """
        db, tax = seeded_taxonomy

        class FlakyConn:
            def __init__(self, real):
                self._real = real
            def execute(self, sql, params=()):
                if "DELETE FROM taxonomy_meta" in sql:
                    raise RuntimeError("sabotaged")
                return self._real.execute(sql, params)
            def commit(self):
                return self._real.commit()
            def rollback(self):
                return self._real.rollback()

        real_conn = tax.conn
        try:
            tax.conn = FlakyConn(real_conn)
            with pytest.raises(RuntimeError, match="sabotaged"):
                tax.purge_collection("docs__doomed")
        finally:
            tax.conn = real_conn

        # After rollback: all seeded rows must still be present.
        topics_remaining = tax.conn.execute(
            "SELECT COUNT(*) FROM topics WHERE collection = ?",
            ("docs__doomed",),
        ).fetchone()[0]
        assert topics_remaining == 2, (
            "purge_collection must be transactional; "
            "a mid-cascade failure must roll back every prior delete"
        )

    def test_purge_unknown_collection_returns_zero_counts(self, seeded_taxonomy):
        """Purging a collection with no rows is a silent no-op."""
        db, tax = seeded_taxonomy
        counts = tax.purge_collection("docs__never-existed")
        assert counts == {"topics": 0, "assignments": 0, "links": 0, "meta": 0}


class TestCollectionDeleteCommandCascades:
    """Integration: `nx collection delete` cascades via Click entry point."""

    def test_cli_delete_calls_purge_collection(self, tmp_path, monkeypatch):
        """The CLI path must invoke purge_collection after the Chroma
        delete — not skip it, not run before (order matters for the
        count report)."""
        from click.testing import CliRunner
        from unittest.mock import MagicMock, patch

        from nexus.db.t2 import T2Database
        from nexus.commands.collection import delete_cmd

        db_path = tmp_path / "memory.db"
        db = T2Database(db_path)
        # Seed one topic for the doomed collection so purge has work
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, centroid_hash, "
            "doc_count, terms, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("Only", "docs__doomed", "h", 1, "[]", "2026-04-16T00:00:00Z"),
        )
        db.taxonomy.conn.execute(
            "INSERT INTO taxonomy_meta (collection, last_discover_at) "
            "VALUES (?, ?)",
            ("docs__doomed", "2026-04-14T00:00:00Z"),
        )
        db.taxonomy.conn.commit()
        db.close()

        fake_t3 = MagicMock()
        fake_t3.delete_collection = MagicMock()

        runner = CliRunner()
        with patch("nexus.commands.collection._t3", return_value=fake_t3), \
             patch("nexus.mcp_infra.default_db_path", return_value=db_path), \
             patch(
                 "nexus.commands._helpers.default_db_path",
                 return_value=db_path,
             ):
            result = runner.invoke(delete_cmd, ["docs__doomed", "--yes"])

        assert result.exit_code == 0, result.output
        assert fake_t3.delete_collection.called
        # Must mention the taxonomy cascade in the report
        assert "taxonomy" in result.output.lower() or "topic" in result.output.lower(), (
            f"Delete report missing taxonomy cleanup count. Output: {result.output!r}"
        )

        # Cascade actually happened
        with T2Database(db_path) as verify_db:
            topics = verify_db.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM topics WHERE collection = ?",
                ("docs__doomed",),
            ).fetchone()[0]
            meta = verify_db.taxonomy.conn.execute(
                "SELECT COUNT(*) FROM taxonomy_meta WHERE collection = ?",
                ("docs__doomed",),
            ).fetchone()[0]
        assert topics == 0
        assert meta == 0
