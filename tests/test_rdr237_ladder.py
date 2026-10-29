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
import pickle
import subprocess
import threading
import time
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

#: The real forced-timeout wait, taken at import, before the autouse fixture replaces it.
_REAL_FAULT_WAIT = pdf_extractor._fault_wait

#: Coarse-timer slack for a wall-clock lower bound (the older wait test allows 20%: 0.04 s of 0.05 s).
_TIMER_SLACK_S = 0.08


@pytest.fixture
def _quiet_progress(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("NEXUS_PDF_PROGRESS_QUIET", "1")
    monkeypatch.delenv("NX_MINERU_FAULT_PAGES", raising=False)
    # pdf.ladder_breaker warns once per process per bad value; each test starts with none warned.
    monkeypatch.setattr("nexus.config._LADDER_BREAKER_WARNED", set())
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

    def __init__(
        self, plan: dict[tuple[int, int], BaseException] | None = None,
        events: list[tuple] | None = None,
    ) -> None:
        self.plan = plan or {}
        self.calls: list[tuple[int, int]] = []
        self.events = events

    def __call__(self, pdf_path, start, end, formula_enable=True):
        page, rung = start + 1, 1 if formula_enable else 2
        self.calls.append((page, rung))
        if self.events is not None:
            self.events.append(("attempt", page, rung))
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
    delivered: list[tuple[int, str, dict]] | None = None,
    events: list[tuple] | None = None,
    stop_on: tuple[int, BaseException] | None = None,
):
    """Run ``_extract_with_mineru`` over a real PDF; return (result, on_page calls, fake, ext).

    *delivered* (filled even when the run raises) collects the ``on_page`` calls; *events* gets
    every MinerU attempt (``("attempt", page, rung)``) and every delivery (``("deliver", index)``)
    in the order they happened. *stop_on* ``(index, error)`` makes ``on_page`` raise *error*
    instead of taking page *index*, as the pipeline's callback does on a cancel or a fence.
    """
    pdf = make_pdf(tmp_path / "doc.pdf", page_texts)
    fake = FakeMineru(plan, events)
    ext = PDFExtractor()
    delivered = [] if delivered is None else delivered

    def on_page(i, t, m):
        if stop_on is not None and i == stop_on[0]:
            raise stop_on[1]
        delivered.append((i, t, m))
        if events is not None:
            events.append(("deliver", i))

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("nexus.pdf_extractor.do_parse", object()))
        stack.enter_context(patch("nexus.config.get_mineru_page_batch", return_value=1))
        stack.enter_context(patch("nexus.config.get_pdf_ladder_breaker", return_value=breaker))
        stack.enter_context(patch.object(ext, "_mineru_run_isolated", side_effect=fake))
        if subprocess_retry is not None:
            stack.enter_context(patch.object(ext, "_mineru_run_subprocess", subprocess_retry))
        result = ext._extract_with_mineru(pdf, on_page=on_page)
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

    with pytest.raises(pdf_extractor.AllPagesUnextractedError, match="every page.*unextracted") as info:
        run_ladder(tmp_path, pdf_texts, plan, breaker=0)

    exc = info.value
    assert isinstance(exc, pdf_extractor.UnextractableContentError), "callers that catch the base keep working"
    assert type(exc) is not pdf_extractor.UnextractableContentError, "and callers can tell it apart"
    assert exc.total_pages == 2
    assert exc.last_error == "rung 1: exit:1:alpha; rung 2: exit:1:alpha"


def test_a_blank_page_document_the_ladder_never_ran_on_is_not_the_all_pages_type(tmp_path: Path) -> None:
    """The pymupdf extractor's "no text layer" is a document with nothing in it, not a MinerU failure:
    it stays the base type, which `nx dt index` and `nx index repo` treat as a skippable record."""
    pdf = make_pdf(tmp_path / "blank.pdf", ["", ""])
    with pytest.raises(pdf_extractor.UnextractableContentError) as info:
        PDFExtractor()._extract_pymupdf(pdf)
    assert type(info.value) is pdf_extractor.UnextractableContentError


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


# A real run's dispatch (`_mineru_run_isolated`) absorbs a dropped connection: it restarts the server,
# retries once, then runs the page on the subprocess, so the failure the ladder sees is the
# subprocess's. The tests in this section cover the ladder's own handling of a drop that REACHES it
# (`tests/test_rdr237_bounds.py` pins the dispatch's absorption).


@pytest.mark.parametrize("dropped", [
    httpx.ConnectError("refused"), httpx.ConnectTimeout("nothing answered"), httpx.ReadError("reset"),
    httpx.RemoteProtocolError("eof"),
], ids=["connect", "connect-timeout", "read", "remote-protocol"])
def test_a_dropped_connection_that_reaches_the_ladder_on_rung_1_retries_once_on_the_subprocess(
    tmp_path: Path, dropped,
) -> None:
    """Covers a drop that reaches the ladder (the dispatch absorbs it in a real run)."""
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


