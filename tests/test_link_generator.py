# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for link_generator.py — RDR filepath linking with resolve_path (RDR-060)
and entity-name matching (RDR-061 E3a)."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.link_generator import (
    generate_code_rdr_links,
    generate_entity_name_links,
    generate_rdr_filepath_links,
)


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


class TestIncrementalCodeRdrLinking:
    """Incremental mode for generate_code_rdr_links via new_tumblers parameter."""

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def test_empty_new_tumblers_returns_zero(self, tmp_path: Path) -> None:
        """Passing new_tumblers=[] skips all work and returns 0."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")
        cat.register(owner, "rdr-1", content_type="rdr", file_path="docs/rdr/rdr-1.md")
        count = generate_code_rdr_links(cat, new_tumblers=[])
        assert count == 0

    def test_full_scan_when_new_tumblers_is_none(self, tmp_path: Path) -> None:
        """new_tumblers=None uses existing full-scan behavior."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")
        cat.register(owner, "rdr-catalog-001", content_type="rdr", file_path="docs/rdr/rdr-catalog-001.md")
        # None = full scan (backward-compatible)
        count = generate_code_rdr_links(cat, new_tumblers=None)
        # "catalog" > 3 chars, matches "rdrcatalog001" normalized title
        assert count == 1

    def test_incremental_new_code_entry(self, tmp_path: Path) -> None:
        """Only the new code tumbler is evaluated against existing RDRs."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))

        # Pre-existing entries
        old_code = cat.register(owner, "indexer.py", content_type="code", file_path="src/indexer.py")
        rdr_t = cat.register(owner, "rdr-indexer-001", content_type="rdr", file_path="docs/rdr/rdr-indexer-001.md")

        # Full scan to establish baseline links
        count_full = generate_code_rdr_links(cat)
        assert count_full >= 1  # indexer matches rdr-indexer-001

        # Add a new code entry that matches the existing RDR
        new_t = cat.register(owner, "chunker.py", content_type="code", file_path="src/chunker.py")

        # Incremental — should only check new_t against all RDRs, not redo old_code
        count_inc = generate_code_rdr_links(cat, new_tumblers=[new_t])
        # "chunker" does not match "rdrindexer001" so count_inc == 0
        assert count_inc == 0

    def test_incremental_new_rdr_matches_existing_code(self, tmp_path: Path) -> None:
        """A new RDR tumbler is checked against all existing code entries."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))

        # Pre-existing code
        code_t = cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")

        # Add a new RDR that matches "catalog"
        new_rdr = cat.register(owner, "rdr-catalog-001", content_type="rdr", file_path="docs/rdr/rdr-catalog-001.md")

        count_inc = generate_code_rdr_links(cat, new_tumblers=[new_rdr])
        assert count_inc == 1

        # Verify the link points correctly
        links = cat.links_from(code_t)
        assert len(links) == 1
        assert str(links[0].to_tumbler) == str(new_rdr)
        assert links[0].link_type == "implements-heuristic"

    def test_incremental_idempotent(self, tmp_path: Path) -> None:
        """Running incremental twice with the same tumbler produces no duplicates."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        code_t = cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")
        new_rdr = cat.register(owner, "rdr-catalog-002", content_type="rdr", file_path="docs/rdr/rdr-catalog-002.md")

        count1 = generate_code_rdr_links(cat, new_tumblers=[new_rdr])
        count2 = generate_code_rdr_links(cat, new_tumblers=[new_rdr])
        assert count1 == 1
        assert count2 == 0  # idempotent


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


class TestGenerateEntityNameLinks:
    """Entity resolution via symbol-name matching (RDR-061 E3a, nexus-ggjt)."""

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def test_exact_camel_case_match(self, tmp_path: Path) -> None:
        """Code entry 'SearchEngine' matches knowledge 'search engine architecture'."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        code_t = cat.register(owner, "SearchEngine", content_type="code", file_path="src/search_engine.py")

        owner_k = cat.register_owner("kbase", "curator")
        know_t = cat.register(owner_k, "search engine architecture", content_type="knowledge")

        count = generate_entity_name_links(cat)
        assert count == 1

        links = cat.links_from(code_t)
        assert len(links) == 1
        assert str(links[0].to_tumbler) == str(know_t)
        assert links[0].link_type == "relates"

    def test_snake_case_match(self, tmp_path: Path) -> None:
        """Code entry 'search_engine' matches knowledge 'search engine'."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "search_engine", content_type="code", file_path="src/search_engine.py")

        owner_k = cat.register_owner("kbase", "curator")
        cat.register(owner_k, "search engine overview", content_type="knowledge")

        count = generate_entity_name_links(cat)
        assert count == 1

    def test_fuzzy_below_threshold_no_link(self, tmp_path: Path) -> None:
        """67% token overlap (2/3) is below 80% threshold — no link."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "LinkGenerator", content_type="code", file_path="src/link_generator.py")

        owner_k = cat.register_owner("kbase", "curator")
        cat.register(owner_k, "link generation pipeline", content_type="knowledge")

        count = generate_entity_name_links(cat)
        # "link" + "generator" vs "link" + "generation" + "pipeline"
        # code tokens: {link, generator}, prose tokens: {link, generation, pipeline}
        # intersection: {link} = 1, union: {link, generator, generation, pipeline} = 4
        # Jaccard: 1/4 = 25% — well below 80%
        assert count == 0

    def test_no_false_positive_short_names(self, tmp_path: Path) -> None:
        """Tokens shorter than 3 chars are filtered — 'id' should not match."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "id", content_type="code", file_path="src/id.py")

        owner_k = cat.register_owner("kbase", "curator")
        cat.register(owner_k, "identity management and id handling", content_type="knowledge")

        count = generate_entity_name_links(cat)
        assert count == 0  # "id" tokens are all too short

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling twice creates same number of links (uses link_if_absent)."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "SearchEngine", content_type="code", file_path="src/search_engine.py")

        owner_k = cat.register_owner("kbase", "curator")
        cat.register(owner_k, "search engine architecture", content_type="knowledge")

        count1 = generate_entity_name_links(cat)
        count2 = generate_entity_name_links(cat)
        assert count1 == 1
        assert count2 == 0  # idempotent

    def test_full_scan_none_new_tumblers(self, tmp_path: Path) -> None:
        """Full scan with new_tumblers=None processes all entries."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "SearchEngine", content_type="code", file_path="src/search_engine.py")
        cat.register(owner, "ResultFormatter", content_type="code", file_path="src/result_formatter.py")

        owner_k = cat.register_owner("kbase", "curator")
        cat.register(owner_k, "search engine design", content_type="knowledge")

        count = generate_entity_name_links(cat, new_tumblers=None)
        assert count == 1  # only SearchEngine matches

    def test_no_same_collection_links(self, tmp_path: Path) -> None:
        """Code entries should not link to other code entries."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "SearchEngine", content_type="code", file_path="src/search_engine.py")
        cat.register(owner, "search_engine_test", content_type="code", file_path="tests/test_search_engine.py")

        count = generate_entity_name_links(cat)
        assert count == 0  # no cross-type match

    def test_code_matches_rdr_title(self, tmp_path: Path) -> None:
        """Code entries match against RDR titles too."""
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("test", "repo", repo_hash="abc", repo_root=str(tmp_path))
        cat.register(owner, "SearchEngine", content_type="code", file_path="src/search_engine.py")
        cat.register(owner, "RDR: search engine improvements", content_type="rdr",
                     file_path="docs/rdr/rdr-042.md")

        count = generate_entity_name_links(cat)
        assert count == 1
