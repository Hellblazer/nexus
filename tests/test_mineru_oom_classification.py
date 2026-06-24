# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-148 Gap 5: catchable MinerU OOM + per-page degrade-to-docling.

Covers the 3-way OOM classification in ``_mineru_run_subprocess`` and the
``on_formula_oom={fail|docling}`` policy in ``_extract_with_mineru``. No API
keys / model weights — MinerU and docling boundaries are mocked.
"""
from __future__ import annotations

import contextlib
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
    """Drive _mineru_run_subprocess with a mocked worker that exits with
    *returncode*. When *ceiling* is True, configure a real RLIMIT_AS ceiling on
    Linux so the method applies it and sets _mineru_ceiling_applied itself (the
    flag is derived from config + platform, not set by the caller)."""
    ext = PDFExtractor()
    proc = MagicMock(pid=4321)
    proc.wait.return_value = returncode
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=180,
        ))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb",
            return_value=512 if ceiling else 0,
        ))
        if ceiling:
            # Ceiling is only applied (and the flag only set) on Linux.
            stack.enter_context(patch("sys.platform", "linux"))
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
    """A multi-page batch fails, bisects to single pages; the page that OOMs
    degrades to docling while the healthy page keeps MinerU extraction —
    exercises the batch//2 ladder + per-page degrade (span > 1)."""
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


# ── Gap 6: RLIMIT_AS ceiling (Linux-gated) + per-page timeout ────────


def _run_capturing_popen(*, ceiling_mb: int, platform: str, start=0, end=1):
    """Run _mineru_run_subprocess with Popen mocked to capture kwargs; the
    worker 'times out' immediately so we never touch the output files."""
    import subprocess as _sp

    ext = PDFExtractor()
    proc = MagicMock(pid=4321)

    # Raise TimeoutExpired only on the budgeted wait(timeout=...); the handler's
    # follow-up reaping wait() (no timeout) must return so the RuntimeError
    # surfaces cleanly instead of a second TimeoutExpired.
    def _wait(timeout=None):
        if timeout is not None:
            raise _sp.TimeoutExpired(cmd="x", timeout=timeout)
        return -9
    proc.wait.side_effect = _wait
    with contextlib.ExitStack() as stack:
        mock_popen = stack.enter_context(patch("subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb", return_value=ceiling_mb,
        ))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=10,
        ))
        stack.enter_context(patch("sys.platform", platform))
        with pytest.raises(RuntimeError) as exc_info:
            ext._mineru_run_subprocess(Path("/tmp/x.pdf"), start, end)
    return ext, mock_popen, proc, exc_info.value


def test_rlimit_ceiling_applied_on_linux() -> None:
    ext, mock_popen, _proc, _ = _run_capturing_popen(ceiling_mb=512, platform="linux")
    _, kwargs = mock_popen.call_args
    assert kwargs["preexec_fn"] is not None  # RLIMIT_AS preexec wired
    assert ext._mineru_ceiling_applied is True


def test_rlimit_ceiling_NOT_applied_on_macos() -> None:
    # darwin raises ValueError on setrlimit(RLIMIT_AS) and does not enforce it,
    # so the preexec must be gated off even when a ceiling is configured.
    ext, mock_popen, _proc, _ = _run_capturing_popen(ceiling_mb=512, platform="darwin")
    _, kwargs = mock_popen.call_args
    assert kwargs["preexec_fn"] is None
    assert ext._mineru_ceiling_applied is False


def test_no_ceiling_means_no_preexec() -> None:
    ext, mock_popen, _proc, _ = _run_capturing_popen(ceiling_mb=0, platform="linux")
    _, kwargs = mock_popen.call_args
    assert kwargs["preexec_fn"] is None
    assert ext._mineru_ceiling_applied is False


def test_per_page_timeout_scales_with_span() -> None:
    # 10 s/page * 3 pages == 30 s budget.
    _ext, _popen, proc, err = _run_capturing_popen(
        ceiling_mb=0, platform="linux", start=0, end=3,
    )
    # First (budgeted) wait used the per-page-scaled timeout.
    assert proc.wait.call_args_list[0].kwargs == {"timeout": 30}
    assert "timed out after 30s" in str(err)


