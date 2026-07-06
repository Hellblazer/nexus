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


class TestByDocId:
    def test_lookup(self, tmp_path):
        catalog_dir, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "Test Entry",
            content_type="knowledge",
            physical_collection="knowledge__test",
            meta={"doc_id": "abc123"},
        )
        entry = cat.by_doc_id("abc123")
        assert entry is not None
        assert entry.title == "Test Entry"

    def test_not_found(self, tmp_path):
        catalog_dir, cat = _make_catalog(tmp_path)
        assert cat.by_doc_id("nonexistent") is None

    def test_multiple_entries_returns_first(self, tmp_path):
        catalog_dir, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        cat.register(owner, "A", content_type="knowledge", meta={"doc_id": "id1"})
        cat.register(owner, "B", content_type="knowledge", meta={"doc_id": "id2"})
        entry = cat.by_doc_id("id1")
        assert entry.title == "A"


class TestListByCollection:
    """RDR-089 P2.2: ``Catalog.list_by_collection`` returns one entry
    per source document (NOT per chunk) for a given physical
    collection. Used by ``nx enrich aspects`` to drive per-document
    iteration.
    """

    def test_returns_entries_for_collection(self, tmp_path: Path) -> None:
        _, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        cat.register(owner, "Paper A",
                     content_type="paper",
                     physical_collection="knowledge__delos",
                     file_path="/papers/a.pdf")
        cat.register(owner, "Paper B",
                     content_type="paper",
                     physical_collection="knowledge__delos",
                     file_path="/papers/b.pdf")
        cat.register(owner, "Paper C",
                     content_type="paper",
                     physical_collection="knowledge__other",
                     file_path="/papers/c.pdf")

        rows = cat.list_by_collection("knowledge__delos")
        titles = sorted(r.title for r in rows)
        assert titles == ["Paper A", "Paper B"]

    def test_returns_empty_list_for_missing_collection(
        self, tmp_path: Path,
    ) -> None:
        _, cat = _make_catalog(tmp_path)
        assert cat.list_by_collection("knowledge__nonexistent") == []

    def test_limit_caps_result(self, tmp_path: Path) -> None:
        _, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        for i in range(5):
            cat.register(
                owner, f"Paper {i}",
                content_type="paper",
                physical_collection="knowledge__delos",
                file_path=f"/papers/p{i}.pdf",
            )
        assert len(cat.list_by_collection("knowledge__delos", limit=3)) == 3
        assert len(cat.list_by_collection("knowledge__delos", limit=None)) == 5


