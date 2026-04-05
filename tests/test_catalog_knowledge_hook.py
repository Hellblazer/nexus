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
        rows = cat._db._conn.execute("SELECT count(*) FROM documents").fetchone()
        assert rows[0] == 1


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
