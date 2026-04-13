# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for L1 context cache generator (RDR-072)."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.db.t2 import T2Database


@pytest.fixture()
def db(tmp_path: Path) -> T2Database:
    database = T2Database(tmp_path / "memory.db")
    yield database
    database.close()


class TestGenerateContextL1:
    """Core L1 cache generation from taxonomy topics."""

    def test_with_topics(self, db: T2Database, tmp_path: Path) -> None:
        """Generates grouped topic map from taxonomy."""
        from nexus.context import generate_context_l1

        # Seed topics across collections
        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("GPU Kernels", "code__art", 100, "2026-01-01T00:00:00Z", "accepted"),
                ("HTTP Handlers", "code__nexus", 50, "2026-01-01T00:00:00Z", "accepted"),
                ("BFT Consensus", "knowledge__delos", 80, "2026-01-01T00:00:00Z", "accepted"),
                ("PDF Extraction", "docs__nexus", 60, "2026-01-01T00:00:00Z", "accepted"),
                ("Catalog Design", "rdr__nexus", 40, "2026-01-01T00:00:00Z", "accepted"),
            ],
        )
        db.taxonomy.conn.commit()

        out = tmp_path / "context_l1.txt"
        result = generate_context_l1(db.taxonomy, output_path=out)

        assert result == out
        assert out.exists()
        content = out.read_text()
        assert "GPU Kernels" in content
        assert "BFT Consensus" in content
        assert "code:" in content
        assert "knowledge:" in content

    def test_empty_taxonomy(self, db: T2Database, tmp_path: Path) -> None:
        """No topics produces no file."""
        from nexus.context import generate_context_l1

        out = tmp_path / "context_l1.txt"
        result = generate_context_l1(db.taxonomy, output_path=out)

        assert result is None
        assert not out.exists()

    def test_groups_by_prefix(self, db: T2Database, tmp_path: Path) -> None:
        """Topics grouped by collection prefix (code/docs/knowledge/rdr)."""
        from nexus.context import generate_context_l1

        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("Topic A", "code__repo1", 10, "2026-01-01T00:00:00Z", "accepted"),
                ("Topic B", "docs__repo1", 20, "2026-01-01T00:00:00Z", "accepted"),
                ("Topic C", "knowledge__kb", 30, "2026-01-01T00:00:00Z", "accepted"),
                ("Topic D", "rdr__repo1", 40, "2026-01-01T00:00:00Z", "accepted"),
            ],
        )
        db.taxonomy.conn.commit()

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        assert "code:" in content
        assert "docs:" in content
        assert "knowledge:" in content
        assert "rdr:" in content

    def test_top5_per_prefix(self, db: T2Database, tmp_path: Path) -> None:
        """Only top 5 topics per prefix by doc_count."""
        from nexus.context import generate_context_l1

        for i in range(8):
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"Code Topic {i}", "code__repo", (8 - i) * 10, "2026-01-01T00:00:00Z", "accepted"),
            )
        db.taxonomy.conn.commit()

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        # Should have topics 0-4 (top 5 by doc_count), not 5-7
        assert "Code Topic 0" in content
        assert "Code Topic 4" in content
        assert "Code Topic 5" not in content

    def test_excludes_child_topics(self, db: T2Database, tmp_path: Path) -> None:
        """Only root topics (parent_id IS NULL), not children from split."""
        from nexus.context import generate_context_l1

        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Root Topic", "code__repo", 100, "2026-01-01T00:00:00Z", "accepted"),
        )
        root_id = db.taxonomy.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, parent_id, collection, doc_count, created_at, review_status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("Child Topic", root_id, "code__repo", 50, "2026-01-01T00:00:00Z", "accepted"),
        )
        db.taxonomy.conn.commit()

        out = tmp_path / "context_l1.txt"
        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        assert "Root Topic" in content
        assert "Child Topic" not in content

    def test_atomic_write(self, db: T2Database, tmp_path: Path) -> None:
        """Cache file is written atomically (no partial reads)."""
        from nexus.context import generate_context_l1

        db.taxonomy.conn.execute(
            "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("Test Topic", "code__repo", 10, "2026-01-01T00:00:00Z", "accepted"),
        )
        db.taxonomy.conn.commit()

        out = tmp_path / "context_l1.txt"
        # Write initial content
        out.write_text("old content")

        generate_context_l1(db.taxonomy, output_path=out)
        content = out.read_text()

        # Should have new content, not partial or old
        assert "Test Topic" in content
        assert "old content" not in content


    def test_repo_scoped(self, db: T2Database, tmp_path: Path) -> None:
        """Only includes collections registered to the specified repo."""
        from unittest.mock import patch

        from nexus.context import generate_context_l1

        # Topics from two different repos
        db.taxonomy.conn.executemany(
            "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                ("My Repo Topic", "code__myrepo-abc123", 100, "2026-01-01T00:00:00Z", "accepted"),
                ("Other Repo Topic", "code__other-def456", 50, "2026-01-01T00:00:00Z", "accepted"),
            ],
        )
        db.taxonomy.conn.commit()

        # Mock registry to return myrepo collections only
        mock_entry = {"collection": "code__myrepo-abc123", "docs_collection": "docs__myrepo-abc123"}
        with patch("nexus.context._repo_collections", return_value={"code__myrepo-abc123", "docs__myrepo-abc123", "rdr__myrepo-abc123"}):
            out = tmp_path / "context_l1.txt"
            generate_context_l1(db.taxonomy, output_path=out, repo_path=Path("/fake/myrepo"))
            content = out.read_text()

        assert "My Repo Topic" in content
        assert "Other Repo Topic" not in content


class TestRefreshContextL1:
    """Convenience wrapper that opens T2 and delegates."""

    def test_refresh(self, tmp_path: Path) -> None:
        """refresh_context_l1 opens DB, generates, returns path."""
        from nexus.context import refresh_context_l1

        db_path = tmp_path / "memory.db"
        out = tmp_path / "context_l1.txt"

        # Create DB with a topic
        with T2Database(db_path) as db:
            db.taxonomy.conn.execute(
                "INSERT INTO topics (label, collection, doc_count, created_at, review_status) "
                "VALUES (?, ?, ?, ?, ?)",
                ("Refresh Test", "code__repo", 10, "2026-01-01T00:00:00Z", "accepted"),
            )
            db.taxonomy.conn.commit()

        result = refresh_context_l1(db_path=db_path, output_path=out)
        assert result == out
        assert "Refresh Test" in out.read_text()
