# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the RDR-101 Phase 3 PR β event-sourced mutator paths.

Covers update / delete_document / set_alias / rename_collection under
NEXUS_EVENT_SOURCED=1. PR α already covered register_owner + register;
this PR β extends the gate to the remaining write methods (link /
unlink stay legacy until a follow-up that handles their merge
semantics in the projector).

CATALOG SUBSTRATE (nexus-i711w terminal deletion). Only the OUTCOME-half
ported tests remain here. The DIE cohort (TestFullReplayEqualsLive,
TestShadowEmitSuppressedAcrossMutators — subject WAS the local event log /
projector) and the 9 GAP-pinned local tests retired WITH
``nexus/catalog/{catalog,catalog_db,event_log,projector,events}.py``; the
GAP contracts (nexus-i711w.1 items 1-8) live on as service-side twins in
``tests/db/test_i711w_gap_contracts.py``.
"""

from __future__ import annotations

import uuid

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, unroutable_write_target


@pytest.fixture()
def active_catalog() -> ActiveCatalog:
    """Seed and read through whichever catalog is live (nexus-i711w Stage 2).

    Deliberately NOT the local ``Catalog`` these tests used to build: the
    subject of the tests that take this fixture is the mutator's OUTCOME
    (``update`` / ``delete_document`` / ``rename_collection``), which
    ``HttpCatalogClient`` implements, so exercising it against the live
    substrate is strictly more coverage than the local-only form.
    """
    return ActiveCatalog()


def _slug() -> str:
    """A per-test discriminator for owner names / paths / collections.

    The service catalog is shared within a tenant; two tests registering
    ``doc.md`` under owner ``nexus`` would collide on ``register``'s
    file_path idempotency and silently share a document. A distinct slug
    keeps each test's cardinality assertions its own.
    """
    return uuid.uuid4().hex[:8]


# ── update ───────────────────────────────────────────────────────────────


class TestUpdateEventSourced:
    def test_update_persists_chunk_count_and_head_hash(self, active_catalog):
        """``update`` lands both fields on the document row.

        nexus-i711w Stage 2: PORTED. Was
        ``test_update_emits_document_registered_via_event_log``, which
        asserted the ``events.jsonl`` line AND the resulting row. Only the
        second half has a service-mode observable; the event half retired
        with the local event log (nexus-i711w terminal deletion).
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-update-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        cat.update(tumbler, chunk_count=42, head_hash="updated")

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert (entry.chunk_count, entry.head_hash) == (42, "updated")

    def test_event_sourced_update_persists_bib_kwargs(self, active_catalog):
        """All 8 ``bib_*`` kwargs passed to ``update`` land on the row.

        nexus-i711w Stage 2: PORTED — the assertion is purely the verb's
        outcome (read back through ``resolve``), and the service arm carries
        every one of the 8 columns (``http_catalog_client._to_entry``,
        sourced from ``CatalogRepository.docRowFromRecord``).

        nexus-6ha8a (was nexus-9l2lg Task 5's deferred decision):
        ``DocumentRegisteredPayload`` now carries all 8 ``bib_*`` fields
        and the projector's ``_v0_document_registered`` writes them into
        its INSERT/ON CONFLICT SET clause. ``update()``'s event-sourced
        branch sources them from ``rec_dict``, which already carries
        bib_* forward from the current row (nexus-9l2lg Task 2) — so a
        caller passing ``bib_*`` kwargs under event-sourced mode now
        persists them, matching the non-event-sourced path.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-bib-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path=f"{slug}/doc.md", chunk_count=0,
        )
        # nexus-cefa1.2: bib_enriched_at is timestamptz now (catalog-031-1-documents-
        # temporal) — CatalogRepository.utcIso reads it back in the catalog's
        # micros+offset convention (INDEXED_AT_FMT, kept per Hal directive
        # 2026-08-15). This literal already carries microseconds so it round-trips
        # exactly; see CatalogRepository.utcIso's javadoc for the one shape
        # (no-fraction input) that would not.
        cat.update(
            tumbler, bib_year=2020, bib_authors="X", bib_venue="V",
            bib_citation_count=5, bib_semantic_scholar_id="ss1",
            bib_openalex_id="W1", bib_doi="10.1/x",
            bib_enriched_at="2026-01-01T00:00:00.000000+00:00",
        )
        entry = cat.resolve(tumbler)
        assert entry.bib_year == 2020
        assert entry.bib_authors == "X"
        assert entry.bib_venue == "V"
        assert entry.bib_citation_count == 5
        assert entry.bib_semantic_scholar_id == "ss1"
        assert entry.bib_openalex_id == "W1"
        assert entry.bib_doi == "10.1/x"
        assert entry.bib_enriched_at == "2026-01-01T00:00:00.000000+00:00"


# ── delete_document ──────────────────────────────────────────────────────


class TestDeleteDocumentEventSourced:
    def test_delete_removes_the_document(self, active_catalog):
        """``delete_document`` reports True and the document stops resolving.

        nexus-i711w Stage 2: PORTED. Was
        ``test_delete_emits_event_and_removes_row``; the DocumentDeleted
        event half retired with the local event log (nexus-i711w).

        ``resolve`` is the substrate-neutral form of the old
        ``SELECT count(*) ... WHERE tumbler = ?``: the service arm
        tombstones rather than DELETEs, and ``resolve`` returning None is
        what BOTH arms say about a deleted document.
        """
        cat = active_catalog
        slug = _slug()
        owner = cat.register_owner(f"es-delete-{slug}", "repo", repo_hash=slug)
        tumbler = cat.register(
            owner, "doc.md", content_type="prose",
            file_path=f"{slug}/doc.md",
        )
        assert cat.resolve(tumbler) is not None
        assert cat.delete_document(tumbler) is True
        assert cat.resolve(tumbler) is None


# ── set_alias ────────────────────────────────────────────────────────────


class TestSetAliasEventSourced:
    def test_set_alias_updates_alias_of(self):
        """``set_alias`` populates the alias row's ``alias_of``.

        nexus-i711w Stage 2: PORTED. Was
        ``test_set_alias_emits_event_and_updates_alias_of``; the
        DocumentAliased event half retired with the local event log
        (nexus-i711w).

        ``unroutable_write_target()`` rather than ``ActiveCatalog``:
        ``set_alias`` mutates but is NOT on ``CATALOG_WRITE_OPS``
        (nexus-iltyk), so a plain ``ActiveCatalog`` refuses to route it as a
        write on the SQLite arm and the assertion could not observe it.

        Read back by scanning ``all_documents`` for the ALIAS row rather
        than ``resolve(alias)``: ``resolve`` follows the alias by default
        and would hand back the CANONICAL entry, whose ``alias_of`` is ""
        — the assertion would be structurally unable to see the write it is
        about.
        """
        cat = unroutable_write_target()
        slug = _slug()
        owner = cat.register_owner(f"es-alias-{slug}", "repo", repo_hash=slug)
        canonical = cat.register(
            owner, "canonical.md", content_type="prose",
            file_path=f"{slug}/canonical.md",
        )
        alias = cat.register(
            owner, "alias.md", content_type="prose",
            file_path=f"{slug}/alias.md",
        )
        cat.set_alias(alias, canonical)

        rows = [
            d for d in cat.all_documents() if str(d.tumbler) == str(alias)
        ]
        assert len(rows) == 1, f"expected exactly one alias row, got {len(rows)}"
        assert rows[0].alias_of == str(canonical)


# ── rename_collection ────────────────────────────────────────────────────


class TestRenameCollectionEventSourced:
    def test_rename_moves_every_row_in_the_collection(self, active_catalog):
        """``rename_collection`` re-homes every document and reports the count.

        nexus-i711w Stage 2: PORTED. Was
        ``test_rename_emits_per_row_events_and_updates_sqlite``; the per-row
        DocumentRegistered emission half retired with the local event log
        (nexus-i711w).

        ``list_by_collection`` is the substrate-neutral form of the old
        ``SELECT count(*) ... WHERE physical_collection = ?``. Counting the
        rows in the NEW collection (not just trusting the returned int)
        keeps the assertion able to see a rename that reports 2 and moves 1.

        MEASURED PRECONDITION DIVERGENCE (found by attempting the port, not
        by reading it): the local ``rename_collection`` renames by scanning
        ``documents.physical_collection``, so a collection that exists only
        as a value on document rows renames fine. The service verb is
        ``POST /collections/rename`` and 404s ``collection not found`` unless
        the collection has a ``catalog_collections`` row. The explicit
        ``register_collection`` below is that precondition, not a workaround
        — and it is why this test had to be attempted rather than assumed.
        """
        cat = active_catalog
        slug = _slug()
        old, new = f"docs__old_{slug}", f"docs__new_{slug}"
        owner = cat.register_owner(f"es-rename-{slug}", "repo", repo_hash=slug)
        cat.register_collection(old, content_type="docs")
        cat.register(
            owner, "a.md", content_type="prose",
            file_path=f"{slug}/a.md",
            physical_collection=old,
        )
        cat.register(
            owner, "b.md", content_type="prose",
            file_path=f"{slug}/b.md",
            physical_collection=old,
        )

        n = cat.rename_collection(old, new)
        assert n == 2

        assert len(cat.list_by_collection(new)) == 2
        assert cat.list_by_collection(old) == []
