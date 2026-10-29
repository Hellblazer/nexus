# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-148 Gap 5: catchable MinerU OOM + per-page degrade.

Covers the 3-way OOM classification in ``_mineru_run_subprocess`` and the per-page ladder in
``_extract_with_mineru`` (RDR-237: a page that runs out of memory is retried without formulas, rung 2,
then read from its text layer; no policy flag chooses). No API keys / model weights - MinerU
boundaries are mocked.
"""
from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nexus.pdf_extractor import (
    _MINERU_OOM_EXIT,
    MineruMemoryError,
    PDFExtractor,
)
from tests._module_seam import patch_in


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
        stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        # nexus-5ny9r: the sibling sweeps by the recorded group id, and the
        # mocked Popen hands the worker a real int pid — unpatched, this
        # would os.killpg that number for real from inside the suite.
        stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=180,
        ))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb",
            return_value=512 if ceiling else 0,
        ))
        if ceiling:
            # Ceiling is only applied (and the flag only set) on Linux.
            stack.enter_context(patch_in("nexus.pdf_extractor", "sys.platform", "linux"))
        return ext._mineru_run_subprocess(Path("/tmp/does-not-matter.pdf"), 0, 1)


def test_windows_shaped_kill_tree_failure_falls_back_to_proc_kill() -> None:
    """nexus-6y4e0 review (SIGNIFICANT): on Windows, safe_killpg_group
    right after kill_tree in _killpg_safe is ALWAYS a no-op there (no
    process groups on Windows), so a failed close_job (job already gone,
    or a genuine API failure) used to leave the worker itself running
    with nothing left to reach it -- and the untimed ``proc.wait()`` reap
    right after, in the TimeoutExpired branch, would then hang on a
    worker that was never actually killed. ``_killpg_safe`` now falls
    back to ``proc.kill()`` when ``kill_tree`` fails, matching the other
    three ``contain()`` call sites (bounded_subprocess, aspect_extractor,
    operators.dispatch).
    """
    import subprocess

    ext = PDFExtractor()
    proc = MagicMock(pid=4321)
    # First call (the bounded wait) times out; the reap call right after
    # the kill attempt succeeds harmlessly -- isolates the ONE behaviour
    # under test (does a failed kill_tree fall back to proc.kill()?) from
    # an unrelated second TimeoutExpired on the reap, which is not this
    # test's subject.
    proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="worker", timeout=1), 0]
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", return_value=proc))
        # Windows shape: contain() returns a job, whose close is the tree kill;
        # (the POSIX worker has no job and takes the group kill, which spares the tracker).
        stack.enter_context(patch("nexus.util.process_group.contain", return_value=object()))
        stack.enter_context(patch("nexus.util.process_group.kill_tree", return_value=False))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=180,
        ))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb", return_value=0,
        ))
        with pytest.raises(RuntimeError, match="timed out"):
            ext._mineru_run_subprocess(Path("/tmp/does-not-matter.pdf"), 0, 1)
    proc.kill.assert_called_once()


def _interrupted_wait_kills(*, job: object | None) -> tuple[MagicMock, MagicMock]:
    """Drive an interrupted bounded wait with ``contain`` returning *job*; return
    the (kill_tree, safe_killpg_group) mocks."""
    ext = PDFExtractor()
    proc = MagicMock(pid=4321)
    proc.wait.side_effect = KeyboardInterrupt()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.contain", return_value=job))
        kill_tree = stack.enter_context(
            patch("nexus.util.process_group.kill_tree", return_value=True),
        )
        group = stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=180,
        ))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb", return_value=0,
        ))
        with pytest.raises(KeyboardInterrupt):
            ext._mineru_run_subprocess(Path("/tmp/does-not-matter.pdf"), 0, 1)
    return kill_tree, group


@pytest.mark.parametrize("job", [None, object()], ids=["posix-group", "windows-job"])
def test_an_interrupted_wait_still_kills_the_worker_tree(job: object | None) -> None:
    """nexus-6y4e0 measurement review: an exception from the bounded wait other
    than TimeoutExpired must still kill the worker tree before propagating.
    nexus-x3m8e: on POSIX the hard group kill that ``kill_tree`` and
    ``safe_killpg_group`` send spares the multiprocessing resource tracker BY
    CONSTRUCTION (``util.process_group``), so this site needs no sparing pass of
    its own and the contract is the original one: both are called, the second by
    the recorded group id so it reaches a worker that already exited."""
    kill_tree, group = _interrupted_wait_kills(job=job)
    kill_tree.assert_called_once()
    group.assert_called_once_with(4321)


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


# ── the per-page ladder in _extract_with_mineru ──────────────────────


def _mineru_extractor_for_one_page() -> PDFExtractor:
    """A PDFExtractor wired so _extract_with_mineru sees a 1-page PDF and a
    MinerU import; the per-page run is patched per test."""
    return PDFExtractor()


def _patch_one_page_pdf():
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 1
    # The page's text layer is empty. Left a MagicMock, get_text() hands the normaliser a
    # non-string and the TypeError is swallowed by rung 3's broad except, so a test passes
    # for a reason that is not the page having no text.
    mock_doc.__getitem__.return_value.get_text.return_value = ""
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_doc
    mock_ctx.__exit__.return_value = False
    return mock_ctx


def test_single_page_oom_settles_on_rung_2() -> None:
    """RDR-237: a page that OOMs with formulas on is retried without them (rung 2),
    replacing RDR-148's degrade to a second extractor."""
    ext = _mineru_extractor_for_one_page()

    def run(pdf_path, start, end, formula_enable=True):
        if formula_enable:
            raise MineruMemoryError("page 1 OOM")
        return ("NO-FORMULA PAGE TEXT", [], [])

    with patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=_patch_one_page_pdf(),
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=1,
    ), patch.object(
        ext, "_mineru_run_isolated", side_effect=run,
    ):
        result = ext._extract_with_mineru(Path("/tmp/math.pdf"))
    # The pathological page was degraded, not failed - the document survives.
    assert "NO-FORMULA PAGE TEXT" in result.text
    # nexus-1oguj: the recorded extraction_method must be the honest mixed aggregate, not a bare
    # "mineru" that overstates coverage - this page lost its formulas.
    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [1]}


