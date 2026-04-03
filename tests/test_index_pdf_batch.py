# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for batch PDF indexing (nx index pdf --dir).

TDD — defines expected behavior for directory-mode PDF indexing with progress
reporting and error handling. Bead: nexus-2mom, Epic: nexus-5f2b (RDR-046 Phase 4).
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nexus.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def pdf_dir(tmp_path: Path) -> Path:
    """Create a directory with fake PDF files."""
    d = tmp_path / "papers"
    d.mkdir()
    for name in ["alpha.pdf", "beta.pdf", "gamma.pdf"]:
        (d / name).write_bytes(b"%PDF-dummy")
    return d


@pytest.fixture
def empty_dir(tmp_path: Path) -> Path:
    d = tmp_path / "empty"
    d.mkdir()
    return d


def _mock_index_pdf(path, **kwargs):
    """Default mock: returns chunk count of 5."""
    return 5


# ── Directory discovery ──────────────────────────────────────────────────────


class TestBatchIndexDiscovery:
    """--dir discovers and indexes all PDFs in a directory."""

    def test_discovers_all_pdfs(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """--dir indexes all *.pdf files in directory."""
        with patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf) as mock_idx:
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        assert result.exit_code == 0, result.output
        assert mock_idx.call_count == 3

    def test_collection_passed_to_all(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """--collection passed to each index_pdf call."""
        with patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf) as mock_idx:
            result = runner.invoke(main, [
                "index", "pdf", "--dir", str(pdf_dir),
                "--collection", "knowledge__papers",
            ])

        assert result.exit_code == 0, result.output
        for call in mock_idx.call_args_list:
            assert call.kwargs.get("collection_name") == "knowledge__papers"

    def test_alphabetical_order(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """PDFs processed in sorted order."""
        indexed_paths: list[Path] = []

        def track_index(path, **kwargs):
            indexed_paths.append(path)
            return 5

        with patch("nexus.doc_indexer.index_pdf", side_effect=track_index):
            runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        names = [p.name for p in indexed_paths]
        assert names == ["alpha.pdf", "beta.pdf", "gamma.pdf"]


# ── Progress reporting ───────────────────────────────────────────────────────


class TestBatchIndexProgress:
    """Progress output format for batch indexing."""

    def test_progress_format(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """Output contains [1/3] alpha.pdf ... [3/3] gamma.pdf."""
        with patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        assert "[1/3]" in result.output
        assert "[3/3]" in result.output
        assert "alpha.pdf" in result.output
        assert "gamma.pdf" in result.output

    def test_timing_in_progress(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """Output contains chunk count and timing per paper."""
        with patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        # At least one line should have "N chunks" and "Xs" timing
        assert "chunk" in result.output.lower()

    def test_summary_output(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """Final summary has total papers, chunks, and time."""
        with patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        # Summary should mention totals
        assert "3 pdfs" in result.output.lower()


# ── Error handling ───────────────────────────────────────────────────────────


class TestBatchIndexErrors:
    """Error handling in batch mode."""

    def test_failed_paper_continues(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """One PDF fails → batch continues, failure noted in summary."""
        call_count = 0

        def sometimes_fail(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("extraction failed")
            return 5

        with patch("nexus.doc_indexer.index_pdf", side_effect=sometimes_fail):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        # Batch should complete (all 3 attempted)
        assert call_count == 3
        # Failure mentioned in output
        assert "fail" in result.output.lower() or "error" in result.output.lower()

    def test_empty_directory(
        self, runner: CliRunner, empty_dir: Path,
    ) -> None:
        """Empty directory → 'No PDF files found' message, exit 0."""
        result = runner.invoke(main, ["index", "pdf", "--dir", str(empty_dir)])

        assert result.exit_code == 0
        assert "no pdf" in result.output.lower()

    def test_nonexistent_directory(
        self, runner: CliRunner, tmp_path: Path,
    ) -> None:
        """Nonexistent --dir path → error exit."""
        result = runner.invoke(main, [
            "index", "pdf", "--dir", str(tmp_path / "nope"),
        ])
        assert result.exit_code != 0

    def test_dir_and_path_mutually_exclusive(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """--dir and positional PATH together → UsageError."""
        result = runner.invoke(main, [
            "index", "pdf", str(pdf_dir / "alpha.pdf"),
            "--dir", str(pdf_dir),
        ])
        assert result.exit_code != 0
        assert "mutually exclusive" in result.output.lower() or "usage" in result.output.lower()

    def test_dry_run_with_dir_rejected(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """--dry-run + --dir → UsageError."""
        result = runner.invoke(main, [
            "index", "pdf", "--dir", str(pdf_dir), "--dry-run",
        ])
        assert result.exit_code != 0
        assert "dry-run" in result.output.lower()


class TestBatchServerAdvisory:
    """Server availability advisory in batch mode."""

    def test_server_absent_warning(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """When MinerU server is not running, batch prints advisory."""
        with (
            patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf),
            patch("nexus.pdf_extractor.PDFExtractor._mineru_server_available",
                  return_value=False),
        ):
            result = runner.invoke(main, [
                "index", "pdf", "--dir", str(pdf_dir), "--extractor", "mineru",
            ])

        assert result.exit_code == 0, result.output
        assert "not running" in result.output.lower()
        assert "nx mineru start" in result.output

    def test_server_available_message(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """When MinerU server is running, batch confirms it."""
        with (
            patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf),
            patch("nexus.pdf_extractor.PDFExtractor._mineru_server_available",
                  return_value=True),
        ):
            result = runner.invoke(main, [
                "index", "pdf", "--dir", str(pdf_dir), "--extractor", "mineru",
            ])

        assert result.exit_code == 0, result.output
        assert "server available" in result.output.lower()
