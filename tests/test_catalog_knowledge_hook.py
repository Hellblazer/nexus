# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, count_documents


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aim ``catalog_path()`` at the dir ``_make_catalog`` initialises.

    nexus-aqbrk: the tests used to hold a direct ``Catalog`` object bound to
    ``tmp_path/"catalog"``, so where ``catalog_path()`` pointed did not
    matter. ``ActiveCatalog`` resolves through the same factories the code
    under test uses, and on the SQLite arm those read ``catalog_path()`` —
    so it has to agree. Tests that deliberately point somewhere else (the
    not-initialised cases) still win: their in-body ``setenv`` runs after
    this autouse fixture.
    """
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _make_catalog(tmp_path: Path) -> tuple[Path, ActiveCatalog]:
    """Init the local catalog and hand back a facade over the LIVE one.

    nexus-aqbrk: this used to return the local ``Catalog`` object, so under
    the engine substrate the test seeded and read ``.catalog.db`` while the
    hook under test (``_catalog_store_hook`` -> ``make_catalog_writer``)
    wrote the SERVICE catalog. Every assertion then read an empty local file.
    ``ActiveCatalog`` routes both halves through the same factories the hook
    uses, so the same test body now covers whichever catalog is real.
    nexus-i711w terminal deletion: the local ``Catalog.init`` seeding is gone.
    """
    catalog_dir = tmp_path / "catalog"
    return catalog_dir, ActiveCatalog()


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
        # nexus-wji11/nexus-5axey (settled): by_doc_id is a TUMBLER-only
        # lookup, permanently — tumbler is the only document identity.
        # "abc123" is a meta.doc_id (chash-shaped) value, not a tumbler, so
        # this is the correct terminal behavior, not a pending gap. Chash
        # lookups go through
        # nexus.catalog.store_hook.resolve_knowledge_doc_for_chash instead
        # (see tests/test_5axey_chash_catalog_lookups.py).
        assert entry is None

    def test_not_found(self, tmp_path):
        catalog_dir, cat = _make_catalog(tmp_path)
        assert cat.by_doc_id("nonexistent") is None

    def test_multiple_entries_returns_first(self, tmp_path):
        catalog_dir, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("knowledge", "curator")
        cat.register(owner, "A", content_type="knowledge", meta={"doc_id": "id1"})
        cat.register(owner, "B", content_type="knowledge", meta={"doc_id": "id2"})
        entry = cat.by_doc_id("id1")
        # nexus-wji11/nexus-5axey (settled): permanent TUMBLER-only lookup.
        assert entry is None


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
        capped = len(cat.list_by_collection("knowledge__delos", limit=3))
        # FIXED by nexus-xoimv (2026-08-01): documentsByCollection now
        # honors (limit, offset); the inverted pin above this line fired
        # exactly as designed when the fix landed. limit is real now.
        assert capped == 3
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
        # nexus-wji11 (settled) / nexus-5axey: by_doc_id is a permanent
        # TUMBLER-only lookup, so an entry carrying meta.doc_id is
        # unreachable by that key — this is the correct terminal contract,
        # not an open question. The hook's OWN chash dedup goes through
        # nexus.catalog.store_hook.resolve_knowledge_doc_for_chash instead
        # (see tests/test_5axey_chash_catalog_lookups.py::TestDedupA1).
        assert entry is None
        # The registration itself must have landed on either substrate.
        assert any(
            e.title == "Test Knowledge"
            for e in cat.list_by_collection("knowledge__test")
        )

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
        rows = (count_documents(),)
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

        rows = (count_documents(),)
        assert rows[0] == 1, "no duplicate document was minted"

        entry = cat.resolve(ghost)
        assert entry.meta.get("doc_id") == "fresh-content-hash"
        assert entry.physical_collection == "knowledge__fresh"

    def test_legacy_ghost_reconcile_stamps_source_uri_for_next_reput(
        self, tmp_path, monkeypatch,
    ):
        """nexus-sdp0u fix-round (round-1 critique CRITICAL): a legacy ghost
        (source_uri="", chunk_count=0 — the pre-fix RDR-145 population) must
        get source_uri STAMPED when the ghost-by-title branch reconciles it,
        not just physical_collection/meta. Without the stamp, the manifest
        write that follows this call makes chunk_count > 0 and the row falls
        out of BOTH identity checks (by_source_uri misses on "", the ghost
        fallback requires chunk_count == 0) — so the VERY NEXT re-put would
        mint a fresh duplicate, reproducing this bead's own bug for the
        legacy population. This test pins both halves: the stamp on the
        first reconcile, and that a SECOND re-put (after simulated manifest
        population) reconciles via by_source_uri onto the SAME tumbler.
        """
        from nexus.aspect_readers import uri_for
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("knowledge", "curator")
        ghost = cat.register(
            owner, "Legacy Ghost", content_type="knowledge",
            physical_collection="knowledge__legacy",
            meta={"doc_id": "stale-legacy-doc-id"},
            # source_uri deliberately omitted (defaults to "") — this is
            # the shape of a pre-sdp0u-fix ghost row.
        )
        assert cat.resolve(ghost).source_uri == "", "fixture must be a legacy (pre-fix) ghost"
        assert cat.resolve(ghost).chunk_count == 0, "fixture must be a ghost"

        # First re-put: reconciles via the ghost-by-title fallback (the
        # source_uri lookup misses because the ghost's source_uri is "").
        first = _catalog_store_hook(
            title="Legacy Ghost", doc_id="first-content-hash",
            collection_name="knowledge__legacy",
        )
        assert first == str(ghost), "must reuse the ghost's tumbler"
        entry = cat.resolve(ghost)
        assert entry.source_uri == uri_for("knowledge__legacy", "Legacy Ghost"), (
            'ghost-reconcile branch must stamp source_uri, not leave it ""'
        )

        # Simulate the manifest population that follows every real re-put
        # (store_put_manifest_direct) — chunk_count becomes > 0, so the
        # ghost-by-title fallback is no longer reachable for this row.
        cat.append_manifest_chunks(
            first, [{"chash": "b" * 64, "position": 0}], collection="knowledge__legacy",
        )
        cat.resync_chunk_count_cache(first)
        assert cat.resolve(first).chunk_count == 1

        # Second re-put with DIFFERENT content: must reconcile via
        # by_source_uri onto the SAME tumbler — no new document minted.
        # This is the regression pin: pre-fix, this second call would have
        # minted a fresh duplicate because source_uri was never stamped by
        # the first (ghost-path) reconcile.
        second = _catalog_store_hook(
            title="Legacy Ghost", doc_id="second-content-hash",
            collection_name="knowledge__legacy",
        )
        assert second == first, (
            "second re-put must reconcile onto the SAME document via "
            "by_source_uri, not mint a duplicate"
        )
        rows = (count_documents(),)
        assert rows[0] == 1, "no duplicate document was minted on the second re-put"
        assert cat.resolve(first).meta.get("doc_id") == "second-content-hash"

    def test_legacy_non_ghost_reput_mints_one_bounded_duplicate_then_converges(
        self, tmp_path, monkeypatch,
    ):
        """nexus-sdp0u round-2 critique: the KNOWN RESIDUAL for legacy
        NON-ghost rows (chunk_count > 0, source_uri="" — populated before
        the fix). All three lookups miss on the first post-fix re-put
        (chash differs, by_source_uri misses on "", the ghost fallback
        requires chunk_count == 0), so it mints ONE bounded duplicate that
        carries the synthesized source_uri; the second-and-later re-puts
        converge onto that new document via by_source_uri. The legacy row
        itself is collapsed by the nexus-n90xg one-shot backfill, not by
        this hook. This test pins the bounded-then-converge lifecycle so
        the residual stays deliberate, not accidental.
        """
        from nexus.aspect_readers import uri_for
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("knowledge", "curator")
        legacy = cat.register(
            owner, "Legacy Note", content_type="knowledge",
            physical_collection="knowledge__legacy",
            meta={"doc_id": "legacy-content-hash"},
            # source_uri deliberately omitted — pre-sdp0u vintage.
        )
        cat.append_manifest_chunks(
            str(legacy), [{"chash": "c" * 64, "position": 0}], collection="knowledge__legacy",
        )
        cat.resync_chunk_count_cache(str(legacy))
        assert cat.resolve(legacy).source_uri == ""
        assert cat.resolve(legacy).chunk_count == 1, "fixture must be a populated (non-ghost) legacy row"

        # First post-fix re-put: every lookup misses -> ONE bounded duplicate.
        first = _catalog_store_hook(
            title="Legacy Note", doc_id="new-content-hash",
            collection_name="knowledge__legacy",
        )
        assert first != str(legacy), "legacy non-ghost row is not reachable; a new doc is minted"
        assert count_documents() == 2, "exactly one bounded duplicate"
        assert cat.resolve(first).source_uri == uri_for("knowledge__legacy", "Legacy Note"), (
            "the minted duplicate must carry the synthesized identity"
        )

        # Second re-put: converges onto the minted doc via by_source_uri.
        second = _catalog_store_hook(
            title="Legacy Note", doc_id="third-content-hash",
            collection_name="knowledge__legacy",
        )
        assert second == first, "second re-put converges via by_source_uri"
        assert count_documents() == 2, "no further duplicates after convergence"

    def test_non_ghost_same_title_same_collection_reconciled_via_source_uri(
        self, tmp_path, monkeypatch,
    ):
        """nexus-sdp0u: a re-put of the SAME (collection, title) identity —
        even with populated chunks (chunk_count > 0) — reconciles onto the
        existing document via the synthesized source_uri instead of
        minting a sibling. Supersedes the pre-fix contract: three puts of
        one title used to mint three documents with contradictory content
        (production: 1.1.1/1.1.2/1.1.3) because source_uri was always ""
        and the engine's upsert-on-source_uri identity never matched."""
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        first = _catalog_store_hook(
            title="Real Doc", doc_id="original-content-hash",
            collection_name="knowledge__real",
        )
        cat.append_manifest_chunks(first, [
            {"chash": "a" * 64, "position": 0},
        ], collection="knowledge__real")
        cat.resync_chunk_count_cache(first)
        assert cat.resolve(first).chunk_count == 1, "fixture must not be a ghost"

        result = _catalog_store_hook(
            title="Real Doc", doc_id="different-content-hash",
            collection_name="knowledge__real",
        )
        assert result == first, "re-put of the same identity reuses the tumbler"

        rows = (count_documents(),)
        assert rows[0] == 1, "no sibling document is minted"

        entry = cat.resolve(first)
        assert entry.meta.get("doc_id") == "different-content-hash"
        assert entry.physical_collection == "knowledge__real"

    def test_cross_collection_same_title_stays_distinct(self, tmp_path, monkeypatch):
        """A same-titled document in a DIFFERENT collection is a distinct
        identity (the synthesized source_uri is collection-scoped) — it
        must mint its own document, not reconcile onto the first."""
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        first = _catalog_store_hook(
            title="Shared Title", doc_id="hash-a",
            collection_name="knowledge__alpha",
        )
        # Populate it (chunk_count > 0) so the ghost-by-title fallback
        # (title-only, no collection scoping) cannot itself reconcile the
        # second call onto this row — isolating the source_uri behavior
        # under test.
        cat.append_manifest_chunks(
            first, [{"chash": "a" * 64, "position": 0}], collection="knowledge__alpha",
        )
        cat.resync_chunk_count_cache(first)

        second = _catalog_store_hook(
            title="Shared Title", doc_id="hash-b",
            collection_name="knowledge__beta",
        )
        assert second != first, "same title in a different collection is a distinct document"

        rows = (count_documents(),)
        assert rows[0] == 2

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
        rows = (count_documents(),)
        assert rows[0] == 2, "empty title must not trigger reconciliation"
        assert result, "a new tumbler is still registered"
        # nexus-sdp0u: empty title synthesizes NO source_uri — a title-less
        # identity would collapse every untitled document onto one row.
        assert cat.resolve(result).source_uri == ""

    def test_source_uri_synthesis_matches_aspect_readers_uri_for(
        self, tmp_path, monkeypatch,
    ):
        """The identity written to the catalog row is EXACTLY
        ``aspect_readers.uri_for``'s convention — one URI format, never a
        second one forked here."""
        from nexus.aspect_readers import uri_for
        from nexus.commands.store import _catalog_store_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        result = _catalog_store_hook(
            title="URI Format Check", doc_id="hash-format",
            collection_name="knowledge__format",
        )
        entry = cat.resolve(result)
        assert entry.source_uri == uri_for("knowledge__format", "URI Format Check")
        assert entry.source_uri == "chroma://knowledge__format/URI Format Check"

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
        rows = (count_documents(),)
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
        # nexus-sdp0u reconcile lookup also misses, so the hook falls
        # through to the ghost-reconciliation lookup next.
        reader.by_source_uri.return_value = None
        # Ghost-reconciliation lookup (GH #1370 Defect 4a) also misses,
        # so the hook falls through to writer.register as before.
        reader.find.return_value = []

        writer = MagicMock()
        writer.register_owner.return_value = Tumbler.parse("1.1")
        # nexus-vfef0: the hook now calls writer.register(..., with_created=True)
        # and unpacks (tumbler, created).
        writer.register.return_value = (Tumbler.parse("1.1.1"), True)

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
    """nexus-9l2lg / nexus-6ha8a: bib_* persists under the ambient
    default (event-sourced ON, RDR-101 Phase 3 PR ζ) as well as the
    legacy non-event-sourced path — nexus-6ha8a extended the event-sourced
    projector to carry bib_* forward across all 4
    DocumentRegisteredPayload emission sites. No env pin needed here; see
    test_catalog_bib_columns.py for the dedicated legacy-path (=0) parity
    suite.
    """

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

    def test_updates_catalog_bib_columns_s2_backend(self, tmp_path, monkeypatch):
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "S2 Paper", content_type="paper")

        _catalog_enrich_hook(
            title="S2 Paper",
            bib_meta={
                "authors": "Alice, Bob",
                "year": 2024,
                "venue": "SIGMOD",
                "citation_count": 42,
                "semantic_scholar_id": "ss123",
            },
            backend="s2",
        )
        entries = cat.find("S2 Paper")
        assert len(entries) >= 1
        entry = cat.resolve(entries[0].tumbler)
        assert entry.bib_year == 2024
        assert entry.bib_venue == "SIGMOD"
        assert entry.bib_authors == "Alice, Bob"
        assert entry.bib_citation_count == 42
        assert entry.bib_semantic_scholar_id == "ss123"
        assert entry.bib_enriched_at != ""

    def test_updates_catalog_bib_columns_openalex_backend(self, tmp_path, monkeypatch):
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "OpenAlex Paper", content_type="paper")

        _catalog_enrich_hook(
            title="OpenAlex Paper",
            bib_meta={
                "authors": "Carol",
                "year": 2023,
                "venue": "ICML",
                "citation_count": 7,
                "openalex_id": "W999",
                "doi": "10.1234/foo",
            },
            backend="openalex",
        )
        entries = cat.find("OpenAlex Paper")
        assert len(entries) >= 1
        entry = cat.resolve(entries[0].tumbler)
        assert entry.bib_openalex_id == "W999"
        assert entry.bib_doi == "10.1234/foo"
        assert entry.bib_semantic_scholar_id == ""

    def test_meta_no_longer_carries_venue_or_citation_count(
        self, tmp_path, monkeypatch,
    ):
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "S2 Meta Paper", content_type="paper")
        cat.register(owner, "OA Meta Paper", content_type="paper")

        _catalog_enrich_hook(
            title="S2 Meta Paper",
            bib_meta={
                "authors": "A", "year": 2020, "venue": "V",
                "citation_count": 1, "semantic_scholar_id": "ss1",
            },
            backend="s2",
        )
        _catalog_enrich_hook(
            title="OA Meta Paper",
            bib_meta={
                "authors": "B", "year": 2021, "venue": "W",
                "citation_count": 2, "openalex_id": "W1", "doi": "10.1/y",
            },
            backend="openalex",
        )

        s2_entry = cat.resolve(cat.find("S2 Meta Paper")[0].tumbler)
        oa_entry = cat.resolve(cat.find("OA Meta Paper")[0].tumbler)
        for entry in (s2_entry, oa_entry):
            assert "venue" not in entry.meta
            assert "citation_count" not in entry.meta
            assert "bib_semantic_scholar_id" not in entry.meta
            assert "bib_openalex_id" not in entry.meta
            assert "bib_doi" not in entry.meta

    def test_catalog_search_surfaces_bib_year_and_citation_count_after_enrich(
        self, tmp_path, monkeypatch,
    ):
        from nexus.commands.enrich import _catalog_enrich_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        owner = cat.register_owner("papers", "curator")
        cat.register(owner, "Searchable Enriched Paper", content_type="paper")

        _catalog_enrich_hook(
            title="Searchable Enriched Paper",
            bib_meta={
                "authors": "Dana", "year": 2019, "venue": "OSDI",
                "citation_count": 314, "semantic_scholar_id": "ss42",
            },
            backend="s2",
        )

        results = cat.find("Searchable Enriched Paper")
        assert len(results) >= 1
        assert results[0].bib_year == 2019
        assert results[0].bib_citation_count == 314


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
        assert entry_a.bib_openalex_id == "WAAA"
        assert entry_a.meta.get("references") == ["WX", "WY"]

        # Paper B is untouched (was the bug: the LIMIT-1 fallback
        # clobbered the first-by-tumbler row regardless of identity).
        entry_b = cat.by_file_path(owner, "papers/B.pdf")
        assert entry_b is not None
        assert entry_b.year == 0
        assert entry_b.author == ""
        assert entry_b.bib_openalex_id == ""

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
        assert a.year == 2021 and a.bib_openalex_id == "WBOTH"
        assert b.year == 2021 and b.bib_openalex_id == "WBOTH"

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
        assert entry.bib_openalex_id == ""

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
