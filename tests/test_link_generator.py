# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for link_generator.py — RDR filepath linking with resolve_path (RDR-060)."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.link_generator import generate_rdr_filepath_links


class TestRdrFilepathLinks:
    """generate_rdr_filepath_links uses resolve_path for relative file_path."""

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def test_filepath_linker_with_relative_paths(self, tmp_path: Path) -> None:
        """RDR with relative file_path resolves via resolve_path and generates links."""
        repo = tmp_path / "myrepo"
        repo.mkdir()

        # Create actual RDR file on disk (resolve_path must return an existing file)
        rdr_dir = repo / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        rdr_file = rdr_dir / "rdr-001.md"
        rdr_file.write_text("# RDR-001\n\nThis affects `src/nexus/catalog.py` directly.\n")

        cat = self._make_catalog(tmp_path)
        # Register repo owner with repo_root
        owner = cat.register_owner("myrepo", "repo", repo_hash="abc12345", repo_root=str(repo))

        # Register RDR entry with RELATIVE file_path
        rdr_tumbler = cat.register(
            owner, "RDR-001", content_type="rdr",
            file_path="docs/rdr/rdr-001.md",
        )

        # Register code entry with matching relative file_path
        code_tumbler = cat.register(
            owner, "catalog.py", content_type="code",
            file_path="src/nexus/catalog.py",
        )

        count = generate_rdr_filepath_links(cat)
        assert count == 1

        # Verify the link was created
        links = cat.links_from(rdr_tumbler)
        assert len(links) == 1
        assert str(links[0].to_tumbler) == str(code_tumbler)
        assert links[0].link_type == "implements"

    def test_filepath_linker_curator_skipped(self, tmp_path: Path) -> None:
        """Curator RDR entries (resolve_path returns None) are skipped."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "paper.pdf", content_type="rdr", file_path="paper.pdf")
        count = generate_rdr_filepath_links(cat)
        assert count == 0

    def test_filepath_linker_file_not_on_disk(self, tmp_path: Path) -> None:
        """resolve_path returns path but file doesn't exist on disk -- entry skipped."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("myrepo", "repo", repo_hash="abc12345", repo_root=str(repo))
        # Register RDR with file_path that doesn't exist on disk
        cat.register(owner, "RDR-999", content_type="rdr", file_path="docs/rdr/nonexistent.md")
        count = generate_rdr_filepath_links(cat)
        assert count == 0

    def test_filepath_linker_idempotent(self, tmp_path: Path) -> None:
        """Running the linker twice produces no duplicate links."""
        repo = tmp_path / "myrepo"
        repo.mkdir()

        rdr_dir = repo / "docs" / "rdr"
        rdr_dir.mkdir(parents=True)
        rdr_file = rdr_dir / "rdr-001.md"
        rdr_file.write_text("# RDR-001\n\nThis affects `src/nexus/catalog.py` directly.\n")

        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("myrepo", "repo", repo_hash="abc12345", repo_root=str(repo))
        rdr_tumbler = cat.register(owner, "RDR-001", content_type="rdr", file_path="docs/rdr/rdr-001.md")
        cat.register(owner, "catalog.py", content_type="code", file_path="src/nexus/catalog.py")

        count1 = generate_rdr_filepath_links(cat)
        count2 = generate_rdr_filepath_links(cat)
        assert count1 == 1
        assert count2 == 0  # idempotent — no new links
        assert len(cat.links_from(rdr_tumbler)) == 1


class TestIncrementalRdrFilepathLinking:
    """Incremental mode for generate_rdr_filepath_links via new_tumblers parameter."""

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def _make_rdr_file(self, repo: Path, name: str, content: str) -> Path:
        rdr_dir = repo / "docs" / "rdr"
        rdr_dir.mkdir(parents=True, exist_ok=True)
        f = rdr_dir / name
        f.write_text(content)
        return f

    def test_empty_new_tumblers_returns_zero(self, tmp_path: Path) -> None:
        """Passing new_tumblers=[] skips all work and returns 0."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        self._make_rdr_file(repo, "rdr-001.md", "# RDR\n\nSee src/catalog.py.\n")
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("myrepo", "repo", repo_hash="abc", repo_root=str(repo))
        cat.register(owner, "RDR-001", content_type="rdr", file_path="docs/rdr/rdr-001.md")
        cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")
        count = generate_rdr_filepath_links(cat, new_tumblers=[])
        assert count == 0

    def test_full_scan_when_new_tumblers_is_none(self, tmp_path: Path) -> None:
        """new_tumblers=None uses existing full-scan behavior."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        self._make_rdr_file(repo, "rdr-001.md", "# RDR\n\nSee src/catalog.py.\n")
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("myrepo", "repo", repo_hash="abc", repo_root=str(repo))
        cat.register(owner, "RDR-001", content_type="rdr", file_path="docs/rdr/rdr-001.md")
        cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")
        count = generate_rdr_filepath_links(cat, new_tumblers=None)
        assert count == 1

    def test_incremental_only_scans_new_rdrs(self, tmp_path: Path) -> None:
        """With new_tumblers, only listed RDR tumblers are scanned."""
        repo = tmp_path / "myrepo"
        repo.mkdir()
        self._make_rdr_file(repo, "rdr-001.md", "# RDR-001\n\nSee src/catalog.py.\n")
        self._make_rdr_file(repo, "rdr-002.md", "# RDR-002\n\nNo file paths here.\n")

        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("myrepo", "repo", repo_hash="abc", repo_root=str(repo))
        rdr1 = cat.register(owner, "RDR-001", content_type="rdr", file_path="docs/rdr/rdr-001.md")
        rdr2 = cat.register(owner, "RDR-002", content_type="rdr", file_path="docs/rdr/rdr-002.md")
        cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")

        # Only process rdr2 (which has no paths) — should not pick up rdr1's path
        count = generate_rdr_filepath_links(cat, new_tumblers=[rdr2])
        assert count == 0

        # Process rdr1 explicitly — should create 1 link
        count2 = generate_rdr_filepath_links(cat, new_tumblers=[rdr1])
        assert count2 == 1