def test_a_dropped_connection_that_reaches_the_ladder_never_counts_toward_the_breaker(tmp_path: Path) -> None:
    """Every page loses its connection on both rungs and its retry loses it too: unclassified.
    Covers a drop that reaches the ladder; in a real run the dispatch absorbs it and the
    subprocess's own class is what the breaker sees."""
    retry = MagicMock(side_effect=httpx.ReadError("reset"))
    plan = {}
    for page in range(1, 6):
        plan[(page, 1)] = httpx.ConnectError("refused")
        plan[(page, 2)] = httpx.ConnectError("refused")

    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan, breaker=3, subprocess_retry=retry)

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 3, 4, 5]}


def test_a_page_whose_rung_1_drop_reached_the_ladder_never_counts_toward_the_breaker(tmp_path: Path) -> None:
    """Covers a drop that reaches the ladder (a real run's dispatch absorbs it). RDR-237 Bounds: a dropped connection counts toward neither the restart rule nor the breaker,
    also when its subprocess retry then dies with a worker class of its own and rung 2 dies the same
    way. The control plan, with the drop replaced by that same worker failure, trips."""
    retry = MagicMock(side_effect=lambda *a, **k: (_ for _ in ()).throw(worker_exit(1, "ImportError: x")))
    dropped = {}
    control = {}
    for page in (1, 2, 3):
        dropped[(page, 1)] = httpx.ReadError("reset")
        dropped[(page, 2)] = worker_exit(1, "ImportError: x")
        control[(page, 1)] = worker_exit(1, "ImportError: x")
        control[(page, 2)] = worker_exit(1, "ImportError: x")

    (tmp_path / "control").mkdir()
    (tmp_path / "dropped").mkdir()
    with pytest.raises(MineruSystemicFailure) as tripped:
        run_ladder(tmp_path / "control", TEXTS[:4], control, breaker=3)
    assert tripped.value.failure_class == "exit:1:ImportError: x", "non-vacuity: this plan does trip"

    result, _, _, _ = run_ladder(tmp_path / "dropped", TEXTS[:4], dropped, breaker=3, subprocess_retry=retry)
    assert retry.call_count == 3, "each page retried its rung 1 once on the subprocess"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 3]}


# ── the failure class ────────────────────────────────────────────────────────


@pytest.mark.parametrize("error,expected", [
    (worker_timeout(), "timeout"),
    (worker_exit(1, "ImportError: libGL.so.1"), "exit:1:ImportError: libGL.so.1"),
    (worker_exit(-6, ""), "signal:6"),
    (worker_exit(-6, "45%|####  | 9/20 [00:03<00:04, 2.9it/s]"), "signal:6"),
    (worker_oom("MemoryError"), "signal:9"),
    (worker_oom(), "signal:9"),
    (MineruMemoryError("sentinel", returncode=42, stderr_last_line="MemoryError"), "oom"),
    (MineruMemoryError("ceiling", returncode=1, stderr_last_line="ImportError: x"), "oom:1:ImportError: x"),
    (MineruMemoryError("no facts"), "oom:"),
    (RuntimeError("MinerU produced no output at /tmp/x.md"), "unclassified"),
    (httpx.ConnectError("refused"), "unclassified"),
    (httpx.ConnectTimeout("nothing answered"), "unclassified"),
    (httpx.ReadError("reset"), "unclassified"),
    (httpx.RemoteProtocolError("eof"), "unclassified"),
], ids=lambda v: v if isinstance(v, str) else type(v).__name__)
def test_the_failure_class_is_built_from_attributes_not_message_text(error, expected) -> None:
    assert pdf_extractor._failure_class(error) == expected


@pytest.mark.parametrize("line_a,line_b,expected_line", [
    ("FileNotFoundError: /var/folders/zz/T/nx-mineru-aa11/page_0007.pdf",
     "FileNotFoundError: /var/folders/yy/T/nx-mineru-bb22/page_0008.pdf", "FileNotFoundError: <path>"),
    ("OSError: [Errno 28] No space left on device: '/tmp/nx-4821/out.md'",
     "OSError: [Errno 28] No space left on device: '/tmp/nx-9930/out.md'",
     "OSError: [Errno 28] No space left on device: '<path>'"),
    ("RuntimeError: CUDA error at 0x7f3a9c001230", "RuntimeError: CUDA error at 0x7f3a9c00ffe0",
     "RuntimeError: CUDA error at <addr>"),
    ("ValueError: pid 48213 refused 1048576 bytes", "ValueError: pid 90417 refused 2097152 bytes",
     "ValueError: pid <n> refused <n> bytes"),
    (r"FileNotFoundError: C:\Users\x\AppData\Local\Temp\nx1\p.pdf",
     r"FileNotFoundError: C:\Users\y\AppData\Local\Temp\nx2\p.pdf", "FileNotFoundError: <path>"),
], ids=["tmp-path", "quoted-path", "hex-address", "pid-and-bytes", "windows-path"])
def test_a_stderr_line_with_a_per_page_value_gives_both_pages_one_failure_class(
    line_a, line_b, expected_line,
) -> None:
    """A temp path, pid, address or byte count differs from page to page. Keyed on it, two pages
    failing alike were two classes and the breaker never counted them."""
    for kind, make in (("exit", worker_exit), ("oom", lambda rc, line: MineruMemoryError(
            "ceiling", returncode=rc, stderr_last_line=line))):
        a = pdf_extractor._failure_class(make(1, line_a))
        b = pdf_extractor._failure_class(make(1, line_b))
        assert a == b == f"{kind}:1:{expected_line}"