def test_per_page_timeout_scales_for_whole_doc_batch() -> None:
    # end=None whole-doc batch: the budget scales by total_pages (supplied by
    # the caller, no PDF re-open), not hardcoded to a single page.
    import subprocess as _sp

    ext = PDFExtractor()
    ext._mineru_run_total_pages = 4  # set by _extract_with_mineru in real flow
    proc = MagicMock(pid=4321)

    def _wait(timeout=None):
        if timeout is not None:
            raise _sp.TimeoutExpired(cmd="x", timeout=timeout)
        return -9
    proc.wait.side_effect = _wait

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb", return_value=0))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=10))
        with pytest.raises(RuntimeError) as err:
            ext._mineru_run_subprocess(Path("/tmp/x.pdf"), 0, None)
    assert proc.wait.call_args_list[0].kwargs == {"timeout": 40}  # 10 s * 4 pages
    assert "timed out after 40s" in str(err.value)


def test_whole_doc_batch_without_total_pages_falls_back_to_one_page() -> None:
    # Direct subprocess call with end=None and no total_pages: 1-page budget
    # (the old flat 180s at the default), never an error.
    import subprocess as _sp

    ext = PDFExtractor()
    proc = MagicMock(pid=4321)

    def _wait(timeout=None):
        if timeout is not None:
            raise _sp.TimeoutExpired(cmd="x", timeout=timeout)
        return -9
    proc.wait.side_effect = _wait

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb", return_value=0))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=10))
        with pytest.raises(RuntimeError):
            ext._mineru_run_subprocess(Path("/tmp/x.pdf"), 0, None)
    assert proc.wait.call_args_list[0].kwargs == {"timeout": 10}  # 1 page


# ── Gap 6: batch//2 bisection ladder ─────────────────────────────────


def test_failed_batch_bisects_before_per_page() -> None:
    """A failed multi-page batch is retried by halving (batch//2 ladder), not
    by dropping straight to 1-page: a 4-page batch that fails bisects into
    [0,2) and [2,4); when those succeed, no 1-page calls are made."""
    ext = _mineru_extractor_for_one_page()
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 4
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_doc
    mock_ctx.__exit__.return_value = False

    calls: list[tuple[int, int | None]] = []

    def fake_isolated(pdf_path, start, end):
        calls.append((start, end))
        if (start, end) == (0, None):
            raise RuntimeError("full-batch failure")
        return (f"R{start}-{end}", [], [])

    with patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=mock_ctx,
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=4,
    ), patch.object(ext, "_mineru_run_isolated", side_effect=fake_isolated):
        result = ext._extract_with_mineru(Path("/tmp/math.pdf"), formula_count=9)

    assert calls == [(0, None), (0, 2), (2, 4)]  # bisected, not per-page
    assert "R0-2" in result.text and "R2-4" in result.text


# ── Gap 6: config knobs ──────────────────────────────────────────────


def test_config_parses_and_clamps_gap6_knobs() -> None:
    from nexus.config import (
        get_mineru_memory_ceiling_mb,
        get_mineru_page_timeout_s,
    )
    with patch("nexus.config.load_config", return_value={"pdf": {
        "mineru_memory_ceiling_mb": 2048, "mineru_page_timeout_s": 60,
    }}):
        assert get_mineru_memory_ceiling_mb() == 2048
        assert get_mineru_page_timeout_s() == 60
    # Defaults: ceiling disabled (0), timeout 180.
    with patch("nexus.config.load_config", return_value={}):
        assert get_mineru_memory_ceiling_mb() == 0
        assert get_mineru_page_timeout_s() == 180
    # Clamps: negative ceiling -> 0; sub-1 timeout -> 1.
    with patch("nexus.config.load_config", return_value={"pdf": {
        "mineru_memory_ceiling_mb": -5, "mineru_page_timeout_s": 0,
    }}):
        assert get_mineru_memory_ceiling_mb() == 0
        assert get_mineru_page_timeout_s() == 1


# ── extract() validates the option ──────────────────────────────────


def test_extract_rejects_invalid_on_formula_oom(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ext = PDFExtractor()
    with pytest.raises(ValueError, match="on_formula_oom"):
        ext.extract(pdf, on_formula_oom="bogus")