class TestStorePutHook:
    def test_registers_knowledge_entry(self, tmp_path, monkeypatch):
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_store_hook(
            title="Test Knowledge",
            doc_id="doc_abc123",
            collection_name="knowledge__test",
        )
        entry = cat.by_doc_id("doc_abc123")
        assert entry is not None
        assert entry.title == "Test Knowledge"
        assert entry.physical_collection == "knowledge__test"

    def test_skipped_when_not_initialized(self, tmp_path, monkeypatch):
        from nexus.commands.store import _catalog_store_hook

        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        # Should not raise
        _catalog_store_hook(
            title="Test",
            doc_id="doc_abc",
            collection_name="knowledge__test",
        )

    def test_idempotent_by_doc_id(self, tmp_path, monkeypatch):
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_store_hook(title="A", doc_id="doc1", collection_name="knowledge__test")
        _catalog_store_hook(title="A", doc_id="doc1", collection_name="knowledge__test")
        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 1

    def test_ghost_reconciled_by_title_instead_of_duplicated(
        self, tmp_path, monkeypatch,
    ):
        """GH #1370 Defect 4a: a pre-existing GHOST entry (chunk_count=0,
        e.g. from a pre-migration catalog or an earlier failed index)
        sharing the new doc's title must be reused, not duplicated.
        """
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("knowledge", "curator")
        ghost = cat.register(
            owner, "Ghost Doc", content_type="knowledge",
            physical_collection="knowledge__stale",
            meta={"doc_id": "stale-legacy-doc-id"},
        )
        assert cat.resolve(ghost).chunk_count == 0, "fixture must be a ghost"

        result = _catalog_store_hook(
            title="Ghost Doc", doc_id="fresh-content-hash",
            collection_name="knowledge__fresh",
        )
        assert result == str(ghost), "must reuse the ghost's tumbler"

        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 1, "no duplicate document was minted"

        entry = cat.resolve(ghost)
        assert entry.meta.get("doc_id") == "fresh-content-hash"
        assert entry.physical_collection == "knowledge__fresh"

    def test_non_ghost_same_title_not_reconciled(self, tmp_path, monkeypatch):
        """A same-titled entry that already HAS chunks (chunk_count > 0)
        must not be repointed at unrelated new content — that would
        orphan its existing document_chunks manifest. Falls through to
        register() exactly as pre-fix, minting a second entry."""
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("knowledge", "curator")
        real = cat.register(
            owner, "Real Doc", content_type="knowledge",
            physical_collection="knowledge__real",
            meta={"doc_id": "real-doc-id"},
        )
        cat.append_manifest_chunks(str(real), [
            {"chash": "a" * 64, "position": 0},
        ])
        cat.resync_chunk_count_cache(str(real))
        assert cat.resolve(real).chunk_count == 1, "fixture must not be a ghost"

        result = _catalog_store_hook(
            title="Real Doc", doc_id="different-content-hash",
            collection_name="knowledge__new",
        )
        assert result != str(real), "must not reuse a populated document's tumbler"

        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 2, "a new document is registered instead"

        untouched = cat.resolve(real)
        assert untouched.meta.get("doc_id") == "real-doc-id", "original entry untouched"
        assert untouched.physical_collection == "knowledge__real"

    def test_empty_title_does_not_reconcile(self, tmp_path, monkeypatch):
        """An empty title must never dedup against arbitrary same-("")-titled
        ghosts — always registers a new document."""
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("knowledge", "curator")
        cat.register(
            owner, "", content_type="knowledge",
            physical_collection="knowledge__blank",
        )

        result = _catalog_store_hook(
            title="", doc_id="some-hash", collection_name="knowledge__blank2",
        )
        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 2, "empty title must not trigger reconciliation"
        assert result, "a new tumbler is still registered"

    def test_ghost_reconciliation_scoped_to_knowledge_owner(
        self, tmp_path, monkeypatch,
    ):
        """A same-titled ghost under a DIFFERENT owner (e.g. a repo owner)
        must not be reconciled — only knowledge-curator-owned ghosts are
        eligible."""
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        other_owner = cat.register_owner(
            "otherproject", "repo", repo_hash="deadbeef",
        )
        other_ghost = cat.register(
            other_owner, "Shared Title", content_type="knowledge",
            physical_collection="knowledge__other",
        )
        assert cat.resolve(other_ghost).chunk_count == 0

        result = _catalog_store_hook(
            title="Shared Title", doc_id="new-hash",
            collection_name="knowledge__mine",
        )
        assert result != str(other_ghost), (
            "must not reconcile onto a ghost owned by a different owner"
        )
        rows = cat._db.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 2

    def test_writes_route_through_factory_writer_not_direct_catalog(
        self, tmp_path, monkeypatch
    ):
        """RDR-146 P1.2 regression (test-validator GAP-2): the hook fires on
        every store_put / memory promote in the long-lived MCP server, so it
        must NOT open a direct .catalog.db writer (the two-writer hazard).
        It lives under catalog/ so the boundary lint cannot catch a bare
        Catalog() reversion; this test is the lock. Writes must route through
        make_catalog_writer; reads through make_catalog_reader; the writer
        handle must be closed.
        """
        from unittest.mock import MagicMock

        from nexus.catalog.tumbler import Tumbler
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, _cat = _make_catalog(tmp_path)  # real init -> is_initialized True
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # Reader: dedup miss + no existing curator owner -> the hook takes the
        # register_owner + register write path. The owner lookup goes through
        # the protocol method (curator_owner_tumbler_by_name — the raw
        # reader._db SQL was removed because it silently no-op'd the whole
        # hook in service mode, GH #1370 review finding); a bare MagicMock
        # would auto-vivify it truthy and skip register_owner, so pin None.
        reader = MagicMock()
        reader.by_doc_id.return_value = None
        reader.curator_owner_tumbler_by_name.return_value = None
        # Ghost-reconciliation lookup (GH #1370 Defect 4a) also misses,
        # so the hook falls through to writer.register as before.
        reader.find.return_value = []

        writer = MagicMock()
        writer.register_owner.return_value = Tumbler.parse("1.1")
        writer.register.return_value = Tumbler.parse("1.1.1")

        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_reader", lambda *a, **k: reader
        )
        monkeypatch.setattr(
            "nexus.catalog.factory.make_catalog_writer", lambda *a, **k: writer
        )

        result = _catalog_store_hook(
            title="T", doc_id="d1", collection_name="knowledge__test"
        )

        # Writes went through the factory writer, NOT a bare Catalog. A
        # reversion to `cat = Catalog(...)` would leave these mocks uncalled.
        writer.register_owner.assert_called_once_with("knowledge", "curator")
        writer.register.assert_called_once()
        assert result == "1.1.1"
        writer.close.assert_called_once()  # hot-path handle closed in finally


