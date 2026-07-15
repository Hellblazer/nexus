# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for nexus-9l2lg: surface bib metadata on catalog Document rows.

``CatalogEntry`` (nexus-rzqto) and the three local readers (``resolve``,
``descendants``, ``HttpCatalogClient._to_entry``) originally carried only
4 of the 8 ``bib_*`` columns the engine already persists+returns
(``bib_year``/``bib_authors``/``bib_venue``/``bib_citation_count``).
Missing: ``bib_semantic_scholar_id``/``bib_openalex_id``/``bib_doi``/
``bib_enriched_at``. ``_WriteOps.update()`` additionally omitted ALL 8
``bib_*`` columns from its non-event-sourced ``INSERT ... ON CONFLICT``
write path entirely (protection-by-omission against a never-built
"BibliographicEnriched event handler"). This file pins the fix: all 8
columns round-trip through ``CatalogEntry``, ``resolve()``,
``descendants()``, and ``update()`` — with carry-through (not omission)
as the clobber-protection mechanism.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nexus.catalog.catalog import Catalog, CatalogEntry
from nexus.catalog.tumbler import read_documents


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


class TestResolveSurfacesAllEightBibColumns:
    def test_local_resolve_surfaces_all_eight_bib_columns(
        self, tmp_path: Path,
    ) -> None:
        _, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        tumbler = cat.register(owner, "Bootstrapped Paper", content_type="paper")

        cat._db.execute(  # epsilon-allow: bootstrap bib_* columns directly to test the reader in isolation from the writer under test
            "UPDATE documents SET bib_year=?, bib_authors=?, bib_venue=?, "
            "bib_citation_count=?, bib_semantic_scholar_id=?, "
            "bib_openalex_id=?, bib_doi=?, bib_enriched_at=? WHERE tumbler=?",
            (
                _ALL_EIGHT["bib_year"], _ALL_EIGHT["bib_authors"],
                _ALL_EIGHT["bib_venue"], _ALL_EIGHT["bib_citation_count"],
                _ALL_EIGHT["bib_semantic_scholar_id"],
                _ALL_EIGHT["bib_openalex_id"], _ALL_EIGHT["bib_doi"],
                _ALL_EIGHT["bib_enriched_at"], str(tumbler),
            ),
        )
        cat._db.commit()

        entry = cat.resolve(tumbler)
        assert entry is not None
        for key, value in _ALL_EIGHT.items():
            assert getattr(entry, key) == value, key


class TestDescendantsSurfacesAllEightBibColumns:
    def test_descendants_surfaces_all_eight_bib_columns(
        self, tmp_path: Path,
    ) -> None:
        _, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        tumbler = cat.register(owner, "Child Paper", content_type="paper")

        cat._db.execute(  # epsilon-allow: bootstrap bib_* columns directly to test the reader in isolation from the writer under test
            "UPDATE documents SET bib_year=?, bib_authors=?, bib_venue=?, "
            "bib_citation_count=?, bib_semantic_scholar_id=?, "
            "bib_openalex_id=?, bib_doi=?, bib_enriched_at=? WHERE tumbler=?",
            (
                _ALL_EIGHT["bib_year"], _ALL_EIGHT["bib_authors"],
                _ALL_EIGHT["bib_venue"], _ALL_EIGHT["bib_citation_count"],
                _ALL_EIGHT["bib_semantic_scholar_id"],
                _ALL_EIGHT["bib_openalex_id"], _ALL_EIGHT["bib_doi"],
                _ALL_EIGHT["bib_enriched_at"], str(tumbler),
            ),
        )
        cat._db.commit()

        prefix = str(owner)
        rows = cat._docs.descendants(prefix)
        matches = [r for r in rows if r["tumbler"] == str(tumbler)]
        assert len(matches) == 1
        row = matches[0]
        for key, value in _ALL_EIGHT.items():
            assert row[key] == value, key


