# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 Phase 5.2 (nexus-tts0d.19): one-shot ``repos.json`` migration.

Tests the migration hook that ``nx upgrade`` calls after T2 migrations
complete. Behaviour per OQ-7 lock:
- repos.json absent → no-op (idempotent).
- catalog has every repos.json entry → delete the file.
- catalog missing any entry → keep the file, log disagreements.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.commands.upgrade import _migrate_repos_json_to_catalog
from nexus.registry import RepoRegistry


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cfg = tmp_path / "config"
    cat_dir = cfg / "catalog"
    cat_dir.mkdir(parents=True)
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(cfg))
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(cat_dir))
    return cfg


@pytest.fixture
def cat(cfg: Path) -> Catalog:
    cat_dir = cfg / "catalog"
    Catalog.init(cat_dir)
    return Catalog(cat_dir, cat_dir / ".catalog.db")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "myrepo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    return r


class TestRepoJsonMigration:
    def test_noop_when_repos_json_absent(self, cfg: Path) -> None:
        """Idempotent: absent file => no-op."""
        assert not (cfg / "repos.json").exists()
        _migrate_repos_json_to_catalog(dry_run=False)
        # No exception, file still absent.
        assert not (cfg / "repos.json").exists()

    def test_deletes_file_on_full_catalog_parity(
        self, cfg: Path, cat: Catalog, repo: Path,
    ) -> None:
        cat.ensure_owner_for_repo(repo)
        reg = RepoRegistry(cfg / "repos.json")
        reg.add(repo)

        _migrate_repos_json_to_catalog(dry_run=False)

        assert not (cfg / "repos.json").exists()

    def test_keeps_file_when_catalog_missing_owner(
        self, cfg: Path, cat: Catalog, repo: Path,
    ) -> None:
        """OQ-7 safety: don't silently delete a registry that has
        entries the catalog doesn't know about (stale config from
        another machine)."""
        # Registry knows the repo, catalog does NOT.
        reg = RepoRegistry(cfg / "repos.json")
        reg.add(repo)

        _migrate_repos_json_to_catalog(dry_run=False)

        # File survives.
        assert (cfg / "repos.json").exists()

    def test_skips_stale_registry_entries(
        self, cfg: Path, cat: Catalog, repo: Path, tmp_path: Path,
    ) -> None:
        """Registry entries pointing at deleted paths don't gate the
        migration — they're skipped (the prune-stale path handles them
        elsewhere)."""
        cat.ensure_owner_for_repo(repo)
        reg_path = cfg / "repos.json"
        # Hand-write a registry with the live repo AND a stale entry.
        reg_path.write_text(json.dumps({
            "repos": {
                str(repo): {"name": "myrepo", "collection": "code__x"},
                str(tmp_path / "deleted_long_ago"): {
                    "name": "ghost", "collection": "code__y",
                },
            },
        }))

        _migrate_repos_json_to_catalog(dry_run=False)

        # Live repo has parity; stale entry filtered; file deleted.
        assert not reg_path.exists()

    def test_dry_run_does_not_delete(
        self, cfg: Path, cat: Catalog, repo: Path,
    ) -> None:
        cat.ensure_owner_for_repo(repo)
        reg = RepoRegistry(cfg / "repos.json")
        reg.add(repo)

        _migrate_repos_json_to_catalog(dry_run=True)

        # File still present in dry-run mode.
        assert (cfg / "repos.json").exists()
