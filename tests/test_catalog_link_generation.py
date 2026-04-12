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


def _make_catalog(tmp_path: Path) -> Catalog:
    catalog_dir = tmp_path / "catalog"
    cat = Catalog.init(catalog_dir)
    return cat


class TestCitationLinks:
    def test_citation_from_ss_id(self, tmp_path):
        from nexus.catalog.link_generator import generate_citation_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        # Paper A cites Paper B via references
        cat.register(
            owner, "Paper A", content_type="paper",
            meta={
                "bib_semantic_scholar_id": "ssA",
                "references": ["ssB"],
            },
        )
        cat.register(
            owner, "Paper B", content_type="paper",
            meta={"bib_semantic_scholar_id": "ssB"},
        )
        count = generate_citation_links(cat)
        assert count == 1
        links = cat.links_from(cat.find("Paper A")[0].tumbler, link_type="cites")
        assert len(links) == 1
        assert links[0].created_by == "bib_enricher"

    def test_no_self_citation(self, tmp_path):
        from nexus.catalog.link_generator import generate_citation_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(
            owner, "Paper A", content_type="paper",
            meta={
                "bib_semantic_scholar_id": "ssA",
                "references": ["ssA"],  # self-reference
            },
        )
        count = generate_citation_links(cat)
        assert count == 0

    def test_no_duplicate_citations(self, tmp_path):
        from nexus.catalog.link_generator import generate_citation_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(
            owner, "Paper A", content_type="paper",
            meta={"bib_semantic_scholar_id": "ssA", "references": ["ssB"]},
        )
        cat.register(
            owner, "Paper B", content_type="paper",
            meta={"bib_semantic_scholar_id": "ssB"},
        )
        generate_citation_links(cat)
        count2 = generate_citation_links(cat)
        assert count2 == 0  # No new links on second run

    def test_no_link_when_target_missing(self, tmp_path):
        from nexus.catalog.link_generator import generate_citation_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(
            owner, "Paper A", content_type="paper",
            meta={"bib_semantic_scholar_id": "ssA", "references": ["ssC"]},
        )
        # ssC not in catalog
        count = generate_citation_links(cat)
        assert count == 0


class TestRdrFilePathLinks:
    """Test generate_rdr_filepath_links — extract file paths from RDR content."""

    def test_backtick_path_creates_link(self, tmp_path):
        from nexus.catalog.link_generator import generate_rdr_filepath_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        code_t = cat.register(
            owner, "catalog.py", content_type="code",
            file_path="src/nexus/catalog/catalog.py",
        )
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("We modified `src/nexus/catalog/catalog.py` to fix the bug.")
        rdr_t = cat.register(
            owner, "Fix Catalog Bug", content_type="rdr",
            file_path=str(rdr_path),
        )
        count = generate_rdr_filepath_links(cat)
        assert count == 1
        links = cat.links_from(rdr_t, link_type="implements")
        assert len(links) == 1
        assert links[0].created_by == "filepath_extractor"

    def test_bare_path_creates_link(self, tmp_path):
        from nexus.catalog.link_generator import generate_rdr_filepath_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(
            owner, "indexer.py", content_type="code",
            file_path="src/nexus/indexer.py",
        )
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("Changes go into src/nexus/indexer.py for the pipeline.")
        rdr_t = cat.register(
            owner, "Pipeline Design", content_type="rdr",
            file_path=str(rdr_path),
        )
        count = generate_rdr_filepath_links(cat)
        assert count == 1

    def test_multiple_paths_in_one_rdr(self, tmp_path):
        from nexus.catalog.link_generator import generate_rdr_filepath_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(owner, "a.py", content_type="code", file_path="src/a.py")
        cat.register(owner, "b.py", content_type="code", file_path="src/b.py")
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("Modify `src/a.py` and `src/b.py` together.")
        cat.register(owner, "Multi-file RDR", content_type="rdr", file_path=str(rdr_path))
        count = generate_rdr_filepath_links(cat)
        assert count == 2

    def test_no_link_for_unindexed_path(self, tmp_path):
        from nexus.catalog.link_generator import generate_rdr_filepath_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("See `src/nexus/missing_file.py` for details.")
        cat.register(owner, "Dangling Ref", content_type="rdr", file_path=str(rdr_path))
        count = generate_rdr_filepath_links(cat)
        assert count == 0

    def test_no_duplicate_on_rerun(self, tmp_path):
        from nexus.catalog.link_generator import generate_rdr_filepath_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("Edit `src/catalog.py`.")
        cat.register(owner, "Catalog Work", content_type="rdr", file_path=str(rdr_path))
        generate_rdr_filepath_links(cat)
        count2 = generate_rdr_filepath_links(cat)
        assert count2 == 0

    def test_rdr_without_file_on_disk_skipped(self, tmp_path):
        from nexus.catalog.link_generator import generate_rdr_filepath_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(owner, "ghost.py", content_type="code", file_path="src/ghost.py")
        cat.register(
            owner, "Ghost RDR", content_type="rdr",
            file_path="/nonexistent/path/rdr.md",
        )
        count = generate_rdr_filepath_links(cat)
        assert count == 0  # RDR file doesn't exist, skip gracefully

    def test_test_file_paths_matched(self, tmp_path):
        from nexus.catalog.link_generator import generate_rdr_filepath_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(
            owner, "test_catalog.py", content_type="code",
            file_path="tests/test_catalog.py",
        )
        rdr_path = tmp_path / "rdr.md"
        rdr_path.write_text("Run `tests/test_catalog.py` to verify.")
        cat.register(owner, "Test Coverage", content_type="rdr", file_path=str(rdr_path))
        count = generate_rdr_filepath_links(cat)
        assert count == 1


class TestCreatedByTracking:
    def test_all_auto_links_have_machine_created_by(self, tmp_path):
        from nexus.catalog.link_generator import generate_citation_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("test", "curator")
        cat.register(
            owner, "Paper A", content_type="paper",
            meta={"bib_semantic_scholar_id": "ssA", "references": ["ssB"]},
        )
        cat.register(
            owner, "Paper B", content_type="paper",
            meta={"bib_semantic_scholar_id": "ssB"},
        )
        generate_citation_links(cat)

        # Check all links have machine-generated created_by
        links = cat.links_from(cat.find("Paper A")[0].tumbler)
        for link in links:
            assert link.created_by in {"bib_enricher", "index_hook"}
