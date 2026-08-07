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

# RDR-109 Phase 2: this file asserts cloud-mode canonical behavior
# (voyage-* embedder names, canonical-set defaults). The cloud_mode
# fixture sets credentials and forces ``is_local_mode()`` to False so
# the assertions hold regardless of the host environment.
pytestmark = pytest.mark.usefixtures("cloud_mode")


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
            # RDR-103 Phase 5: ``t3_collection_name`` auto-promotes
            # ``--collection knowledge__papers`` to conformant.
            assert (
                call.kwargs.get("collection_name")
                == "knowledge__papers__voyage-context-3__v1"
            )

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


class TestBatchIndexReconciliationSummary:
    """nexus-2t63u round 2 (critic recommendation, non-blocking add before
    commit): the ``--dir`` batch Summary line must surface a mass mistaken
    ``--collection`` retarget as a count, not require the operator to
    scroll back through WARNING-level structlog output to notice it.

    Pins the accumulation subtlety the critic's probe falsified empirically:
    ``reset_identity_drop_collectors()`` (which also resets the
    reconciliation collector) fires PER FILE inside the ``--dir`` loop, so
    a naive single read of ``get_reconciled_collections_count()`` taken
    AFTER the loop would see only the LAST file's count — undercounting a
    2-of-3 reconciling batch to 1. The fix accumulates a running total
    inside the loop instead.
    """

    def test_two_of_three_files_reconcile_collections_summary_shows_count(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """alpha.pdf and beta.pdf each simulate a successful
        ``physical_collection`` reconciliation (mirroring what
        ``doc_indexer._register_or_lookup_doc_id``'s real reconcile branch
        does via ``_record_physical_collection_reconciled()`` — the mocked
        ``index_pdf`` call stands in for that whole production call, so the
        collector is populated the same way production would populate it,
        at the same point in the per-file sequence); gamma.pdf does not.
        The naive post-loop-read alternative the critic falsified would
        report "1 collection reconciliation(s)" here (only gamma.pdf's —
        actually zero — or beta.pdf's count, depending on reset timing);
        the fix must report exactly 2.
        """
        from nexus.mcp_infra import _record_physical_collection_reconciled

        def index_with_reconcile(path, **kwargs):
            if path.name in ("alpha.pdf", "beta.pdf"):
                _record_physical_collection_reconciled()
            return 5

        with patch("nexus.doc_indexer.index_pdf", side_effect=index_with_reconcile):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        assert result.exit_code == 0, result.output
        assert "2 collection reconciliation(s), see WARNINGs above" in result.output, result.output

    def test_no_reconciliations_summary_carries_no_suffix(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """Success-path regression: no reconciliations -> no suffix at all
        (not "0 collection reconciliation(s)")."""
        with patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        assert result.exit_code == 0, result.output
        assert "collection reconciliation" not in result.output


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
        # nexus-uqq9z: a batch with any per-file failure must exit non-zero
        # — pre-fix this stayed rc=0 (a script/CI job keying on the exit
        # code never noticed the extraction failure).
        assert result.exit_code != 0, result.output

    def test_batch_exits_nonzero_when_one_of_several_files_fails(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """nexus-uqq9z: the batch-wide non-zero-exit contract, mirroring
        ``nx dt index``'s run-level fail-loud behavior. Both the failing
        and the succeeding files must still be processed (batch isolation
        is unchanged — only the exit code changes), the failures list
        must still show the failure, and a trailing count line must name
        how many of the total failed."""
        call_count = 0

        def fail_first(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("boom")
            return 5

        with patch("nexus.doc_indexer.index_pdf", side_effect=fail_first):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        # All 3 files attempted — batch isolation preserved.
        assert call_count == 3
        assert result.exit_code != 0, result.output
        assert "1 failure(s)" in result.output
        assert "boom" in result.output
        # The count-line naming N of M files failed.
        assert "1 of 3 file(s) failed" in result.output, result.output
        assert "see list above" in result.output.lower(), result.output

    def test_batch_exit_zero_when_all_files_succeed(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """Success-path regression: no failures → rc stays 0, no count
        line printed."""
        with patch("nexus.doc_indexer.index_pdf", side_effect=_mock_index_pdf):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        assert result.exit_code == 0, result.output
        assert "file(s) failed" not in result.output

    def test_conflict_running_lands_in_failures_bucket(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """nexus-lcmbp fix-list #1 (accepted design): --dir batch mode
        does NOT get special-cased CLI wiring for PipelineConflictRunning —
        it's a RuntimeError subclass, so the batch loop's existing generic
        `except Exception` catches it exactly like any other per-file
        failure, isolates it to that one file, and continues the batch.
        The FAILED line must carry the error + remedy text, not just a
        bare exception repr."""
        from nexus.db.http_pipeline_client import PipelineConflictRunning

        call_count = 0

        def conflict_on_second(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise PipelineConflictRunning(
                    "pipeline for content_hash=abc123 is already running",
                    content_hash="abc123",
                    started_at="2026-08-01T12:00:00+00:00",
                    heartbeat_age_seconds=42,
                    stale_threshold_seconds=300,
                    remedy="wait for the resume window (retry after the "
                           "heartbeat exceeds the stale threshold) or "
                           "inspect the pipeline row via GET "
                           "/v1/pipeline/state (engine route; requires "
                           "service auth)",
                )
            return 5

        with patch("nexus.doc_indexer.index_pdf", side_effect=conflict_on_second):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        # All 3 attempted — one file's conflict does not abort the batch.
        assert call_count == 3
        assert "1 failure(s)" in result.output
        assert "already running" in result.output
        assert "resume window" in result.output
        # nexus-uqq9z: a conflict landing in the failures bucket must also
        # drive the batch-wide exit code non-zero.
        assert result.exit_code != 0, result.output

    def test_quality_gate_failure_lands_in_failures_bucket(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        """nexus-wi1uv: a per-file post-extraction quality-gate failure
        (the space-stripped-garbage signature) is a plain ``ExtractionQualityError``
        (a ``NexusError``, not a ``RuntimeError``) — the batch loop's
        generic ``except Exception`` isolation must catch it exactly like
        any other per-file failure: batch continues, failure named in the
        list, batch-wide exit non-zero (nexus-uqq9z contract)."""
        from nexus.errors import ExtractionQualityError

        call_count = 0

        def fail_second(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ExtractionQualityError(
                    "PDF failed the post-extraction quality gate: "
                    "whitespace_ratio=0.0114 < floor 0.05"
                )
            return 5

        with patch("nexus.doc_indexer.index_pdf", side_effect=fail_second):
            result = runner.invoke(main, ["index", "pdf", "--dir", str(pdf_dir)])

        assert call_count == 3, "batch isolation: all 3 files must still be attempted"
        assert result.exit_code != 0, result.output
        assert "quality gate" in result.output
        assert "1 of 3 file(s) failed" in result.output, result.output

    def test_allow_degraded_extraction_flag_threads_through_batch(
        self, runner: CliRunner, pdf_dir: Path,
    ) -> None:
        with patch("nexus.doc_indexer.index_pdf", return_value=5) as m:
            result = runner.invoke(
                main, ["index", "pdf", "--dir", str(pdf_dir), "--allow-degraded-extraction"],
            )
        assert result.exit_code == 0, result.output
        assert m.call_args is not None
        _, kw = m.call_args
        assert kw.get("allow_degraded_extraction") is True

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
