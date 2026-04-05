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


class TestCodeRdrLinks:
    def test_code_rdr_heuristic(self, tmp_path):
        from nexus.catalog.link_generator import generate_code_rdr_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(
            owner, "catalog.py", content_type="code",
            file_path="src/nexus/catalog/catalog.py",
        )
        cat.register(
            owner, "Git-Backed Catalog Design", content_type="rdr",
            file_path="docs/rdr/rdr-049-git-backed-catalog.md",
        )
        count = generate_code_rdr_links(cat)
        assert count == 1
        # Link direction: code → implements → RDR
        code_entry = cat.by_file_path(owner, "src/nexus/catalog/catalog.py")
        links = cat.links_from(code_entry.tumbler, link_type="implements")
        assert len(links) == 1
        assert links[0].created_by == "index_hook"

    def test_short_names_not_matched(self, tmp_path):
        from nexus.catalog.link_generator import generate_code_rdr_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(owner, "db.py", content_type="code", file_path="src/db.py")
        cat.register(owner, "Database Design", content_type="rdr", file_path="docs/rdr/db.md")
        count = generate_code_rdr_links(cat)
        assert count == 0  # "db" is too short (<=3 chars)

    def test_no_duplicates(self, tmp_path):
        from nexus.catalog.link_generator import generate_code_rdr_links

        cat = _make_catalog(tmp_path)
        owner = cat.register_owner("nexus", "repo", repo_hash="abcd1234")
        cat.register(owner, "catalog.py", content_type="code", file_path="src/catalog.py")
        cat.register(owner, "Catalog Design", content_type="rdr", file_path="docs/rdr/catalog.md")
        generate_code_rdr_links(cat)
        count2 = generate_code_rdr_links(cat)
        assert count2 == 0


class TestCreatedByTracking:
    def test_all_auto_links_have_machine_created_by(self, tmp_path):
        from nexus.catalog.link_generator import (
            generate_citation_links,
            generate_code_rdr_links,
        )

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
