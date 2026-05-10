# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for link_generator.py — RDR filepath linking with resolve_path (RDR-060)."""
from __future__ import annotations

from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog
from nexus.catalog.link_generator import (
    generate_citation_links,
    generate_pdf_corpus_links,
    generate_prose_filepath_links,
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


class TestCitationLinksNoneMeta:
    """generate_citation_links must tolerate entries with meta=None (nexus-8d6e)."""

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def test_meta_none_does_not_crash(self, tmp_path: Path) -> None:
        """Entries with meta=None (legacy rows) are skipped without crashing."""
        from unittest.mock import patch

        from nexus.catalog.catalog import CatalogEntry
        from nexus.catalog.tumbler import Tumbler

        cat = self._make_catalog(tmp_path)
        # Construct a CatalogEntry with meta=None — the shape seen on legacy
        # JSONL rows where the "meta" key was absent when parsed.
        legacy = CatalogEntry(
            tumbler=Tumbler.parse("1.1.1"),
            title="legacy",
            author="",
            year=0,
            content_type="code",
            file_path="src/x.py",
            corpus="default",
            physical_collection="code__x",
            chunk_count=1,
            head_hash="",
            indexed_at="",
            meta=None,  # type: ignore[arg-type]
        )
        with patch.object(cat, "all_documents", return_value=[legacy]):
            count = generate_citation_links(cat)
        assert count == 0


# ── nexus-sob9: prose filepath linker ─────────────────────────────────────


class TestProseFilepathLinks:
    """generate_prose_filepath_links closes the prose=0.1% catalog
    auto-link coverage gap by scanning prose/markdown content for
    code file paths and creating ``implements`` links. Same shape
    as the RDR linker but with a wider source-side filter (content_type
    in {prose, markdown, docs}) and a relaxed regex (no source-root
    anchor; ``docs/`` and ``nx/`` paths match).
    """

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def test_prose_doc_in_docs_dir_links_to_code(self, tmp_path: Path) -> None:
        repo = tmp_path / "myrepo"
        repo.mkdir()
        docs_dir = repo / "docs"
        docs_dir.mkdir()
        # The prose doc mentions a file under ``docs/runbook.md`` and
        # ``src/nexus/foo.py``. The RDR linker would only catch the
        # second (source-root anchored). The prose linker catches both
        # if both are registered as code; here only foo.py is.
        prose_file = docs_dir / "runbook.md"
        prose_file.write_text(
            "# Runbook\n\n"
            "See ``src/nexus/foo.py`` for the impl. "
            "Also ``docs/legacy.md`` for context.\n"
        )

        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo),
        )
        prose_tumbler = cat.register(
            owner, "Runbook", content_type="prose",
            file_path="docs/runbook.md",
        )
        code_tumbler = cat.register(
            owner, "foo.py", content_type="code",
            file_path="src/nexus/foo.py",
        )

        count = generate_prose_filepath_links(cat)
        assert count == 1

        links = cat.links_from(prose_tumbler)
        assert len(links) == 1
        assert str(links[0].to_tumbler) == str(code_tumbler)
        assert links[0].link_type == "implements"

    def test_prose_doc_in_non_source_root_dir_links(self, tmp_path: Path) -> None:
        """nexus-sob9 widening contract: a docs/ -> nx/ reference (no
        ``src/`` anchor) MUST link. Pre-fix the RDR regex required a
        source-root prefix; ``nx/`` plugin paths never matched.
        Reverting the relaxed prose regex makes this test fail.
        """
        repo = tmp_path / "myrepo"
        repo.mkdir()
        prose_file = repo / "docs" / "guide.md"
        prose_file.parent.mkdir(parents=True)
        prose_file.write_text("See ``nx/skills/foo.md`` for usage.\n")

        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo),
        )
        prose_tumbler = cat.register(
            owner, "Guide", content_type="prose",
            file_path="docs/guide.md",
        )
        code_tumbler = cat.register(
            owner, "foo.md", content_type="code",
            file_path="nx/skills/foo.md",
        )

        count = generate_prose_filepath_links(cat)
        assert count == 1
        links = cat.links_from(prose_tumbler)
        assert len(links) == 1
        assert str(links[0].to_tumbler) == str(code_tumbler)

    def test_bare_filename_does_not_match(self, tmp_path: Path) -> None:
        """A prose mention of bare ``foo.py`` (no directory segment)
        must NOT match (too noisy). The regex requires at least one
        ``/`` to disambiguate against generic mentions.
        """
        repo = tmp_path / "myrepo"
        repo.mkdir()
        prose_file = repo / "docs" / "loose.md"
        prose_file.parent.mkdir(parents=True)
        prose_file.write_text("Run ``foo.py`` to start.\n")

        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo),
        )
        prose_tumbler = cat.register(
            owner, "Loose", content_type="prose",
            file_path="docs/loose.md",
        )
        cat.register(
            owner, "foo.py", content_type="code",
            file_path="foo.py",
        )

        count = generate_prose_filepath_links(cat)
        assert count == 0
        assert len(cat.links_from(prose_tumbler)) == 0

    def test_incremental_only_scans_new_prose(self, tmp_path: Path) -> None:
        """When ``new_tumblers`` lists only the new prose entry, only
        that one is scanned (mirrors RDR linker incremental contract).
        """
        repo = tmp_path / "myrepo"
        repo.mkdir()
        for name in ("a.md", "b.md"):
            f = repo / "docs" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(f"See ``src/nexus/{name.replace('.md', '.py')}``.\n")

        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner(
            "myrepo", "repo", repo_hash="abc12345", repo_root=str(repo),
        )
        # Register both prose docs + their code targets.
        prose_a = cat.register(
            owner, "a", content_type="prose", file_path="docs/a.md",
        )
        prose_b = cat.register(
            owner, "b", content_type="prose", file_path="docs/b.md",
        )
        cat.register(
            owner, "a.py", content_type="code", file_path="src/nexus/a.py",
        )
        cat.register(
            owner, "b.py", content_type="code", file_path="src/nexus/b.py",
        )

        count = generate_prose_filepath_links(cat, new_tumblers=[prose_a])
        assert count == 1
        # Only prose_a got linked.
        assert len(cat.links_from(prose_a)) == 1
        assert len(cat.links_from(prose_b)) == 0


