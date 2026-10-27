# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 Phase 1 Step 1 (nexus-tr1rj.1): the per-page MinerU fallback ladder.

A fake MinerU (`FakeMineru`) fails on chosen pages and rungs; the PDFs are real
(PyMuPDF writes them), so rung 3 reads a real text layer. No MinerU, no model
weights, no engine.

Rung 1 is MinerU with formulas, rung 2 the same one-page call with
``formula_enable=False``, rung 3 the page's PyMuPDF text layer, rung 4 nothing.
"""
from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pymupdf
import pytest
from structlog.testing import capture_logs

from nexus import pdf_extractor
from nexus.config import PDFConfig, get_pdf_config, get_pdf_ladder_breaker
from nexus.db.http_pipeline_client import PipelineRunFenced
from nexus.pdf_extractor import (
    MineruMachineResourceError,
    MineruMemoryError,
    MineruSystemicFailure,
    MineruWorkerError,
    MineruWorkerStopped,
    PageOutcome,
    PDFExtractor,
)
from nexus.pipeline_stages import PipelineCancelled
from tests._module_seam import patch_in

pytestmark = pytest.mark.usefixtures("_quiet_progress")


@pytest.fixture
def _quiet_progress(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.delenv("NX_MINERU_FAULT_PAGES", raising=False)
    # A forced timeout waits out the real per-page budget (180 s by default); no test here may
    # ever really wait, so the wait is a recorder the timeout test reads.
    with patch("nexus.pdf_extractor._fault_wait") as wait:
        yield wait


# ── fixtures: a real PDF and a scripted MinerU ───────────────────────────────


def make_pdf(path: Path, page_texts: list[str]) -> Path:
    """A PDF with one page per entry; an empty entry is a page with no text layer."""
    doc = pymupdf.open()
    for text in page_texts:
        page = doc.new_page()
        if text:
            page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()
    return path


def worker_exit(returncode: int, line: str) -> MineruWorkerError:
    return MineruWorkerError(
        f"MinerU subprocess exited with code {returncode}",
        returncode=returncode, stderr_last_line=line,
    )


def worker_timeout() -> MineruWorkerError:
    return MineruWorkerError("MinerU subprocess timed out after 180s", timed_out=True)


def worker_oom(line: str = "") -> MineruMemoryError:
    return MineruMemoryError("MinerU subprocess killed", returncode=-9, stderr_last_line=line)


class FakeMineru:
    """Stands in for ``_mineru_run_isolated``. *plan* maps ``(page, rung)`` (page 1-based,
    rung 1 = formulas on, 2 = formulas off) to an exception to raise; anything else succeeds."""

    def __init__(self, plan: dict[tuple[int, int], BaseException] | None = None) -> None:
        self.plan = plan or {}
        self.calls: list[tuple[int, int]] = []

    def __call__(self, pdf_path, start, end, formula_enable=True):
        page, rung = start + 1, 1 if formula_enable else 2
        self.calls.append((page, rung))
        exc = self.plan.get((page, rung))
        if exc is not None:
            raise exc
        return f"MINERU r{rung} page {page}", [], [{"page_idx": 0, "para_blocks": []}]


def run_ladder(
    tmp_path: Path,
    page_texts: list[str],
    plan: dict[tuple[int, int], BaseException] | None = None,
    *,
    breaker: int = 3,
    subprocess_retry=None,
):
    """Run ``_extract_with_mineru`` over a real PDF; return (result, on_page calls, fake, ext)."""
    pdf = make_pdf(tmp_path / "doc.pdf", page_texts)
    fake = FakeMineru(plan)
    ext = PDFExtractor()
    delivered: list[tuple[int, str, dict]] = []
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("nexus.pdf_extractor.do_parse", object()))
        stack.enter_context(patch("nexus.config.get_mineru_page_batch", return_value=1))
        stack.enter_context(patch("nexus.config.get_pdf_ladder_breaker", return_value=breaker))
        stack.enter_context(patch.object(ext, "_mineru_run_isolated", side_effect=fake))
        if subprocess_retry is not None:
            stack.enter_context(patch.object(ext, "_mineru_run_subprocess", subprocess_retry))
        result = ext._extract_with_mineru(
            pdf, on_page=lambda i, t, m: delivered.append((i, t, m)),
        )
    return result, delivered, fake, ext


TEXTS = ["alpha one", "bravo two", "charlie three", "delta four", "echo five"]


# ── the ladder, rung by rung ─────────────────────────────────────────────────


def test_every_page_succeeds_on_rung_1(tmp_path: Path) -> None:
    result, delivered, fake, _ = run_ladder(tmp_path, TEXTS[:3])

    assert result.metadata["extraction_method"] == "mineru"
    assert result.metadata["extraction_pages"] == {}
    assert [d[0] for d in delivered] == [0, 1, 2]
    assert fake.calls == [(1, 1), (2, 1), (3, 1)]
    assert result.metadata["page_count"] == 3


def test_one_page_failing_rung_1_settles_on_rung_2_without_formulas(tmp_path: Path) -> None:
    plan = {(2, 1): worker_exit(1, "boom")}
    result, delivered, fake, _ = run_ladder(tmp_path, TEXTS[:3], plan)

    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}
    assert dict((i, t) for i, t, _ in delivered)[1] == "MINERU r2 page 2"
    assert dict((i, t) for i, t, _ in delivered)[0] == "MINERU r1 page 1"
    assert (2, 2) in fake.calls and (1, 2) not in fake.calls and (3, 2) not in fake.calls


def test_one_page_failing_rungs_1_and_2_settles_on_its_text_layer(tmp_path: Path) -> None:
    plan = {(2, 1): worker_exit(1, "boom"), (2, 2): worker_exit(1, "boom")}
    result, delivered, _, _ = run_ladder(tmp_path, TEXTS[:3], plan)

    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2]}
    assert dict((i, t) for i, t, _ in delivered)[1] == "bravo two"


def test_an_image_only_page_failing_everywhere_is_unextracted_and_the_document_finishes(
    tmp_path: Path,
) -> None:
    texts = ["alpha one", "", "charlie three"]  # page 2 has no text layer
    plan = {(2, 1): worker_exit(1, "boom"), (2, 2): worker_exit(1, "boom")}
    result, delivered, _, _ = run_ladder(tmp_path, texts, plan)

    assert result.metadata["extraction_pages"] == {"unextracted": [2]}
    assert result.metadata["extraction_method"] == "mineru+degraded"
    assert [d[0] for d in delivered] == [0, 1, 2], "the empty page is still delivered, in order"
    assert dict((i, t) for i, t, _ in delivered)[1] == ""


def test_a_document_whose_every_page_is_unextracted_raises_with_a_named_reason(
    tmp_path: Path,
) -> None:
    plan = {}
    for page in (1, 2):
        plan[(page, 1)] = worker_exit(1, "alpha")
        plan[(page, 2)] = worker_exit(1, "alpha")
    pdf_texts = ["", ""]

    with pytest.raises(pdf_extractor.UnextractableContentError, match="every page.*unextracted"):
        run_ladder(tmp_path, pdf_texts, plan, breaker=0)


def test_pages_that_degrade_are_listed_sorted_by_rung(tmp_path: Path) -> None:
    plan = {
        (4, 1): worker_exit(1, "x"),
        (1, 1): worker_exit(1, "x"), (1, 2): worker_exit(1, "x"),
        (3, 1): worker_exit(1, "x"),
        (5, 1): worker_exit(1, "y"), (5, 2): worker_exit(1, "z"),
    }
    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan)

    assert result.metadata["extraction_pages"] == {
        "mineru-no-formula": [3, 4], "pymupdf-text": [1, 5],
    }


# ── what descends and what propagates ────────────────────────────────────────


@pytest.mark.parametrize("rung", [1, 2])
@pytest.mark.parametrize("error", [
    MineruMachineResourceError("no semaphores left"),
    MineruWorkerStopped("stopped"),
    KeyboardInterrupt(),
    "PipelineCancelled",
    "PipelineRunFenced",
], ids=["machine-resource", "worker-stopped", "keyboard-interrupt", "cancelled", "fenced"])
def test_a_stop_propagates_from_either_rung_and_degrades_no_page(
    tmp_path: Path, rung: int, error,
) -> None:
    if error == "PipelineCancelled":
        error = PipelineCancelled("pipeline cancelled")
    elif error == "PipelineRunFenced":
        error = PipelineRunFenced(
            "stale", pipeline_id=1, content_hash="h", run_epoch=1, current_epoch=2, remedy="",
        )
    plan = {(2, 1): worker_exit(1, "boom")}
    plan[(2, rung)] = error  # at rung 2 the page first fails rung 1 ordinarily

    pdf = make_pdf(tmp_path / "doc.pdf", TEXTS[:3])
    fake = FakeMineru(plan)
    ext = PDFExtractor()
    delivered: list[int] = []
    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch.object(ext, "_mineru_run_isolated", side_effect=fake):
        with pytest.raises(type(error)):
            ext._extract_with_mineru(pdf, on_page=lambda i, t, m: delivered.append(i))

    assert delivered == [0], "page 2 was not delivered, by rung 3 or any other"
    assert (3, 1) not in fake.calls, "the document stopped"


def test_a_callback_failure_is_never_a_descent(tmp_path: Path) -> None:
    """on_page raising a RuntimeError (the fenced run is one) is not a MinerU failure."""
    pdf = make_pdf(tmp_path / "doc.pdf", TEXTS[:3])
    fake = FakeMineru()
    ext = PDFExtractor()

    def on_page(index, text, meta):
        if index == 1:
            raise RuntimeError("callback boom")

    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch.object(ext, "_mineru_run_isolated", side_effect=fake):
        with pytest.raises(RuntimeError, match="callback boom"):
            ext._extract_with_mineru(pdf, on_page=on_page)

    assert fake.calls == [(1, 1), (2, 1)], "no rung 2 attempt followed the callback's failure"


def test_a_pdf_info_excess_still_stops_the_document(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "doc.pdf", TEXTS[:2])
    ext = PDFExtractor()

    def two_entries(pdf_path, start, end, formula_enable=True):
        return "text", [], [{"page_idx": 0}, {"page_idx": 1}]

    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch.object(ext, "_mineru_run_isolated", side_effect=two_entries):
        with pytest.raises(ValueError, match="parsed the whole document"):
            ext._extract_with_mineru(pdf)


# ── dropped connections ──────────────────────────────────────────────────────


@pytest.mark.parametrize("dropped", [
    httpx.ConnectError("refused"), httpx.ReadError("reset"), httpx.RemoteProtocolError("eof"),
], ids=["connect", "read", "remote-protocol"])
def test_a_dropped_connection_on_rung_1_retries_once_on_the_subprocess_with_formulas_on(
    tmp_path: Path, dropped,
) -> None:
    retry = MagicMock(return_value=("SUBPROCESS page 2", [], [{"page_idx": 0}]))
    plan = {(2, 1): dropped}
    result, delivered, fake, _ = run_ladder(tmp_path, TEXTS[:3], plan, subprocess_retry=retry)

    retry.assert_called_once()
    assert retry.call_args.args[1:] == (1, 2), "page 2, the same one-page range"
    assert "formula_enable" not in retry.call_args.kwargs, "formulas stay on (the default)"
    assert result.metadata["extraction_method"] == "mineru"
    assert result.metadata["extraction_pages"] == {}
    assert dict((i, t) for i, t, _ in delivered)[1] == "SUBPROCESS page 2"
    assert (2, 2) not in fake.calls, "rung 2 was not reached"


def test_a_dropped_connection_whose_retry_also_fails_descends_to_rung_2(tmp_path: Path) -> None:
    retry = MagicMock(side_effect=worker_exit(1, "boom"))
    plan = {(2, 1): httpx.ReadError("reset")}
    result, _, fake, _ = run_ladder(tmp_path, TEXTS[:3], plan, subprocess_retry=retry)

    retry.assert_called_once()
    assert (2, 2) in fake.calls
    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}


def test_a_dropped_connection_on_rung_2_descends_to_rung_3_with_no_retry(tmp_path: Path) -> None:
    retry = MagicMock(side_effect=worker_exit(1, "boom"))
    plan = {(2, 1): worker_exit(1, "boom"), (2, 2): httpx.ConnectError("refused")}
    result, _, _, _ = run_ladder(tmp_path, TEXTS[:3], plan, subprocess_retry=retry)

    retry.assert_not_called()
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2]}


def test_a_dropped_connection_never_counts_toward_the_breaker(tmp_path: Path) -> None:
    """Every page loses its connection on both rungs and its retry loses it too: unclassified."""
    retry = MagicMock(side_effect=httpx.ReadError("reset"))
    plan = {}
    for page in range(1, 6):
        plan[(page, 1)] = httpx.ConnectError("refused")
        plan[(page, 2)] = httpx.ConnectError("refused")

    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan, breaker=3, subprocess_retry=retry)

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 3, 4, 5]}


# ── the failure class ────────────────────────────────────────────────────────


@pytest.mark.parametrize("error,expected", [
    (worker_timeout(), "timeout"),
    (worker_exit(1, "ImportError: libGL.so.1"), "exit:1:ImportError: libGL.so.1"),
    (worker_exit(-6, ""), "exit:-6:"),
    (worker_oom("MemoryError"), "oom:MemoryError"),
    (worker_oom(), "oom:"),
    (RuntimeError("MinerU produced no output at /tmp/x.md"), "unclassified"),
    (httpx.ConnectError("refused"), "unclassified"),
    (httpx.ReadError("reset"), "unclassified"),
    (httpx.RemoteProtocolError("eof"), "unclassified"),
], ids=lambda v: v if isinstance(v, str) else type(v).__name__)
def test_the_failure_class_is_built_from_attributes_not_message_text(error, expected) -> None:
    assert pdf_extractor._failure_class(error) == expected


def test_the_failure_class_ignores_the_message() -> None:
    a = worker_exit(1, "ImportError: x")
    b = MineruWorkerError("pages 7-8 of /other/path.pdf", returncode=1, stderr_last_line="ImportError: x")
    assert pdf_extractor._failure_class(a) == pdf_extractor._failure_class(b)


# ── the systemic-failure rule ────────────────────────────────────────────────


def both_rungs(pages: list[int], error_factory) -> dict:
    plan = {}
    for page in pages:
        plan[(page, 1)] = error_factory()
        plan[(page, 2)] = error_factory()
    return plan


def test_three_consecutive_pages_failing_both_rungs_with_one_class_trip_the_breaker(
    tmp_path: Path,
) -> None:
    plan = both_rungs([2, 3, 4], lambda: worker_exit(1, "ImportError: libGL.so.1"))
    pdf = make_pdf(tmp_path / "doc.pdf", TEXTS)
    ext = PDFExtractor()
    delivered: list[int] = []
    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch("nexus.config.get_pdf_ladder_breaker", return_value=3), \
         patch.object(ext, "_mineru_run_isolated", side_effect=FakeMineru(plan)):
        with pytest.raises(MineruSystemicFailure) as info:
            ext._extract_with_mineru(pdf, on_page=lambda i, t, m: delivered.append(i))

    exc = info.value
    assert not isinstance(exc, RuntimeError), "an Exception, so no RuntimeError handler descends on it"
    assert exc.failure_class == "exit:1:ImportError: libGL.so.1"
    assert "exit:1:ImportError: libGL.so.1" in str(exc)
    assert "ImportError: libGL.so.1" in str(exc)
    assert delivered == [0, 1, 2], "pages before the trip are delivered; the tripping page is not"


def test_a_class_that_changes_restarts_the_count_at_one(tmp_path: Path) -> None:
    plan = both_rungs([1, 2], lambda: worker_exit(1, "ImportError: a"))
    plan.update(both_rungs([3], lambda: worker_exit(2, "OSError: b")))
    result, _, _, _ = run_ladder(tmp_path, TEXTS[:4], plan, breaker=3)

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 3]}


def test_a_page_whose_two_rungs_fail_with_different_classes_resets_the_count(tmp_path: Path) -> None:
    plan = both_rungs([1, 2], lambda: worker_exit(1, "ImportError: a"))
    plan[(3, 1)] = worker_exit(1, "ImportError: a")
    plan[(3, 2)] = worker_timeout()
    plan.update(both_rungs([4, 5], lambda: worker_exit(1, "ImportError: a")))
    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan, breaker=3)

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 3, 4, 5]}


def test_a_page_rung_2_settles_resets_the_count(tmp_path: Path) -> None:
    plan = both_rungs([1, 2], lambda: worker_exit(1, "ImportError: a"))
    plan[(3, 1)] = worker_exit(1, "ImportError: a")  # rung 2 succeeds
    plan.update(both_rungs([4, 5], lambda: worker_exit(1, "ImportError: a")))
    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan, breaker=3)

    assert result.metadata["extraction_pages"] == {
        "pymupdf-text": [1, 2, 4, 5], "mineru-no-formula": [3],
    }


def test_three_consecutive_oom_pages_that_rung_2_settles_do_not_trip(tmp_path: Path) -> None:
    plan = {(page, 1): worker_oom("MemoryError") for page in (1, 2, 3, 4)}
    result, _, _, _ = run_ladder(tmp_path, TEXTS[:4], plan, breaker=3)

    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [1, 2, 3, 4]}


def test_timeouts_on_both_rungs_form_one_class(tmp_path: Path) -> None:
    plan = both_rungs([1, 2, 3], worker_timeout)
    with pytest.raises(MineruSystemicFailure) as info:
        run_ladder(tmp_path, TEXTS[:4], plan, breaker=3)
    assert info.value.failure_class == "timeout"


def test_a_breaker_of_zero_never_trips(tmp_path: Path) -> None:
    plan = both_rungs([1, 2, 3, 4, 5], lambda: worker_exit(1, "ImportError: a"))
    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan, breaker=0)

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 3, 4, 5]}


def test_a_breaker_of_four_needs_four_pages(tmp_path: Path) -> None:
    plan = both_rungs([1, 2, 3], lambda: worker_exit(1, "ImportError: a"))
    result, _, _, _ = run_ladder(tmp_path, TEXTS[:4], plan, breaker=4)
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 3]}

    plan = both_rungs([1, 2, 3, 4], lambda: worker_exit(1, "ImportError: a"))
    with pytest.raises(MineruSystemicFailure):
        run_ladder(tmp_path, TEXTS[:5], plan, breaker=4)


# ── pdf.ladder_breaker ───────────────────────────────────────────────────────


def _breaker_from(value) -> tuple[int, list[dict]]:
    with patch("nexus.config.load_config", return_value={"pdf": {"ladder_breaker": value}}):
        with capture_logs() as logs:
            cfg = get_pdf_config()
    return cfg.ladder_breaker, [e for e in logs if e.get("log_level") == "warning"]


def test_ladder_breaker_defaults_to_three() -> None:
    assert PDFConfig().ladder_breaker == 3
    with patch("nexus.config.load_config", return_value={}):
        assert get_pdf_config().ladder_breaker == 3


@pytest.mark.parametrize("value,expected", [(0, 0), ("0", 0), (3, 3), ("3", 3), (4, 4), ("10", 10)])
def test_ladder_breaker_accepts_zero_and_three_or_more(value, expected) -> None:
    got, warnings = _breaker_from(value)
    assert got == expected
    assert warnings == []


@pytest.mark.parametrize("value", [1, 2, -1, "2", "x", "", 2.5, True, None, [3]])
def test_ladder_breaker_below_three_or_not_an_integer_warns_and_becomes_three(value) -> None:
    got, warnings = _breaker_from(value)
    assert got == 3
    assert len(warnings) == 1, warnings
    assert "ladder_breaker" in str(warnings[0])


def test_the_accessor_reads_the_config() -> None:
    with patch("nexus.config.load_config", return_value={"pdf": {"ladder_breaker": 5}}):
        assert get_pdf_ladder_breaker() == 5


# ── page count and page boundaries ───────────────────────────────────────────


def test_a_degraded_middle_page_keeps_the_page_count_and_later_page_numbers(tmp_path: Path) -> None:
    plan = {(3, 1): worker_exit(1, "x"), (3, 2): worker_exit(1, "x")}
    pdf = make_pdf(tmp_path / "doc.pdf", TEXTS)
    ext = PDFExtractor()

    def short_info(pdf_path, start, end, formula_enable=True):
        if (start + 1, formula_enable) in {(3, True), (3, False)}:
            raise plan[(3, 1 if formula_enable else 2)]
        return f"MINERU page {start + 1}", [], [{"page_idx": 0, "para_blocks": []}]

    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch.object(ext, "_mineru_run_isolated", side_effect=short_info):
        result = ext._extract_with_mineru(pdf)

    # Page 3 settled on rung 3, so MinerU's own pdf_info holds 4 entries for 5 pages.
    assert result.metadata["page_count"] == 5
    bounds = result.metadata["page_boundaries"]
    assert [b["page_number"] for b in bounds] == [1, 2, 3, 4, 5]
    texts = ["MINERU page 1", "MINERU page 2", "charlie three", "MINERU page 4", "MINERU page 5"]
    for bound, expected in zip(bounds, texts):
        got = result.text[bound["start_char"]: bound["start_char"] + len(expected)]
        assert got == expected
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [3]}


def test_an_unextracted_page_keeps_its_boundary(tmp_path: Path) -> None:
    texts = ["alpha one", "", "charlie three"]
    plan = both_rungs([2], lambda: worker_exit(1, "x"))
    result, _, _, _ = run_ladder(tmp_path, texts, plan)

    bounds = result.metadata["page_boundaries"]
    assert [b["page_number"] for b in bounds] == [1, 2, 3]
    assert bounds[1]["page_text_length"] == 1, "an empty page is just its separator"
    third = bounds[2]
    assert result.text[third["start_char"]:] == "MINERU r1 page 3"


def test_the_page_count_is_the_pdfs_not_mineru_pdf_info() -> None:
    result = PDFExtractor._mineru_build_result(
        Path("/tmp/x.pdf"), "a\nb\nc", [], [{"page_idx": 0, "para_blocks": []}],
        per_page_lengths=[(0, 1), (1, 1), (2, 1)], total_pages=3,
    )
    assert result.metadata["page_count"] == 3
    assert [b["page_number"] for b in result.metadata["page_boundaries"]] == [1, 2, 3]


# ── rung 3: one page's text layer ────────────────────────────────────────────


def test_the_text_layer_of_one_page_is_normalised_as_extract_normalized_does(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "doc.pdf", ["alpha   one", "bravo two"])
    whole = PDFExtractor()._extract_normalized(pdf)

    with pymupdf.open(pdf) as doc:
        pages = [pdf_extractor._pymupdf_page_text(p) for p in doc]
    at = [pdf_extractor._pymupdf_page_text_at(pdf, i) for i in (0, 1)]

    assert pages == at
    assert "\n".join(pages) == whole.text
    assert pages[0] == "alpha one", "runs of spaces collapse, as in the whole-document path"


def test_the_page_normalisation_collapses_spaces_blank_lines_and_trailing_space() -> None:
    assert pdf_extractor._normalize_page_text("a   b \n\n\n\nc  \n") == "a b\n\nc"


# ── attributes on the worker's failures ──────────────────────────────────────


def _popen_writing(stderr_text: bytes, returncode_or_timeout):
    """A Popen stand-in that writes to the stderr file it is handed, then exits or times out."""
    def popen(args, **kwargs):
        kwargs["stderr"].write(stderr_text)
        kwargs["stderr"].flush()
        proc = MagicMock(pid=4321)
        if returncode_or_timeout == "timeout":
            def wait(timeout=None):
                if timeout is not None:
                    raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
                return -9
            proc.wait.side_effect = wait
        else:
            proc.wait.return_value = returncode_or_timeout
        return proc
    return popen


def _run_subprocess_failing(tmp_path: Path, stderr_text: bytes, outcome, *, ceiling_mb=0):
    ext = PDFExtractor()
    work = tmp_path / "work"
    work.mkdir()
    with contextlib.ExitStack() as stack:
        if ceiling_mb:
            # The ceiling is Linux-only; the module-local platform seam makes the test take the
            # Linux branch on any host, so it asserts the same thing everywhere.
            stack.enter_context(patch_in("nexus.pdf_extractor", "sys.platform", "linux"))
        stack.enter_context(patch_in("nexus.pdf_extractor", "subprocess.Popen", _popen_writing(stderr_text, outcome)))
        stack.enter_context(patch_in("nexus.pdf_extractor", "tempfile.mkdtemp", return_value=str(work)))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg"))
        stack.enter_context(patch("nexus.util.process_group.safe_killpg_group"))
        stack.enter_context(patch("nexus.config.get_mineru_page_timeout_s", return_value=10))
        stack.enter_context(patch("nexus.config.get_mineru_memory_ceiling_mb", return_value=ceiling_mb))
        with pytest.raises(RuntimeError) as info:
            ext._mineru_run_subprocess(Path("/tmp/x.pdf"), 0, 1)
    return info.value


def test_a_worker_timeout_carries_timed_out_not_a_return_code(tmp_path: Path) -> None:
    exc = _run_subprocess_failing(tmp_path, b"", "timeout")
    assert isinstance(exc, MineruWorkerError)
    assert exc.timed_out is True
    assert exc.returncode is None
    assert pdf_extractor._failure_class(exc) == "timeout"


def test_a_worker_exit_carries_the_return_code_and_the_last_non_empty_stderr_line(
    tmp_path: Path,
) -> None:
    stderr = b"Traceback (most recent call last):\n  File x\nImportError: libGL.so.1\n\n  \n"
    exc = _run_subprocess_failing(tmp_path, stderr, 1)
    assert isinstance(exc, MineruWorkerError) and not isinstance(exc, MineruMemoryError)
    assert exc.timed_out is False
    assert exc.returncode == 1
    assert exc.stderr_last_line == "ImportError: libGL.so.1"
    assert pdf_extractor._failure_class(exc) == "exit:1:ImportError: libGL.so.1"


def test_a_memory_ceiling_turns_an_exit_into_oom_keyed_on_the_stderr_line(tmp_path: Path) -> None:
    exc = _run_subprocess_failing(tmp_path, b"ImportError: libGL.so.1\n", 1, ceiling_mb=4096)
    assert isinstance(exc, MineruMemoryError)
    assert exc.returncode == 1 and exc.stderr_last_line == "ImportError: libGL.so.1"
    assert pdf_extractor._failure_class(exc) == "oom:ImportError: libGL.so.1"


def test_a_sigkill_is_oom_with_its_return_code(tmp_path: Path) -> None:
    exc = _run_subprocess_failing(tmp_path, b"", -9)
    assert isinstance(exc, MineruMemoryError)
    assert exc.returncode == -9
    assert pdf_extractor._failure_class(exc) == "oom:"


def test_the_stderr_last_line_is_redacted_like_the_log_event(tmp_path: Path) -> None:
    exc = _run_subprocess_failing(tmp_path, b"401 Bearer abcdefghijklmnopqrstuvwxyz0123456789\n", 1)
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in exc.stderr_last_line


# ── rung 2 reaches the worker and the server ─────────────────────────────────


def test_the_worker_script_takes_the_formula_switch_from_argv() -> None:
    script = pdf_extractor._MINERU_WORKER_SCRIPT
    assert "formula_enable=True" not in script
    assert "formula_enable=formula_enable" in script


@pytest.mark.parametrize("formula_enable,argv5", [(True, "1"), (False, "0")])
def test_the_subprocess_receives_the_formula_switch(tmp_path: Path, formula_enable, argv5) -> None:
    seen: dict = {}

    def popen(args, **kwargs):
        seen["args"] = list(args)
        raise RuntimeError("stop here")

    ext = PDFExtractor()
    with patch_in("nexus.pdf_extractor", "subprocess.Popen", popen), \
         patch("nexus.config.get_mineru_page_timeout_s", return_value=10), \
         patch("nexus.config.get_mineru_memory_ceiling_mb", return_value=0):
        with pytest.raises(RuntimeError, match="stop here"):
            ext._mineru_run_subprocess(Path("/tmp/x.pdf"), 0, 1, formula_enable=formula_enable)

    assert seen["args"][-3:] == ["0", "0", argv5], "start, inclusive end, then the formula switch"


@pytest.mark.parametrize("formula_enable,field", [(True, "true"), (False, "false")])
def test_the_server_request_carries_the_formula_switch(tmp_path: Path, formula_enable, field) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ext = PDFExtractor()
    resp = MagicMock()
    resp.json.return_value = {"results": {"x": {"md_content": "t"}}}
    with patch("nexus.config.get_mineru_server_url", return_value="http://127.0.0.1:1"), \
         patch("httpx.post", return_value=resp) as post:
        ext._mineru_run_via_server(pdf, 0, 1, formula_enable=formula_enable)

    assert post.call_args.kwargs["data"]["formula_enable"] == field


def test_rung_1_calls_keep_their_old_shape_and_rung_2_adds_the_switch(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ext = PDFExtractor()
    with patch.object(ext, "_mineru_server_available", return_value=True), \
         patch.object(ext, "_mineru_run_via_server", return_value=("m", [], [])) as server:
        ext._mineru_run_isolated(pdf, 0, 1)
        ext._mineru_run_isolated(pdf, 0, 1, formula_enable=False)

    assert server.call_args_list[0].args == (pdf, 0, 1) and server.call_args_list[0].kwargs == {}
    assert server.call_args_list[1].kwargs == {"formula_enable": False}


def test_the_subprocess_fallback_after_a_server_error_keeps_the_switch(tmp_path: Path) -> None:
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    ext = PDFExtractor()
    request = httpx.Request("POST", "http://127.0.0.1:1/file_parse")
    err = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    with patch.object(ext, "_mineru_server_available", return_value=True), \
         patch.object(ext, "_mineru_run_via_server", side_effect=err), \
         patch.object(ext, "_mineru_run_subprocess", return_value=("m", [], [])) as sub:
        ext._mineru_run_isolated(pdf, 0, 1, formula_enable=False)

    assert sub.call_args.kwargs == {"formula_enable": False}


# ── result metadata ──────────────────────────────────────────────────────────


def test_build_result_reports_extraction_pages_and_the_method() -> None:
    clean = PDFExtractor._mineru_build_result(
        Path("/tmp/x.pdf"), "t", [], [{"page_idx": 0, "para_blocks": []}], total_pages=1,
    )
    assert clean.metadata["extraction_method"] == "mineru"
    assert clean.metadata["extraction_pages"] == {}

    degraded = PDFExtractor._mineru_build_result(
        Path("/tmp/x.pdf"), "t", [], [{"page_idx": 0, "para_blocks": []}],
        total_pages=3, extraction_pages={"pymupdf-text": [3], "mineru-no-formula": [1]},
    )
    assert degraded.metadata["extraction_method"] == "mineru+degraded"
    assert degraded.metadata["extraction_pages"] == {"mineru-no-formula": [1], "pymupdf-text": [3]}


def test_a_page_outcome_is_a_frozen_record() -> None:
    outcome = PageOutcome(page=2, text="x", method="mineru-no-formula", seconds=0.5, error=None)
    assert (outcome.page, outcome.method, outcome.error) == (2, "mineru-no-formula", None)
    with pytest.raises(AttributeError):
        outcome.page = 3  # type: ignore[misc]


# ── NX_MINERU_FAULT_PAGES ────────────────────────────────────────────────────


def test_the_fault_hook_does_nothing_unset(tmp_path: Path) -> None:
    with capture_logs() as logs:
        result, _, fake, _ = run_ladder(tmp_path, TEXTS[:3])
    assert result.metadata["extraction_pages"] == {}
    assert not [e for e in logs if e.get("event") == "mineru_fault_pages_forced"]


def test_the_fault_hook_forces_a_page_rung_1_to_fail_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_MINERU_FAULT_PAGES", "2:1")
    with capture_logs() as logs:
        result, delivered, fake, _ = run_ladder(tmp_path, TEXTS[:3])

    assert result.metadata["extraction_pages"] == {"mineru-no-formula": [2]}
    assert (2, 1) not in fake.calls, "the forced attempt never reached MinerU"
    assert (2, 2) in fake.calls
    warned = [e for e in logs if e.get("event") == "mineru_fault_pages_forced"]
    assert len(warned) == 1 and warned[0]["log_level"] == "warning"
    assert "2" in str(warned[0]["pages"])


def test_the_fault_hook_forces_a_page_to_time_out_on_both_rungs_after_the_real_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_MINERU_FAULT_PAGES", "2:1,2:timeout")
    waits: list[float] = []
    with patch("nexus.pdf_extractor._fault_wait", side_effect=waits.append), \
         patch("nexus.config.get_mineru_page_timeout_s", return_value=77):
        result, _, fake, _ = run_ladder(tmp_path, TEXTS[:3])

    assert waits == [77, 77], "each forced timeout waits out the real per-page timeout"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2]}


def test_a_forced_failure_is_classified_so_the_breaker_sees_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_MINERU_FAULT_PAGES", "1:1,2;2:1,2;3:1,2")
    with pytest.raises(MineruSystemicFailure) as info:
        run_ladder(tmp_path, TEXTS, breaker=3)
    assert info.value.failure_class.startswith("exit:1:")
    assert "NX_MINERU_FAULT_PAGES" in info.value.failure_class


def test_the_fault_hook_does_not_skip_a_page_it_did_not_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_MINERU_FAULT_PAGES", "5:1")
    result, _, _, _ = run_ladder(tmp_path, TEXTS[:3])
    assert result.metadata["extraction_pages"] == {}


@pytest.mark.parametrize("raw,expected", [
    ("5:1", {5: ({1}, "error")}),
    ("5:1,2:timeout", {5: ({1, 2}, "timeout")}),
    ("5:1;7:2:oom", {5: ({1}, "error"), 7: ({2}, "oom")}),
    ("", {}),
])
def test_the_fault_grammar(raw, expected) -> None:
    assert pdf_extractor._parse_fault_pages(raw) == expected


@pytest.mark.parametrize("raw", ["x", "5", "5:3", "0:1", "5:1:bogus", "5:"])
def test_a_malformed_fault_spec_is_refused_not_ignored(raw) -> None:
    with pytest.raises(ValueError, match="NX_MINERU_FAULT_PAGES"):
        pdf_extractor._parse_fault_pages(raw)
