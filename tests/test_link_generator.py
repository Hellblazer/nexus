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
