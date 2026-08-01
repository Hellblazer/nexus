# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus-9l2lg: surface bib metadata on catalog Document rows.

nexus-i711w terminal deletion: the six local-substrate tests — the
``resolve``/``descendants`` reader surfacing over a raw-UPDATE bootstrap,
the non-event-sourced ``INSERT ... ON CONFLICT`` write path, the JSONL
append/replay filter, and the legacy-schema on-open ALTER — retired WITH
``nexus.catalog.catalog``. Their surviving contracts are pinned
service-side: all-8-column ``update()`` round-trip in
tests/test_catalog_event_sourced_mutators.py
(``test_event_sourced_update_persists_bib_kwargs``), the omit-then-
preserve carry-forward in tests/db/test_i711w_gap_contracts.py (item 4),
and enrich write-back parity in
tests/test_bib_enricher_write_back_service_mode.py. The PG schema is
Liquibase-managed, so the on-open-ALTER migration half has no service
analogue at all. What remains here is the substrate-neutral
``CatalogEntry`` dataclass shape.
"""
from __future__ import annotations


_ALL_EIGHT = {
    "bib_year": 2020,
    "bib_authors": "A. Author",
    "bib_venue": "Some Venue",
    "bib_citation_count": 5,
    "bib_semantic_scholar_id": "ss1",
    "bib_openalex_id": "W1",
    "bib_doi": "10.1/x",
    "bib_enriched_at": "2026-01-01T00:00:00Z",
}


class TestCatalogEntryDataclass:
    def test_has_all_eight_bib_fields(self) -> None:
        from nexus.catalog.types import CatalogEntry
        from nexus.catalog.tumbler import Tumbler

        entry = CatalogEntry(
            tumbler=Tumbler.parse("1.1.1"),
            title="t", author="", year=0, content_type="", file_path="",
            corpus="", physical_collection="", chunk_count=0, head_hash="",
            indexed_at="",
            **_ALL_EIGHT,
        )
        assert entry.bib_year == 2020
        assert entry.bib_authors == "A. Author"
        assert entry.bib_venue == "Some Venue"
        assert entry.bib_citation_count == 5
        assert entry.bib_semantic_scholar_id == "ss1"
        assert entry.bib_openalex_id == "W1"
        assert entry.bib_doi == "10.1/x"
        assert entry.bib_enriched_at == "2026-01-01T00:00:00Z"

        d = entry.to_dict()
        for key, value in _ALL_EIGHT.items():
            assert d[key] == value
