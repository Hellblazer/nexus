# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


class TestInit:
    def test_creates_structure(self, tmp_path):
        cat = Catalog.init(tmp_path / "catalog")
        catalog_dir = tmp_path / "catalog"
        assert (catalog_dir / ".git").exists()
        assert (catalog_dir / "documents.jsonl").exists()
        assert (catalog_dir / "owners.jsonl").exists()
        assert (catalog_dir / "links.jsonl").exists()
        assert ".catalog.db" in (catalog_dir / ".gitignore").read_text()

    def test_creates_db(self, tmp_path):
        cat = Catalog.init(tmp_path / "catalog")
        assert (tmp_path / "catalog" / ".catalog.db").exists()

    def test_initial_commit_exists(self, tmp_path):
        Catalog.init(tmp_path / "catalog")
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=tmp_path / "catalog",
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "init catalog" in result.stdout.lower()

    def test_init_with_remote(self, tmp_path):
        bare = tmp_path / "bare"
        subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)
        Catalog.init(tmp_path / "catalog", remote=str(bare))
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=tmp_path / "catalog",
            capture_output=True, text=True,
        )
        assert str(bare) in result.stdout

    def test_init_idempotent(self, tmp_path):
        Catalog.init(tmp_path / "catalog")
        # Second init should not fail
        cat = Catalog.init(tmp_path / "catalog")
        assert cat is not None


class TestIsInitialized:
    def test_true_after_init(self, tmp_path):
        Catalog.init(tmp_path / "catalog")
        assert Catalog.is_initialized(tmp_path / "catalog")

    def test_false_on_empty_dir(self, tmp_path):
        (tmp_path / "catalog").mkdir()
        assert not Catalog.is_initialized(tmp_path / "catalog")

    def test_false_on_nonexistent(self, tmp_path):
        assert not Catalog.is_initialized(tmp_path / "nonexistent")


class TestSync:
    def test_sync_commits_changes(self, tmp_path):
        cat = Catalog.init(tmp_path / "catalog")
        cat.register_owner("test", "curator")
        cat.sync("add test owner")
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=tmp_path / "catalog",
            capture_output=True, text=True,
        )
        assert "add test owner" in result.stdout

    def test_sync_nothing_to_commit(self, tmp_path):
        cat = Catalog.init(tmp_path / "catalog")
        # Should not raise when nothing changed
        cat.sync("no changes")

    def test_sync_pushes_to_remote(self, tmp_path):
        bare = tmp_path / "bare"
        subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True)
        cat = Catalog.init(tmp_path / "catalog", remote=str(bare))
        # Push initial commit
        cat.sync("initial")
        cat.register_owner("test", "curator")
        cat.sync("add owner")
        # Verify remote has the commits
        result = subprocess.run(
            ["git", "log", "--oneline", "origin/main"],
            cwd=tmp_path / "catalog",
            capture_output=True, text=True,
        )
        # May be main or master depending on git config
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "log", "--oneline", "origin/master"],
                cwd=tmp_path / "catalog",
                capture_output=True, text=True,
            )
        assert "add owner" in result.stdout


class TestPull:
    def test_pull_triggers_rebuild(self, tmp_path):
        cat = Catalog.init(tmp_path / "catalog")
        owner = cat.register_owner("test", "curator")
        doc = cat.register(owner, "paper.pdf", content_type="paper")

        # Create fresh Catalog pointing at same dir with new DB
        cat2 = Catalog(tmp_path / "catalog", tmp_path / "catalog" / ".catalog2.db")
        # Before pull/rebuild, the new DB has no data
        assert cat2.resolve(doc) is None
        cat2.pull()
        entry = cat2.resolve(doc)
        assert entry is not None
        assert entry.title == "paper.pdf"


class TestCatalogPath:
    def test_env_override(self, tmp_path, monkeypatch):
        from nexus.config import catalog_path
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "custom"))
        assert catalog_path() == tmp_path / "custom"

    def test_default_path(self, monkeypatch):
        from nexus.config import catalog_path
        monkeypatch.delenv("NEXUS_CATALOG_PATH", raising=False)
        result = catalog_path()
        assert str(result).endswith("nexus/catalog")
