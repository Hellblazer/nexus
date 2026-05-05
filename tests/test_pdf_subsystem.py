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


# ── nexus-2fyb: real-fixture formula-preservation regression guard ──────────

class TestFormulaPreservationOnRealPdf:
    """End-to-end formula extraction on a real math paper.

    The fixture ``tests/fixtures/distributed-bloom-filter.pdf`` is an academic
    paper containing visible LaTeX-renderable formulas (false-positive-rate
    derivation for Bloom filters). It is the canonical witness that the auto
    extractor either preserves formulas or fails loudly — never silently
    strips them.

    Pre-fix history: this fixture was committed to ``tests/fixtures/`` but
    never imported by any test, which is why a regression that wiped formulas
    from every indexed PDF for weeks went undetected. Adding these assertions
    closes the gap.
    """

    _FIXTURE = Path(__file__).parent / "fixtures" / "distributed-bloom-filter.pdf"

    # Exact empirical counts for this fixture, locked deliberately. Inequalities
    # were the original bug shape: ``>= 5`` would still pass if MinerU regressed
    # from 44 formulas to 5, silently dropping 39. Every value below is a
    # frozen invariant of (this PDF) × (pinned MinerU+conexus versions). If any
    # number drifts, the test fails and a human reviews the diff — that's the
    # correct fail-loud contract for fixture-based regression guards.
    _EXPECTED_QUICK_SCREEN = 11             # _has_formulas_quick() return
    _EXPECTED_META_FORMULA_COUNT = 44       # MinerU's structured count
    _EXPECTED_REGEX_MARKERS = 4             # _count_formula_markers (regex
                                            # alternation undercounts: each
                                            # $$..$$ block consumes whole)
    _EXPECTED_DOLLAR_DOLLAR_COUNT = 8       # 8 $$ markers = 4 paired blocks
    _EXPECTED_FRAC_COUNT = 12               # \frac{...} occurrences
    _EXPECTED_TEXT_LENGTH = 60135           # full extracted text
    _EXPECTED_PAGE_COUNT = 33               # PyMuPDF page count

    # The canonical false-positive-rate formula from the paper, in the exact
    # form MinerU emits. Pinned verbatim so any change to formula rendering
    # is caught loudly.
    _EXPECTED_FORMULA_SNIPPET = (
        r"\left( 1 - { \bigg ( } 1 - { \frac { 1 } { m } } "
        r"{ \bigg ) } ^ { k n }"
    )

    def test_fixture_quick_screen_detects_formulas(self) -> None:
        """Sanity: the fixture must produce exactly the locked formula count.
        Drift indicates either the fixture changed, PyMuPDF behavior shifted,
        or the math-Unicode set was edited — any of which invalidates the
        downstream regression assertions and demands a human review.
        """
        from nexus.pdf_extractor import _has_formulas_quick

        assert self._FIXTURE.exists(), f"missing fixture: {self._FIXTURE}"
        actual = _has_formulas_quick(self._FIXTURE)
        assert actual == self._EXPECTED_QUICK_SCREEN, (
            f"_has_formulas_quick returned {actual}; locked value is "
            f"{self._EXPECTED_QUICK_SCREEN}. If this is a legitimate change "
            f"(fixture replaced, PyMuPDF upgraded, _MATH_UNICODE edited), "
            f"update the constant and re-derive all sibling expected values."
        )

    def test_auto_raises_when_mineru_unavailable(self) -> None:
        """Auto mode on a formula-bearing PDF must raise when the
        formula-aware extractor (MinerU) is unavailable. Before this fix,
        every install without the ``mineru`` extra silently received
        formula-stripped Docling output stamped with formula_count=0."""
        from nexus.pdf_extractor import PDFExtractor

        extractor = PDFExtractor()
        with patch.object(
            extractor,
            "_extract_with_mineru",
            side_effect=ImportError("No module named 'mineru'"),
        ):
            with pytest.raises(RuntimeError) as excinfo:
                extractor.extract(self._FIXTURE, extractor="auto")
        msg = str(excinfo.value)
        assert "formulas" in msg
        # mineru is a default dep since nexus-2fyb — missing = corrupt install
        assert "uv tool install --reinstall conexus" in msg
        assert "--extractor docling" in msg

    def test_mineru_path_preserves_formulas(self) -> None:
        """End-to-end formula extraction on a real math paper. All assertions
        are EXACT against locked empirical values.

        nexus-2fyb code-review (round 2): inequalities were the original bug
        shape. ``meta_count > 0`` shipped formula_count=0 silently; even
        ``meta_count >= 5`` would pass if MinerU regressed from 44 to 5,
        dropping 39 formulas invisibly. Every assertion here is exact: a
        locked count, a locked text length, a locked verbatim formula
        snippet. The test fails loudly on ANY drift; a human reviews and
        either updates the constants (legit upgrade) or files a bug
        (regression). This is the only assertion shape that doesn't
        smuggle the original failure mode back in.

        Skipped when MinerU is not importable in the dev environment.
        """
        pytest.importorskip("mineru.cli.common")
        from nexus.pdf_extractor import PDFExtractor, _count_formula_markers

        result = PDFExtractor().extract(self._FIXTURE, extractor="auto")
        assert result.metadata["extraction_method"] == "mineru", (
            f"expected mineru; got {result.metadata['extraction_method']!r}"
        )
        assert result.metadata["page_count"] == self._EXPECTED_PAGE_COUNT
        text = result.text

        # MinerU's authoritative structured formula count.
        assert result.metadata["formula_count"] == self._EXPECTED_META_FORMULA_COUNT, (
            f"MinerU reported formula_count={result.metadata['formula_count']}; "
            f"locked at {self._EXPECTED_META_FORMULA_COUNT}. Any drift means "
            f"MinerU's behavior on this fixture changed — investigate before "
            f"updating the locked value."
        )

        # Text length — catches truncation, page drops, whitespace shifts.
        assert len(text) == self._EXPECTED_TEXT_LENGTH, (
            f"extracted text is {len(text)} chars; locked at "
            f"{self._EXPECTED_TEXT_LENGTH}. Length drift indicates partial "
            f"extraction, encoding shift, or whitespace handling change."
        )

        # Formula motifs — what chunkers/embedders/search actually see.
        assert text.count("$$") == self._EXPECTED_DOLLAR_DOLLAR_COUNT
        assert text.count(r"\frac") == self._EXPECTED_FRAC_COUNT
        assert _count_formula_markers(text) == self._EXPECTED_REGEX_MARKERS

        # Verbatim snippet — the bloom-filter false-positive-rate formula.
        # If the symbolic content of the canonical formula in this paper
        # changes, formula extraction has regressed somewhere.
        assert self._EXPECTED_FORMULA_SNIPPET in text, (
            f"locked formula snippet not found in extracted text. The "
            f"false-positive-rate derivation has changed form — verify "
            f"the formula is still being extracted correctly before "
            f"updating the snippet."
        )
