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


class TestRunHousekeeping:
    """Tests for _run_housekeeping() — orphan detection with miss_count tracking."""

    def _make_cat(self, tmp_path: Path) -> tuple[Path, "Catalog"]:
        return _make_catalog(tmp_path)

    def test_miss_count_incremented_for_missing_file(self, tmp_path, monkeypatch):
        """Entry not in indexed_set → miss_count goes from 0 to 1, not deleted."""
        from nexus.indexer import _run_housekeeping
        from nexus.catalog.tumbler import Tumbler

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="aaa111")
        owner_t = cat.owner_for_repo("aaa111")
        t = cat.register(owner_t, "missing.py", content_type="code", file_path="src/missing.py")

        _run_housekeeping(cat, owner_t, indexed_set=set())

        entry = cat.resolve(t)
        assert entry is not None  # not deleted yet
        assert entry.meta.get("miss_count") == 1

    def test_miss_count_reset_when_file_seen(self, tmp_path, monkeypatch):
        """Entry in indexed_set with miss_count=1 → reset to 0."""
        from nexus.indexer import _run_housekeeping
        from nexus.catalog.tumbler import Tumbler

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="bbb222")
        owner_t = cat.owner_for_repo("bbb222")
        t = cat.register(owner_t, "present.py", content_type="code", file_path="src/present.py")
        # Simulate a prior miss
        cat.update(t, meta={"miss_count": 1})

        _run_housekeeping(cat, owner_t, indexed_set={"src/present.py"})

        entry = cat.resolve(t)
        assert entry is not None
        assert entry.meta.get("miss_count", 0) == 0

    def test_orphan_deleted_at_threshold(self, tmp_path, monkeypatch):
        """Entry with miss_count=1, not in indexed_set → increments to 2 → deleted."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="ccc333")
        owner_t = cat.owner_for_repo("ccc333")
        t = cat.register(owner_t, "stale.py", content_type="code", file_path="src/stale.py")
        cat.update(t, meta={"miss_count": 1})

        _run_housekeeping(cat, owner_t, indexed_set=set())

        # Should be deleted after reaching threshold of 2
        assert cat.resolve(t) is None

    def test_already_at_threshold_gets_deleted(self, tmp_path, monkeypatch):
        """Entry with miss_count already >= 2 and not in indexed_set is deleted."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="ddd444")
        owner_t = cat.owner_for_repo("ddd444")
        t = cat.register(owner_t, "dead.py", content_type="code", file_path="src/dead.py")
        cat.update(t, meta={"miss_count": 2})

        _run_housekeeping(cat, owner_t, indexed_set=set())

        assert cat.resolve(t) is None

    def test_present_files_not_affected(self, tmp_path, monkeypatch):
        """Files in indexed_set are never modified (miss_count stays at 0 if already 0)."""
        from nexus.indexer import _run_housekeeping

        catalog_dir, cat = self._make_cat(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("nexus", "repo", repo_hash="eee555")
        owner_t = cat.owner_for_repo("eee555")
        t = cat.register(owner_t, "ok.py", content_type="code", file_path="src/ok.py")

        _run_housekeeping(cat, owner_t, indexed_set={"src/ok.py"})

        entry = cat.resolve(t)
        assert entry is not None
        assert entry.meta.get("miss_count", 0) == 0
