# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


def _make_catalog(tmp_path: Path) -> tuple[Path, Catalog]:
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    return catalog_dir, cat


class TestCatalogHookSkipped:
    def test_skipped_when_not_initialized(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        # Should not raise
        _catalog_hook(
            repo=tmp_path,
            repo_name="test",
            repo_hash="abcd1234",
            head_hash="abc",
            indexed_files=[],
        )


class TestCatalogHookOwner:
    def test_owner_auto_created(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_hook(
            repo=tmp_path,
            repo_name="nexus",
            repo_hash="571b8edd",
            head_hash="abc123",
            indexed_files=[],
        )
        assert cat.owner_for_repo("571b8edd") is not None

    def test_owner_reused_on_reindex(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc", indexed_files=[],
        )
        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="def", indexed_files=[],
        )
        # Should still be the same owner
        rows = cat._db.execute("SELECT count(*) FROM owners").fetchone()
        assert rows[0] == 1


class TestCatalogHookDocuments:
    def test_document_registered_on_first_index(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # Create a file to index
        src = tmp_path / "src" / "main.py"
        src.parent.mkdir(parents=True)
        src.write_text("print('hello')")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc123",
            indexed_files=[(src, "code", "code__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entry = cat.by_file_path(owner, "src/main.py")
        assert entry is not None
        assert entry.title == "main.py"
        assert entry.content_type == "code"
        assert entry.physical_collection == "code__nexus"

    def test_document_updated_on_reindex(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        src = tmp_path / "main.py"
        src.write_text("v1")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="aaa",
            indexed_files=[(src, "code", "code__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entry1 = cat.by_file_path(owner, "main.py")
        tumbler1 = entry1.tumbler

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="bbb",
            indexed_files=[(src, "code", "code__nexus")],
        )
        entry2 = cat.by_file_path(owner, "main.py")
        assert entry2.tumbler == tumbler1  # same tumbler
        assert entry2.head_hash == "bbb"  # updated hash

    def test_multiple_files(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        a = tmp_path / "a.py"
        b = tmp_path / "b.md"
        a.write_text("code")
        b.write_text("prose")

        _catalog_hook(
            repo=tmp_path, repo_name="nexus", repo_hash="571b8edd",
            head_hash="abc",
            indexed_files=[(a, "code", "code__nexus"), (b, "prose", "docs__nexus")],
        )
        owner = cat.owner_for_repo("571b8edd")
        entries = cat.by_owner(owner)
        assert len(entries) == 2


class TestCatalogHookErrorSafe:
    def test_hook_does_not_propagate_errors(self, tmp_path, monkeypatch):
        from nexus.indexer import _catalog_hook

        catalog_dir, _ = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # Pass a non-existent file — relative_to will work but that's fine
        # Force an error by passing bad data
        _catalog_hook(
            repo=Path("/nonexistent/repo"),
            repo_name="bad",
            repo_hash="xxx",
            head_hash="abc",
            indexed_files=[(Path("/nonexistent/repo/file.py"), "code", "code__test")],
        )
        # Should not raise — errors are caught internally