class TestEnrichHook:
    def test_updates_catalog_metadata(self, tmp_path, monkeypatch):
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # Pre-register a paper
        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Attention Is All You Need", content_type="paper")

        _catalog_enrich_hook(
            title="Attention Is All You Need",
            bib_meta={
                "authors": "Vaswani et al.",
                "year": 2017,
                "venue": "NeurIPS",
                "semantic_scholar_id": "ss123",
                "citation_count": 50000,
            },
        )
        entries = cat.find("Attention")
        assert len(entries) >= 1
        entry = cat.resolve(entries[0].tumbler)
        assert entry.author == "Vaswani et al."
        assert entry.year == 2017

    def test_skipped_when_not_initialized(self, tmp_path, monkeypatch):
        from nexus.commands.enrich import _catalog_enrich_hook

        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        _catalog_enrich_hook(title="Test", bib_meta={})


class TestEnrichHookSourcePathMatching:
    """nexus-tv22: when chunk title and catalog title diverge (e.g. after
    a migration that rewrites chunk titles via derive_title while
    catalog rows retain their original placeholder titles), the hook
    must NOT fall back to a LIMIT-1 collection-only match. That
    fallback caused all 75 ART enrich calls to silently clobber the
    same first row instead of finding the right one per paper.

    Fix: caller passes ``source_paths`` (the unique identity of each
    document on disk) and the hook matches by ``file_path``. The
    chunk metadata always carries source_path; the catalog row's
    ``file_path`` mirrors it (anchored relative to repo_root by the
    nexus-3e4s register-time guard).
    """

    def test_source_path_match_picks_right_row_when_titles_differ(
        self, tmp_path, monkeypatch,
    ):
        """Two papers in same collection. Titles in catalog don't
        match the title used for bib lookup. Hook should still update
        only the matching row, by source_path."""
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner(
            "myproject", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myproject",
        )
        # Two papers, both with placeholder-shaped catalog titles
        # (the post-migration drift state). The bib lookup will use
        # the derive_title-shaped name which doesn't match either.
        cat.register(
            owner, "papers/A.pdf:page-1",
            content_type="paper",
            physical_collection="knowledge__myproject-papers",
            file_path="papers/A.pdf",
        )
        cat.register(
            owner, "papers/B.pdf:page-1",
            content_type="paper",
            physical_collection="knowledge__myproject-papers",
            file_path="papers/B.pdf",
        )

        # Enrich paper A only.
        _catalog_enrich_hook(
            title="A Real Paper Title",  # placeholder-divergent
            bib_meta={
                "authors": "Author A",
                "year": 2020,
                "venue": "Venue A",
                "openalex_id": "WAAA",
                "references": ["WX", "WY"],
            },
            collection_name="knowledge__myproject-papers",
            backend="openalex",
            source_paths=["/tmp/myproject/papers/A.pdf"],
        )

        # Paper A has the new metadata.
        entry_a = cat.by_file_path(owner, "papers/A.pdf")
        assert entry_a is not None
        assert entry_a.year == 2020
        assert entry_a.author == "Author A"
        assert entry_a.meta.get("bib_openalex_id") == "WAAA"
        assert entry_a.meta.get("references") == ["WX", "WY"]

        # Paper B is untouched (was the bug: the LIMIT-1 fallback
        # clobbered the first-by-tumbler row regardless of identity).
        entry_b = cat.by_file_path(owner, "papers/B.pdf")
        assert entry_b is not None
        assert entry_b.year == 0
        assert entry_b.author == ""
        assert entry_b.meta.get("bib_openalex_id", "") == ""

    def test_source_paths_fan_out_across_multiple_rows(
        self, tmp_path, monkeypatch,
    ):
        """One title group may map to multiple source_paths (rare but
        legal — duplicate titles across files). Hook updates each."""
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner(
            "myproject", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myproject",
        )
        cat.register(
            owner, "Same Title",
            content_type="paper",
            physical_collection="knowledge__myproject-papers",
            file_path="papers/A.pdf",
        )
        cat.register(
            owner, "Same Title",
            content_type="paper",
            physical_collection="knowledge__myproject-papers",
            file_path="papers/B.pdf",
        )

        _catalog_enrich_hook(
            title="Same Title",
            bib_meta={"year": 2021, "openalex_id": "WBOTH"},
            collection_name="knowledge__myproject-papers",
            backend="openalex",
            source_paths=[
                "/tmp/myproject/papers/A.pdf",
                "/tmp/myproject/papers/B.pdf",
            ],
        )

        a = cat.by_file_path(owner, "papers/A.pdf")
        b = cat.by_file_path(owner, "papers/B.pdf")
        assert a.year == 2021 and a.meta.get("bib_openalex_id") == "WBOTH"
        assert b.year == 2021 and b.meta.get("bib_openalex_id") == "WBOTH"

    def test_no_source_paths_no_silent_clobber(self, tmp_path, monkeypatch):
        """Caller passes no source_paths and the title doesn't match
        any catalog row. Hook must NOT clobber an arbitrary row.
        Old behavior: LIMIT-1 fallback updated whichever row had the
        smallest tumbler. New behavior: silent no-op."""
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner(
            "myproject", "repo", repo_hash="abcd1234",
            repo_root="/tmp/myproject",
        )
        cat.register(
            owner, "X.pdf:page-1", content_type="paper",
            physical_collection="knowledge__myproject-papers",
            file_path="papers/X.pdf",
        )

        _catalog_enrich_hook(
            title="Not Matching Anything",
            bib_meta={"year": 2099, "openalex_id": "WBOGUS"},
            collection_name="knowledge__myproject-papers",
            backend="openalex",
            source_paths=[],
        )

        entry = cat.by_file_path(owner, "papers/X.pdf")
        assert entry.year == 0  # untouched
        assert entry.meta.get("bib_openalex_id", "") == ""

    def test_references_list_propagates_with_openalex(
        self, tmp_path, monkeypatch,
    ):
        """The OpenAlex backend returns a ``references`` list
        (W-id strings); the hook must persist it on the catalog row
        so generate_citation_links can build cites edges. Pre-fix,
        references were dropped on the hook's collection-only fallback
        because that fallback used the wrong row."""
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner(
            "p", "repo", repo_hash="abcd1234", repo_root="/tmp/p",
        )
        cat.register(
            owner, "p.pdf:page-1", content_type="paper",
            physical_collection="knowledge__p-papers",
            file_path="papers/p.pdf",
        )

        _catalog_enrich_hook(
            title="Real Title",
            bib_meta={
                "year": 2020, "openalex_id": "W1",
                "references": ["WA", "WB", "WC"],
            },
            collection_name="knowledge__p-papers",
            backend="openalex",
            source_paths=["/tmp/p/papers/p.pdf"],
        )

        entry = cat.by_file_path(owner, "papers/p.pdf")
        assert entry.meta.get("references") == ["WA", "WB", "WC"]
