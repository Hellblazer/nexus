# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-148 Gap 5: catchable MinerU OOM + per-page degrade-to-docling.

Covers the 3-way OOM classification in ``_mineru_run_subprocess`` and the
``on_formula_oom={fail|docling}`` policy in ``_extract_with_mineru``. No API
keys / model weights — MinerU and docling boundaries are mocked.
"""
from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import (
    _MINERU_OOM_EXIT,
    MineruMemoryError,
    PDFExtractor,
)


# ── worker script carries the MemoryError -> sentinel exit ───────────


def test_worker_script_maps_memoryerror_to_sentinel() -> None:
    from nexus.pdf_extractor import _MINERU_WORKER_SCRIPT

    assert "except MemoryError:" in _MINERU_WORKER_SCRIPT
    # The placeholder must be substituted with the actual sentinel int.
    assert "__MINERU_OOM_EXIT__" not in _MINERU_WORKER_SCRIPT
    assert f"os._exit({_MINERU_OOM_EXIT})" in _MINERU_WORKER_SCRIPT


# ── 3-way OOM classification in _mineru_run_subprocess ───────────────


def _run_with_returncode(returncode: int, *, ceiling: bool):
    ext = PDFExtractor()
    ext._mineru_ceiling_applied = ceiling
    proc = MagicMock(pid=4321)
    proc.wait.return_value = returncode
    with patch("subprocess.Popen", return_value=proc), patch(
        "nexus.util.process_group.safe_killpg",
    ):
        return ext._mineru_run_subprocess(Path("/tmp/does-not-matter.pdf"), 0, 1)


@pytest.mark.parametrize("returncode,ceiling", [
    (-signal.SIGKILL, False),   # OS OOM-killer / jetsam
    (_MINERU_OOM_EXIT, False),  # in-process MemoryError sentinel (RLIMIT_AS)
    (1, True),                  # any non-zero once a ceiling was applied
], ids=["sigkill", "sentinel", "ceiling_nonzero"])
def test_oom_returncodes_raise_mineru_memory_error(returncode, ceiling) -> None:
    with pytest.raises(MineruMemoryError):
        _run_with_returncode(returncode, ceiling=ceiling)


def test_nonoom_failure_raises_plain_runtimeerror_not_oom() -> None:
    # Non-zero exit with no ceiling applied is NOT classified as OOM.
    with pytest.raises(RuntimeError) as exc_info:
        _run_with_returncode(1, ceiling=False)
    assert not isinstance(exc_info.value, MineruMemoryError)


# ── on_formula_oom policy in _extract_with_mineru ────────────────────


def _mineru_extractor_for_one_page() -> PDFExtractor:
    """A PDFExtractor wired so _extract_with_mineru sees a 1-page PDF and a
    MinerU import; the per-page run + docling degrade are patched per test."""
    return PDFExtractor()


def _patch_one_page_pdf():
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 1
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_doc
    mock_ctx.__exit__.return_value = False
    return mock_ctx


def test_single_page_oom_degrades_to_docling_when_opted_in() -> None:
    ext = _mineru_extractor_for_one_page()
    with patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=_patch_one_page_pdf(),
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=1,
    ), patch.object(
        ext, "_mineru_run_isolated",
        side_effect=MineruMemoryError("page 1 OOM"),
    ), patch.object(
        ext, "_extract_page_via_docling", return_value="DEGRADED PAGE TEXT",
    ) as mock_degrade:
        result = ext._extract_with_mineru(
            Path("/tmp/math.pdf"), formula_count=9, on_formula_oom="docling",
        )
    # The pathological page was degraded, not failed — document survives.
    mock_degrade.assert_called_once()
    assert "DEGRADED PAGE TEXT" in result.text


def test_multipage_batch_retry_degrades_only_oom_page() -> None:
    """A multi-page batch fails, retries at 1-page granularity; the page that
    OOMs degrades to docling while the healthy page keeps MinerU extraction —
    exercises the _run_page_or_degrade retry path (span > 1)."""
    ext = _mineru_extractor_for_one_page()
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 2  # two pages
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_doc
    mock_ctx.__exit__.return_value = False

    def fake_isolated(pdf_path, start, end):
        if (start, end) == (0, None):
            raise RuntimeError("multi-page batch failure")  # forces 1-page retry
        if (start, end) == (1, 2):
            raise MineruMemoryError("page 2 OOM")
        return (f"PAGE{start} MINERU", [], [])

    with patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=mock_ctx,
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=2,
    ), patch.object(
        ext, "_mineru_run_isolated", side_effect=fake_isolated,
    ), patch.object(
        ext, "_extract_page_via_docling", return_value="PAGE1 DEGRADED",
    ) as mock_degrade:
        result = ext._extract_with_mineru(
            Path("/tmp/math.pdf"), formula_count=9, on_formula_oom="docling",
        )
    # Page 0 kept MinerU; page 1 degraded to docling — only the OOM page.
    assert "PAGE0 MINERU" in result.text
    assert "PAGE1 DEGRADED" in result.text
    mock_degrade.assert_called_once()


def test_single_page_oom_fails_by_default() -> None:
    ext = _mineru_extractor_for_one_page()
    with patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=_patch_one_page_pdf(),
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=1,
    ), patch.object(
        ext, "_mineru_run_isolated",
        side_effect=MineruMemoryError("page 1 OOM"),
    ), patch.object(
        ext, "_extract_page_via_docling", return_value="SHOULD NOT BE USED",
    ) as mock_degrade:
        with pytest.raises(MineruMemoryError):
            ext._extract_with_mineru(
                Path("/tmp/math.pdf"), formula_count=9, on_formula_oom="fail",
            )
    mock_degrade.assert_not_called()


# ── extract() validates the option ──────────────────────────────────


def test_extract_rejects_invalid_on_formula_oom(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ext = PDFExtractor()
    with pytest.raises(ValueError, match="on_formula_oom"):
        ext.extract(pdf, on_formula_oom="bogus")