def test_normalising_the_key_keeps_the_exception_type_and_message_words() -> None:
    keyed = pdf_extractor._failure_class(worker_exit(1, "ImportError: libGL.so.1: cannot open shared object file"))
    assert keyed == "exit:1:ImportError: libGL.so.1: cannot open shared object file"
    assert pdf_extractor._failure_class(worker_exit(1, "ModuleNotFoundError: No module named 'torch'")) == (
        "exit:1:ModuleNotFoundError: No module named 'torch'"
    )
    assert pdf_extractor._failure_class(worker_exit(1, "KeyError: 7 of 12")) == "exit:1:KeyError: 7 of 12", (
        "one- and two-digit numbers are message words, not per-page noise"
    )


def test_pages_whose_workers_fail_with_different_tmp_paths_trip_the_breaker(tmp_path: Path) -> None:
    plan = {}
    for page in (2, 3, 4):
        err = worker_exit(1, f"FileNotFoundError: /var/folders/q{page}/T/nx-mineru-{page}{page}{page}/p.pdf")
        plan[(page, 1)] = err
        plan[(page, 2)] = worker_exit(1, f"FileNotFoundError: /var/folders/r{page}/T/nx-mineru-{page}{page}{page}9/p.pdf")
    with pytest.raises(MineruSystemicFailure) as info:
        run_ladder(tmp_path, TEXTS, plan, breaker=3)
    assert info.value.failure_class == "exit:1:FileNotFoundError: <path>"
    assert info.value.stderr_line.startswith("FileNotFoundError: /var/folders/"), "the shown line stays raw"


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
    text = str(exc)
    assert "pages before page 2 stay in the kept buffer" in text
    assert "MinerU runs over them again" in text, "a rerun reruns MinerU; the buffer spares only the writes"
    assert "resumes" not in text
    assert delivered == [0], (
        "only the page before the window is delivered: pages 2 and 3 were counted toward the trip "
        "and are held, page 4 tripped it, and a rerun redoes all three with MinerU"
    )


# The rule, stated once (`_SystemicFailureCounter`): a page counts only when BOTH rungs failed with the
# SAME class and that class is not `unclassified`; it extends the run only when that class equals the
# previous counted page's; every other page (settled, mixed classes, unclassified) resets the count.


def test_the_counter_trips_on_the_threshold_page_of_one_class() -> None:
    counter = pdf_extractor._SystemicFailureCounter(3)
    assert counter.failed(1, "timeout", "timeout") is None
    assert counter.failed(2, "timeout", "timeout") is None
    assert counter.failed(3, "timeout", "timeout") == "timeout"
    assert counter.pages == [1, 2, 3]


def test_the_counter_needs_the_same_class_on_both_rungs_of_a_page() -> None:
    counter = pdf_extractor._SystemicFailureCounter(2)
    assert counter.failed(1, "timeout", "timeout") is None
    assert counter.failed(2, "timeout", "signal:9") is None, "rungs disagree: not counted, and it resets"
    assert counter.pages == []
    assert counter.failed(3, "timeout", "timeout") is None, "the run starts over at one"


def test_the_counter_needs_a_page_to_match_the_previous_counted_pages_class() -> None:
    counter = pdf_extractor._SystemicFailureCounter(2)
    assert counter.failed(1, "timeout", "timeout") is None
    assert counter.failed(2, "signal:9", "signal:9") is None, "a new class restarts the count at one"
    assert counter.pages == [2]
    assert counter.failed(3, "signal:9", "signal:9") == "signal:9"
    assert counter.pages == [2, 3]


def test_the_counter_never_counts_the_unclassified_class() -> None:
    counter = pdf_extractor._SystemicFailureCounter(2)
    assert counter.failed(1, "timeout", "timeout") is None
    assert counter.failed(2, "unclassified", "unclassified") is None
    assert counter.pages == [], "it also ended the run in progress"
    assert counter.failed(3, "unclassified", "unclassified") is None
    assert counter.failed(4, "unclassified", "unclassified") is None


def test_a_settled_page_resets_the_counter_and_a_zero_threshold_never_trips() -> None:
    counter = pdf_extractor._SystemicFailureCounter(2)
    counter.failed(1, "timeout", "timeout")
    counter.settled()
    assert counter.failed(2, "timeout", "timeout") is None
    off = pdf_extractor._SystemicFailureCounter(0)
    assert all(off.failed(p, "timeout", "timeout") is None for p in range(1, 20))


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


def test_a_page_rung_1_settles_resets_the_count(tmp_path: Path) -> None:
    """Two counted pages, a clean page, two more: a healthy install with sporadic bad pages."""
    plan = both_rungs([1, 2, 4, 5], lambda: worker_exit(1, "ImportError: a"))
    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan, breaker=3)

    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 4, 5]}