# ── nexus-sob9: pdf-content-hash linker ───────────────────────────────────


class TestPdfCorpusLinks:
    """generate_pdf_corpus_links closes the pdf=0% catalog auto-link
    coverage gap by linking PDFs that share head_hash via ``same-as``.
    Group anchors are lexicographically-first tumbler in each hash
    group (stable across runs).
    """

    def _make_catalog(self, tmp_path: Path) -> Catalog:
        cat_dir = tmp_path / "catalog"
        cat_dir.mkdir()
        (cat_dir / "owners.jsonl").touch()
        (cat_dir / "documents.jsonl").touch()
        (cat_dir / "links.jsonl").touch()
        return Catalog(cat_dir, cat_dir / ".catalog.db")

    def test_two_pdfs_with_same_hash_get_linked(self, tmp_path: Path) -> None:
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        a = cat.register(
            owner, "Paper1", content_type="paper",
            head_hash="abc123", physical_collection="knowledge__delos",
        )
        b = cat.register(
            owner, "Paper2", content_type="paper",
            head_hash="abc123", physical_collection="knowledge__art-papers",
        )

        count = generate_pdf_corpus_links(cat)
        assert count == 1

        # Anchor is the lexicographically-first tumbler.
        anchor = a if str(a) < str(b) else b
        member = b if anchor == a else a
        links = cat.links_from(member)
        assert len(links) == 1
        assert str(links[0].to_tumbler) == str(anchor)
        assert links[0].link_type == "same-as"

    def test_no_link_when_hash_unique(self, tmp_path: Path) -> None:
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        a = cat.register(
            owner, "Unique", content_type="paper",
            head_hash="unique-hash", physical_collection="knowledge__delos",
        )
        count = generate_pdf_corpus_links(cat)
        assert count == 0
        assert len(cat.links_from(a)) == 0

    def test_idempotent(self, tmp_path: Path) -> None:
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(
            owner, "P1", content_type="paper",
            head_hash="h1", physical_collection="knowledge__delos",
        )
        cat.register(
            owner, "P2", content_type="paper",
            head_hash="h1", physical_collection="knowledge__art-papers",
        )
        first = generate_pdf_corpus_links(cat)
        second = generate_pdf_corpus_links(cat)
        assert first == 1
        # Re-running creates zero new links (link_if_absent).
        assert second == 0

    def test_pdfs_without_head_hash_skipped(self, tmp_path: Path) -> None:
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        cat.register(
            owner, "NoHash1", content_type="paper",
            head_hash="", physical_collection="knowledge__delos",
        )
        cat.register(
            owner, "NoHash2", content_type="paper",
            head_hash="", physical_collection="knowledge__art-papers",
        )
        count = generate_pdf_corpus_links(cat)
        assert count == 0

    def test_three_pdfs_one_anchor(self, tmp_path: Path) -> None:
        """Three PDFs sharing a hash: two ``same-as`` links emitted
        (member -> anchor), not three (no self-link, no pairwise
        explosion).
        """
        cat = self._make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        for i, coll in enumerate(("delos", "art-papers", "rag-papers"), start=1):
            cat.register(
                owner, f"P{i}", content_type="paper",
                head_hash="trio-hash",
                physical_collection=f"knowledge__{coll}",
            )
        count = generate_pdf_corpus_links(cat)
        assert count == 2