class TestUpdatePreservesAndSetsBibColumns:
    """nexus-9l2lg Task 2's target: the non-event-sourced ``INSERT ... ON
    CONFLICT`` write path. nexus-6ha8a extended the event-sourced
    projector path to ALSO persist bib_* (see
    test_catalog_event_sourced_mutators.py for that coverage) — this
    class stays pinned to ``NEXUS_EVENT_SOURCED=0`` deliberately, as the
    dedicated legacy-path parity suite proving the non-event-sourced
    write path (still the default for fresh SQLite schemas opened
    directly, and the path most local single-writer installs exercise)
    independently persists bib_* correctly. ``_read_event_sourced_gate()``
    defaults ON (RDR-101 Phase 3 PR ζ) — leaving this ambient would
    silently switch which branch this suite exercises.
    """

    def test_update_without_bib_kwargs_preserves_existing_bib_columns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        _, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        tumbler = cat.register(owner, "Carried Paper", content_type="paper")

        cat.update(tumbler, **_ALL_EIGHT)
        # A subsequent update that does NOT touch bib_* must not clobber it —
        # the regression this codebase originally (over-)protected against
        # by omitting bib_* from the write path entirely.
        cat.update(tumbler, chunk_count=9)

        entry = cat.resolve(tumbler)
        assert entry is not None
        assert entry.chunk_count == 9
        for key, value in _ALL_EIGHT.items():
            assert getattr(entry, key) == value, key

    def test_update_with_bib_kwargs_sets_all_eight_columns_exactly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        _, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        tumbler = cat.register(owner, "Single Update Paper", content_type="paper")

        cat.update(tumbler, **_ALL_EIGHT)

        entry = cat.resolve(tumbler)
        assert entry is not None
        for key, value in _ALL_EIGHT.items():
            assert getattr(entry, key) == value, key

    def test_update_bib_fields_survive_jsonl_append_and_replay_filter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        _, cat = _make_catalog(tmp_path)
        owner = cat.register_owner("papers", "curator")
        tumbler = cat.register(owner, "JSONL Paper", content_type="paper")

        cat.update(tumbler, **_ALL_EIGHT)

        # DocumentRecord has no bib_* fields; _filter_fields silently drops
        # unknown keys on reconstruction. This must not raise even though
        # rec_dict (and therefore the appended JSONL row) now carries the
        # new keys.
        records = read_documents(cat._documents_path)
        assert str(tumbler) in records

    def test_update_bib_columns_on_fresh_and_migrated_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("NEXUS_EVENT_SOURCED", "0")
        # (a) fresh schema — CREATE TABLE already ships all 8 columns.
        _, fresh_cat = _make_catalog(tmp_path / "fresh")
        owner = fresh_cat.register_owner("papers", "curator")
        fresh_tumbler = fresh_cat.register(owner, "Fresh Paper", content_type="paper")
        fresh_cat.update(fresh_tumbler, **_ALL_EIGHT)
        fresh_entry = fresh_cat.resolve(fresh_tumbler)
        assert fresh_entry is not None
        for key, value in _ALL_EIGHT.items():
            assert getattr(fresh_entry, key) == value, key

        # (b) migrated schema — pre-create a legacy pre-bib ``documents``
        # table (same fixture shape as
        # tests/test_catalog_documents_bib_columns.py's proven
        # TestUpgradeFromLegacySchema pattern) BEFORE the Catalog is ever
        # constructed. CatalogStore.__init__'s ``CREATE TABLE IF NOT
        # EXISTS`` no-ops on the pre-existing table and its inline ALTER
        # TABLE guards add the 8 bib_* columns back — the real upgrade
        # path a pre-nexus-knn3 install goes through. register()/update()
        # afterwards operate on a documents table whose bib_* columns
        # originated via ALTER, not the original CREATE TABLE.
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        db_path = legacy_dir / ".catalog.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(
                """
                CREATE TABLE documents (
                    tumbler TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    author TEXT,
                    year INTEGER,
                    content_type TEXT,
                    file_path TEXT,
                    corpus TEXT,
                    physical_collection TEXT,
                    chunk_count INTEGER,
                    head_hash TEXT,
                    indexed_at TEXT,
                    metadata JSON,
                    source_mtime REAL NOT NULL DEFAULT 0,
                    alias_of TEXT NOT NULL DEFAULT '',
                    source_uri TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        migrated_cat = Catalog(legacy_dir, db_path)
        legacy_owner = migrated_cat.register_owner("papers", "curator")
        legacy_tumbler = migrated_cat.register(
            legacy_owner, "Legacy Paper", content_type="paper",
        )
        migrated_cat.update(legacy_tumbler, **_ALL_EIGHT)
        migrated_entry = migrated_cat.resolve(legacy_tumbler)
        assert migrated_entry is not None
        for key, value in _ALL_EIGHT.items():
            assert getattr(migrated_entry, key) == value, key
