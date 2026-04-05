# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import json
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


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    catalog_dir = tmp_path / "catalog"
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))
    Catalog.init(catalog_dir)
    return catalog_dir


def _mock_registry(tmp_path: Path, repos: dict | None = None) -> MagicMock:
    """Create a mock RepoRegistry."""
    mock = MagicMock()
    if repos is None:
        repos = {
            str(tmp_path / "myrepo"): {
                "name": "myrepo",
                "collection": "code__myrepo",
                "code_collection": "code__myrepo",
                "docs_collection": "docs__myrepo",
                "head_hash": "abc123",
                "status": "ready",
            }
        }
    mock.all_info.return_value = repos
    return mock


def _mock_t3(collections: list[dict] | None = None) -> MagicMock:
    """Create a mock T3Database."""
    mock = MagicMock()
    if collections is None:
        collections = [
            {"name": "code__myrepo", "count": 100},
            {"name": "docs__myrepo", "count": 50},
            {"name": "knowledge__delos", "count": 20},
        ]
    mock.list_collections.return_value = collections

    # Mock get_or_create_collection to return a mock col with get()
    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": ["chunk1"], "metadatas": [{"title": "test doc"}]}
    mock.get_or_create_collection.return_value = mock_col
    return mock


class TestBackfillRepos:
    def test_backfill_creates_owner_and_docs(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        count = _backfill_repos(cat, registry, dry_run=False)
        assert count >= 0
        # Owner should exist
        owner = cat.owner_for_repo(cat._db._conn.execute(
            "SELECT repo_hash FROM owners WHERE owner_type='repo'"
        ).fetchone()[0])
        assert owner is not None

    def test_backfill_repos_dry_run(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        _backfill_repos(cat, registry, dry_run=True)
        # No owners should be created
        rows = cat._db._conn.execute("SELECT count(*) FROM owners").fetchone()
        assert rows[0] == 0

    def test_backfill_repos_idempotent(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        _backfill_repos(cat, registry, dry_run=False)
        _backfill_repos(cat, registry, dry_run=False)
        rows = cat._db._conn.execute("SELECT count(*) FROM owners WHERE owner_type='repo'").fetchone()
        assert rows[0] == 1


class TestBackfillKnowledge:
    def test_backfill_knowledge(self, catalog_env):
        from nexus.commands.catalog import _backfill_knowledge

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        t3 = _mock_t3([{"name": "knowledge__delos", "count": 20}])

        count = _backfill_knowledge(cat, t3, dry_run=False)
        assert count == 1
        rows = cat._db._conn.execute(
            "SELECT title FROM documents WHERE content_type='knowledge'"
        ).fetchone()
        assert rows is not None

    def test_backfill_knowledge_dry_run(self, catalog_env):
        from nexus.commands.catalog import _backfill_knowledge

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        t3 = _mock_t3([{"name": "knowledge__delos", "count": 20}])

        _backfill_knowledge(cat, t3, dry_run=True)
        rows = cat._db._conn.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 0


class TestBackfillCommand:
    @patch("nexus.commands.catalog._make_t3")
    @patch("nexus.commands.catalog._make_registry")
    def test_backfill_dry_run_cli(self, mock_reg_fn, mock_t3_fn, catalog_env, tmp_path):
        mock_reg_fn.return_value = _mock_registry(tmp_path)
        mock_t3_fn.return_value = _mock_t3()

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "backfill", "--dry-run"])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()

    @patch("nexus.commands.catalog._make_t3")
    @patch("nexus.commands.catalog._make_registry")
    def test_backfill_cli(self, mock_reg_fn, mock_t3_fn, catalog_env, tmp_path):
        mock_reg_fn.return_value = _mock_registry(tmp_path)
        mock_t3_fn.return_value = _mock_t3()

        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "backfill"])
        assert result.exit_code == 0
        assert "complete" in result.output.lower()

    def test_backfill_not_initialized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        runner = CliRunner()
        result = runner.invoke(main, ["catalog", "backfill"])
        assert result.exit_code != 0
        assert "not initialized" in result.output.lower()