def test_a_dropped_connection_retry_that_succeeds_resets_the_count(tmp_path: Path) -> None:
    retry = MagicMock(return_value=("SUBPROCESS page 3", [], [{"page_idx": 0}]))
    plan = both_rungs([1, 2, 4, 5], lambda: worker_exit(1, "ImportError: a"))
    plan[(3, 1)] = httpx.ReadError("reset")
    result, _, _, _ = run_ladder(tmp_path, TEXTS, plan, breaker=3, subprocess_retry=retry)

    retry.assert_called_once()
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [1, 2, 4, 5]}


# ── the breaker window is held, then flushed in order or dropped ─────────────

A = lambda: worker_exit(1, "ImportError: a")  # noqa: E731 — the failing class most tests count
B = lambda: worker_exit(2, "OSError: b")  # noqa: E731 — a different class


def _texts(delivered) -> list[tuple[int, str]]:
    return [(i, t) for i, t, _ in delivered]


def test_a_window_that_a_clean_page_resolves_is_delivered_in_order_before_that_page(
    tmp_path: Path,
) -> None:
    plan = both_rungs([2, 3], A)  # page 4 settles on rung 1
    delivered: list = []
    result, _, _, _ = run_ladder(tmp_path, TEXTS[:4], plan, breaker=3, delivered=delivered)

    assert _texts(delivered) == [
        (0, "MINERU r1 page 1"), (1, "bravo two"), (2, "charlie three"), (3, "MINERU r1 page 4"),
    ]
    assert result.text.split("\n") == [
        "MINERU r1 page 1", "bravo two", "charlie three", "MINERU r1 page 4",
    ]
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2, 3]}


def test_a_window_that_a_rung_2_page_resolves_is_delivered_in_order_before_that_page(
    tmp_path: Path,
) -> None:
    plan = both_rungs([2, 3], A)
    plan[(4, 1)] = worker_exit(1, "boom")  # page 4 fails rung 1 only
    delivered: list = []
    run_ladder(tmp_path, TEXTS[:4], plan, breaker=3, delivered=delivered)

    assert _texts(delivered) == [
        (0, "MINERU r1 page 1"), (1, "bravo two"), (2, "charlie three"), (3, "MINERU r2 page 4"),
    ]


def test_a_window_that_a_dropped_connection_retry_resolves_is_delivered_in_order(
    tmp_path: Path,
) -> None:
    retry = MagicMock(return_value=("SUBPROCESS page 4", [], [{"page_idx": 0}]))
    plan = both_rungs([2, 3], A)
    plan[(4, 1)] = httpx.ReadError("reset")
    delivered: list = []
    run_ladder(tmp_path, TEXTS[:4], plan, breaker=3, delivered=delivered, subprocess_retry=retry)

    assert _texts(delivered) == [
        (0, "MINERU r1 page 1"), (1, "bravo two"), (2, "charlie three"), (3, "SUBPROCESS page 4"),
    ]


def test_a_class_change_delivers_the_old_window_and_holds_the_page_that_changed_it(
    tmp_path: Path,
) -> None:
    plan = {**both_rungs([2, 3], A), **both_rungs([4], B)}  # page 5 is clean
    events: list = []
    delivered: list = []
    run_ladder(tmp_path, TEXTS, plan, breaker=3, delivered=delivered, events=events)

    assert _texts(delivered) == [
        (0, "MINERU r1 page 1"), (1, "bravo two"), (2, "charlie three"), (3, "delta four"),
        (4, "MINERU r1 page 5"),
    ]
    at = {e: n for n, e in enumerate(events)}
    # The old window is flushed when page 4 shows the class changed, after page 4's attempts...
    assert at[("attempt", 4, 2)] < at[("deliver", 1)] < at[("deliver", 2)] < at[("attempt", 5, 1)]
    # ...and page 4 itself, now the window, waits for page 5 to resolve it.
    assert at[("attempt", 5, 1)] < at[("deliver", 3)] < at[("deliver", 4)]


def test_a_page_whose_two_rungs_fail_differently_delivers_the_window_and_itself(
    tmp_path: Path,
) -> None:
    plan = both_rungs([2, 3], A)
    plan[(4, 1)] = A()
    plan[(4, 2)] = worker_timeout()  # two classes: page 4 is not counted
    events: list = []
    delivered: list = []
    run_ladder(tmp_path, TEXTS, plan, breaker=3, delivered=delivered, events=events)

    at = {e: n for n, e in enumerate(events)}
    assert at[("attempt", 4, 2)] < at[("deliver", 1)] < at[("deliver", 2)] < at[("deliver", 3)]
    assert at[("deliver", 3)] < at[("attempt", 5, 1)], "page 4 went straight through"
    assert [i for i, _, _ in delivered] == [0, 1, 2, 3, 4]


def test_a_window_still_open_at_the_end_of_the_document_is_delivered_in_order(
    tmp_path: Path,
) -> None:
    plan = both_rungs([3, 4], A)  # two counted pages, a breaker of 3, then no more pages
    events: list = []
    delivered: list = []
    result, _, _, _ = run_ladder(
        tmp_path, TEXTS[:4], plan, breaker=3, delivered=delivered, events=events,
    )

    assert _texts(delivered) == [
        (0, "MINERU r1 page 1"), (1, "MINERU r1 page 2"), (2, "charlie three"), (3, "delta four"),
    ]
    at = {e: n for n, e in enumerate(events)}
    assert at[("attempt", 4, 2)] < at[("deliver", 2)], "held until the document ended"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [3, 4]}
    assert result.metadata["page_count"] == 4
    assert [b["page_number"] for b in result.metadata["page_boundaries"]] == [1, 2, 3, 4]


