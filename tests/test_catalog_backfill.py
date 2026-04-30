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
    """Create a mock RepoRegistry with a repo that exists on disk."""
    mock = MagicMock()
    # Create the repo dir so the path-existence check passes
    repo_dir = tmp_path / "myrepo"
    repo_dir.mkdir(exist_ok=True)
    if repos is None:
        repos = {
            str(repo_dir): {
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

        count, claimed = _backfill_repos(cat, registry, dry_run=False)
        assert count >= 0
        assert len(claimed) >= 1  # Should claim at least one collection
        # Owner should exist
        owner = cat.owner_for_repo(cat._db.execute(
            "SELECT repo_hash FROM owners WHERE owner_type='repo'"
        ).fetchone()[0])
        assert owner is not None

    def test_backfill_repos_dry_run(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        _backfill_repos(cat, registry, dry_run=True)
        # No owners should be created
        rows = cat._db.execute("SELECT count(*) FROM owners").fetchone()
        assert rows[0] == 0

    def test_backfill_repos_idempotent(self, catalog_env, tmp_path):
        from nexus.commands.catalog import _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        registry = _mock_registry(tmp_path)

        _backfill_repos(cat, registry, dry_run=False)
        _backfill_repos(cat, registry, dry_run=False)
        rows = cat._db.execute("SELECT count(*) FROM owners WHERE owner_type='repo'").fetchone()
        assert rows[0] == 1


class TestBackfillKnowledge:
    def test_backfill_knowledge(self, catalog_env):
        from nexus.commands.catalog import _backfill_knowledge

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        t3 = _mock_t3([{"name": "knowledge__delos", "count": 20}])

        count = _backfill_knowledge(cat, t3, dry_run=False)
        assert count == 1
        rows = cat._db.execute(
            "SELECT title FROM documents WHERE content_type='knowledge'"
        ).fetchone()
        assert rows is not None

    def test_backfill_knowledge_dry_run(self, catalog_env):
        from nexus.commands.catalog import _backfill_knowledge

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        t3 = _mock_t3([{"name": "knowledge__delos", "count": 20}])

        _backfill_knowledge(cat, t3, dry_run=True)
        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
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


class TestBackfillRdrsRepoOwner:
    """nexus-3e4s critique-followup S1.

    Pre-fix ``_backfill_rdrs`` unconditionally created a curator owner
    for every ``rdr__*`` collection, which made the register-time
    cross-project guard skip (curator owners legitimately span
    sources). The disaster-recovery path could therefore re-introduce
    contamination it was supposed to clean up.

    Post-fix: when the collection's hash suffix matches a registered
    repo, ``_backfill_rdrs`` looks up the existing repo owner and
    registers under it. Curator is the legitimate fallback for orphan
    ``rdr__*`` collections only.
    """

    def _mock_t3_with_rdr(self, repo_dir: Path, repo_hash: str):
        from unittest.mock import MagicMock

        mock = MagicMock()
        col_name = f"rdr__myrepo-{repo_hash}"
        mock.list_collections.return_value = [{"name": col_name, "count": 1}]
        mock_col = MagicMock()
        # Two pages: first returns the doc, second returns empty (loop exit).
        mock_col.get.side_effect = [
            {
                "metadatas": [{
                    "source_path": str(repo_dir / "docs" / "rdr" / "RDR-001.md"),
                    "title": "RDR-001",
                }],
            },
            {"metadatas": []},
        ]
        mock.get_or_create_collection.return_value = mock_col
        return mock, col_name

    def test_backfill_rdrs_uses_repo_owner_when_collection_matches_repo(
        self, catalog_env, tmp_path,
    ):
        import hashlib

        from nexus.commands.catalog import _backfill_rdrs, _backfill_repos

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        repo_dir = tmp_path / "myrepo"
        repo_dir.mkdir()
        repo_hash = hashlib.sha256(str(repo_dir).encode()).hexdigest()[:8]

        # Step 1: register the repo via _backfill_repos so the owner exists.
        registry = _mock_registry(tmp_path, {
            str(repo_dir): {
                "name": "myrepo",
                "code_collection": f"code__myrepo-{repo_hash}",
                "docs_collection": f"docs__myrepo-{repo_hash}",
                "head_hash": "abc",
                "status": "ready",
            },
        })
        _backfill_repos(cat, registry, dry_run=False)

        # Step 2: backfill RDRs. The collection name carries the same
        # hash, so the function should pick up the existing repo owner.
        t3, col_name = self._mock_t3_with_rdr(repo_dir, repo_hash)
        with patch(
            "nexus.catalog.catalog._default_registry_path",
            return_value=tmp_path / "repos.json",
        ):
            (tmp_path / "repos.json").write_text(json.dumps({
                "repos": {
                    str(repo_dir): {"name": "myrepo", "head_hash": "abc"},
                },
            }))
            count = _backfill_rdrs(cat, t3, dry_run=False)

        assert count == 1
        # Verify the doc landed under the repo owner (NOT a curator).
        rows = cat._db.execute(
            "SELECT d.tumbler, o.owner_type FROM documents d "
            "JOIN owners o ON d.tumbler LIKE o.tumbler_prefix || '.%' "
            "WHERE d.physical_collection = ? AND d.content_type = 'rdr'",
            (col_name,),
        ).fetchall()
        assert rows, f"no RDR row in {col_name}"
        # The matching owner row must be a repo owner.
        repo_owner_match = [r for r in rows if r[1] == "repo"]
        assert repo_owner_match, (
            f"backfill registered RDR under non-repo owner: {rows!r} — "
            "this lets the cross-project guard skip on the disaster-"
            "recovery path"
        )

    def test_backfill_rdrs_falls_back_to_curator_when_no_match(
        self, catalog_env, tmp_path,
    ):
        from nexus.commands.catalog import _backfill_rdrs

        cat = Catalog(catalog_env, catalog_env / ".catalog.db")
        # No matching repo registered — curator fallback is correct.
        t3, col_name = self._mock_t3_with_rdr(tmp_path / "fake", "deadbeef")
        with patch(
            "nexus.catalog.catalog._default_registry_path",
            return_value=tmp_path / "repos.json",
        ):
            (tmp_path / "repos.json").write_text(json.dumps({"repos": {}}))
            count = _backfill_rdrs(cat, t3, dry_run=False)
        assert count == 1
        rows = cat._db.execute(
            "SELECT o.owner_type FROM documents d "
            "JOIN owners o ON d.tumbler LIKE o.tumbler_prefix || '.%' "
            "WHERE d.physical_collection = ?",
            (col_name,),
        ).fetchall()
        assert any(r[0] == "curator" for r in rows)