def test_multipage_batch_retry_degrades_only_oom_page() -> None:
    """A multi-page batch fails, bisects to single pages; the page that OOMs
    settles on rung 2 (no formulas) while the healthy page keeps MinerU
    extraction - exercises the batch//2 bisect + the per-page ladder (span > 1)."""
    ext = _mineru_extractor_for_one_page()
    mock_doc = MagicMock()
    mock_doc.__len__.return_value = 2  # two pages
    mock_ctx = MagicMock()
    mock_ctx.__enter__.return_value = mock_doc
    mock_ctx.__exit__.return_value = False

    def fake_isolated(pdf_path, start, end, formula_enable=True):
        if (start, end) == (0, None):
            raise RuntimeError("multi-page batch failure")  # forces 1-page retry
        if (start, end) == (1, 2) and formula_enable:
            raise MineruMemoryError("page 2 OOM")
        suffix = "MINERU" if formula_enable else "NOFORMULA"
        return (f"PAGE{start} {suffix}", [], [])

    with patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=mock_ctx,
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=2,
    ), patch(
        "nexus.config.get_mineru_page_timeout_s", return_value=30,
    ), patch.object(
        ext, "_mineru_run_isolated", side_effect=fake_isolated,
    ):
        result = ext._extract_with_mineru(Path("/tmp/math.pdf"))
    # Page 0 kept MinerU with formulas; page 1 lost them - only the OOM page.
    assert "PAGE0 MINERU" in result.text
    assert "PAGE1 NOFORMULA" in result.text
    # nexus-1oguj: even one degraded page flips the whole-document value to the honest mixed aggregate.
    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}


def test_single_page_oom_on_both_rungs_is_no_longer_a_document_failure() -> None:
    """RDR-237: a page that OOMs with and without formulas used to fail the document
    (the old ``fail`` policy). It now walks on to the text layer, and a lone page
    with none ends the document with a named reason instead."""
    from structlog.testing import capture_logs  # noqa: PLC0415 — test-local import

    from nexus.errors import UnextractableContentError

    ext = _mineru_extractor_for_one_page()
    pdf = _patch_one_page_pdf()
    page = pdf.__enter__.return_value.__getitem__.return_value
    with capture_logs() as logs, patch("nexus.pdf_extractor.do_parse", object()), patch(
        "pymupdf.open", return_value=pdf,
    ), patch(
        "nexus.config.get_mineru_page_batch", return_value=1,
    ), patch.object(
        ext, "_mineru_run_isolated",
        side_effect=MineruMemoryError("page 1 OOM"),
    ):
        with pytest.raises(UnextractableContentError, match="every page.*unextracted"):
            ext._extract_with_mineru(Path("/tmp/math.pdf"))
    page.get_text.assert_called_once()  # rung 3 read the page's text layer, and it was empty
    assert not [e for e in logs if e.get("event") == "mineru_page_text_layer_unreadable"], (
        "the page ended unextracted because it has no text, not because reading it raised"
    )


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
        mock_popen = stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        # nexus-5ny9r: the sibling sweeps by the recorded group id, and the
        # mocked Popen hands the worker a real int pid — unpatched, this
        # would os.killpg that number for real from inside the suite.
        stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
        stack.enter_context(patch(
            "nexus.config.get_mineru_memory_ceiling_mb", return_value=ceiling_mb,
        ))
        stack.enter_context(patch(
            "nexus.config.get_mineru_page_timeout_s", return_value=10,
        ))
        stack.enter_context(patch_in("nexus.pdf_extractor", "sys.platform", platform))
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
        stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        # nexus-5ny9r: the sibling sweeps by the recorded group id, and the
        # mocked Popen hands the worker a real int pid — unpatched, this
        # would os.killpg that number for real from inside the suite.
        stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
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
        stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", return_value=proc))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        # nexus-5ny9r: the sibling sweeps by the recorded group id, and the
        # mocked Popen hands the worker a real int pid — unpatched, this
        # would os.killpg that number for real from inside the suite.
        stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
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
    ), patch(
        "nexus.config.get_mineru_page_timeout_s", return_value=30,
    ), patch.object(ext, "_mineru_run_isolated", side_effect=fake_isolated):
        result = ext._extract_with_mineru(Path("/tmp/math.pdf"))

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