def test_a_stop_in_the_middle_of_a_window_drops_the_held_pages(tmp_path: Path) -> None:
    plan = both_rungs([2, 3], A)
    plan[(4, 1)] = MineruMachineResourceError("no semaphores left")
    delivered: list = []
    with pytest.raises(MineruMachineResourceError):
        run_ladder(tmp_path, TEXTS[:4], plan, breaker=3, delivered=delivered)

    assert [i for i, _, _ in delivered] == [0], "the rerun redoes the window"


@pytest.mark.parametrize("stop", ["fenced", "cancelled"])
def test_a_stop_raised_while_the_window_is_flushed_ends_the_run_with_a_contiguous_buffer(
    tmp_path: Path, stop: str,
) -> None:
    """Page 4 resolves the window (pages 2-3); the callback stops on page 3 mid-flush."""
    error = (
        PipelineRunFenced(
            "stale", pipeline_id=1, content_hash="h", run_epoch=1, current_epoch=2, remedy="",
        ) if stop == "fenced" else PipelineCancelled("pipeline cancelled")
    )
    plan = both_rungs([2, 3], A)
    events: list = []
    delivered: list = []
    with pytest.raises(type(error)) as info:
        run_ladder(
            tmp_path, TEXTS, plan, breaker=3, delivered=delivered, events=events,
            stop_on=(2, error),
        )

    assert info.value is error, "the stop arrives as itself, never wrapped"
    assert [i for i, _, _ in delivered] == [0, 1], "a contiguous prefix: page 2 flushed, page 3 stopped"
    assert ("attempt", 4, 1) in events, "the window was being flushed by page 4's success"
    assert ("attempt", 5, 1) not in events, "the run ended at the stop"


@pytest.mark.parametrize("failure_class", ["signal:9", "oom", "oom:1:MemoryError"])
def test_a_trip_by_a_killed_worker_names_memory_not_the_install(failure_class: str) -> None:
    from nexus.pdf_extractor import mineru_stop_reason

    exc = MineruSystemicFailure(failure_class, "", [1, 2, 3], "doc.pdf")
    text = str(exc)
    assert "killed by the OS, usually out of memory" in text
    assert "pdf.mineru_page_batch" in text and "pdf.mineru_memory_ceiling_mb" in text
    assert "repair the install" not in text and "nx doctor" not in text
    what, detail, remedy = mineru_stop_reason(exc)
    assert detail == f"failure class {failure_class}"
    assert "usually out of memory" in remedy and "pdf.mineru_page_batch" in remedy
    assert "Repair the install" not in remedy and "nx doctor" not in remedy


@pytest.mark.parametrize("failure_class", ["timeout", "signal:11", "exit:1:ImportError: libGL.so.1"])
def test_a_trip_by_any_other_class_still_says_repair_the_install(failure_class: str) -> None:
    from nexus.pdf_extractor import mineru_stop_reason

    exc = MineruSystemicFailure(failure_class, "", [2, 3, 4], "doc.pdf")
    assert "repair the install first" in str(exc) and "run `nx doctor`" in str(exc)
    assert "out of memory" not in str(exc)
    assert mineru_stop_reason(exc)[2] == "Repair the install (`nx doctor`), then rerun."


def test_a_trip_that_starts_on_page_1_says_no_page_was_kept() -> None:
    first = MineruSystemicFailure("timeout", "", [1, 2, 3], "doc.pdf")
    later = MineruSystemicFailure("timeout", "", [4, 5, 6], "doc.pdf")

    assert "No page was kept" in str(first)
    assert "before page 1" not in str(first)
    assert "pages before page 4 stay in the kept buffer" in str(later)
    assert "No page was kept" not in str(later)


def test_a_breaker_of_zero_holds_nothing(tmp_path: Path) -> None:
    plan = both_rungs([1, 2, 3], A)
    events: list = []
    run_ladder(tmp_path, TEXTS[:4], plan, breaker=0, events=events)

    at = {e: n for n, e in enumerate(events)}
    assert at[("deliver", 0)] < at[("attempt", 2, 1)], "page 1 reached the buffer before page 2 began"
    assert at[("deliver", 1)] < at[("attempt", 3, 1)]


def test_a_page_that_never_counts_is_never_held(tmp_path: Path) -> None:
    """Two rungs that fail without worker facts are 'unclassified': the breaker ignores them."""
    plan = both_rungs([1, 2], lambda: RuntimeError("MinerU produced no output"))
    events: list = []
    run_ladder(tmp_path, TEXTS[:3], plan, breaker=3, events=events)

    at = {e: n for n, e in enumerate(events)}
    assert at[("deliver", 0)] < at[("attempt", 2, 1)]


# ── the default extractor path keeps the ladder's outcomes typed ─────────────


