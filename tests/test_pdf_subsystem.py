# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subsystem tests for the PDF indexing pipeline.

Real PDF extraction + chunking; mocked embed + T3.  These tests prove that the
pipeline stitches together correctly without requiring API keys or network access.

AC-S1 through AC-S6 from RDR-011.
AC-S7 through AC-S8 from RDR-012 (pdfplumber tier).
"""
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.corpus import index_model_for_collection
from nexus.doc_indexer import _pdf_chunks, _sha256, index_pdf
from nexus.indexer import _git_metadata, _index_pdf_file
from nexus.pdf_extractor import PDFExtractor
from tests.conftest import set_credentials


def _make_ruled_table_pdf(path: Path) -> None:
    """Create a PDF with a visible ruled table (borders drawn as lines).

    PyMuPDF's find_tables() detects tables via ruling lines; this creates
    a 2×2 table with explicit line borders so find_tables() returns non-empty.
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()

    # Draw a 2-column, 3-row table (header + 2 data rows) with ruling lines.
    # Coordinates: (x0, y0, x1, y1); origin is top-left in pymupdf.
    col_xs = [72, 230, 400]   # x positions of 3 vertical borders
    row_ys = [100, 130, 160, 190]  # y positions of 4 horizontal borders
    shape = page.new_shape()
    for y in row_ys:
        shape.draw_line((col_xs[0], y), (col_xs[-1], y))
    for x in col_xs:
        shape.draw_line((x, row_ys[0]), (x, row_ys[-1]))
    shape.finish(color=(0, 0, 0), width=1)
    shape.commit()

    # Insert text into cells (positioned inside cell interiors)
    cell_positions = [
        (col_xs[0] + 5, row_ys[0] + 22, "Column A"),
        (col_xs[1] + 5, row_ys[0] + 22, "Column B"),
        (col_xs[0] + 5, row_ys[1] + 22, "Value 1"),
        (col_xs[1] + 5, row_ys[1] + 22, "Value 2"),
        (col_xs[0] + 5, row_ys[2] + 22, "Data X"),
        (col_xs[1] + 5, row_ys[2] + 22, "Data Y"),
    ]
    for x, y, text in cell_positions:
        page.insert_text((x, y), text, fontsize=10)

    # Add some prose text below the table
    page.insert_text((72, 250), "This document contains a ruled table above.", fontsize=11)

    doc.save(str(path))
    doc.close()


@pytest.fixture(scope="session")
def ruled_table_pdf(pdf_fixtures_dir: Path) -> Path:
    """PDF with a ruled (bordered) table detectable by find_tables()."""
    path = pdf_fixtures_dir / "ruled_table.pdf"
    _make_ruled_table_pdf(path)
    return path


def _fake_embed(chunks, model, api_key, input_type="document", timeout=120.0, on_progress=None):
    """Embedding stub: returns unit vectors without calling Voyage AI."""
    return [[0.1] * 5] * len(chunks), "test-local"


# ── AC-S1 / AC-S2 / AC-S2b — _pdf_chunks metadata ───────────────────────────

