# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

from pathlib import Path

import pytest

from tests._catalog_fixture_ops import ActiveCatalog, count_documents, only_document


@pytest.fixture(autouse=True)
def git_identity(monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@test.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@test.invalid")


@pytest.fixture(autouse=True)
def _point_catalog_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Aim ``catalog_path()`` at the dir ``_make_catalog`` initialises
    (nexus-aqbrk) — ActiveCatalog resolves through the same factories the
    PDF hook uses, and the SQLite arm of those reads ``catalog_path()``."""
    monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "catalog"))


def _make_catalog(tmp_path: Path) -> tuple[Path, ActiveCatalog]:
    # nexus-i711w: the local Catalog.init that used to run here died with
    # the local catalog; the PDF hook registers into the live service catalog.
    catalog_dir = tmp_path / "catalog"
    return catalog_dir, ActiveCatalog()


class TestPdfCatalogHook:
    def test_registers_pdf(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/papers/attention.pdf"),
            collection_name="docs__papers",
            title="Attention Is All You Need",
            author="Vaswani et al.",
            year=2017,
            corpus="papers",
        )
        # Should have created curator owner + document
        rows = (count_documents(),)
        assert rows[0] == 1
        rows = (only_document().title,)
        assert rows[0] == "Attention Is All You Need"

    def test_skipped_when_not_initialized(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(tmp_path / "no-catalog"))
        # Should not raise
        _catalog_pdf_hook(
            pdf_path=Path("/data/test.pdf"),
            collection_name="docs__test",
            title="Test",
        )

    def test_uses_filename_when_no_title(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/my-paper.pdf"),
            collection_name="docs__test",
            title="",
        )
        rows = (only_document().title,)
        assert rows[0] == "my-paper"

    def test_update_on_reindex(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"),
            collection_name="docs__v1",
            title="Paper",
        )
        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"),
            collection_name="docs__v2",
            title="Paper",
        )
        # Should still be 1 document, updated collection
        rows = (count_documents(),)
        assert rows[0] == 1
        rows = (only_document().physical_collection,)
        assert rows[0] == "docs__v2"


class TestPdfCatalogHookTitleBackfill:
    """Mirror of nexus-ivzw8 for PDFs (papers/2512.11001.pdf, 2026-08-19).

    The RDR-102 D1 pre-flight registers every PDF with ``title=stem`` before
    extraction runs, so the post-index hook always takes the ``existing``
    branch — which wrote only chunk_count/indexed_at/mtime. The extracted
    title/author/year therefore NEVER reached the catalog for any PDF; it
    only looked right when the filename happened to be the title. The
    update branch must stem-guard backfill exactly like the markdown hook.
    """

    def test_reindex_backfills_stem_placeholder(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        # pre-flight shape: no title yet -> stem placeholder
        _catalog_pdf_hook(
            pdf_path=Path("/data/2512.11001.pdf"),
            collection_name="knowledge__x",
            title="",
        )
        assert only_document().title == "2512.11001"

        _catalog_pdf_hook(
            pdf_path=Path("/data/2512.11001.pdf"),
            collection_name="knowledge__x",
            title="Rethinking Query Optimization for Multi-Agent Systems [Vision]",
            author="Zoi Kaoudi, Ioana Giurgiu",
            year=2025,
            chunk_count=78,
        )
        doc = only_document()
        assert doc.title == "Rethinking Query Optimization for Multi-Agent Systems [Vision]"
        assert doc.author == "Zoi Kaoudi, Ioana Giurgiu"
        assert doc.year == 2025
        assert doc.chunk_count == 78

    def test_reindex_never_clobbers_curated_title(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="Curated Title", author="A. Author", year=2020,
        )
        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="Re-extracted Different Title", author="B. Other", year=2021,
        )
        doc = only_document()
        assert doc.title == "Curated Title"
        assert doc.author == "A. Author"
        assert doc.year == 2020

    def test_reindex_with_empty_title_keeps_existing(self, tmp_path, monkeypatch):
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="Real Title",
        )
        _catalog_pdf_hook(
            pdf_path=Path("/data/paper.pdf"), collection_name="knowledge__x",
            title="",
        )
        assert only_document().title == "Real Title"

    def test_normalised_stem_is_also_a_placeholder(self, tmp_path, monkeypatch):
        """resolve_pdf_title falls to derive_title's NORMALISED stem when no
        extractor/H1 title exists; that must not be backfilled-then-locked
        (nexus-ov5tc critique S1): a later real title still lands."""
        from nexus.pipeline_stages import _catalog_pdf_hook

        catalog_dir, cat = _make_catalog(tmp_path)
        monkeypatch.setenv("NEXUS_CATALOG_PATH", str(catalog_dir))

        p = Path("/data/attention-is-all.pdf")
        _catalog_pdf_hook(pdf_path=p, collection_name="knowledge__x", title="")   # pre-flight shape
        _catalog_pdf_hook(pdf_path=p, collection_name="knowledge__x", title="Attention Is All")  # normalised stem
        assert only_document().title in ("attention-is-all", "Attention Is All")
        _catalog_pdf_hook(pdf_path=p, collection_name="knowledge__x", title="Attention Is All You Need")
        assert only_document().title == "Attention Is All You Need"