def _default_extract(tmp_path: Path, page_texts: list[str], plan, *, breaker=3, on_page=None):
    """``PDFExtractor.extract()`` with the default extractor (MinerU) over the fake MinerU."""
    pdf = make_pdf(tmp_path / "doc.pdf", page_texts)
    ext = PDFExtractor()
    with patch("nexus.pdf_extractor.do_parse", object()), \
         patch("nexus.config.get_mineru_page_batch", return_value=1), \
         patch("nexus.config.get_pdf_ladder_breaker", return_value=breaker), \
         patch.object(ext, "_mineru_run_isolated", side_effect=FakeMineru(plan)):
        return ext.extract(pdf, on_page=on_page)


def test_the_default_path_raises_the_breaker_unwrapped_with_the_window_dropped(tmp_path: Path) -> None:
    """The pipeline reads the TYPE to keep the buffer; a RuntimeError here wiped it."""
    delivered: list[int] = []
    plan = both_rungs([2, 3, 4], A)
    with pytest.raises(MineruSystemicFailure) as info:
        _default_extract(tmp_path, TEXTS, plan, on_page=lambda i, t, m: delivered.append(i))

    assert info.value.failure_class == "exit:1:ImportError: a"
    assert delivered == [0]


def test_the_default_path_raises_all_unextracted_as_unextractable_content(tmp_path: Path) -> None:
    plan = both_rungs([1, 2], A)
    with pytest.raises(pdf_extractor.AllPagesUnextractedError, match="every page.*unextracted"):
        _default_extract(tmp_path, ["", ""], plan, breaker=0)


@pytest.mark.parametrize("stop", ["fenced", "cancelled"])
def test_the_default_path_does_not_wrap_a_stop_raised_by_the_callback(tmp_path: Path, stop: str) -> None:
    error = (
        PipelineRunFenced(
            "stale", pipeline_id=1, content_hash="h", run_epoch=1, current_epoch=2, remedy="",
        ) if stop == "fenced" else PipelineCancelled("pipeline cancelled")
    )

    def on_page(index, text, meta):
        raise error

    with pytest.raises(type(error)) as info:
        _default_extract(tmp_path, TEXTS[:2], {}, on_page=on_page)
    assert info.value is error


def test_the_default_path_wraps_any_other_failure_and_points_at_nx_doctor(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "doc.pdf", TEXTS[:2])
    ext = PDFExtractor()
    with patch.object(ext, "_extract_with_mineru", side_effect=ValueError("page rewrite failed")):
        with pytest.raises(RuntimeError) as info:
            ext.extract(pdf)

    msg = str(info.value)
    assert "MinerU extraction failed: ValueError: page rewrite failed" in msg
    assert "nx doctor" in msg
    assert "formulas" not in msg


# ── the stderr line a failure class carries ──────────────────────────────────


def test_a_stderr_line_is_cut_to_the_cap_and_its_edges_are_stripped() -> None:
    cap = pdf_extractor._STDERR_LINE_CAP
    assert pdf_extractor._last_stderr_line("noise\n" + "y" * (cap + 200) + "\n\n") == "y" * cap
    assert pdf_extractor._last_stderr_line("z" * cap) == "z" * cap, "a line of exactly the cap is whole"
    assert pdf_extractor._last_stderr_line("z" * (cap + 1)) == "z" * cap
    assert pdf_extractor._last_stderr_line("  padded  \n") == "padded"
    assert pdf_extractor._last_stderr_line(" \n\n") == ""


def test_a_progress_bar_frame_is_not_the_last_meaningful_stderr_line() -> None:
    bar = "Predict:  45%|####5     | 9/20 [00:03<00:04,  2.9it/s]"
    assert pdf_extractor._last_stderr_line(f"ImportError: libGL.so.1\n{bar}\n") == "ImportError: libGL.so.1"
    # tqdm redraws one line with carriage returns: every frame is noise, none is the answer.
    assert pdf_extractor._last_stderr_line(f"OSError: weights missing\r{bar}\r{bar}") == "OSError: weights missing"
    assert pdf_extractor._last_stderr_line(f"{bar}\n{bar}\n") == ""
    assert pdf_extractor._last_stderr_line("Downloading: 100%|##########| 1/1 [00:01<00:00, 1.0s/it]\n") == ""
    assert pdf_extractor._last_stderr_line("ValueError: 50% of 12 pages failed\n") == "ValueError: 50% of 12 pages failed", (
        "a percentage in a real error line is not a progress bar"
    )


def test_a_real_message_glued_after_a_progress_frame_survives_the_frame() -> None:
    """tqdm redraws with ``\\r`` and no newline, so a message written next can share the frame's
    physical line. The frame goes, the message stays (the review finding behind this test)."""
    bar = "Predict:  45%|####5     | 9/20 [00:03<00:04,  2.9it/s]"
    assert pdf_extractor._last_stderr_line(f"{bar}ImportError: libGL.so.1\n") == "ImportError: libGL.so.1"
    assert pdf_extractor._last_stderr_line(f"{bar}{bar}ImportError: libGL.so.1") == "ImportError: libGL.so.1"
    assert pdf_extractor._last_stderr_line(f"{bar}\r{bar}\rOSError: weights missing") == "OSError: weights missing"
    assert pdf_extractor._last_stderr_line(f"Batch inference: 9%|# | 1/11 [00:01<00:09, 1.0s/it]KeyError: 'x'") == "KeyError: 'x'"
    assert pdf_extractor._last_stderr_line(f"ImportError: x\n{bar}") == "ImportError: x", "a frame alone is still noise"
    assert pdf_extractor._last_stderr_line(f"{bar}\n{bar}") == ""
    assert pdf_extractor._last_stderr_line("45%|####5     | 9/20 [00:03<00:04") == "", "a cut frame with no close is noise"


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


