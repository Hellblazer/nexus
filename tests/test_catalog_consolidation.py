# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.catalog.catalog import Catalog
from nexus.cli import main


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _make_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    return cat


def _mock_t3_col(ids: list[str], documents: list[str] | None = None) -> MagicMock:
    """Mock a ChromaDB collection with get() and count()."""
    col = MagicMock()
    n = len(ids)
    col.get.return_value = {
        "ids": ids,
        "documents": documents or [f"doc_{i}" for i in range(n)],
        "metadatas": [{"chunk_index": i} for i in range(n)],
        "embeddings": [[0.1 * i] * 10 for i in range(n)],
    }
    col.count.return_value = n
    return col


class TestByCopus:
    def test_by_corpus(self, tmp_path):
        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Paper A", content_type="paper", corpus="ml",
                     physical_collection="docs__La")
        cat.register(owner, "Paper B", content_type="paper", corpus="ml",
                     physical_collection="docs__Lb")
        cat.register(owner, "Paper C", content_type="paper", corpus="systems",
                     physical_collection="docs__Lc")
        results = cat.by_corpus("ml")
        assert len(results) == 2

    def test_by_corpus_empty(self, tmp_path):
        cat = _make_catalog(tmp_path)
        assert cat.by_corpus("nonexistent") == []


class TestMergeCorpus:
    def test_merge_two_collections(self, tmp_path):
        from nexus.catalog.consolidation import merge_corpus

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Paper A", content_type="paper", corpus="test",
                     physical_collection="docs__La", chunk_count=3)
        cat.register(owner, "Paper B", content_type="paper", corpus="test",
                     physical_collection="docs__Lb", chunk_count=2)

        # Mock T3
        t3 = MagicMock()
        col_a = _mock_t3_col(["a1", "a2", "a3"])
        col_b = _mock_t3_col(["b1", "b2"])
        target_col = MagicMock()
        target_col.count.return_value = 5

        def get_or_create(name):
            if name == "docs__La":
                return col_a
            elif name == "docs__Lb":
                return col_b
            else:
                return target_col

        t3.get_or_create_collection.side_effect = get_or_create

        result = merge_corpus(cat, t3, "test")
        assert result["merged"] == 2
        assert result["errors"] == []
        # Target should have had upsert called twice
        assert target_col.upsert.call_count == 2
        # Catalog pointers should be updated
        entries = cat.by_corpus("test")
        for e in entries:
            assert e.physical_collection == "docs__test"

    def test_dry_run_no_changes(self, tmp_path):
        from nexus.catalog.consolidation import merge_corpus

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Paper A", content_type="paper", corpus="test",
                     physical_collection="docs__La", chunk_count=3)

        t3 = MagicMock()
        result = merge_corpus(cat, t3, "test", dry_run=True)
        assert result["merged"] == 0
        assert result["would_merge"] == 1
        # T3 should not have been touched
        t3.get_or_create_collection.assert_not_called()

    def test_no_entries_for_corpus(self, tmp_path):
        from nexus.catalog.consolidation import merge_corpus

        cat = _make_catalog(tmp_path)
        t3 = MagicMock()
        result = merge_corpus(cat, t3, "nonexistent")
        assert result["merged"] == 0
        assert len(result["errors"]) > 0

    def test_rollback_on_upsert_failure(self, tmp_path):
        from nexus.catalog.consolidation import merge_corpus

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Paper A", content_type="paper", corpus="test",
                     physical_collection="docs__La", chunk_count=2)
        cat.register(owner, "Paper B", content_type="paper", corpus="test",
                     physical_collection="docs__Lb", chunk_count=2)

        t3 = MagicMock()
        col_a = _mock_t3_col(["a1", "a2"])
        col_b = _mock_t3_col(["b1", "b2"])
        target_col = MagicMock()
        # First upsert succeeds, second fails
        target_col.upsert.side_effect = [None, RuntimeError("ChromaDB error")]
        target_col.count.return_value = 2

        def get_or_create(name):
            if name == "docs__La":
                return col_a
            elif name == "docs__Lb":
                return col_b
            else:
                return target_col

        t3.get_or_create_collection.side_effect = get_or_create

        result = merge_corpus(cat, t3, "test")
        assert result["merged"] == 1  # First succeeded
        assert len(result["errors"]) == 1  # Second failed
        # Paper B should still point to original collection
        entry_b = cat.find("Paper B")[0]
        resolved = cat.resolve(entry_b.tumbler)
        assert resolved.physical_collection == "docs__Lb"


class TestConsolidateCommand:
    @patch("nexus.commands.catalog._make_t3")
    def test_consolidate_dry_run(self, mock_t3_fn, tmp_path, monkeypatch):
        catalog_dir = tmp_path / "catalog"
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
        cat = Catalog.init(catalog_dir)
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Paper A", content_type="paper", corpus="test",
                     physical_collection="docs__La")

        mock_t3_fn.return_value = MagicMock()

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "consolidate", "test", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()