class TestPdfChunksMetadata:
    """AC-S1 / AC-S2 / AC-S2b: _pdf_chunks produces correct per-chunk metadata."""

    def test_simple_pdf_full_metadata(self, simple_pdf: Path) -> None:
        """AC-S1: Every chunk from simple.pdf carries the expected metadata values.

        RDR-021: extraction_method is now 'docling'. source_title comes from
        docling_title (content-extracted) or filename stem; pdf_title XMP metadata
        is no longer populated by Docling.
        """
        content_hash = _sha256(simple_pdf)
        result = _pdf_chunks(
            simple_pdf, content_hash, "voyage-context-3", "2026-01-01T00:00:00", "mybook"
        )
        assert result, "Expected at least one chunk from simple.pdf"
        for chunk_id, text, meta in result:
            # RDR-101 Phase 5c (nexus-o6aa.13) dropped store_type, corpus,
            # git_meta. content_type is the canonical routing field.
            assert meta["content_type"] == "pdf"
            assert "store_type" not in meta
            assert "corpus" not in meta
            assert "git_meta" not in meta
            assert meta["content_hash"] == content_hash
            # page_count + extraction_method are dropped by normalize() —
            # not in ALLOWED_TOP_LEVEL.
            assert "page_count" not in meta
            assert "extraction_method" not in meta
            assert meta["chunk_count"] == len(result)
            # title (was source_title): Docling content-extracted or filename fallback
            assert isinstance(meta["title"], str)
            # source_author: Docling does not expose XMP author; may be empty
            assert isinstance(meta["source_author"], str)
            assert meta["embedding_model"] == "voyage-context-3"
            assert isinstance(chunk_id, str) and chunk_id
            assert isinstance(text, str) and text.strip()

    def test_multipage_pdf_page_numbers(self, multipage_pdf: Path) -> None:
        """AC-S2: page_number values are drawn from {1, 2, 3}; no zeros present."""
        content_hash = _sha256(multipage_pdf)
        result = _pdf_chunks(
            multipage_pdf, content_hash, "voyage-context-3", "2026-01-01T00:00:00", "test"
        )
        assert result
        page_numbers = {meta["page_number"] for _, _, meta in result}
        assert page_numbers <= {1, 2, 3}, f"Unexpected page numbers: {page_numbers}"
        assert 0 not in page_numbers, "Page 0 should not appear when boundaries are present"
        assert len(page_numbers) > 1, (
            f"Expected chunks from multiple pages, got only pages: {page_numbers}"
        )

    def test_pdf_without_metadata_source_fields(self, tmp_path: Path) -> None:
        """AC-S2b: PDF with no embedded XMP metadata.

        RDR-021: Docling path does not expose XMP metadata (pdf_author/date are '').
        source_title falls back to filename stem when docling_title is also empty.
        """
        import pymupdf
        bare = tmp_path / "bare.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text(
            (72, 100),
            "Content without any PDF document metadata set. " * 5,
            fontsize=12,
        )
        doc.save(str(bare))
        doc.close()

        content_hash = _sha256(bare)
        result = _pdf_chunks(bare, content_hash, "test-model", "2026-01-01T00:00:00", "test")
        assert result
        for _, _, meta in result:
            # title (was source_title): docling_title may be empty; filename fallback
            assert isinstance(meta["title"], str)
            # source_author: Docling doesn't expose XMP author
            assert meta["source_author"] == "", f"Expected '', got {meta['source_author']!r}"
            # source_date: not in ALLOWED_TOP_LEVEL — dropped by normalize().
            assert "source_date" not in meta


# ── AC-S3 / AC-S4 — index_pdf pipeline ───────────────────────────────────────

class TestIndexPdfPipeline:
    """AC-S3 / AC-S4: index_pdf with real extraction, mocked embed + T3."""

    @staticmethod
    def _fresh_mock_t3():
        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = mock_col
        return mock_t3, mock_col

    def test_upserts_chunks_with_real_extraction(
        self, simple_pdf: Path, monkeypatch
    ) -> None:
        """AC-S3: Real extraction + mocked embed → upsert called, return count > 0."""
        set_credentials(monkeypatch)
        mock_t3, _ = self._fresh_mock_t3()

        with patch("nexus.doc_indexer._embed_with_fallback", side_effect=_fake_embed):
            count = index_pdf(simple_pdf, corpus="test", t3=mock_t3)

        assert count > 0
        mock_t3.upsert_chunks_with_embeddings.assert_called_once()

    def test_skip_when_already_indexed(self, simple_pdf: Path, monkeypatch) -> None:
        """AC-S4: Same hash + model already stored → staleness guard returns 0."""
        set_credentials(monkeypatch)
        content_hash = _sha256(simple_pdf)
        model = index_model_for_collection("docs__test")

        mock_col = MagicMock()
        mock_col.get.return_value = {
            "ids": ["existing"],
            "metadatas": [{"content_hash": content_hash, "embedding_model": model}],
        }
        mock_t3 = MagicMock()
        mock_t3.get_or_create_collection.return_value = mock_col

        with patch("nexus.doc_indexer._embed_with_fallback", side_effect=_fake_embed):
            count = index_pdf(simple_pdf, corpus="test", t3=mock_t3)

        assert count == 0
        mock_t3.upsert_chunks_with_embeddings.assert_not_called()


# ── AC-S5 / AC-S6 — _index_pdf_file git metadata ─────────────────────────────