@pytest.mark.parametrize("value", [1, 2, -1, "2", "x", "", 2.5, True, False, None, [3]])
def test_ladder_breaker_below_three_or_not_an_integer_warns_and_becomes_three(value) -> None:
    got, warnings = _breaker_from(value)
    assert got == 3
    assert len(warnings) == 1, warnings
    assert "ladder_breaker" in str(warnings[0])


def test_a_malformed_ladder_breaker_warns_once_per_process_per_value() -> None:
    """The accessor re-reads the config on every call (once per document), so a bad value that
    warned each time would repeat the same line for every PDF indexed."""
    first = _breaker_from("x")
    second = _breaker_from("x")
    other = _breaker_from("y")

    assert first[0] == second[0] == other[0] == 3
    assert len(first[1]) == 1
    assert second[1] == [], "the same value does not warn again"
    assert len(other[1]) == 1, "a different bad value is a different mistake"


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


def test_the_text_layer_of_one_page_is_read_and_normalised(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "doc.pdf", ["alpha   one", "bravo two"])

    with pymupdf.open(pdf) as doc:
        pages = [pdf_extractor._pymupdf_page_text(p) for p in doc]
    at = [pdf_extractor._pymupdf_page_text_at(pdf, i) for i in (0, 1)]

    assert pages == at
    assert pages == ["alpha one", "bravo two"], "runs of spaces collapse; each page is its own text layer"


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
    assert pdf_extractor._failure_class(exc) == "oom:1:ImportError: libGL.so.1"


def test_a_sigkill_is_oom_keyed_on_its_signal(tmp_path: Path) -> None:
    exc = _run_subprocess_failing(tmp_path, b"", -9)
    assert isinstance(exc, MineruMemoryError)
    assert exc.returncode == -9 and exc.signal == 9
    assert pdf_extractor._failure_class(exc) == "signal:9"


