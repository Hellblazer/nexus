# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for RDR-060 path rationalization: OwnerRecord.repo_root + DDL + resolve_path + relative paths."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from nexus.catalog.catalog import Catalog, make_relative
from nexus.catalog.catalog_db import CatalogDB
from nexus.catalog.tumbler import OwnerRecord, Tumbler, _filter_fields


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


# ── resolve_path (nexus-1p4g.2) ──────────────────────────────────────────────


class TestResolvePath:
    """Catalog.resolve_path() resolution tests."""

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def test_resolve_path_with_repo_root(self, tmp_path: Path) -> None:
        cat = self._make_catalog(tmp_path)
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        owner = cat.register_owner(
            "test-repo", "repo", repo_hash="abc12345", repo_root=str(repo_dir),
        )
        tumbler = cat.register(
            owner, "test.py", content_type="code", file_path="src/test.py",
        )
        result = cat.resolve_path(tumbler)
        assert result == repo_dir / "src" / "test.py"

    def test_resolve_path_curator_returns_none(self, tmp_path: Path) -> None:
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        tumbler = cat.register(
            owner, "paper.pdf", content_type="paper", file_path="paper.pdf",
        )
        assert cat.resolve_path(tumbler) is None

    def test_resolve_path_unknown_tumbler(self, tmp_path: Path) -> None:
        cat = self._make_catalog(tmp_path)
        assert cat.resolve_path(Tumbler.parse("1.99.99")) is None

    def test_resolve_path_absolute_file_path(self, tmp_path: Path) -> None:
        """Existing absolute file_path returned as-is."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test-repo", "repo", repo_hash="abc12345")
        tumbler = cat.register(
            owner, "test.py", content_type="code", file_path="/absolute/path/test.py",
        )
        assert cat.resolve_path(tumbler) == Path("/absolute/path/test.py")

    def test_resolve_path_empty_repo_root_no_registry(self, tmp_path: Path) -> None:
        """repo_root empty and no registry -> None."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test-repo", "repo", repo_hash="abc12345")
        tumbler = cat.register(
            owner, "test.py", content_type="code", file_path="src/test.py",
        )
        with patch(
            "nexus.catalog.catalog._default_registry_path",
            return_value=tmp_path / "nonexistent" / "repos.json",
        ):
            assert cat.resolve_path(tumbler) is None

    def test_resolve_path_fallback_to_registry(self, tmp_path: Path) -> None:
        """repo_root empty but registry has matching hash -> resolve via registry."""
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        repo_hash = hashlib.sha256(str(repo_dir).encode()).hexdigest()[:8]

        db_path = tmp_path / "db"
        db_path.mkdir()
        registry_data = {
            "repos": {str(repo_dir): {"name": "myrepo", "collection": "code__myrepo"}},
        }
        (db_path / "repos.json").write_text(json.dumps(registry_data))

        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test-repo", "repo", repo_hash=repo_hash)
        tumbler = cat.register(
            owner, "test.py", content_type="code", file_path="src/test.py",
        )
        with patch(
            "nexus.catalog.catalog._default_registry_path",
            return_value=db_path / "repos.json",
        ):
            assert cat.resolve_path(tumbler) == repo_dir / "src" / "test.py"


# ── make_relative (nexus-1p4g.3) ─────────────────────────────────────────────


class TestMakeRelative:
    """make_relative() helper for path normalization."""

    def test_relativizes_path_under_root(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        assert make_relative(root / "src" / "foo.py", root) == "src/foo.py"

    def test_returns_original_if_not_under_root(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        other = tmp_path / "other" / "bar.py"
        assert make_relative(other, root) == str(other)

    def test_returns_original_string_for_relative_input(self) -> None:
        assert make_relative("src/foo.py", Path("/repo")) == "src/foo.py"

    def test_accepts_string_input(self, tmp_path: Path) -> None:
        root = tmp_path / "repo"
        assert make_relative(str(root / "src" / "foo.py"), root) == "src/foo.py"


# ── _markdown_chunks relative source_path (nexus-1p4g.3) ─────────────────────


class TestMarkdownChunksRelativePath:
    """_markdown_chunks stores relative source_path when base_path given."""

    def test_markdown_chunks_absolute_source_path_by_default(self, tmp_path: Path) -> None:
        from nexus.doc_indexer import _markdown_chunks

        md = tmp_path / "doc.md"
        md.write_text("# Hello\n\nSome content here for chunking.")
        result = _markdown_chunks(md, "abc123", "voyage-context-3", "2026-01-01", "corp")
        assert result  # non-empty
        assert result[0][2]["source_path"] == str(md)  # absolute

    def test_markdown_chunks_relative_source_path_with_base(self, tmp_path: Path) -> None:
        from nexus.doc_indexer import _markdown_chunks

        repo = tmp_path / "myrepo"
        repo.mkdir()
        md = repo / "docs" / "rdr" / "rdr-001.md"
        md.parent.mkdir(parents=True)
        md.write_text("# RDR-001\n\nSome research content for chunking.")
        result = _markdown_chunks(md, "abc123", "voyage-context-3", "2026-01-01", "corp", base_path=repo)
        assert result
        assert result[0][2]["source_path"] == "docs/rdr/rdr-001.md"


# ── _index_document source_key (nexus-1p4g.3) ───────────────────────────────


class TestIndexDocumentSourceKey:
    """_index_document uses source_key for staleness check and pruning."""

    def test_staleness_check_uses_source_key(self, tmp_path: Path, monkeypatch) -> None:
        """When source_key is provided, staleness check uses it instead of abs path."""
        from unittest.mock import MagicMock, call

        from tests.conftest import set_credentials
        from nexus.doc_indexer import _index_document

        set_credentials(monkeypatch)

        md = tmp_path / "doc.md"
        md.write_text("# Test\n\nContent for staleness check.")

        mock_col = MagicMock()
        # Simulate staleness hit — same hash and model
        mock_col.get.return_value = {
            "ids": ["existing"],
            "metadatas": [{"content_hash": hashlib.sha256(md.read_bytes()).hexdigest(), "embedding_model": "voyage-context-3"}],
        }
        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = mock_col

        def dummy_chunk_fn(file_path, content_hash, target_model, now_iso, corpus):
            return [("id1", "text", {"source_path": "relative/doc.md"})]

        with patch("nexus.doc_indexer.make_t3", return_value=mock_t3):
            result = _index_document(md, "corp", dummy_chunk_fn, t3=mock_t3, source_key="relative/doc.md")

        # Staleness check should use source_key, not str(md)
        staleness_call = mock_col.get.call_args
        assert staleness_call.kwargs["where"] == {"source_path": "relative/doc.md"}
        # Skipped (same hash) — returns 0
        assert result == 0
