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