def test_the_oom_sentinel_exit_is_one_class_whatever_the_stderr_says(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    a = _run_subprocess_failing(tmp_path, b"MemoryError\n", pdf_extractor._MINERU_OOM_EXIT)
    b = _run_subprocess_failing(tmp_path / "b", b"Traceback\n  ...\nMemoryError: other words\n",
                                pdf_extractor._MINERU_OOM_EXIT)
    assert isinstance(a, MineruMemoryError) and isinstance(b, MineruMemoryError)
    assert pdf_extractor._failure_class(a) == pdf_extractor._failure_class(b) == "oom"


def test_two_killed_workers_with_different_progress_output_share_one_class(tmp_path: Path) -> None:
    """The last stderr line of a worker killed mid-run is whatever progress bar frame it was on.
    Keyed on the line, two pages killed the same way would be two classes and the breaker would
    never count them (the review finding behind this test)."""
    (tmp_path / "b").mkdir()
    first = _run_subprocess_failing(tmp_path, b"Predict:  12%|#2   | 3/25 [00:09<01:02,  2.8s/it]\n", -9)
    second = _run_subprocess_failing(tmp_path / "b", b"Predict:  64%|######4 | 16/25 [00:41<00:23,  2.6s/it]\n", -9)
    assert first.stderr_last_line == second.stderr_last_line == ""
    assert pdf_extractor._failure_class(first) == pdf_extractor._failure_class(second) == "signal:9"


def test_a_worker_signal_is_none_for_an_exit_and_a_timeout() -> None:
    assert worker_exit(1, "x").signal is None
    assert worker_timeout().signal is None
    assert worker_exit(-11, "").signal == 11


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


def test_the_fault_hook_timeout_mode_runs_the_real_wait_through_the_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every other test here replaces `_fault_wait`; this one lets `NX_MINERU_FAULT_PAGES` reach the
    real function. A spy delegates to it, so the wait is real (a 0.2 s page budget) and observable."""
    monkeypatch.setenv("NX_MINERU_FAULT_PAGES", "2:1,2:timeout")
    waits: list[float] = []

    def spy(seconds: float) -> None:
        waits.append(seconds)
        _REAL_FAULT_WAIT(seconds)

    started = time.monotonic()
    with patch("nexus.pdf_extractor._fault_wait", side_effect=spy), \
         patch("nexus.config.get_mineru_page_timeout_s", return_value=0.2):
        result, _, fake, _ = run_ladder(tmp_path, TEXTS[:3])
    elapsed = time.monotonic() - started

    assert waits == [0.2, 0.2], "non-vacuity: the forced timeout reached the wait, once per rung"
    # Two 0.2 s waits; allow the coarse-timer slack the older _fault_wait test allows.
    assert elapsed >= 0.4 - _TIMER_SLACK_S, f"the real wait slept out both budgets (took {elapsed:.2f}s)"
    assert result.metadata["extraction_pages"] == {"pymupdf-text": [2]}
    assert (2, 1) not in fake.calls and (2, 2) not in fake.calls, "the forced failure stood in for MinerU"


def test_a_run_cancel_during_the_fault_hook_wait_stops_the_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NX_MINERU_FAULT_PAGES", "1:1:timeout")
    cancel = threading.Event()
    cancel.set()
    pdf_extractor.bind_run_cancel(cancel)
    try:
        with patch("nexus.pdf_extractor._fault_wait", _REAL_FAULT_WAIT), \
             patch("nexus.config.get_mineru_page_timeout_s", return_value=180):
            started = time.monotonic()
            with pytest.raises(MineruWorkerStopped, match="forced-timeout wait ended"):
                run_ladder(tmp_path, TEXTS[:2])
            assert time.monotonic() - started < 5.0, "the cancel ended the 180 s wait at once"
    finally:
        pdf_extractor.bind_run_cancel(None)


# ── the forced-timeout wait itself (every other test here replaces it) ──────


def test_the_forced_timeout_wait_sleeps_out_its_budget_without_a_run_cancel() -> None:
    pdf_extractor.bind_run_cancel(None)
    with patch_in("nexus.pdf_extractor", "time.sleep") as sleep:
        _REAL_FAULT_WAIT(180.0)
    sleep.assert_called_once_with(180.0)


def test_the_forced_timeout_wait_returns_when_the_budget_passes_uncancelled() -> None:
    cancel = threading.Event()
    pdf_extractor.bind_run_cancel(cancel)
    try:
        started = time.monotonic()
        _REAL_FAULT_WAIT(0.05)
        assert time.monotonic() - started >= 0.04, "it waited out the budget"
    finally:
        pdf_extractor.bind_run_cancel(None)


def test_a_cancel_ends_the_forced_timeout_wait_as_a_stop() -> None:
    cancel = threading.Event()
    cancel.set()
    pdf_extractor.bind_run_cancel(cancel)
    try:
        started = time.monotonic()
        with pytest.raises(MineruWorkerStopped, match="forced-timeout wait ended"):
            _REAL_FAULT_WAIT(180.0)
        assert time.monotonic() - started < 5.0, "the cancel ended the wait at once"
    finally:
        pdf_extractor.bind_run_cancel(None)


# ── exceptions cross a process boundary ──────────────────────────────────────


def _roundtrip(exc: BaseException, protocol: int) -> BaseException:
    return pickle.loads(pickle.dumps(exc, protocol=protocol))


@pytest.mark.parametrize("protocol", range(2, pickle.HIGHEST_PROTOCOL + 1))
def test_a_systemic_failure_round_trips_through_pickle(protocol: int) -> None:
    """A process-pool caller gets the exception back by pickle. `__init__` takes four arguments and
    the base stores one message, so the default reconstruction raised TypeError."""
    exc = MineruSystemicFailure("exit:1:ImportError: x", "ImportError: x", [4, 5, 6], "doc.pdf")
    back = _roundtrip(exc, protocol)
    assert type(back) is MineruSystemicFailure
    assert (back.failure_class, back.stderr_line, back.pages) == ("exit:1:ImportError: x", "ImportError: x", [4, 5, 6])
    assert str(back) == str(exc)


@pytest.mark.parametrize("exc", [
    MineruWorkerError("timed out", timed_out=True),
    MineruWorkerError("exit", returncode=1, stderr_last_line="ImportError: x"),
    MineruMemoryError("killed", returncode=-9, stderr_last_line=""),
    pdf_extractor.MineruServerTimeout("slow", timed_out=True),
], ids=lambda e: type(e).__name__ + ("-t" if e.timed_out else f"-{e.returncode}"))
def test_a_worker_error_keeps_its_failure_facts_through_pickle(exc: MineruWorkerError) -> None:
    back = _roundtrip(exc, pickle.HIGHEST_PROTOCOL)
    assert type(back) is type(exc)
    assert (back.timed_out, back.returncode, back.signal, back.stderr_last_line) == (
        exc.timed_out, exc.returncode, exc.signal, exc.stderr_last_line,
    )
    assert pdf_extractor._failure_class(back) == pdf_extractor._failure_class(exc)
    assert str(back) == str(exc)


@pytest.mark.parametrize("exc", [
    MineruMachineResourceError("no semaphores left"),
    MineruWorkerStopped("stopped"),
    pdf_extractor.MineruBoundsError("timeout x batch is too long"),
], ids=lambda e: type(e).__name__)
def test_the_other_stops_of_a_mineru_run_round_trip_through_pickle_too(exc: BaseException) -> None:
    back = _roundtrip(exc, pickle.HIGHEST_PROTOCOL)
    assert type(back) is type(exc) and str(back) == str(exc)


def test_the_all_pages_unextracted_error_round_trips_through_pickle() -> None:
    exc = pdf_extractor.AllPagesUnextractedError("every page of x.pdf is unextracted", total_pages=9, last_error="rung 1: timeout")
    back = _roundtrip(exc, pickle.HIGHEST_PROTOCOL)
    assert type(back) is pdf_extractor.AllPagesUnextractedError
    assert (back.total_pages, back.last_error, str(back)) == (9, "rung 1: timeout", str(exc))