@pytest.fixture(scope="module")
def pdf_git_repo(tmp_path_factory: pytest.TempPathFactory, simple_pdf: Path) -> Path:
    """Real git repo with simple.pdf committed — module-scoped, created once."""
    import shutil
    repo = tmp_path_factory.mktemp("pdf-git-repo")
    dest = repo / "docs" / "simple.pdf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(simple_pdf, dest)

    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@nexus"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Nexus Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add PDF fixture"], cwd=repo, check=True, capture_output=True)
    return repo


class TestIndexPdfFileGitMetadata:
    """AC-S5 / AC-S6: _index_pdf_file augments chunks with git metadata."""

    def _run_index_pdf_file(self, pdf: Path, repo: Path, git_meta: dict) -> list[dict]:
        """Helper: run _index_pdf_file with mocked embed/db, return captured metadatas."""
        collection_name = "docs__pdf-subsystem-git"
        model = index_model_for_collection(collection_name)
        now_iso = datetime.now(UTC).isoformat()

        mock_col = MagicMock()
        mock_col.get.return_value = {"ids": [], "metadatas": []}
        mock_db = MagicMock()

        captured: list[list[dict]] = []

        def capture(collection_name, ids, documents, embeddings, metadatas):
            captured.append(metadatas)

        mock_db.upsert_chunks_with_embeddings.side_effect = capture

        with patch("nexus.doc_indexer._embed_with_fallback", side_effect=_fake_embed):
            _index_pdf_file(
                file=pdf,
                repo=repo,
                collection_name=collection_name,
                target_model=model,
                col=mock_col,
                db=mock_db,
                voyage_key="vk_test",
                git_meta=git_meta,
                now_iso=now_iso,
                score=0.5,
            )

        return captured[0] if captured else []

    def test_git_fields_populated(self, pdf_git_repo: Path) -> None:
        """AC-S5: Indexed chunks carry non-empty git_commit_hash, correct branch."""
        pdf = pdf_git_repo / "docs" / "simple.pdf"
        git_meta = _git_metadata(pdf_git_repo)

        metadatas = self._run_index_pdf_file(pdf, pdf_git_repo, git_meta)

        assert metadatas, "Expected at least one chunk to be upserted"
        for meta in metadatas:
            assert meta["git_commit_hash"], "git_commit_hash must be non-empty"
            assert meta["git_branch"] == "main"
            assert meta["git_project_name"], "git_project_name must be non-empty"
            assert meta["tags"] == "pdf"
            assert meta["category"] == "prose"
            assert isinstance(meta["frecency_score"], float)

    def test_no_git_repo_empty_git_fields(self, tmp_path: Path, simple_pdf: Path) -> None:
        """AC-S6: Non-git directory → empty git metadata, no exception raised."""
        import shutil
        pdf = tmp_path / "simple.pdf"
        shutil.copy2(simple_pdf, pdf)

        git_meta = _git_metadata(tmp_path)
        # Verify _git_metadata itself returns empty strings for non-git dirs
        assert git_meta["git_commit_hash"] == ""
        assert git_meta["git_branch"] == ""

        metadatas = self._run_index_pdf_file(pdf, tmp_path, git_meta)

        assert metadatas, "Expected at least one chunk to be upserted"
        for meta in metadatas:
            # Empty git fields are filtered out by _index_pdf_file to stay
            # under ChromaDB's 32-key metadata limit. Verify absent or empty.
            assert meta.get("git_commit_hash", "") == ""
            assert meta.get("git_branch", "") == ""


# ── RDR-021: Docling single-tier regression guard ────────────────────────────

class TestDoclingRegressionGuard:
    """RDR-021 regression guard: Docling is the sole extraction tier.

    Verifies that ruled-table PDFs are now handled by Docling directly
    (no pdfplumber rescue tier, no Type3 detection, no 3-tier routing).
    """

    def test_ruled_table_pdf_uses_docling(self, ruled_table_pdf: Path) -> None:
        """Ruled-table PDF (formerly pdfplumber rescue path) now uses Docling."""
        result = PDFExtractor().extract(ruled_table_pdf)
        assert result.metadata["extraction_method"] == "docling", (
            f"Expected docling; got {result.metadata['extraction_method']!r}"
        )

    def test_simple_pdf_uses_docling(self, simple_pdf: Path) -> None:
        """Simple PDF uses Docling (single-tier, no conditional routing)."""
        result = PDFExtractor().extract(simple_pdf)
        assert result.metadata["extraction_method"] == "docling", (
            f"Expected docling; got {result.metadata['extraction_method']!r}"
        )
